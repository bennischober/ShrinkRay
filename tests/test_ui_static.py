"""Tests for the served UI's root static files (issue #464).

ui/index.html references favicon.ico, favicon.svg and apple-touch-icon.png
from the dist root; only `vite dev` resolves public/ itself, so the served
app needs explicit routes for them — without one the tab icon 404s in every
deployed instance. The routes serve a fixed set of filenames, never a
user-controlled path segment, so containment is by construction; these
tests pin that only the declared files are reachable.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import shrinkray.server.app as app_module
from shrinkray.server.app import AppState
from shrinkray.server.app import app as fast_app


class TestRootStaticFiles(unittest.TestCase):
    """The favicon routes serve the declared files from the built UI's dist
    root and nothing else (_ui_dist mocked: no built UI is required)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name)
        (self.dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
        (self.dist / "favicon.ico").write_bytes(b"fake-ico")
        (self.dist / "apple-touch-icon.png").write_bytes(b"fake-png")
        self._ui_dist = mock.patch.object(app_module, "_ui_dist", return_value=self.dist)
        self._ui_dist.start()
        app_module.state = AppState(self.dist / "test.db")

    def tearDown(self) -> None:
        app_module.state.shutdown()
        app_module.state = None
        self._ui_dist.stop()
        self.tmp.cleanup()

    def test_the_svg_favicon_is_served(self) -> None:
        client = TestClient(fast_app)
        response = client.get("/favicon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertIn("svg", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_the_ico_favicon_is_served(self) -> None:
        client = TestClient(fast_app)
        response = client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-ico")

    def test_the_apple_touch_icon_is_served(self) -> None:
        client = TestClient(fast_app)
        response = client.get("/apple-touch-icon.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-png")

    def test_an_undeclared_root_file_is_not_served(self) -> None:
        # files present in the dist root without a route stay unreachable:
        # the route set is the boundary, not the dist directory's contents
        (self.dist / "icons.svg").write_text("<svg></svg>", encoding="utf-8")
        (self.dist / "metadata.db").write_bytes(b"not-served")
        client = TestClient(fast_app)
        for name in ("icons.svg", "metadata.db", "test.db"):
            response = client.get(f"/{name}")
            self.assertEqual(response.status_code, 404)

    def test_an_unbuilt_ui_is_a_404_not_a_crash(self) -> None:
        with mock.patch.object(app_module, "_ui_dist", return_value=self.dist / "absent"):
            client = TestClient(fast_app)
            for name in ("favicon.ico", "favicon.svg", "apple-touch-icon.png"):
                response = client.get(f"/{name}")
                self.assertEqual(response.status_code, 404)
