"""Tests for the file-stability witness and its one-shot wait (issues #437, #479).

The witness (`check_stability`) decides from a single `stat()`: the file's
last write is at least STABLE_MIN_AGE old, and the file is not sparse
(allocated bytes below SPARSE_ALLOCATED_FLOOR of its size). It never sleeps,
so a settled file costs nothing (#479: the old witness slept a 30 s size
re-check on every file). The one-shot CLI waits through `wait_until_settled`
(bounded, cancel-aware); the watcher asks the witness once per pass.

NO test here sleeps: the clock is mocked around the fixture's REAL mtime,
`time.monotonic` is a deterministic sequence driving the deadline, and
`time.sleep` is patched and asserted for WHAT it would sleep. Allocation is
mocked with a stand-in stat result, except in the tests that ask the real
filesystem (skipped where it cannot answer).
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from shrinkray.engine import watch

# `Stability` is always read through the module, never imported by name:
# test_cli's env-guard tests reload `engine.watch`, after which a class
# imported here would be a stale copy that no longer compares equal

MIB = 1024 * 1024


class _StatPath:
    """A path whose `stat()` returns a chosen result (the witness's only read)."""

    def __init__(self, **fields: float) -> None:
        self.result = SimpleNamespace(**fields)

    def stat(self) -> SimpleNamespace:
        return self.result


def _old(size: int, blocks: int | None, age: float = 3600.0) -> _StatPath:
    """A stand-in file `age` seconds old (against a clock frozen at 10^9)."""
    fields: dict[str, float] = {"st_mtime": 1e9 - age, "st_size": size}
    if blocks is not None:
        fields["st_blocks"] = blocks
    return _StatPath(**fields)


def _frozen_clock():
    return patch("shrinkray.engine.watch.time.time", return_value=1e9)


class TestCheckStability(unittest.TestCase):
    """The witness itself: age plus not-sparse, from one stat, never a sleep."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.stack.enter_context(_frozen_clock())
        self.stack.enter_context(patch.object(watch, "STABLE_MIN_AGE", 60.0))
        self.sleep = self.stack.enter_context(patch("shrinkray.engine.watch.time.sleep"))

    def tearDown(self) -> None:
        self.stack.close()

    def test_a_settled_file_is_accepted_without_any_sleep(self) -> None:
        """The #479 cost: a long-settled, fully allocated file used to pay a
        30 s size re-check; it is now accepted from one stat, zero sleep."""
        verdict = watch.check_stability(_old(100 * MIB, blocks=100 * MIB // 512))
        self.assertTrue(verdict.settled)
        self.assertEqual(verdict, watch.Stability(young_for=0.0, incomplete=None))
        self.sleep.assert_not_called()

    def test_a_young_file_reports_its_remaining_age(self) -> None:
        verdict = watch.check_stability(_old(100 * MIB, blocks=100 * MIB // 512, age=5.0))
        self.assertFalse(verdict.settled)
        self.assertEqual(verdict.young_for, 55.0)
        self.assertIsNone(verdict.incomplete)
        self.sleep.assert_not_called()

    def test_an_old_sparse_file_is_incomplete_with_a_reason(self) -> None:
        """A stalled sparse download: full size from the start, 41 % filled.
        The size never changed, so the old size re-check could not see it."""
        size = 100 * MIB
        verdict = watch.check_stability(_old(size, blocks=int(size * 0.41) // 512))
        self.assertFalse(verdict.settled)
        self.assertEqual(verdict.young_for, 0.0)
        assert verdict.incomplete is not None
        self.assertIn("appears incomplete", verdict.incomplete)
        self.assertIn("41 %", verdict.incomplete)

    def test_a_young_sparse_file_is_young_not_incomplete(self) -> None:
        """A sparse file still being written is a copy in progress: it waits on
        its age like any other, it is not skipped as stalled."""
        size = 100 * MIB
        verdict = watch.check_stability(_old(size, blocks=size // 10 // 512, age=5.0))
        self.assertEqual(verdict.young_for, 55.0)
        self.assertIsNone(verdict.incomplete)

    def test_the_sparse_floor_is_ninety_percent_allocated(self) -> None:
        """Exactly 90 % allocated passes; one block less fails, and its reason
        never rounds up to the 90 % that passes."""
        size = 1000 * 512
        at_floor = watch.check_stability(_old(size, blocks=900))
        below = watch.check_stability(_old(size, blocks=899))
        self.assertTrue(at_floor.settled)
        assert below.incomplete is not None
        self.assertIn("89 %", below.incomplete)

    def test_zero_allocated_blocks_means_unknown_and_age_alone_decides(self) -> None:
        """Refinement A: some FUSE/SMB filesystems report st_blocks = 0 for a
        complete file. That is "cannot judge", never "sparse" - otherwise no
        file on such a mount would ever be encoded."""
        verdict = watch.check_stability(_old(100 * MIB, blocks=0))
        self.assertTrue(verdict.settled)

    def test_a_platform_without_st_blocks_uses_age_alone(self) -> None:
        """Windows reports no st_blocks: the age check alone applies."""
        self.assertTrue(watch.check_stability(_old(100 * MIB, blocks=None)).settled)
        young = watch.check_stability(_old(100 * MIB, blocks=None, age=10.0))
        self.assertEqual(young.young_for, 50.0)

    def test_an_empty_file_is_not_judged_sparse(self) -> None:
        self.assertTrue(watch.check_stability(_old(0, blocks=0)).settled)

    def test_a_vanished_file_raises_oserror(self) -> None:
        """The callers own the unreadable case (the watcher: later; the
        one-shot wait: retried until its limit)."""
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(OSError):
            watch.check_stability(Path(tmp) / "gone.mkv")


@unittest.skipIf(sys.platform == "win32", "Windows reports no st_blocks")
class TestCheckStabilityOnTheRealFilesystem(unittest.TestCase):
    """The allocation arithmetic against real files: st_blocks is in 512-byte
    units, so a fully written file must read as complete and a truncated-to-
    size file as sparse. Only the clock is mocked (to age the files)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _aged(self, path: Path) -> watch.Stability:
        with patch("shrinkray.engine.watch.time.time", return_value=path.stat().st_mtime + 3600):
            return watch.check_stability(path)

    def test_a_fully_written_file_is_settled(self) -> None:
        movie = self.root / "complete.mkv"
        movie.write_bytes(os.urandom(4 * MIB))
        self.assertTrue(self._aged(movie).settled)

    def test_a_truncated_to_size_file_is_incomplete(self) -> None:
        movie = self.root / "stalled.mkv"
        with movie.open("wb") as f:
            f.write(os.urandom(MIB))
            f.truncate(16 * MIB)
        stat = movie.stat()
        if stat.st_blocks * 512 >= stat.st_size:
            self.skipTest("this filesystem does not create sparse files")
        verdict = self._aged(movie)
        assert verdict.incomplete is not None
        self.assertIn("appears incomplete", verdict.incomplete)


class TestWaitUntilSettled(unittest.TestCase):
    """The one-shot wait: bounded, cancel-aware, no sleep for a settled file."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x" * 16)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(watch, "STABLE_MIN_AGE", 60.0))
        self.sleep = self.stack.enter_context(patch("shrinkray.engine.watch.time.sleep"))

    def tearDown(self) -> None:
        self.stack.close()
        self.tmp.cleanup()

    def _verdicts(self, *verdicts: watch.Stability):
        return self.stack.enter_context(
            patch.object(watch, "check_stability", side_effect=list(verdicts))
        )

    def _monotonic(self, *values: float) -> None:
        self.stack.enter_context(
            patch("shrinkray.engine.watch.time.monotonic", side_effect=list(values))
        )

    def test_a_settled_file_returns_at_once_without_sleeping(self) -> None:
        self._monotonic(0.0)
        witness = self._verdicts(watch.Stability(0.0, None))
        self.assertIsNone(watch.wait_until_settled(self.movie, limit=600.0))
        witness.assert_called_once_with(self.movie)
        self.sleep.assert_not_called()

    def test_a_real_long_settled_file_costs_zero_sleep(self) -> None:
        """The acceptance's pinned cost, through the real witness: a file
        aged past the floor is accepted with nothing slept."""
        old = time.time() - 3600
        os.utime(self.movie, (old, old))
        self._monotonic(0.0)
        self.assertIsNone(watch.wait_until_settled(self.movie, limit=600.0))
        self.sleep.assert_not_called()

    def test_a_young_file_sleeps_only_its_remaining_age(self) -> None:
        """A complete fresh file costs its remaining age, nothing more (the
        old witness added a 30 s size interval on top)."""
        self._monotonic(0.0, 1.0, 56.0)
        self._verdicts(watch.Stability(55.0, None), watch.Stability(0.0, None))
        self.assertIsNone(watch.wait_until_settled(self.movie, limit=600.0))
        self.assertEqual(self.sleep.call_args_list, [call(55.0)])

    def test_a_file_written_during_the_wait_waits_again_from_its_new_mtime(self) -> None:
        self._monotonic(0.0, 1.0, 56.0, 106.0)
        self._verdicts(
            watch.Stability(55.0, None), watch.Stability(50.0, None), watch.Stability(0.0, None)
        )
        self.assertIsNone(watch.wait_until_settled(self.movie, limit=600.0))
        self.assertEqual(self.sleep.call_args_list, [call(55.0), call(50.0)])

    def test_a_growing_file_never_settles_and_expires_at_the_limit(self) -> None:
        """#437's protection, with a real growing fixture and the real
        witness: a producer writing during every wait stamps a fresh mtime,
        so the file always reads young and is never accepted."""
        clock = {"now": self.movie.stat().st_mtime + 5.0}

        def grow(seconds: float) -> None:
            # the producer appends while the wait sleeps; its write stamps
            # mtime = now, so the next judgement sees a fresh file again
            clock["now"] += seconds
            with self.movie.open("ab") as f:
                f.write(b"x" * 16)
            os.utime(self.movie, (clock["now"], clock["now"]))

        self.sleep.side_effect = grow
        self.stack.enter_context(
            patch("shrinkray.engine.watch.time.time", side_effect=lambda: clock["now"])
        )
        ticks = iter(range(0, 10_000, 60))
        self.stack.enter_context(
            patch("shrinkray.engine.watch.time.monotonic", side_effect=lambda: float(next(ticks)))
        )
        reason = watch.wait_until_settled(self.movie, limit=600.0)
        assert reason is not None
        self.assertIn("did not settle within 600 s", reason)
        self.assertGreater(self.sleep.call_count, 1)

    def test_an_old_sparse_file_is_skipped_at_once_with_its_reason(self) -> None:
        """Refinement B: a stalled download is not waited on - another 600 s
        would not finish it. Its reason goes to the caller unchanged."""
        self._monotonic(0.0)
        self._verdicts(watch.Stability(0.0, "appears incomplete: only 41 % ..."))
        self.assertEqual(
            watch.wait_until_settled(self.movie, limit=600.0),
            "appears incomplete: only 41 % ...",
        )
        self.sleep.assert_not_called()

    def test_a_wait_that_would_pass_the_limit_gives_up_without_sleeping(self) -> None:
        self._monotonic(0.0, 590.0)
        self._verdicts(watch.Stability(55.0, None))
        reason = watch.wait_until_settled(self.movie, limit=600.0)
        assert reason is not None
        self.assertIn("did not settle within 600 s", reason)
        self.sleep.assert_not_called()

    def test_the_default_limit_is_the_module_constant(self) -> None:
        self._monotonic(0.0, 1.0)
        self._verdicts(watch.Stability(watch.STABLE_WAIT_LIMIT + 1.0, None))
        reason = watch.wait_until_settled(self.movie)
        assert reason is not None
        self.assertIn(f"{int(watch.STABLE_WAIT_LIMIT)} s", reason)

    def test_a_vanished_file_is_retried_on_the_interval_until_the_limit(self) -> None:
        gone = self.movie.parent / "vanished.mkv"
        self._monotonic(0.0, 1.0, 31.0, 61.0)
        reason = watch.wait_until_settled(gone, limit=90.0)
        assert reason is not None
        self.assertIn("could not be read within 90 s", reason)
        # every turn sleeps the interval (never a busy-spin); the third would
        # end past the limit (61 + 30 >= 90), so the wait gives up instead
        self.assertEqual(
            self.sleep.call_args_list,
            [call(watch.STABLE_CHECK_INTERVAL), call(watch.STABLE_CHECK_INTERVAL)],
        )

    def test_a_set_cancel_event_raises_before_the_witness(self) -> None:
        """The #326 contract: a signal cancels the wait, never absorbed."""
        cancel = threading.Event()
        cancel.set()
        self._monotonic(0.0)
        witness = self._verdicts(watch.Stability(0.0, None))
        with self.assertRaises(watch.Cancelled):
            watch.wait_until_settled(self.movie, limit=600.0, cancel=cancel)
        witness.assert_not_called()

    def test_a_cancel_landing_during_the_age_sleep_raises(self) -> None:
        """A signal during the remaining-age sleep raises before the next
        judgement - even when that judgement would have accepted the file."""
        cancel = threading.Event()
        self.sleep.side_effect = lambda seconds: cancel.set()
        self._monotonic(0.0, 1.0)
        witness = self._verdicts(watch.Stability(55.0, None), watch.Stability(0.0, None))
        with self.assertRaises(watch.Cancelled):
            watch.wait_until_settled(self.movie, limit=600.0, cancel=cancel)
        self.assertEqual(witness.call_count, 1)


class TestCliCallsTheSeam(unittest.TestCase):
    """The CLI encode loop consults the wait before any gate or sample; a file
    the wait rejects is skipped with the wait's reason."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"x" * 16)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_rejected_file_is_skipped_with_the_reason(self) -> None:
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        reason = "appears incomplete: only 41 % of its size is allocated on disk"
        with (
            patch("shrinkray.engine.watch.wait_until_settled", return_value=reason) as seam,
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
        self.assertIn(reason, log_error.call_args[0][0])
        self.assertIn("movie.mkv", log_error.call_args[0][0])

    def test_a_settled_file_is_encoded_without_a_wait(self) -> None:
        """Through the REAL wait: a long-settled file reaches the engine and
        nothing is slept on the way."""
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        old = time.time() - 3600
        os.utime(self.movie, (old, old))
        with (
            patch("shrinkray.engine.watch.time.sleep") as sleep,
            patch("shrinkray.service.all_problems", return_value=[]),
            patch("shrinkray.engine.pipeline.encode_with_quality") as engine,
        ):
            CliRunner().invoke(app, ["encode", str(self.movie), str(self.root / "out")])
        engine.assert_called_once()
        sleep.assert_not_called()

    def test_a_cancel_during_the_wait_exits_nonzero(self) -> None:
        """The #326 contract through the CLI: a signal while the seam waits
        unwinds through the CLI's cancel handler (exit 1, honest message).
        """
        from typer.testing import CliRunner

        from shrinkray.__main__ import app
        from shrinkray.tools.process import Cancelled

        with (
            patch(
                "shrinkray.engine.watch.wait_until_settled",
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


class TestWatcherUsesTheWitness(unittest.TestCase):
    """The watcher asks the same witness once per candidate per pass, never
    the bounded wait; an old sparse file is reported once, not every pass."""

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

    def _common(self, stack: ExitStack):
        from shrinkray.engine.plan import QualityResult

        def fake_encode(*args, **kwargs):
            return QualityResult(input=Path("x"), output=Path("x"), success=True)

        stack.enter_context(patch.object(watch, "WATCH_LOCK_FILE", self.tmp_path / "watch.lock"))
        stack.enter_context(patch.object(watch, "disk_free_bytes", return_value=1 << 40))
        return stack.enter_context(
            patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_encode)
        )

    def test_the_watcher_asks_the_witness_not_the_bounded_wait(self) -> None:
        with ExitStack() as stack:
            encode = self._common(stack)
            witness = stack.enter_context(
                patch.object(watch, "check_stability", return_value=watch.Stability(0.0, None))
            )
            seam = stack.enter_context(patch.object(watch, "wait_until_settled"))
            watch.watch([self.inbox], self.out, once=True)
        witness.assert_called()
        seam.assert_not_called()
        encode.assert_called_once()

    def test_an_old_sparse_file_is_reported_once_and_never_decided(self) -> None:
        stop = threading.Event()
        passes = {"n": 0}

        def two_passes(interval, stop_event, **kwargs):
            passes["n"] += 1
            if passes["n"] >= 2 and stop_event is not None:
                stop_event.set()

        reason = "appears incomplete: only 41 % of its size is allocated on disk"
        with ExitStack() as stack:
            encode = self._common(stack)
            stack.enter_context(
                patch.object(watch, "check_stability", return_value=watch.Stability(0.0, reason))
            )
            stack.enter_context(patch.object(watch, "_wait_or_stop", side_effect=two_passes))
            log = stack.enter_context(patch.object(watch, "log"))
            watch.watch([self.inbox], self.out, once=False, stop_event=stop)
        self.assertEqual(passes["n"], 2, "the watcher did not run a second pass")
        encode.assert_not_called()
        reports = [c for c in log.call_args_list if reason in c[0][0]]
        self.assertEqual(len(reports), 1, "a stalled sparse file was re-reported every pass")
        self.assertIn("w.mkv", reports[0][0][0])

    def test_a_young_file_is_left_for_a_later_pass(self) -> None:
        with ExitStack() as stack:
            encode = self._common(stack)
            stack.enter_context(
                patch.object(watch, "check_stability", return_value=watch.Stability(30.0, None))
            )
            log = stack.enter_context(patch.object(watch, "log"))
            watch.watch([self.inbox], self.out, once=True)
        encode.assert_not_called()
        self.assertTrue(any("not stable yet" in c[0][0] for c in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
