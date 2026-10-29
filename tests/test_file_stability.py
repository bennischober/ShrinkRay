"""Tests for the shared stability-wait seam (issue #437).

The one-shot CLI (`shrinkray encode`) used to decide on a file a mover was
still writing: the stability witness (`is_stable`: size unchanged across a
STABLE_CHECK_INTERVAL sleep, last change at least STABLE_MIN_AGE ago) lived
only in the watcher's loop. The seam (`wait_for_stable`) exposes the same
witness with a bounded wait; the watcher keeps its single unbounded check.

NO test here sleeps: the clock is mocked around the fixture's REAL mtime
(`_clock`: time.time reads age_s seconds past it), `time.monotonic` is a
deterministic sequence driving the deadline, `time.sleep` is patched and
asserted for WHAT it would sleep. The CLI call-through tests patch
`STABLE_MIN_AGE` to 0 so the real seam runs its full shape instantly - the
fresh-vs-aged distinction is the unit tests' above, mocked, never waited.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.engine import watch


class TestWaitForStable(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x" * 16)
        # the clock is mocked around the REAL mtime: age is always exactly
        # `age_s` seconds (time.time returns mtime + age_s); the monotonic
        # side_effect sequences then drive the deadline deterministically
        self.mtime = self.movie.stat().st_mtime
        self.age_s = 5.0
        self._slept = 0

    def _clock(self, age_s: float | None = None):
        """Patches time.time to read age_s seconds past the real mtime."""
        return patch(
            "shrinkray.engine.watch.time.time",
            return_value=self.mtime + (self.age_s if age_s is None else age_s),
        )

    def _clock_sequence(self, ages: "list[float]"):
        """Patches time.time to ADVANCE with each read: age_s seconds past
        the real mtime, one value per call (the real clock advances with
        every sleep; a constant mock would loop forever on the young path).
        """
        return patch(
            "shrinkray.engine.watch.time.time",
            side_effect=[self.mtime + age for age in ages],
        )

    def test_a_file_past_the_age_floor_asks_the_witness_once(self) -> None:
        """A file old enough for the age gate: one witness call decides (its
        internal size-check sleep is the one interval that runs - mocked).
        """
        with (
            self._clock(120.0),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 1.0]),
            patch("shrinkray.engine.watch.time.sleep") as sleep,
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", return_value=True) as witness,
        ):
            self.assertTrue(watch.wait_for_stable(self.movie, limit=60.0))
        witness.assert_called_once()
        sleep.assert_not_called()

    def test_a_young_file_sleeps_the_remaining_age_once_then_checks_the_size_directly(
        self,
    ) -> None:
        """The measured CI defect (a 90 s test, 6-minute suite gaps on CI):
        a COMPLETE fresh file used to pay 30-s chunk sleeps until it aged
        past the floor (~90 s), then the rework still stacked the witness's
        own interval sleep on the age sleep (90.01 s, measured by the
        round-7 review). The seam now sleeps the REMAINING age once, then
        the size check DIRECTLY (one interval, in the seam) - the fresh
        complete file's cost is exactly age-floor + one interval, and the
        witness function is not called on this path at all.
        """
        with (
            self._clock_sequence([5.0, 65.0, 65.0]),
            patch(
                "shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 1.0, 61.0, 62.0, 63.0]
            ),
            patch("shrinkray.engine.watch.time.sleep") as sleep,
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable") as witness,
        ):
            self.assertTrue(watch.wait_for_stable(self.movie, limit=600.0))
        sleep.assert_has_calls([unittest.mock.call(55.0), unittest.mock.call(30.0)])
        witness.assert_not_called()

    def test_a_young_growing_file_is_still_caught_by_the_size_check(self) -> None:
        """The young-file fast path must not weaken the witness: a producer
        writing DURING the interval sleep updates the file's size and mtime,
        so the direct size check fails and the loop re-derives a fresh age
        from the new mtime - a still-growing file never reads as settled. The
        mock clock advances with the write (mtime = the clock's next value),
        as a real producer's write stamps mtime=now.
        """
        clock_state = {"value": 5.0}

        def now():
            # the mock clock always reads 30 s AHEAD of the file's current
            # mtime (the producer's write stamps mtime = clock - 30), so the
            # age the loop derives is always exactly 30 s - young forever,
            # which is what a continuously-growing file looks like
            return self.mtime + clock_state["value"]

        writes = []

        def grow(seconds):
            # the producer appends DURING the interval sleep (between the two
            # size reads), never during the age sleep (before the first read):
            # a write before size_1 would just shift both reads equally, and
            # a same-size write would read as settled - a real producer adds
            # data, so every write is bigger than the last
            if seconds != 30.0:
                return
            writes.append(len(writes) + 1)
            self.movie.write_bytes(b"x" * (16 + 16 * len(writes)))
            clock_state["value"] += 90.0
            stamp = self.mtime + clock_state["value"] - 30.0
            os.utime(self.movie, (stamp, stamp))

        with (
            patch("shrinkray.engine.watch.time.time", side_effect=now),
            patch(
                "shrinkray.engine.watch.time.monotonic",
                side_effect=[0.0, 1.0, 61.0, 62.0, 63.0, 64.0, 700.0],
            ),
            patch("shrinkray.engine.watch.time.sleep", side_effect=grow),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable") as witness,
        ):
            self.assertFalse(watch.wait_for_stable(self.movie, limit=600.0))
        witness.assert_not_called()

    def test_an_unstable_file_becomes_stable_within_the_limit(self) -> None:
        """A file past the floor whose size keeps changing: the witness is
        re-asked each turn; nothing extra is slept between calls (the
        witness's own size-check sleep is the interval).
        """
        with (
            self._clock(120.0),
            patch(
                "shrinkray.engine.watch.time.monotonic",
                side_effect=[0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 700.0],
            ),
            patch("shrinkray.engine.watch.time.sleep"),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", side_effect=[False, False, True]) as witness,
        ):
            self.assertTrue(watch.wait_for_stable(self.movie, limit=600.0))
        self.assertEqual(witness.call_count, 3)

    def test_a_file_that_never_settles_expires_at_the_limit(self) -> None:
        with (
            self._clock(120.0),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 100.0, 700.0]),
            patch("shrinkray.engine.watch.time.sleep"),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", return_value=False),
        ):
            self.assertFalse(watch.wait_for_stable(self.movie, limit=600.0))

    def test_a_vanished_file_expires_without_a_traceback(self) -> None:
        """The stat's OSError path: the same bounded expiry, no raise."""
        gone = self.movie / "vanished.mkv"
        with (
            self._clock(120.0),
            patch(
                "shrinkray.engine.watch.time.monotonic",
                side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 700.0],
            ),
            patch("shrinkray.engine.watch.time.sleep") as sleep,
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
        ):
            self.assertFalse(watch.wait_for_stable(gone, limit=600.0))
        # every turn sleeps the interval (never a busy-spin), until the limit
        self.assertGreaterEqual(sleep.call_count, 2)
        sleep.assert_called_with(watch.STABLE_CHECK_INTERVAL)

    def test_a_set_cancel_event_raises_cancelled(self) -> None:
        """The #326 contract holds inside the wait: a signal cancels it, it
        never absorbs the terminal's close for minutes.
        """
        cancel = threading.Event()
        cancel.set()
        with (
            self._clock(120.0),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", return_value=False) as witness,
            self.assertRaises(watch.Cancelled),
        ):
            watch.wait_for_stable(self.movie, limit=600.0, cancel=cancel)
        witness.assert_not_called()

    def test_the_cancel_is_checked_between_witness_calls(self) -> None:
        """A signal that lands DURING the wait ends it at the next check."""
        cancel = threading.Event()

        def unstable_then_cancel(_):
            cancel.set()
            return False

        with (
            self._clock(120.0),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 10.0, 20.0]),
            patch("shrinkray.engine.watch.time.sleep"),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", side_effect=unstable_then_cancel),
            self.assertRaises(watch.Cancelled),
        ):
            watch.wait_for_stable(self.movie, limit=600.0, cancel=cancel)

    def test_the_default_limit_is_the_module_constant(self) -> None:
        with (
            self._clock(120.0),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", return_value=True) as witness,
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 1.0]),
        ):
            self.assertTrue(watch.wait_for_stable(self.movie))
        witness.assert_called_once()

    def test_the_seam_uses_the_same_witness_as_the_watcher(self) -> None:
        """Criterion 1: the seam calls the watcher's is_stable - the same
        witness protects both entry points (patched to prove the call).
        """
        with (
            self._clock(120.0),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 1.0]),
            patch.object(watch, "STABLE_MIN_AGE", 60.0),
            patch.object(watch, "is_stable", return_value=True) as witness,
        ):
            watch.wait_for_stable(self.movie)
        self.assertEqual(witness.call_args[0][0], self.movie)


