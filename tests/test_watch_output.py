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
            patch.object(app_module, "_serve_forever"),
            patch("shrinkray.service.all_problems", return_value=[]),
        ):
            app_module.run_app(Path(self.tmp.name) / "test.db", **kwargs)
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


if __name__ == "__main__":
    unittest.main()
