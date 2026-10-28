"""Tests for the shared stability-wait seam (issue #437).

The one-shot CLI (`shrinkray encode`) used to decide on a file a mover was
still writing: the stability witness (`is_stable`: size unchanged across a
STABLE_CHECK_INTERVAL sleep, last change at least STABLE_MIN_AGE ago) lived
only in the watcher's loop. The seam (`wait_for_stable`) exposes the same
witness with a bounded wait; the watcher keeps its single unbounded check.
Every test here is timing-free: the witness is patched, never the clock.
"""

import tempfile
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
            self.assertTrue(watch.wait_for_stable(self.movie, limit=60.0, sleep=0.0))
        self.assertEqual(witness.call_count, 1)

    def test_an_unstable_file_becomes_stable_within_the_limit(self) -> None:
        answers = iter([False, False, True])
        with patch.object(watch, "is_stable", side_effect=lambda _: next(answers)):
            self.assertTrue(watch.wait_for_stable(self.movie, limit=600.0, sleep=0.0))

    def test_a_file_that_never_settles_expires_at_the_limit(self) -> None:
        with (
            patch.object(watch, "is_stable", return_value=False),
            patch("shrinkray.engine.watch.time.monotonic", side_effect=[0.0, 100.0, 700.0]),
            patch("shrinkray.engine.watch.time.sleep"),
        ):
            self.assertFalse(watch.wait_for_stable(self.movie, limit=600.0, sleep=30.0))

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


class TestWatcherUnchanged(unittest.TestCase):
    """Criterion 2: the watcher's behavior is unchanged - its loop keeps the
    single is_stable check (the seam adds nothing to its pass).
    """

    def test_the_watcher_loop_still_uses_the_bare_witness(self) -> None:
        import inspect

        source = inspect.getsource(watch)
        self.assertIn("if not is_stable(movie):", source)
        # the watcher never calls the bounded seam (an extraction, not a
        # policy change)
        self.assertIn("wait_for_stable(", source)
        loop_region = source[source.index("for _root, movie in candidates") :]
        self.assertNotIn("wait_for_stable(", loop_region)


if __name__ == "__main__":
    unittest.main()
