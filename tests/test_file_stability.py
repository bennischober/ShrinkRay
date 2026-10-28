"""Tests for the shared stability-wait seam (issue #437).

The one-shot CLI (`shrinkray encode`) used to decide on a file a mover was
still writing: the stability witness (`is_stable`: size unchanged across a
STABLE_CHECK_INTERVAL sleep, last change at least STABLE_MIN_AGE ago) lived
only in the watcher's loop. The seam (`wait_for_stable`) exposes the same
witness with a bounded wait; the watcher keeps its single unbounded check.
Every test here is timing-free: the witness is patched, never the clock -
is_stable's own 30 s interval sleep is behind the patch.
"""

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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_stable_file_returns_immediately(self) -> None:
        with patch.object(watch, "is_stable", return_value=True) as witness:
            self.assertTrue(watch.wait_for_stable(self.movie, limit=60.0))
        self.assertEqual(witness.call_count, 1)

    def test_an_unstable_file_becomes_stable_within_the_limit(self) -> None:
        answers = iter([False, False, True])
        with (
            patch.object(watch, "is_stable", side_effect=lambda _: next(answers)),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 10.0, 20.0, 30.0]),
        ):
            self.assertTrue(watch.wait_for_stable(self.movie, limit=600.0))

    def test_a_file_that_never_settles_expires_at_the_limit(self) -> None:
        with (
            patch.object(watch, "is_stable", return_value=False),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 100.0, 700.0]),
        ):
            self.assertFalse(watch.wait_for_stable(self.movie, limit=600.0))

    def test_the_loop_sleeps_between_checks_on_the_early_false_paths(self) -> None:
        """The round-2 review's busy-loop finding: the witness's early-False
        paths (too young, unreadable) return WITHOUT its internal sleep, so
        the LOOP sleeps the interval between checks - a young file (the exact
        still-copying case this seam exists for) must not busy-spin on stat
        pairs for the whole limit.
        """
        # a REAL young file exercises the early-False path (age < MIN_AGE);
        # is_stable is NOT patched here
        with (
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 100.0, 700.0]),
            patch("shrinkray.engine.watch.time.sleep") as sleep,
        ):
            self.assertFalse(watch.wait_for_stable(self.movie, limit=600.0))
        sleep.assert_called_once_with(watch.STABLE_CHECK_INTERVAL)

    def test_a_set_cancel_event_raises_cancelled(self) -> None:
        """The #326 contract holds inside the wait: a signal cancels it, it
        never absorbs the terminal's close for minutes.
        """
        cancel = threading.Event()
        cancel.set()
        with (
            patch.object(watch, "is_stable", return_value=False) as witness,
            self.assertRaises(watch.Cancelled),
        ):
            watch.wait_for_stable(self.movie, limit=600.0, cancel=cancel)
        witness.assert_not_called()

    def test_the_cancel_is_checked_between_witness_calls(self) -> None:
        """A signal that lands DURING the wait ends it at the next check."""
        cancel = threading.Event()
        answers = iter([False, False, True])

        def unstable_then_cancel(_):
            if next(answers, True):
                cancel.set()
            return False

        with (
            patch.object(watch, "is_stable", side_effect=unstable_then_cancel),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 10.0, 20.0, 30.0]),
            self.assertRaises(watch.Cancelled),
        ):
            watch.wait_for_stable(self.movie, limit=600.0, cancel=cancel)

    def test_the_default_limit_is_the_module_constant(self) -> None:
        with patch.object(watch, "is_stable", return_value=True) as witness:
            self.assertTrue(watch.wait_for_stable(self.movie))
        self.assertEqual(witness.call_count, 1)

    def test_the_seam_uses_the_same_witness_as_the_watcher(self) -> None:
        """Criterion 1: the seam calls the watcher's is_stable - the same
        witness protects both entry points (patched to prove the call).
        """
        with patch.object(watch, "is_stable", return_value=True) as witness:
            watch.wait_for_stable(self.movie)
        self.assertEqual(witness.call_args[0][0], self.movie)


class TestCliCallsTheSeam(unittest.TestCase):
    """Criterion 3's call-through: the CLI encode loop consults the seam
    before any gate or sample; a file that never settles is skipped.
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
        """The watcher's loop keeps its unbounded bare check: one real pass
        (once=True, the test_watch.py pattern) consults is_stable, never the
        bounded seam, and encodes as before - behaviourally, not by source
        inspection.
        """
        from contextlib import ExitStack

        from shrinkray.engine import watch as watch_mod
        from shrinkray.engine.plan import QualityResult

        def fake_encode(*args, **kwargs):
            return QualityResult(input=Path("x"), output=Path("x"), success=True)

        with (
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