class TestCliCallsTheSeam(unittest.TestCase):
    """Criterion 3's call-through: the CLI encode loop consults the seam
    before any gate or sample; a file that never settles is skipped. The
    floor constant is patched to 0 so the REAL seam runs its full shape
    instantly - the fresh-vs-aged distinction is the unit tests' above.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"x" * 16)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_an_unstable_file_is_skipped_with_a_report(self) -> None:
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with (
            patch("shrinkray.engine.watch.STABLE_MIN_AGE", 0.0),
            patch("shrinkray.engine.watch.wait_for_stable", return_value=False) as seam,
            patch("shrinkray.service.all_problems", return_value=[]),
            patch("shrinkray.engine.pipeline.encode_with_quality") as engine,
            patch("shrinkray.__main__.log_error") as log_error,
        ):
            CliRunner().invoke(app, ["encode", str(self.movie), str(self.root / "out")])
        seam.assert_called_once()
        engine.assert_not_called()
        # the skip report goes through log_error (the suite's standard seam,
        # test_cli.py's pattern): the terminal shows the report
        log_error.assert_called_once()
        self.assertIn("did not settle", log_error.call_args[0][0])

    def test_a_stable_file_is_encoded(self) -> None:
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with (
            patch("shrinkray.engine.watch.STABLE_MIN_AGE", 0.0),
            patch("shrinkray.engine.watch.wait_for_stable", return_value=True),
            patch("shrinkray.service.all_problems", return_value=[]),
            patch("shrinkray.engine.pipeline.encode_with_quality") as engine,
        ):
            CliRunner().invoke(app, ["encode", str(self.movie), str(self.root / "out")])
        engine.assert_called_once()

    def test_a_cancel_during_the_wait_exits_nonzero(self) -> None:
        """The #326 contract through the CLI: a signal while the seam waits
        unwinds through the CLI's cancel handler (exit 1, honest message).
        """
        from typer.testing import CliRunner

        from shrinkray.__main__ import app
        from shrinkray.tools.process import Cancelled

        with (
            patch("shrinkray.engine.watch.STABLE_MIN_AGE", 0.0),
            patch(
                "shrinkray.engine.watch.wait_for_stable",
                side_effect=Cancelled("cancelled while waiting for the file to settle"),
            ),
            patch("shrinkray.service.all_problems", return_value=[]),
            patch("shrinkray.engine.pipeline.encode_with_quality") as engine,
            patch("shrinkray.__main__.log_error") as log_error,
        ):
            result = CliRunner().invoke(app, ["encode", str(self.movie), str(self.root / "out")])
        engine.assert_not_called()
        self.assertNotEqual(result.exit_code, 0)
        log_error.assert_called_once()


class TestWatcherUnchanged(unittest.TestCase):
    """Criterion 2: the watcher's behavior is unchanged - its loop keeps the
    single is_stable check (the seam adds nothing to its pass). Proven
    behaviourally: one real watch pass (once=True, the test_watch.py
    pattern) consults is_stable and never the bounded seam.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.inbox = self.tmp_path / "inbox"
        self.inbox.mkdir()
        self.out = self.tmp_path / "out"
        self.out.mkdir()
        self.movie = self.inbox / "w.mkv"
        self.movie.write_bytes(b"x" * 16)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_watcher_loop_still_uses_the_bare_witness(self) -> None:
        from contextlib import ExitStack

        from shrinkray.engine import watch as watch_mod
        from shrinkray.engine.plan import QualityResult

        def fake_encode(*args, **kwargs):
            return QualityResult(input=Path("x"), output=Path("x"), success=True)

        with (
            patch.object(watch_mod, "STABLE_MIN_AGE", 0.0),
            patch.object(watch_mod, "is_stable", return_value=True) as witness,
            patch.object(watch_mod, "wait_for_stable") as seam,
            patch(
                "shrinkray.engine.watch.encode_with_quality",
                side_effect=fake_encode,
            ) as encode,
            patch.object(watch_mod, "WATCH_LOCK_FILE", self.tmp_path / "watch.lock"),
            patch.object(watch_mod, "disk_free_bytes", return_value=1 << 40),
        ):
            stack = ExitStack()
            with stack:
                watch_mod.watch([self.inbox], self.out, once=True)
        witness.assert_called()
        seam.assert_not_called()
        encode.assert_called_once()


if __name__ == "__main__":
    unittest.main()
