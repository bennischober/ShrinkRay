"""Tests for the filesystem-event waiter (issue #176).

The waiter shortens the watch loop's sleep when the filesystem changes; it
never bypasses the stability check and falls back to the plain sleep on any
platform without events (or on any error). The tests use real inotify events
on Linux (the CI platform) and fake waiters elsewhere.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shrinkray.engine.watch import _FsWaiter, _sleep_plain


class TestFsWaiter(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.waiter = _FsWaiter([self.root])

    def tearDown(self) -> None:
        self.waiter.stop()
        self._tmp.cleanup()

    def test_an_fs_event_wakes_the_wait_early(self) -> None:
        """Creating a file inside a watched root returns from wait() well
        before the timeout (the issue's core acceptance)."""
        if not self.waiter._supported or self.waiter._failed:
            self.skipTest("no fs events on this platform")
        self.waiter.start()
        self.assertFalse(self.waiter._failed, "waiter setup failed")
        fired = threading.Event()

        def late_wake() -> None:
            time.sleep(0.5)
            (self.root / "new.mkv").write_bytes(b"x")

        threading.Thread(target=late_wake, daemon=True).start()
        started = time.monotonic()
        self.waiter.wait(30.0, None)  # would sleep 30 s without the event
        elapsed = time.monotonic() - started
        fired.set()
        self.assertLess(elapsed, 10.0, f"wait took {elapsed:.1f}s — the event did not wake it")

    def test_the_wake_never_bypasses_the_stability_check(self) -> None:
        """A wake only shortens the sleep: the stability check still defers
        a still-writing file (the waiter's contract)."""
        from shrinkray.engine import watch as watch_mod

        wakes: list[int] = []

        class FakeWaiter:
            def __init__(self, roots) -> None:
                pass

            def start(self) -> None:
                pass

            def wait(self, seconds, stop_event) -> None:
                wakes.append(1)
                _sleep_plain(0.0, stop_event)

            def stop(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            # NOT stable (fresh mtime): the pass must defer it
            with (
                mock.patch.object(watch_mod, "_FsWaiter", FakeWaiter),
                mock.patch.object(watch_mod, "encode_with_quality") as fake_encode,
            ):
                watch_mod.watch([inbox], out, once=True)
            fake_encode.assert_not_called()  # deferred by stability, not woken past it

    def test_a_failed_setup_falls_back_to_the_plain_sleep(self) -> None:
        """An OSError in setup disables the waiter; wait() sleeps plainly."""
        waiter = _FsWaiter([Path("/nonexistent-root-for-test")])
        with mock.patch.object(waiter, "_start_linux", side_effect=OSError("no inotify")):
            waiter.start()
        self.assertTrue(waiter._failed)
        started = time.monotonic()
        waiter.wait(0.2, None)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.15)  # it actually slept

    def test_stop_is_idempotent_and_quiet(self) -> None:
        """stop() after start() (or twice) does not raise."""
        self.waiter.start()
        self.waiter.stop()
        self.waiter.stop()


if __name__ == "__main__":
    unittest.main()
