"""scan's failure honesty (issue #196): a scan where every file failed to
probe used to exit 0 AND write a fabricated report (a `Total;None;0` CSV
row). The run failed; the output must say so. The empty-folder case keeps
its warning + exit 0 (nothing was found is not a failure).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app


def _scan_result(movies, errors):
    """A scan_movies return: (by_category, movies, error_messages)."""
    return ({}, movies, errors)


class TestScanFailureExit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movie = self.root / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str]:
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        # loguru bound the real stderr at import time; route the sink into
        # the runner's capture so log_error's summary is assertable. The
        # scan preflight (#194) must see ffprobe too (CI has none): these
        # tests exercise the all-fail reporting, not the preflight.
        from shrinkray.logger import logger

        sink_id = logger.add(lambda message: runner.output.write(str(message)), level="INFO")
        try:
            with patch("shrinkray.service.shutil.which", return_value="ffprobe"):
                result = runner.invoke(app, list(argv))
            return result.exit_code, result.output
        finally:
            logger.remove(sink_id)
            os.chdir(previous)

    def test_a_scan_where_every_file_failed_exits_nonzero_and_writes_nothing(self) -> None:
        with patch(
            "shrinkray.__main__.scan_movies",
            return_value=_scan_result([self.movie], ["one.mkv: unreadable"]),
        ):
            code, output = self._run("scan", str(self.movie), "--csv")
        self.assertEqual(code, 1, "every file failed, yet scan exited 0")
        self.assertIn("None of the 1 file(s) could be read", output)
        self.assertFalse((self.root / "movies.txt").exists(), "a report was written anyway")
        self.assertFalse((self.root / "movies.csv").exists(), "a CSV was written anyway")

    def test_a_scan_with_results_and_errors_exits_zero_and_writes(self) -> None:
        second = self.root / "two.mkv"
        second.write_bytes(b"x" * 8)

        def fake_scan(root_dir, config, **kwargs):
            # one success (a real category dict, the shape generate_string
            # consumes - every column key present) + one error
            by_category = {
                "two": [
                    {
                        "category": "two",
                        "name": "two.mkv",
                        "duration": "00:00:01",
                        "size": 8,
                        "codec": "h264",
                        "bitrate": None,
                        "aspect_ratio": None,
                        "dimensions": {"width": 1920, "height": 1080},
                        "compression_rate": 2.5,
                        "fps": None,
                        "crop": False,
                        "hdr": False,
                        "other_codecs": "",
                        "audio_languages": "",
                    }
                ]
            }
            return by_category, [self.movie, second], ["one.mkv: unreadable"]

        with patch("shrinkray.__main__.scan_movies", side_effect=fake_scan):
            code, _output = self._run("scan", str(self.root), "--csv")
        self.assertEqual(code, 0, "a partial scan is still a scan")
        self.assertTrue((self.root / "movies.txt").exists())
        self.assertTrue((self.root / "movies.csv").exists())

    def test_a_scan_of_an_empty_folder_keeps_its_warning_and_exit_zero(self) -> None:
        with (
            patch("shrinkray.service.shutil.which", return_value="ffprobe"),
            patch("shrinkray.__main__.scan_movies", return_value=_scan_result([], [])),
        ):
            code, _output = self._run("scan", str(self.root), "--csv")
        self.assertEqual(code, 0, "an empty folder is not a failure")
        self.assertFalse((self.root / "movies.txt").exists())


if __name__ == "__main__":
    unittest.main()
