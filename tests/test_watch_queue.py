"""Tests for the watcher as a thin loop over the job queue (issue #231).

The watcher was a second engine: it wrote no database rows, kept its memory
in an in-process set (a restart re-probed the whole library), never retried a
failure, and never verified. The queue owns the work now; the watcher discovers
and scans, and the app hands it the enqueue seam.
"""

import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from shrinkray import db
from shrinkray.engine import watch as watch_mod
from shrinkray.engine.plan import QualityResult


def _isolated_lock(stack: ExitStack, tmp) -> None:
    # the lock path AND the pass guard's free-space reading (a busy CI
    # runner's disk dip must not eat a pass - the #303 CI face of #291)
    stack.enter_context(mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "watch.lock"))
    stack.enter_context(mock.patch.object(watch_mod, "disk_free_bytes", return_value=1 << 40))


class TestWatcherSeedsItsMemoryFromTheDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db_file = self.root / "meta.db"
        self.connection = db.connect(self.db_file)
        self.movie = self.root / "one.mkv"
        self.movie.write_bytes(b"x" * 100)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=str(self.movie),
                mtime=self.movie.stat().st_mtime,
                size=self.movie.stat().st_size,
                name="one",
                category="movies",
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_a_scanned_file_is_not_a_candidate_after_a_restart(self) -> None:
        """The database is the memory: a restart does not re-probe.

        The old shape started from an empty in-process set, so every container
        restart re-derived every decision (hours of CPU on a real library).
        """
        found = watch_mod.find_new_movies(
            self.root, [".mkv"], watch_mod._known_identities_from_db(self.db_file)
        )
        self.assertEqual(found, [])

    def test_a_changed_file_is_a_candidate_again(self) -> None:
        """A changed file is a new question: its mtime/size differ."""
        # the SIZE changes, not just the mtime: inode timestamps are quantized
        # to the kernel's tick (~10 ms on WSL2 and loaded CI runners), so two
        # same-size writes inside one tick carry the SAME mtime and the
        # identity would not change - the flake of issue #291. A rewrite that
        # changes only the mtime within one tick is undetectable by ANY
        # metadata; the watcher's memory contract degrades there, and the
        # test must not depend on the clock having ticked.
        self.movie.write_bytes(b"y" * 140)
        found = watch_mod.find_new_movies(
            self.root, [".mkv"], watch_mod._known_identities_from_db(self.db_file)
        )
        self.assertEqual(found, [self.movie])

    def test_a_restart_seeds_the_watch_loop(self) -> None:
        """The watch loop itself starts with the database's identities."""
        submitted: list = []

        def fake_encode(movie, output, **kwargs):
            raise AssertionError("the encode path must not run when submit is given")

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch(
                    [self.root],
                    None,
                    once=True,
                    db_file=self.db_file,
                    submit=lambda movie: (submitted.append(movie), True)[1],
                )
        self.assertEqual(submitted, [], "a scanned file was re-decided after a restart")


class TestWatcherSubmitsToTheQueue(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db_file = self.root / "meta.db"
        self.connection = db.connect(self.db_file)
        self.movie = self.root / "one.mkv"
        self.movie.write_bytes(b"x" * 100)
        old = time.time() - 3600
        os.utime(self.movie, (old, old))

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_a_stable_candidate_is_scanned_then_submitted(self) -> None:
        """The contract's two halves: scanned into the db, then handed over."""
        submitted: list = []

        def fake_encode(movie, output, **kwargs):
            raise AssertionError("the queue owns the encode; the watcher must not encode")

        def fake_scan(filename, root, config, **kwargs):
            # the real process_movie writes the row itself; the mock must
            # preserve that behaviour or the assertion below tests nothing
            db.upsert_file(
                self.connection,
                db.MovieRecord(
                    path=filename,
                    mtime=filename.stat().st_mtime,
                    size=filename.stat().st_size,
                    name="one",
                    category="movies",
                ),
            )
            return {"name": "one", "category": "movies"}, None

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
            mock.patch.object(watch_mod, "process_movie", side_effect=fake_scan),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch(
                    [self.root],
                    None,
                    once=True,
                    db_file=self.db_file,
                    submit=lambda movie: (submitted.append(movie), True)[1],
                )
        self.assertEqual(submitted, [self.movie])
        row = self.connection.execute(
            "SELECT path FROM files WHERE path = ?", (str(self.movie),)
        ).fetchone()
        self.assertIsNotNone(row)

    def test_no_submit_keeps_the_direct_path(self) -> None:
        """A stateless/test run without the seam encodes directly (unchanged)."""
        encoded: list = []

        def fake_encode(movie, output, **kwargs):
            encoded.append(movie)
            return QualityResult(
                input=movie, output=output, success=False, skipped_reason="no size gain"
            )

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([self.root], None, once=True)
        self.assertEqual(encoded, [self.movie])

    def test_a_failed_scan_stays_eligible(self) -> None:
        """An unreadable file is logged, not submitted, and retried on change."""
        submitted: list = []

        def fake_encode(movie, output, **kwargs):
            raise AssertionError("the encode path must not run")

        stack = ExitStack()
        with (
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
            mock.patch.object(watch_mod, "process_movie", return_value=(None, "unreadable")),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch(
                    [self.root],
                    None,
                    once=True,
                    db_file=self.db_file,
                    submit=lambda movie: (submitted.append(movie), True)[1],
                )
        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
