"""Tests that watch() forwards the progress callback to the engine (issue
#178): the CLI's live display is fed by the engine's events THROUGH watch,
so the forwarding is the wiring under test.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shrinkray.engine.plan import QualityResult


class TestWatchProgressForwarding(unittest.TestCase):
    def test_watch_forwards_progress_to_encode_with_quality(self) -> None:
        """The progress callback reaches encode_with_quality (issue #178)."""
        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            old = time.time() - 3600
            os.utime(movie, (old, old))
            events: list[dict] = []

            def callback(data: dict) -> None:
                events.append(data)

            def fake_quality(movie, output_file, **kwargs):
                # the engine would emit these; the fake asserts the FORWARDING
                kwargs["progress"]({"stage": "encode", "percent": 55.0, "movie": movie.name})
                return QualityResult(
                    input=movie,
                    output=output_file,
                    success=True,
                    chosen_crf=22,
                    achieved_vmaf=95.2,
                    input_size=100,
                    output_size=40,
                )

            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            ):
                watch_mod.watch([inbox], out, once=True, progress=callback)
            self.assertEqual(events, [{"stage": "encode", "percent": 55.0, "movie": "movie.mkv"}])

    def test_watch_without_progress_still_encodes(self) -> None:
        """progress=None (the default) changes nothing — the old path."""
        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            old = time.time() - 3600
            os.utime(movie, (old, old))
            called: dict = {}

            def fake_quality(movie, output_file, **kwargs):
                called["progress_is_none"] = kwargs.get("progress") is None
                return QualityResult(
                    input=movie,
                    output=output_file,
                    success=True,
                    chosen_crf=22,
                    achieved_vmaf=95.2,
                    input_size=100,
                    output_size=40,
                )

            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            ):
                watch_mod.watch([inbox], out, once=True)
            self.assertTrue(called["progress_is_none"])


if __name__ == "__main__":
    unittest.main()
