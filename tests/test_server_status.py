"""The /api/status payload's watcher fields (issue #648).

`watcher` says whether the resident watcher's thread is alive; `watcher_error`
says why it is not — the refusal reason watch() records (a held lock) or the
crash the server's run_watch wrapper records. The dashboard reads both (the
WATCHER chip and its strip); this module pins the route so the field cannot
silently disappear.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import shrinkray.server.app as app_module
from shrinkray.engine import watch as watch_mod
from shrinkray.server.app import AppState
from shrinkray.server.app import app as fast_app


class TestTheWatchLoopCrashIsRecorded(unittest.TestCase):
    """A watch-loop crash reaches /api/status (issue #648 round 2): the
    server's run_watch wrapper records the reason, so deleting that line
    fails this test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.tmp.cleanup()

    def test_a_crashing_watch_loop_names_the_reason(self) -> None:
        import threading

        watch_stop = threading.Event()
        app_module.state.watch_stop = watch_stop
        with mock.patch.object(watch_mod, "watch", side_effect=OSError("the inbox vanished")):
            app_module.start_watch(app_module.state, [Path(self.tmp.name)], None)
        # the module state is reset even when an assertion above fails, so
        # the failure cannot poison the next test
        self.addCleanup(setattr, watch_mod, "WATCHER_ERROR", None)
        thread = app_module.state.watch_thread
        assert thread is not None
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(watch_mod.WATCHER_ERROR)
        self.assertIn("the inbox vanished", watch_mod.WATCHER_ERROR or "")


class TestTheStatusCarriesTheWatcherError(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.tmp.cleanup()

    def test_a_stopped_watcher_names_the_reason(self) -> None:
        client = TestClient(fast_app)
        with mock.patch.object(
            watch_mod,
            "WATCHER_ERROR",
            "another watcher holds the single-instance lock",
        ):
            body = client.get("/api/status").json()
        self.assertEqual(body["watcher"], "stopped")
        self.assertEqual(
            body["watcher_error"],
            "another watcher holds the single-instance lock",
        )

    def test_a_dead_worker_names_the_reason(self) -> None:
        """Issue #649's AC: /api/status exposes the worker's liveness and,
        when it gave up, the reason - dropping either field fails this."""
        client = TestClient(fast_app)
        app_module.state.worker = mock.Mock(
            _thread=mock.Mock(is_alive=lambda: False),
            fatal_error="the worker gave up after 5 consecutive failed iterations",
        )
        body = client.get("/api/status").json()
        self.assertEqual(body["worker"], "stopped")
        self.assertEqual(
            body["worker_error"], "the worker gave up after 5 consecutive failed iterations"
        )

    def test_a_running_worker_reports_no_reason(self) -> None:
        client = TestClient(fast_app)
        app_module.state.worker = mock.Mock(
            _thread=mock.Mock(is_alive=lambda: True), fatal_error=None
        )
        body = client.get("/api/status").json()
        self.assertEqual(body["worker"], "running")
        self.assertIsNone(body["worker_error"])

    def test_a_running_watcher_reports_no_reason(self) -> None:
        client = TestClient(fast_app)
        # a live THREAD would make shutdown's join (timeout=10) wait out a
        # dummy target's lifetime - a fixed wait a unit test must not pay;
        # the route reads watch_thread.is_alive(), which a mock states
        app_module.state.watch_thread = mock.Mock(is_alive=lambda: True)
        body = client.get("/api/status").json()
        self.assertEqual(body["watcher"], "running")
        self.assertIsNone(body["watcher_error"])

    def test_the_snapshot_status_carries_the_reason_too(self) -> None:
        """The dashboard reads /api/snapshot's status dict (App.svelte's
        strip and WATCHER chip), not /api/status - the field must survive
        there as well (issue #648 round 2)."""
        client = TestClient(fast_app)
        with mock.patch.object(
            watch_mod,
            "WATCHER_ERROR",
            "another watcher holds the single-instance lock",
        ):
            body = client.get("/api/snapshot").json()
        self.assertEqual(
            body["status"]["watcher_error"],
            "another watcher holds the single-instance lock",
        )
