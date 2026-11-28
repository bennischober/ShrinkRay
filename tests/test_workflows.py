"""The CI workflows' action pins are internally consistent.

CodeQL loads a configuration file written by `init` and refuses to run when
`autobuild`/`analyze` are a different version of the same action: "Loaded a
configuration file for version '4.39.0', but running version '4.39.1'". Dependabot
tracks `github/codeql-action/{init,autobuild,analyze}` as three separate
dependencies, so a one-sub-action bump produced two PRs (#448, #449) that could
never pass on their own - green tests, a red CodeQL job, and no signal that the
version skew was the cause rather than a code defect.

The two checks here are the mechanical version of that lesson: every step of the
action in a workflow must name the same commit, and Dependabot must keep handing
us all three in one PR so the uniform pin survives the next bump.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODEQL_WORKFLOW = REPO / ".github" / "workflows" / "codeql-analysis.yml"
DEPENDABOT_CONFIG = REPO / ".github" / "dependabot.yml"
PIN_CHECK_WORKFLOW = REPO / ".github" / "workflows" / "pin-check.yml"
DOCKERFILE = REPO / "Dockerfile"

# `uses: github/codeql-action/analyze@<40-hex sha> # v4.39.1`
CODEQL_USE_RE = re.compile(r"uses:\s+github/codeql-action/([a-z-]+)@([0-9a-f]{40})")
# the group identifier and the pattern that must cover every sub-action
CODEQL_GROUP_PATTERN = "github/codeql-action/*"


class TestCodeqlActionIsPinnedUniformly(unittest.TestCase):
    """One workflow cannot run two versions of github/codeql-action."""

    def test_every_codeql_step_pins_the_same_commit(self) -> None:
        """A mixed pin is a startup failure, not a partial upgrade."""
        text = CODEQL_WORKFLOW.read_text(encoding="utf-8")
        pins = CODEQL_USE_RE.findall(text)

        self.assertEqual(
            sorted(sub for sub, _ in pins),
            ["analyze", "autobuild", "init"],
            "the workflow must use init, autobuild and analyze",
        )

        by_subaction = dict(pins)
        distinct = set(by_subaction.values())
        self.assertEqual(
            len(distinct),
            1,
            "github/codeql-action steps disagree on the pinned commit, which CodeQL "
            f"rejects at startup: {by_subaction}",
        )

    def test_dependabot_updates_the_codeql_steps_as_one_group(self) -> None:
        """Otherwise Dependabot re-creates #448/#449 on the next release."""
        text = DEPENDABOT_CONFIG.read_text(encoding="utf-8")
        lines = text.splitlines()

        starts = [
            index
            for index, line in enumerate(lines)
            if line.strip() == '- package-ecosystem: "github-actions"'
        ]
        self.assertEqual(len(starts), 1, "expected one github-actions ecosystem entry")

        # the entry runs to the next ecosystem entry (or the end of the file)
        block: list[str] = []
        for line in lines[starts[0] + 1 :]:
            if line.strip().startswith("- package-ecosystem:"):
                break
            block.append(line)

        self.assertIn(
            CODEQL_GROUP_PATTERN,
            "\n".join(block),
            "dependabot.yml must group github/codeql-action/* inside the "
            "github-actions entry, or the next release reopens the version skew",
        )


# `ARG FFMPEG_SHA256=<64-hex>`
FFMPEG_SHA_ARG_RE = re.compile(r"^ARG FFMPEG_SHA256=([0-9a-f]{64})$", re.M)
FFMPEG_VERSION_ARG_RE = re.compile(r"^ARG FFMPEG_VERSION=(\S+)$", re.M)
FFMPEG_ASSET_ARG_RE = re.compile(r"^ARG FFMPEG_ASSET=(\S+)$", re.M)


class TestFfmpegPinCheckWatchesTheDockerfile(unittest.TestCase):
    """#742: the container's ffmpeg pin points at a rolling BtbN autobuild tag
    that upstream prunes; the v0.7.0 release failed mid-tag because the rot was
    noticed only when the image build hit the 404. The check must exist, run on
    a schedule, read its pin from the Dockerfile (never restate it), and land
    the failure in an issue."""

    def test_the_pin_check_is_scheduled(self) -> None:
        """A check that never runs protects nothing, and one that hangs holds a
        runner for GitHub's 360-minute default: the schedule, the job timeout
        and the racing-dispatch dedupe are all part of the contract."""
        text = PIN_CHECK_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("schedule:", text)
        self.assertIn("cron:", text)
        self.assertIn("timeout-minutes:", text)
        self.assertIn("concurrency:", text)

    def test_the_check_reads_the_pin_from_the_dockerfile(self) -> None:
        """A re-pin that edits only the Dockerfile must be picked up: the
        workflow greps the three ARG lines and never restates their values."""
        text = PIN_CHECK_WORKFLOW.read_text(encoding="utf-8")
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        for name in ("FFMPEG_VERSION", "FFMPEG_ASSET", "FFMPEG_SHA256"):
            self.assertIn(f"ARG {name}=", dockerfile)
            self.assertIn(name, text)
        pinned_sha = FFMPEG_SHA_ARG_RE.search(dockerfile)
        self.assertIsNotNone(pinned_sha, "the Dockerfile lost its FFMPEG_SHA256 ARG")
        self.assertNotIn(
            pinned_sha.group(1),
            text,
            "the workflow restates the pin's digest - a re-pin of the Dockerfile "
            "alone would go unnoticed",
        )
        for regex, name in (
            (FFMPEG_VERSION_ARG_RE, "FFMPEG_VERSION"),
            (FFMPEG_ASSET_ARG_RE, "FFMPEG_ASSET"),
        ):
            value = regex.search(dockerfile)
            self.assertIsNotNone(value, f"the Dockerfile lost its {name} ARG")
            self.assertNotIn(
                value.group(1),
                text,
                f"the workflow restates {name}'s value - a re-pin of the Dockerfile "
                "alone would go unnoticed",
            )

    def test_a_rotted_pin_lands_in_an_issue(self) -> None:
        """A silent red run protects nothing: the failure path must create or
        update an issue - but only for the two CHECK steps' failures. A
        Dockerfile reformat breaking the read step, or a checkout failure, is
        a workflow defect and must file no false "rotted upstream" issue."""
        text = PIN_CHECK_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("id: url-check", text)
        self.assertIn("id: digest-check", text)
        self.assertIn(
            "steps.url-check.outcome == 'failure'",
            text,
            "the issue step must key on the check steps' outcomes, not a bare "
            "failure() that files a false rot issue for any workflow defect",
        )
        self.assertIn("steps.digest-check.outcome == 'failure'", text)
        self.assertIn("gh issue create", text)


if __name__ == "__main__":
    unittest.main()
