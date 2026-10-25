"""One named file must touch exactly one file (issue #197): the three CLI
commands resolved a file to its PARENT folder and rescanned it, so a flat
library named by one file got batch-scanned/planned/encoded. The fix passes
resolve_input_paths' file list through scan_movies' new `files` parameter.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app


class TestSingleFileScope(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.names = ["a.mkv", "b.mkv", "c.mkv"]
        for name in self.names:
            (self.root / name).write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> str:
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            return runner.invoke(app, list(argv)).output
        finally:
            os.chdir(previous)

    def test_scan_on_one_file_scans_one_movie(self) -> None:
        with patch("shrinkray.engine.scan.process_movie", return_value=None) as probe:
            self._run("scan", "a.mkv")
        self.assertEqual(probe.call_count, 1, "a one-file scan scanned the folder")

    def test_scan_on_a_folder_scans_the_folder(self) -> None:
        with patch("shrinkray.engine.scan.process_movie", return_value=None) as probe:
            self._run("scan", str(self.root))
        self.assertEqual(probe.call_count, 3, "a folder scan did not walk the folder")

    def test_scan_on_a_glob_scans_the_matched_files(self) -> None:
        with patch("shrinkray.engine.scan.process_movie", return_value=None) as probe:
            self._run("scan", "*.mkv")
        self.assertEqual(probe.call_count, 3, "a glob scan did not match its files")

    def test_plan_on_one_file_plans_one_movie(self) -> None:
        def fake_plan(movie, **kwargs):
            from shrinkray.engine.plan import QualityResult

            return QualityResult(
                input=movie,
                output=movie,
                success=False,
                skipped_reason="too short",
                input_size=movie.stat().st_size,
                output_size=movie.stat().st_size,
            )

        with (
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan) as plan,
        ):
            self._run("plan", "a.mkv")
        self.assertEqual(plan.call_count, 1, "a one-file plan planned the folder")

    def test_encode_on_one_file_encodes_one_movie(self) -> None:
        # encode_movies' inner worker calls find_handbrake; a None return
        # makes run_one raise => the command exits 1 naming the missing
        # binary. The touched count is the SCAN's count (1/1 vs the old bug's
        # 3/3), and the untouched files keep their mtimes.
        mtimes_before = {name: (self.root / name).stat().st_mtime_ns for name in self.names}
        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value=None),
            patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
        ):
            output = self._run("encode", "a.mkv")
        scan_line = next((line for line in output.splitlines() if "Scanning movies" in line), "")
        self.assertIn("1/1", scan_line, f"a one-file encode scanned the folder: {scan_line}")
        mtimes_after = {name: (self.root / name).stat().st_mtime_ns for name in self.names}
        self.assertEqual(mtimes_before, mtimes_after, "the untouched files changed")

    def test_encode_on_a_folder_scans_the_folder(self) -> None:
        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value=None),
            patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
        ):
            output = self._run("encode", str(self.root))
        scan_line = next((line for line in output.splitlines() if "Scanning movies" in line), "")
        self.assertIn("3/3", scan_line, f"a folder encode scanned only one file: {scan_line}")


if __name__ == "__main__":
    unittest.main()
