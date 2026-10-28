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


if __name__ == "__main__":
    unittest.main()
