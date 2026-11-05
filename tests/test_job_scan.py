"""Tests for the encode job's payload scan (issue #374) and the scan job's
category root (issue #489).

`execute_encode` used to resolve the payload's movies and pass the result
to `_encode_measured`, which ignored its `movies` argument and resolved the
payload again - two full library walks per encode job (the backlog payload
is the whole library root), the first thrown away. The job now scans once,
inside `_encode_measured`, and the one exclusion is the directory the job
writes into: the user's `output_dir` in two-directory mode, the per-run
staging directory in place.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db
from shrinkray.server import jobs


class TestCacheHitKeepsTheStoredHash(unittest.TestCase):
    """Issue #573: a scan's cache-hit write-back must not null the stored
    content hash - record_from_cache returns the row's own hash, so the
    upsert preserves it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "library"
        self.root.mkdir()
        self.movie = self.root / "film.mkv"
        self.movie.write_bytes(b"x" * 64)
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _record_with_hash(self, hash_value: "str | None") -> None:
        self.connection.execute(
            "INSERT INTO files (path, mtime, size, name, category, hash, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(self.movie),
                self.movie.stat().st_mtime,
                self.movie.stat().st_size,
                "film",
                "Uncategorized",
                hash_value,
                "2026-01-01T00:00:00",
            ),
        )
        self.connection.commit()

    def _stored_hash(self) -> "str | None":
        row = self.connection.execute(
            "SELECT hash FROM files WHERE path = ?", (str(self.movie),)
        ).fetchone()
        return row["hash"] if row is not None else None

    def _scan(self) -> None:
        from shrinkray.config import Conf
        from shrinkray.engine.scan import scan_movies

        scan_movies(self.root, Conf(), connection=self.connection, db_file=None)

    def test_a_cache_hit_keeps_the_stored_hash(self) -> None:
        self._record_with_hash("abc123")
        self._scan()  # the file is unchanged: the cache path, no re-probe
        self.assertEqual(self._stored_hash(), "abc123")

    def test_a_real_reprobe_overwrites_with_the_fresh_hash(self) -> None:
        """The fix must not freeze a stale hash: a re-probe's record (rescan
        bypasses the cache) carries the fresh hash and the write-back stores
        it - the upsert's overwrite is the re-probe's own path."""
        from unittest.mock import patch

        from shrinkray.config import Conf
        from shrinkray.engine.scan import scan_movies

        self._record_with_hash("abc123")
        # rescan=True bypasses the cache branch entirely (scan_movies skips
        # the lookup when rescan is set); the changed bytes make the scenario
        # the realistic one (a file that changed since its last scan)
        self.movie.write_bytes(b"CHANGED" * 32)

        def fake_process_movie(movie, root, config, db_file=None, **kwargs):
            data = TestScanJobCategoryRoot._fake_data(movie, root)
            data["hash"] = "fresh-hash"
            return data, None

        with patch("shrinkray.engine.scan.process_movie", side_effect=fake_process_movie):
            scan_movies(self.root, Conf(), connection=self.connection, rescan=True, db_file=None)
        self.assertEqual(self._stored_hash(), "fresh-hash")


