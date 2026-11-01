"""The config read path: one reader (issue #541).

Each test names one acceptance criterion from the maintainer's 2026-11-01
decision comment. Run against main they must fail for the reason the issue
describes; after the fix they must pass.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from shrinkray import config as config_module
from shrinkray.config import reset_config
from shrinkray.server import app as app_module
from shrinkray.server.app import AppState
from shrinkray.server.app import app as fast_app


class _ConfigCase(unittest.TestCase):
    """A temp db directory holds config.json, so the route resolves it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(app_module.AUTH_TOKEN_ENV, None)
        os.environ.pop("SHRINKRAY_CONFIG", None)
        reset_config()
        self.addCleanup(reset_config)
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        self.addCleanup(self._teardown_state)

    def _teardown_state(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None

    def _load(self, text: str) -> None:
        # AppState's construction loads the config once; drop that cache so this
        # fixture's file is the one in effect (the route under test then reads
        # `config_file_used`, exactly as it does in production).
        config_module.reset_config()
        self.path.write_text(text, encoding="utf-8")
        config_module.load_config(config_file=self.path)


class TestTheRouteServesWhatTheLoaderApplied(_ConfigCase):
    """GET /api/config answers the loader's in-effect Conf, never its own
    parse of the file (the maintainer's decision, 2026-11-01)."""

    def test_a_syntax_error_answers_the_last_good_values_and_the_reason(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        self.path.write_text("{ not json", encoding="utf-8")
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200, "a broken file is not a 500 any more")
        self.assertEqual(response.json()["quality"]["target_vmaf"], 92)
        self.assertIn("not applied", response.json()["config_error"])

    def test_bad_bytes_answer_the_last_good_values_not_an_opaque_500(self) -> None:
        self._load('{"quality": {"target_vmaf": 93}}')
        self.path.write_bytes(b'{"language": "d' + b"\xc3")
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 93)
        self.assertIn("not applied", response.json()["config_error"])

    def test_a_validation_rejected_value_is_not_served_as_in_effect(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        self.path.write_text('{"quality": {"target_vmaf": "high"}}', encoding="utf-8")
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 92)
        self.assertIn("not applied", response.json()["config_error"])

    def test_a_deleted_file_keeps_the_last_good_settings(self) -> None:
        self._load('{"quality": {"target_vmaf": 91}}')
        self.path.unlink()
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 91)
        self.assertIn("not applied", response.json()["config_error"])

    def test_a_valid_edit_shows_the_new_values_after_the_refresh(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        # a DIFFERENT LENGTH so the file signature changes (same-size writes
        # inside one mtime tick are the loader's own business, not this test's)
        self.path.write_text('{"quality": {"target_vmaf": 95.5}}', encoding="utf-8")
        config_module.refresh_config()

        body = TestClient(fast_app).get("/api/config").json()
        self.assertEqual(body["quality"]["target_vmaf"], 95.5)
        self.assertIsNone(body["config_error"])

    def test_the_route_serves_a_config_it_could_not_have_read(self) -> None:
        """The one-reader rule, pinned at the filesystem: the loader has the
        settings in memory, then the file becomes unreadable. A route that
        parses the file answers 500 or the defaults; the loader's answer is 200
        with the in-effect value."""
        self._load('{"quality": {"target_vmaf": 94}}')
        self.path.chmod(0o000)
        self.addCleanup(self.path.chmod, 0o644)

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 94)


class TestTheStatusDetectsABrokenEdit(_ConfigCase):
    """/api/status triggers refresh_config(), so a broken edit is reported
    within one poll (seconds), not one watcher pass (up to 4 min)."""

    def test_a_broken_edit_surfaces_on_the_next_status_call(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        client = TestClient(fast_app)
        self.assertIsNone(client.get("/api/status").json()["config_error"])

        self.path.write_text("{ broken", encoding="utf-8")
        body = client.get("/api/status").json()
        self.assertIsNotNone(body["config_error"], "status must refresh the config itself")
        self.assertIn("not applied", body["config_error"])


if __name__ == "__main__":
    unittest.main()
