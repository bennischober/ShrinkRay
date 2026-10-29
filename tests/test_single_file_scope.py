"""One named file must touch exactly one file (issue #197): The remaining CLI
commands resolved a file to its PARENT folder and rescanned it, so a flat
library named by one file got batch-planned/encoded. The fix passes
resolve_input_paths' file list through scan_movies' new `files` parameter.
"""

import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app


@contextmanager
def _toolchain_present():
    """The plan/encode preflight (#194) checks the toolchain before any work;
    these tests mock the engine itself, so the check must see a working one
    (CI runners have no ffprobe on PATH). The stability constants are patched
    to 0 (issue #437): these tests run the REAL seam on fresh fixtures, and
    their subject is the one-file scan scope, not stability - no test sleeps.
    """
    from shrinkray.engine import watch as watch_mod

    with (
        patch("shrinkray.service.shutil.which", return_value="ffprobe"),
        patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
        patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
        patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value="modern"),
        patch.object(watch_mod, "STABLE_MIN_AGE", 0.0),
        patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
    ):
        yield


class TestSingleFileScope(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.names = ["a.mkv", "b.mkv", "c.mkv"]
        for name in self.names:
            (self.root / name).write_bytes(b"x" * 8)
        # the files read as COMPLETE (issue #437): these tests' subject is the
        # one-file scan scope, not stability - age them past STABLE_MIN_AGE so
        # the real seam does not hold every encode test for the age floor
        old = time.time() - 120
        for name in self.names:
            path = self.root / name
            os.utime(path, (old, old))

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
            _toolchain_present(),
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan) as plan,
        ):
            self._run("plan", "a.mkv")
        self.assertEqual(plan.call_count, 1, "a one-file plan planned the folder")

    def test_encode_on_one_file_encodes_one_movie(self) -> None:
        # the engine's encode path calls find_handbrake; a None return
        # makes the encode raise => the command exits 1 naming the missing
        # binary. The touched count is the SCAN's count (1/1 vs the old bug's
        # 3/3), and the untouched files keep their mtimes.
        mtimes_before = {name: (self.root / name).stat().st_mtime_ns for name in self.names}
        with (
            _toolchain_present(),
            patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
        ):
            output = self._run("encode", "a.mkv")
        scan_line = next((line for line in output.splitlines() if "Scanning movies" in line), "")
        self.assertIn("1/1", scan_line, f"a one-file encode scanned the folder: {scan_line}")
        mtimes_after = {name: (self.root / name).stat().st_mtime_ns for name in self.names}
        self.assertEqual(mtimes_before, mtimes_after, "the untouched files changed")

    def test_encode_on_a_folder_scans_the_folder(self) -> None:
        with (
            _toolchain_present(),
            patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
        ):
            output = self._run("encode", str(self.root))
        scan_line = next((line for line in output.splitlines() if "Scanning movies" in line), "")
        self.assertIn("3/3", scan_line, f"a folder encode scanned only one file: {scan_line}")


if __name__ == "__main__":
    unittest.main()
