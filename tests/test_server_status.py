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

    def test_a_running_watcher_reports_no_reason(self) -> None:
        client = TestClient(fast_app)
        body = client.get("/api/status").json()
        self.assertIsNone(body["watcher_error"])
