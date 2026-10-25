"""The release-notes extractor (tools/release_notes.py): the CHANGELOG
section IS the release body, so the extraction must be exact — a heading that
does not match falls back to generic text, a link-reference block ends the
section, trailing blank lines are stripped.
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from release_notes import extract_section


class TestExtractSection(unittest.TestCase):
    def test_a_matching_section_is_extracted(self) -> None:
        changelog = (
            "# Changelog\n\n## [Unreleased]\n\n- wip\n\n"
            "## [0.4.0] - 2026-10-25\n\n- one fix\n- two features\n\n"
            "## [0.3.0] - 2026-10-01\n\n- old\n"
        )
        body = extract_section(changelog, "0.4.0")
        self.assertEqual(body, "- one fix\n- two features\n")

    def test_the_section_ends_at_the_link_reference_block(self) -> None:
        changelog = "## [0.4.0]\n\n- notes\n\n[0.4.0]: https://github.com/x/released/0.4.0\n\n## [0.3.0]\n\n- old\n"
        body = extract_section(changelog, "0.4.0")
        self.assertEqual(body, "- notes\n")

    def test_a_prefix_version_does_not_match_a_longer_heading(self) -> None:
        changelog = "## [0.4.1] - 2026-11-01\n\n- new\n\n## [0.4.0]\n\n- old\n"
        self.assertIsNone(extract_section(changelog, "0.4"))

    def test_a_missing_version_is_none(self) -> None:
        self.assertIsNone(extract_section("# Changelog\n\n## [0.4.0]\n\n- x\n", "9.9.9"))

    def test_an_empty_section_is_an_empty_string(self) -> None:
        changelog = "## [0.4.0]\n\n## [0.3.0]\n\n- old\n"
        self.assertEqual(extract_section(changelog, "0.4.0"), "")

    def test_trailing_blank_lines_are_stripped(self) -> None:
        changelog = "## [0.4.0]\n\n- notes\n\n\n\n## [0.3.0]\n"
        self.assertEqual(extract_section(changelog, "0.4.0"), "- notes\n")


class TestMain(unittest.TestCase):
    def _run(self, *args: str, cwd: Path) -> tuple[int, str]:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "tools" / "release_notes.py"),
                *args,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=60,
        )
        return result.returncode, result.stdout

    def test_the_cli_strips_the_v_prefix_and_prints_the_body(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "CHANGELOG.md").write_text("## [0.4.0]\n\n- notes\n", encoding="utf-8")
            code, out = self._run("v0.4.0", cwd=Path(tmp))
        self.assertEqual(code, 0)
        self.assertEqual(out, "- notes\n")

    def test_a_missing_section_prints_the_fallback(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "CHANGELOG.md").write_text("## [0.3.0]\n\n- old\n", encoding="utf-8")
            code, out = self._run("v9.9.9", cwd=Path(tmp))
        self.assertEqual(code, 0)
        self.assertIn("## ShrinkRay v9.9.9", out)
        self.assertIn("CHANGELOG", out)

    def test_a_missing_changelog_exits_nonzero(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            code, _out = self._run("v0.4.0", cwd=Path(tmp))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
