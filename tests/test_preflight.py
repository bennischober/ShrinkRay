"""The toolchain preflight (issue #194): scan/plan/encode refused nothing
when a binary was missing - the first evidence was a wall of per-file errors
(scan even exited 0). `toolchain_problems` now also checks ffprobe (which it
never did), the commands gate on their OWN prerequisites (scan needs ffprobe
only, plan/encode the whole toolchain), and the refusal happens before any
work.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app


def _capture_logs():
    """A loguru sink that appends into a list (CliRunner cannot see loguru's
    own stderr binding, made at import time)."""
    from shrinkray.logger import logger

    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    return captured, sink_id, logger.remove


class TestToolchainPreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movie = self.root / "a.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str]:
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        captured, sink_id, remove = _capture_logs()
        try:
            result = runner.invoke(app, list(argv))
            return result.exit_code, "\n".join(captured) + "\n" + result.output
        finally:
            remove(sink_id)
            os.chdir(previous)

    def test_toolchain_problems_reports_a_missing_ffprobe(self) -> None:
        from shrinkray.service import all_problems

        with patch("shrinkray.service.shutil.which", return_value=None):
            problems = all_problems()
        ffprobe_lines = [p for p in problems if "ffprobe" in p]
        self.assertTrue(ffprobe_lines, f"no ffprobe entry in {problems}")
        self.assertIn("install ffmpeg", ffprobe_lines[0])

    def test_plan_without_ffprobe_refuses(self) -> None:
        with patch("shrinkray.service.shutil.which", return_value=None):
            code, output = self._run("plan", str(self.movie))
        self.assertEqual(code, 1, "plan ran without ffprobe")
        self.assertIn("ffprobe", output)

    def test_encode_without_handbrake_refuses(self) -> None:
        from shrinkray.tools import handbrake as hb

        with patch.object(hb, "find_handbrake", return_value=None):
            code, output = self._run("encode", str(self.movie))
        self.assertEqual(code, 1, "encode ran without HandBrakeCLI")
        self.assertIn("HandBrakeCLI", output)


if __name__ == "__main__":
    unittest.main()
