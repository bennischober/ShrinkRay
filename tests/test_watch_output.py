"""The resident watcher's output directory knob (issue #193).

In place is the default and needs a writable library mount; a read-only
/media (test containers, special setups) crashed the watch thread on its
staging directory with no way to point the encodes elsewhere. Zef-X's
decision on the issue: compose mounts /media writable by default (done in
#198), and an env var ($SHRINKRAY_WATCH_OUTPUT) covers testing and special
usecases. The app validates the path AT BOOT — a typo must refuse to start,
not crash the watch thread hours later (the issue's silent-loss failure
mode).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.server import app as app_module
from shrinkray.server.app import WATCH_OUTPUT_ENV


class TestWatchOutputEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(WATCH_OUTPUT_ENV, None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _boot(self, **kwargs):
        """Boots run_app with everything except the knob mocked away.

        The toolchain preflight (issue #194) must see a complete toolchain:
        these tests exercise the OUTPUT KNOB, not the preflight (CI has no
        ffprobe/HandBrake).
        """
        with (
            patch.object(app_module, "allow_roots"),
            patch.object(app_module, "AppState"),
            patch.object(app_module, "auto_backlog"),
            patch.object(app_module, "_recover_interrupted_swaps"),
            patch.object(app_module, "start_watch") as start_watch,
            # patch the seam run_app resolves, never a third-party attribute: uvicorn
            # re-exports Server from its __init__, so patch("uvicorn.Server") raises
            # AttributeError whenever that re-export has not materialized yet - an
            # import-order dependency that failed on a fresh CI session (issue #373)
            patch.object(app_module, "_serve_forever") as serve,
            patch("shrinkray.service.all_problems", return_value=[]),
        ):
            app_module.run_app(Path(self.tmp.name) / "test.db", **kwargs)
        # the seam must actually be reached: without this the tests stay green even
        # if run_app never calls the server (verified by deleting the call)
        self.assertIsNotNone(serve.call_args, "run_app must reach the ASGI server")
        return start_watch

    def test_unset_starts_the_watcher_in_place(self) -> None:
        start_watch = self._boot()
        args = start_watch.call_args[0]
        self.assertIsNone(args[2], "no env var must mean in-place (None)")

    def test_a_set_var_is_threaded_to_the_watcher(self) -> None:
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        os.environ[WATCH_OUTPUT_ENV] = str(out)
        start_watch = self._boot()
        args = start_watch.call_args[0]
        self.assertEqual(args[2], out)
        self.assertTrue(out.is_dir(), "the output dir is created at boot")

    def test_a_missing_dir_is_created_at_boot(self) -> None:
        out = Path(self.tmp.name) / "made" / "by" / "boot"
        os.environ[WATCH_OUTPUT_ENV] = str(out)
        self._boot()
        self.assertTrue(out.is_dir())

    def test_an_unwritable_dir_refuses_to_start(self) -> None:
        # a READ-ONLY directory inside the tmp tree: os.access(W_OK) is False
        out = Path(self.tmp.name) / "ro"
        out.mkdir()
        out.chmod(0o555)
        os.environ[WATCH_OUTPUT_ENV] = str(out)
        with self.assertRaises(SystemExit) as caught:
            self._boot()
        self.assertEqual(caught.exception.code, 1)
        out.chmod(0o755)

    def test_the_shutdown_runs_exactly_once(self) -> None:
        """Issue #386: a copy-paste had run_app's finally block shut the app
        down twice - the second call against an already-closed AppState, and
        the log reading "app stopped" twice per stop. The boot tests' mocked
        AppState records every shutdown call; one boot, one stop.
        """
        with (
            patch.object(app_module, "allow_roots"),
            patch.object(app_module, "AppState") as state_cls,
            patch.object(app_module, "auto_backlog"),
            patch.object(app_module, "_recover_interrupted_swaps"),
            patch.object(app_module, "start_watch"),
            patch.object(app_module, "_serve_forever"),
            patch.object(app_module, "log") as log_mock,
            patch("shrinkray.service.all_problems", return_value=[]),
        ):
            app_module.run_app(Path(self.tmp.name) / "test.db")
        shutdown = state_cls.return_value.shutdown
        self.assertEqual(shutdown.call_count, 1, "the shutdown must run exactly once per stop")
        # the log lies were half the issue: "app stopped" must appear once
        stopped = [c for c in log_mock.call_args_list if c.args and c.args[0] == "app stopped"]
        self.assertEqual(len(stopped), 1, "the stop must be logged exactly once")


class TestServeForever(unittest.TestCase):
    """The seam's own body, which every other test here patches away.

    Without this the seam could construct a wrong `uvicorn.Config` - or none at all -
    and the whole file would stay green (verified: an invalid Config kwarg passed all
    four output-knob tests before this class existed).
    """

    def test_it_builds_the_config_from_its_arguments_and_runs_the_server(self) -> None:
        # a non-wildcard host on purpose: the assertion is about the arguments being
        # threaded through, and bandit flags a hardcoded bind-all address (S104)
        with (
            patch("uvicorn.Config") as config,
            patch("uvicorn.Server") as server,
        ):
            app_module._serve_forever("127.0.0.2", 9999)
        config.assert_called_once_with(
            app_module.app, host="127.0.0.2", port=9999, log_level="info"
        )
        server.assert_called_once_with(config.return_value)
        server.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
