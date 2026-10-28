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


if __name__ == "__main__":
    unittest.main()
