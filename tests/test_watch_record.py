"""Tests for the watcher recording its outcomes (issue #232).

The watcher was the one writer that recorded nothing: no scan row, no encode
row, no skip row - so the resident mode's work was invisible to the dashboard
and `doctor`, and a declined swap left a row pointing at a staging path the
run deletes. The record callback (passed in by the app, because the engine
layer cannot import the service layer) closes that gap and threads the encode
id into the swap.
"""

import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from shrinkray.engine import watch as watch_mod
from shrinkray.engine.plan import QualityResult


def _isolated_lock(stack: ExitStack, tmp) -> None:
    stack.enter_context(mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "watch.lock"))


class TestWatchRecordsItsOutcomes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.movie = self.inbox / "one.mkv"
        self.movie.write_bytes(b"x" * 100)
        old = time.time() - 3600
        os.utime(self.movie, (old, old))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _encode_result(self, output: Path) -> QualityResult:
        return QualityResult(
            input=self.movie,
            output=output,
            success=True,
            chosen_crf=22,
            achieved_vmaf=95.2,
            input_size=100,
            output_size=40,
        )

    def test_a_successful_encode_is_recorded_with_its_real_location(self) -> None:
        """In place, the record callback receives the SOURCE path as the
        artifact's location - not the staging path the run deletes."""
        recorded: list = []

        def record(movie, result, final_output):
            recorded.append((movie, result, final_output))
            return 42

        def fake_encode(movie, output, **kwargs):
            return self._encode_result(output)

        def fake_swap(db_file, source, output, encode_id=None):
            recorded.append(("swap", source, output, encode_id))

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "_swap", side_effect=fake_swap),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([self.inbox], None, once=True, record=record)
        self.assertEqual(recorded[0][2], self.movie)
        self.assertEqual(recorded[1][3], 42)

    def test_a_declined_swap_corrects_the_row(self) -> None:
        """A swap refused for a retained original clears the row's output.

        The row was written pointing at the source path (the artifact's
        location on success); the swap's refusal discards the encode with its
        staging directory, so the row must be corrected to 'discarded'.
        """
        from shrinkray import db as db_mod
        from shrinkray import replace as replace_mod

        def record(movie, result, final_output):
            return 7

        def fake_encode(movie, output, **kwargs):
            output.write_bytes(b"y" * 40)
            return self._encode_result(output)

        db_file = Path(self.tmp.name) / "meta.db"
        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
            mock.patch.object(replace_mod, "stage", side_effect=replace_mod.SwapError("no")),
            mock.patch.object(db_mod, "set_encode_output") as fake_set,
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([self.inbox], None, once=True, db_file=db_file, record=record)
        fake_set.assert_called_once()

    def test_a_skip_is_recorded_too(self) -> None:
        """A skip verdict reaches the record callback with no final output."""
        recorded: list = []

        def record(movie, result, final_output):
            recorded.append((movie, result, final_output))
            return None

        def fake_encode(movie, output, **kwargs):
            return QualityResult(
                input=movie,
                output=output,
                success=False,
                skipped_reason="no size gain",
            )

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([self.inbox], None, once=True, record=record)
        self.assertEqual(len(recorded), 1)
        self.assertIsNone(recorded[0][2])

    def test_no_callback_records_nothing(self) -> None:
        """A stateless run (record=None) behaves exactly as before."""
        calls: list = []

        def fake_encode(movie, output, **kwargs):
            calls.append(movie)
            return self._encode_result(output)

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "_swap"),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([self.inbox], None, once=True)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