class TestScanJobCategoryRoot(unittest.TestCase):
    """Issue #489: a single-file scan job RECORDS the category the watch path
    records — the row goes through scan_movies's write-back with the most
    specific allowed root as the category root, so a file in a Title subfolder
    lands in the files table under its folder, not Uncategorized."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "library"
        self.root.mkdir()
        (self.root / "Title").mkdir()
        self.movie = self.root / "Title" / "film.mkv"
        self.movie.write_bytes(b"x" * 16)
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    @staticmethod
    def _fake_data(movie: Path, root: Path) -> dict:
        """The get_data shape movie_record_from_data persists, with the
        category the real get_category computes from the passed root."""
        from shrinkray.engine.scan import get_category

        return {
            "name": movie.stem,
            "category": get_category(movie, root),
            "duration": 12.0,
            "duration_raw": 12.0,
            "fps": 25.0,
            "codec": "h264",
            "bitrate": 1000,
            "aspect_ratio": "16:9",
            "dimensions": {"width": 1920, "height": 1080},
            "crop": None,
            "hdr": False,
            "other_codecs": "",
            "audio_languages": "",
            "hash": None,
        }

    def _scan(self, roots: "list[Path]"):
        import contextlib
        from unittest.mock import patch

        from shrinkray.server import app as server_app

        captured: dict = {}

        def fake_process_movie(movie, root, config, db_file=None, **kwargs):
            captured["root"] = root
            return self._fake_data(movie, root), None

        job = jobs.Job(kind=jobs.JobKind.SCAN, payload={"paths": [str(self.movie)]})
        patches = [
            patch("shrinkray.engine.scan.process_movie", side_effect=fake_process_movie),
            patch.object(server_app, "allowed_roots", return_value=roots),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            result = jobs.execute_scan(job, self.connection, threading.Event())
        return result, captured

    def _recorded_category(self) -> "str | None":
        row = self.connection.execute(
            "SELECT category FROM files WHERE path = ?", (str(self.movie.resolve()),)
        ).fetchone()
        return row["category"] if row is not None else None

    def test_a_title_subfolder_file_records_title(self) -> None:
        """The watch root is the category root AND the row is recorded: the
        files table's category is the folder, as the watch path records."""
        result, captured = self._scan([self.root])
        # the root the recording used is the allowed root (the seam)
        self.assertEqual(captured["root"], self.root)
        self.assertEqual(result["movies"], 1)
        self.assertEqual(self._recorded_category(), "Title")

    def test_the_most_specific_root_wins_when_roots_nest(self) -> None:
        """Nested --allow roots: the branch picks the deepest containing root
        - a deterministic rule that does not depend on the roots' submission
        order (the watcher's own multi-root scan order is its own behaviour,
        outside this branch)."""
        outer = self.root
        inner = self.root / "Title"
        _result, captured = self._scan([outer, inner])
        self.assertEqual(captured["root"], inner)
        self.assertEqual(self._recorded_category(), "Uncategorized")

    def test_a_file_outside_every_root_keeps_parent_as_root(self) -> None:
        """The ad-hoc branch's original semantics stay for a file the resident
        app does not watch: parent-as-root (documented CLI behaviour)."""
        _result, captured = self._scan([])
        self.assertEqual(captured["root"], self.movie.parent)
        self.assertEqual(self._recorded_category(), "Uncategorized")


class TestEncodeJobSingleScan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "library"
        self.root.mkdir()
        (self.root / "movie.mkv").write_bytes(b"x" * 16)
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _counting_resolve(self, calls: "list[Path | None]"):
        """Wraps the real resolve_movies behind a call recorder.

        The recorded value is the call's `exclude` argument; the return
        value is an empty list, so the job's encode loop stays out of the
        test - the pin is the scan wiring, not the per-file outcomes.
        """

        def wrapper(*args, **kwargs):
            calls.append(kwargs.get("exclude"))
            return []

        return wrapper

    def _run(self, payload: dict, calls: "list[Path | None]") -> dict:
        job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)
        with patch.object(jobs, "resolve_movies", side_effect=self._counting_resolve(calls)):
            return jobs.execute_encode(job, self.connection, threading.Event())

    def test_an_encode_job_resolves_its_payload_once(self) -> None:
        """One encode job, one payload scan (issue #374): the second walk the
        job used to run decided nothing and threw its result away.
        """
        out = self.root / "out"
        out.mkdir()
        calls: list[Path | None] = []
        result = self._run(
            {"paths": [str(self.root)], "quality": True, "output_dir": str(out)}, calls
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, {"outcomes": [], "cancelled": False})

    def test_the_scan_excludes_the_output_directory_in_two_directory_mode(self) -> None:
        """The user's output_dir sits inside the scanned root and holds this
        job's (and later jobs') outputs: it must never re-enter the payload.
        """
        out = self.root / "out"
        out.mkdir()
        calls: list[Path | None] = []
        self._run({"paths": [str(self.root)], "quality": True, "output_dir": str(out)}, calls)
        self.assertEqual(calls, [out])

    def test_the_scan_excludes_the_staging_directory_in_place(self) -> None:
        """In place the job writes into a staging directory inside the
        scanned root; the scan must exclude exactly that directory.
        """
        calls: list[Path | None] = []
        self._run({"paths": [str(self.root)], "quality": True}, calls)
        self.assertEqual(len(calls), 1)
        exclude = calls[0]
        self.assertIsNotNone(exclude)
        assert exclude is not None
        self.assertEqual(exclude.parent, self.root)
        self.assertTrue(exclude.name.startswith(jobs.STAGING_DIR_PREFIX))


if __name__ == "__main__":
    unittest.main()
