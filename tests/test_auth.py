"""Tests for the dashboard's optional bearer-token authentication (issue #36).

Auth is opt-in: with no token configured the loopback development flow is
unchanged. With $SHRINKRAY_TOKEN set, every route requires the token - the
middleware is the guard, so a new route cannot be added unauthenticated by
forgetting a decorator.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import Request, Response
from fastapi.testclient import TestClient

import shrinkray.server.app as app_module
from shrinkray.server.app import AUTH_TOKEN_ENV, AppState
from shrinkray.server.app import app as fast_app

TOKEN = "s3cret-token"


class TestAuthDisabled(unittest.TestCase):
    """No token configured: the loopback development flow is unchanged."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(AUTH_TOKEN_ENV, None)
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.env.stop()
        self.tmp.cleanup()

    def test_routes_work_without_a_token(self) -> None:
        client = TestClient(fast_app)
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(client.get("/api/library").status_code, 200)


class TestAuthEnabled(unittest.TestCase):
    """A configured token guards every route, SSE included."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {AUTH_TOKEN_ENV: TOKEN})
        self.env.start()
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        self.client = TestClient(fast_app)

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.env.stop()
        self.tmp.cleanup()

    def test_every_api_route_rejects_a_missing_token(self) -> None:
        """A route added without auth must fail this test, not the deployment.

        The SPA shell is deliberately absent from this list (issue #600): the
        document and its static assets are the login surface, and their
        serving without a token is pinned by the shell tests below.
        """
        for method, path in (
            ("GET", "/api/status"),
            ("GET", "/api/library"),
            ("GET", "/api/jobs"),
            ("GET", "/api/snapshot"),
            ("GET", "/api/events"),
            ("GET", "/api/config"),
            ("GET", "/metrics"),
            ("POST", "/api/reveal"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401, f"{method} {path} was not guarded")

    def test_the_spa_document_serves_without_a_token(self) -> None:
        """Issue #600: a browser navigation cannot send the Authorization
        header, so the document must load unauthenticated — the paste-token
        prompt (issue #36) lives inside the SPA and can never appear
        otherwise."""
        dist = Path(__file__).parent.parent / "ui" / "dist"
        response = self.client.get("/")
        self.assertNotEqual(response.status_code, 401)
        if not (dist / "index.html").is_file():
            self.skipTest("UI not built")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<html", response.text.lower())

    def test_the_shell_assets_serve_without_a_token(self) -> None:
        """Issue #600: the document's static assets load unauthenticated too,
        and the asset route's containment still holds under auth — a
        traversal attempt is 404, never served and never a 401-shaped
        fallback."""
        response = self.client.get("/assets/..%2F..%2Fpyproject.toml")
        self.assertEqual(response.status_code, 404)
        dist = Path(__file__).parent.parent / "ui" / "dist" / "assets"
        if dist.is_dir():
            asset = sorted(p.name for p in dist.iterdir() if p.is_file())[0]
            ok = self.client.get(f"/assets/{asset}")
            self.assertNotEqual(ok.status_code, 401)
            self.assertEqual(ok.status_code, 200)
        else:
            self.skipTest("UI not built")

    def test_the_shell_exemption_is_get_and_head_only(self) -> None:
        """The shell paths are exempt for safe reads only: a write to them
        still needs the token."""
        for method, path in (("POST", "/"), ("PUT", "/assets/x.js")):
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401)

    def test_the_favicon_routes_serve_without_a_token(self) -> None:
        """Issue #600: the document references the favicon files from the dist
        root; a browser fetches them during the unauthenticated load."""
        for path in ("/favicon.ico", "/favicon.svg", "/apple-touch-icon.png"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotEqual(response.status_code, 401)

    def test_a_valid_token_is_accepted(self) -> None:
        response = self.client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200)

    def test_a_wrong_token_is_rejected(self) -> None:
        response = self.client.get("/api/status", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)

    def test_a_malformed_header_is_rejected(self) -> None:
        """The token must arrive as 'Bearer <token>', not bare or in another scheme."""
        for header in (TOKEN, f"Basic {TOKEN}", f"bearer {TOKEN}"):
            with self.subTest(header=header):
                response = self.client.get("/api/status", headers={"Authorization": header})
                self.assertEqual(response.status_code, 401)

    def test_the_sse_stream_accepts_a_query_token(self) -> None:
        """EventSource cannot set a header, so /api/events takes ?token=.

        The generator streams until the client disconnects, so the middleware
        decision is exercised directly rather than through an open response.
        """
        request = self._request("GET", "/api/events", query=f"token={TOKEN}")
        response = asyncio.run(app_module.require_token(request, self._passthrough))
        self.assertEqual(response.status_code, 200)

    def test_the_sse_stream_still_rejects_a_wrong_query_token(self) -> None:
        request = self._request("GET", "/api/events", query="token=nope")
        response = asyncio.run(app_module.require_token(request, self._passthrough))
        self.assertEqual(response.status_code, 401)

    async def _passthrough(self, request: object) -> Response:
        return Response(status_code=200)

    def _request(self, method: str, path: str, query: str = "") -> Request:
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [],
        }
        return Request(scope)

    def test_a_query_token_is_only_accepted_on_the_sse_route(self) -> None:
        """A token in a URL is logged by proxies, so no other route reads it."""
        response = self.client.get(f"/api/status?token={TOKEN}")
        self.assertEqual(response.status_code, 401)

    def test_the_error_never_echoes_the_token(self) -> None:
        response = self.client.get("/api/status", headers={"Authorization": "Bearer wrong-value"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn("wrong-value", response.text)

    def test_the_www_authenticate_header_names_the_scheme(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")


class TestNonLoopbackRequiresAToken(unittest.TestCase):
    """A published port with no auth is the failure mode this prevents."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(AUTH_TOKEN_ENV, None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_binding_a_public_address_without_a_token_fails_fast(self) -> None:
        with self.assertRaises(ValueError) as caught:
            app_module.run_app(
                Path(self.tmp.name) / "test.db",
                host="0.0.0.0",  # noqa: S104 - the address the guard must reject
                allowed_roots=[Path(self.tmp.name)],
            )
        message = str(caught.exception)
        self.assertIn(AUTH_TOKEN_ENV, message)
        self.assertIn("authentication", message)

    def test_loopback_is_recognised_as_local(self) -> None:
        """The guard must not fire for the development default."""
        self.assertTrue(app_module._is_loopback("127.0.0.1"))
        self.assertTrue(app_module._is_loopback("::1"))
        self.assertTrue(app_module._is_loopback("localhost"))
        self.assertFalse(app_module._is_loopback("0.0.0.0"))  # noqa: S104
        self.assertFalse(app_module._is_loopback("192.168.1.10"))


class TestGeneratedToken(unittest.TestCase):
    """The image's generated token (issue #249): with $SHRINKRAY_TOKEN empty and
    $SHRINKRAY_TOKEN_FILE set, the server reads the file, generating it first,
    so a published container is never tokenless and never asks for a secret."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "token"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(AUTH_TOKEN_ENV, None)
        os.environ.pop(app_module.TOKEN_FILE_ENV, None)

    def tearDown(self) -> None:
        app_module._file_token = None
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.env.stop()
        self.tmp.cleanup()

    def test_a_missing_file_gets_a_strong_token_that_is_never_logged(self) -> None:
        with (
            mock.patch.object(app_module, "log") as log,
            mock.patch.object(app_module, "log_warning") as log_warning,
        ):
            token = app_module.ensure_token_file(self.path)
        self.assertGreaterEqual(len(token), 43)  # 32 random bytes, urlsafe base64
        self.assertRegex(token, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), token)
        logged = [str(call) for call in log.call_args_list + log_warning.call_args_list]
        self.assertTrue(logged, "the generation must be reported")
        self.assertFalse([line for line in logged if token in line], "the token was logged")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_file_is_private(self) -> None:
        app_module.ensure_token_file(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_the_token_is_reused_not_regenerated(self) -> None:
        first = app_module.ensure_token_file(self.path)
        self.assertEqual(app_module.ensure_token_file(self.path), first)

    def test_an_operator_written_token_is_kept(self) -> None:
        self.path.write_text("my-own-secret\n", encoding="utf-8")
        self.assertEqual(app_module.ensure_token_file(self.path), "my-own-secret")

    def test_an_empty_file_is_an_error_not_no_authentication(self) -> None:
        self.path.write_text("\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            app_module.ensure_token_file(self.path)
        self.assertIn(str(self.path), str(caught.exception))

    def _boot_until_the_toolchain_check(self) -> str:
        """Runs run_app on a public address up to the check AFTER the auth
        guard: reaching it proves the guard let the bind through."""
        with (
            mock.patch("shrinkray.service.all_problems", return_value=["stop here"]),
            self.assertRaises(ValueError) as caught,
        ):
            app_module.run_app(
                Path(self.tmp.name) / "test.db",
                host="0.0.0.0",  # noqa: S104 - the public bind the token must allow
                allowed_roots=[Path(self.tmp.name)],
            )
        return str(caught.exception)

    def test_the_server_generates_the_file_and_binds(self) -> None:
        os.environ[app_module.TOKEN_FILE_ENV] = str(self.path)
        message = self._boot_until_the_toolchain_check()
        self.assertIn("toolchain", message)
        self.assertNotIn("authentication", message)
        self.assertTrue(self.path.is_file())
        self.assertEqual(app_module._configured_token(), self.path.read_text().strip())

    def test_an_explicit_token_wins_and_no_file_is_written(self) -> None:
        os.environ[AUTH_TOKEN_ENV] = TOKEN
        os.environ[app_module.TOKEN_FILE_ENV] = str(self.path)
        self._boot_until_the_toolchain_check()
        self.assertFalse(self.path.exists())
        self.assertEqual(app_module._configured_token(), TOKEN)

    def test_the_middleware_checks_the_generated_token(self) -> None:
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        app_module._file_token = "generated-token"
        client = TestClient(fast_app)
        self.assertEqual(client.get("/api/status").status_code, 401)
        ok = client.get("/api/status", headers={"Authorization": "Bearer generated-token"})
        self.assertEqual(ok.status_code, 200)

    def test_the_healthcheck_reads_the_same_source(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import healthcheck

        self.path.write_text("from-the-file\n", encoding="utf-8")
        os.environ[app_module.TOKEN_FILE_ENV] = str(self.path)
        self.assertEqual(healthcheck.token(), "from-the-file")
        os.environ[AUTH_TOKEN_ENV] = TOKEN
        self.assertEqual(healthcheck.token(), TOKEN)

    def test_only_the_image_opts_in(self) -> None:
        """A native serve never generates a secret: the variable is the image's."""
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
        self.assertIn(
            f"{app_module.TOKEN_FILE_ENV}=/data/token", dockerfile.split("FROM runtime AS dev")[0]
        )


if __name__ == "__main__":
    unittest.main()


class TestStatusPollSilence(unittest.TestCase):
    """Issue #471: the healthcheck's /api/status polls produce no transcript
    line; other /api routes still do."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(AUTH_TOKEN_ENV, None)
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None
        self.env.stop()
        self.tmp.cleanup()

    def test_status_polls_are_silent_other_routes_are_not(self) -> None:
        from loguru import logger

        captured: list[str] = []
        sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            client = TestClient(fast_app)
            self.assertEqual(client.get("/api/status").status_code, 200)
            self.assertEqual(client.get("/api/library").status_code, 200)
        finally:
            logger.remove(sink_id)
        text = "".join(captured)
        self.assertNotIn("GET /api/status", text, text)
        self.assertIn("GET /api/library", text, text)

    def test_the_uvicorn_access_filter_drops_status_polls(self) -> None:
        """Issue #471: the filter drops uvicorn's own /api/status access line
        (the second of the two lines per healthcheck poll) and keeps the rest."""
        import logging

        from shrinkray.server.app import _StatusAccessFilter

        f = _StatusAccessFilter()
        status = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "p",
            1,
            '%s - "%s %s HTTP/1.1" %d',
            ("x", "GET", "/api/status", 200),
            None,
        )
        library = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "p",
            1,
            '%s - "%s %s HTTP/1.1" %d',
            ("x", "GET", "/api/library", 200),
            None,
        )
        self.assertFalse(f.filter(status))
        self.assertTrue(f.filter(library))


class TestEncodeWindowStatusField(unittest.TestCase):
    """Issue #434: the status payload carries the window's state."""

    def test_the_field_reflects_the_configured_window(self) -> None:
        from unittest.mock import patch

        from shrinkray import config as config_helper
        from shrinkray.server.app import _encode_window_state

        empty = config_helper.Conf({})
        configured = config_helper.Conf(
            {"app": {"encode_window_start": "09:00", "encode_window_end": "17:00"}}
        )
        with patch("shrinkray.server.app.refresh_config", return_value=empty):
            self.assertIsNone(_encode_window_state())
        with (
            patch("shrinkray.server.app.refresh_config", return_value=configured),
            patch("shrinkray.server.app.encode_window_open", return_value=True),
        ):
            self.assertEqual(_encode_window_state(), "open")
        with (
            patch("shrinkray.server.app.refresh_config", return_value=configured),
            patch("shrinkray.server.app.encode_window_open", return_value=False),
        ):
            self.assertEqual(_encode_window_state(), "closed")
