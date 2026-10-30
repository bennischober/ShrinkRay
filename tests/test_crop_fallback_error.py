"""Tests for the sampled crop fallback's missing-ffmpeg behavior (issue #401).

On a host where ffmpeg is missing from PATH or cannot spawn (EMFILE/ENOMEM),
a plan or encode must report the problem honestly and leave the file alone -
never a FileNotFoundError traceback out of the probe step. The timeline pass
got its guard in #398/#384; the sampled fallback (detect_crop_rect) is this
issue's fix. It raises instead of degrading: the timeline pass spawns first
and already degrades there, so a second degrade would report an unmeasured
file as "no bars" (invariant 8), and the RuntimeError it raises is the shape
the entry points catch.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.engine import crop


class TestSampledFallbackMissingBinary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_missing_ffmpeg_raises_a_clear_error_not_a_traceback(self) -> None:
        """ffmpeg cannot spawn: the fallback reports it through the probe
        step's documented failure shape (RuntimeError), with the cause named.
        """
        with (
            patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
            patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
            patch(
                "shrinkray.engine.crop._run",
                side_effect=FileNotFoundError(2, "No such file or directory: 'ffmpeg'"),
            ),
            self.assertRaisesRegex(RuntimeError, "ffmpeg") as ctx,
        ):
            crop.detect_crop_rect(self.movie, Path(self.tmp.name), count=3)
        self.assertIn("No such file or directory", str(ctx.exception))

    def test_an_unspawnable_ffmpeg_reports_the_oserror_cause(self) -> None:
        """EMFILE/ENOMEM (PermissionError on a failed execve, WSL shape):
        same defined outcome, cause reported, never a bare raise.
        """
        with (
            patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
            patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.crop._run", side_effect=PermissionError(13, "denied")),
            self.assertRaisesRegex(RuntimeError, "denied"),
        ):
            crop.detect_crop_rect(self.movie, Path(self.tmp.name), count=3)

    def test_the_guard_covers_the_fallback_not_the_timeline_pass(self) -> None:
        """The timeline pass owns its own #398 guard; this fix must not turn
        its OSError into a RuntimeError (the caller falls back on any failed
        pass and must keep reaching detect_crop_rect).
        """
        with (
            patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
            patch(
                "shrinkray.engine.crop._run",
                side_effect=FileNotFoundError(2, "No such file or directory: 'ffmpeg'"),
            ),
        ):
            readings, frame = crop.run_crop_timeline(self.movie, cancel=None)
        self.assertEqual(readings, [])
        self.assertIsNone(frame)


class TestSampledWindowPositiveExit(unittest.TestCase):
    """A sampled window whose ffmpeg exits with a positive code (a failed
    decode, or 255 from a caught outside signal) measured nothing: the veto
    rule (bars common to every sampled frame) cannot be evaluated for that
    window, so treating it as bar-free would risk cutting into picture -
    the exact loss the veto exists to prevent (issue #403). The decided
    rule is fail-closed: the whole file is left uncropped and the failure
    is logged with ffmpeg's exit code (the same outcome a window killed by
    a signal already gets, crop.py's negative-code branch).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_windows(self, outcomes: list[tuple[int, str]]) -> tuple:
        """Drives detect_crop_rect with one _run outcome per window (in
        order); returns (result, log lines)."""
        calls: list = []

        def fake_run(command, cancel=None):
            calls.append(command)
            return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

        lines: list[str] = []
        with (
            patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
            patch(
                "shrinkray.engine.crop.subprocess.run",
                return_value=type(
                    "P",
                    (),
                    {
                        "returncode": 0,
                        "stdout": b'{"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}',
                    },
                )(),
            ),
            patch("shrinkray.engine.crop._run", side_effect=fake_run),
            patch("shrinkray.engine.crop.log", lines.append),
        ):
            result = crop.detect_crop_rect(self.movie, Path(self.tmp.name), count=len(outcomes))
        return result, lines

    def test_a_failed_window_leaves_the_file_uncropped_and_logs_the_exit(self) -> None:
        """windows [bars, (255, error), bars]: the failed window is the veto
        the rule cannot evaluate - the crop is None and ffmpeg's exit code
        is in the log (issue #403's acceptance criterion).
        """
        result, lines = self._run_windows(
            [(0, "crop=1920:800:0:140"), (255, "some ffmpeg error"), (0, "crop=1920:800:0:140")]
        )
        self.assertIsNone(result, "a failed window must veto the whole crop")
        self.assertTrue(
            any("exited 255" in l for l in lines),
            f"the failure must be logged with the exit code; got: {lines}",
        )

    def test_a_failed_first_window_vetoes_before_any_rect_is_collected(self) -> None:
        """windows [(1, error), bars, bars]: same rule, failure first - the
        remaining windows' bars must not be intersected without the failed
        window's measurement.
        """
        result, lines = self._run_windows(
            [(1, "decode error"), (0, "crop=1920:800:0:140"), (0, "crop=1920:800:0:140")]
        )
        self.assertIsNone(result)
        self.assertTrue(any("exited 1" in l for l in lines), f"log: {lines}")

    def test_a_failed_window_alone_still_logs_and_uncrops(self) -> None:
        """windows [(255, error)] only: the file is uncropped and the exit
        is logged - the failure never reads as 'no bars'.
        """
        result, lines = self._run_windows([(255, "signal caught")])
        self.assertIsNone(result)
        self.assertTrue(any("exited 255" in l for l in lines), f"log: {lines}")


if __name__ == "__main__":
    unittest.main()
