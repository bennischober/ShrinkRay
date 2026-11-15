"""The status payload's watcher and worker fields (issues #648 and #649).

`watcher` says whether the resident watcher's thread is alive; `watcher_error`
says why it is not — the refusal reason watch() records (a held lock) or the
crash the server's run_watch wrapper records. `worker`/`worker_error` are the
job worker's analogue (alive; why it gave up). The dashboard reads the
/snapshot dict's copies (the WATCHER/WORKER chips and their strips); this
module pins the routes and the wiring so no field can silently disappear.
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


class TestTheStatusCarriesTheWatcherError(unittest.TestCase):
    """The watcher fields on /api/status and /api/snapshot (issue #648)."""

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


class TestTheAppWiring(unittest.TestCase):
    """The queue/lock wiring the resident app builds (issue #649's root
    cause): the queue's connection use must hold the SAME lock the routes
    hold - a private lock keeps every other test green while the startup
    collision returns."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.tmp.cleanup()

    def test_the_app_queue_holds_the_app_lock(self) -> None:
        self.assertIs(
            app_module.state.queue._connection_lock,
            app_module.state.db_lock,
        )


class TestTheStatusCarriesTheWorkerFields(unittest.TestCase):
    """The worker analogue (issue #649): /api/status's `worker` says whether
    the worker thread is alive; `worker_error` says why it gave up."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.tmp.cleanup()

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

    def test_the_snapshot_status_carries_the_worker_fields_too(self) -> None:
        """The dashboard reads /api/snapshot's status dict (the WORKER chip
        and its strip) - deleting the snapshot's worker fields fails this
        while /api/status stays green (issue #660 round 2, finding 3)."""
        client = TestClient(fast_app)
        app_module.state.worker = mock.Mock(
            _thread=mock.Mock(is_alive=lambda: False),
            fatal_error="the worker gave up after 5 consecutive failed iterations",
        )
        body = client.get("/api/snapshot").json()
        self.assertEqual(body["status"]["worker"], "stopped")
        self.assertEqual(
            body["status"]["worker_error"],
            "the worker gave up after 5 consecutive failed iterations",
        )


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
