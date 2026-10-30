"""Tests for an explicit file/glob scan never pruning beyond its files (issue #453).

`scan_movies(root, config, files=[a])` reconciles the database through
`prune_missing(..., scope=str(root))`: every catalogued row under the root
that the explicit call did not enumerate gets flagged as missing and, after
MISSING_GRACE scans, hard-deleted together with its encode history - although
the explicit scan never inspected those files. An explicit scan inspects
exactly its files and has no opinion about the rest of the scope.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray import db
from shrinkray.engine import scan as engine_scan


def _make_scan_config():
    """A scan config that finds .mkv files (the test_cli harness's shape)."""
    from tests import test_cli as test_cli_mod

    return test_cli_mod.config_helper.Conf()


class TestExplicitScanDoesNotPruneBeyondItsFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "a.mkv").write_bytes(b"x" * 16)
        (self.root / "b.mkv").write_bytes(b"x" * 16)
        (self.root / ".previews").mkdir()
        (self.root / ".previews" / "h.mkv").write_bytes(b"x" * 16)
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _catalogue_all(self) -> None:
        """Catalogues a, b and h the way a successful folder scan leaves them.

        The rows are inserted at the db seam, not produced by running a scan
        over the bytes: every probe here fails (the bytes are not media) and
        a failed probe upserts no row - the issue's scenario is rows EXISTING
        before the explicit scans, which is what this manufactures. An encode
        row per file proves the encode history survives the explicit scans.
        """
        for path in (
            self.root / "a.mkv",
            self.root / "b.mkv",
            self.root / ".previews" / "h.mkv",
        ):
            self.connection.execute(
                "INSERT INTO files (path, mtime, size, name, category) "
                "VALUES (?, 0, 16, ?, 'movies')",
                (str(path), path.name),
            )
            file_id = self.connection.execute(
                "SELECT id FROM files WHERE path = ?", (str(path),)
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, "
                "output_size, saved_percent) VALUES (?, '5', 'svt_av1', 16, 8, 50.0)",
                (file_id,),
            )
        self.connection.commit()

    def _row_state(self, path: Path) -> tuple[int, bool]:
        """(missing_scans, row_exists) for one path."""
        row = self.connection.execute(
            "SELECT missing_scans FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        if row is None:
            return (-1, False)
        return (int(row[0]), True)

    def test_three_explicit_scans_do_not_delete_unenumerated_rows(self) -> None:
        """The maintainer's reproduction (issue #453): a full folder scan, then
        three explicit scans of a.mkv - b.mkv and .previews/h.mkv must keep
        their rows and encode history (missing_scans untouched).
        """
        self._catalogue_all()
        a = self.root / "a.mkv"
        b = self.root / "b.mkv"
        h = self.root / ".previews" / "h.mkv"

        for _ in range(3):
            engine_scan.scan_movies(
                self.root,
                _make_scan_config(),
                files=[a],
                connection=self.connection,
                workers=1,
            )

        self.assertEqual(self._row_state(a), (0, True))
        self.assertEqual(self._row_state(b), (0, True))
        self.assertEqual(self._row_state(h), (0, True))

    def test_an_explicit_glob_does_not_delete_the_files_it_missed(self) -> None:
        """The *.mkv glob case: files=[a, b] must leave c.mp4's row alone."""
        c = self.root / "c.mp4"
        c.write_bytes(b"x" * 16)
        self._catalogue_all()
        self.connection.execute(
            "INSERT INTO files (path, mtime, size, name, category) "
            "VALUES (?, 0, 16, 'c.mp4', 'movies')",
            (str(c),),
        )
        self.connection.commit()
        for _ in range(3):
            engine_scan.scan_movies(
                self.root,
                _make_scan_config(),
                files=[self.root / "a.mkv", self.root / "b.mkv"],
                connection=self.connection,
                workers=1,
            )
        self.assertEqual(self._row_state(c), (0, True))

    def test_an_explicit_scan_still_flags_its_own_gone_file(self) -> None:
        """An explicit scan DOES reconcile its own files: an enumerated file
        that vanished from disk is flagged (grace applies) - the scan looked
        at it and it is not there.
        """
        self._catalogue_all()
        gone = self.root / "b.mkv"
        gone.unlink()
        for _ in range(3):
            engine_scan.scan_movies(
                self.root,
                _make_scan_config(),
                files=[self.root / "a.mkv", gone],
                connection=self.connection,
                workers=1,
            )
        # b was enumerated in every scan and is gone: after 3 scans the grace
        # is exhausted and the row is hard-deleted (its own reconciliation)
        self.assertEqual(self._row_state(gone), (-1, False))

    def test_prune_missing_only_restriction(self) -> None:
        """db-level: `only` reconciles exactly those rows; nothing else in
        the scope is touched, with or without an extra scope filter.
        """
        a = str(self.root / "a.mkv")
        b = str(self.root / "b.mkv")
        h = str(self.root / ".previews" / "h.mkv")
        for p in (a, b, h):
            self.connection.execute(
                "INSERT INTO files (path, mtime, size, name, category, missing_scans) "
                "VALUES (?, 0, 16, ?, 'movies', 0)",
                (p, Path(p).name),
            )
        self.connection.commit()
        result = db.prune_missing(self.connection, {a}, scope=self.root, only={a})
        self.assertEqual(result.flagged, 0)
        self.assertEqual(result.pruned, 0)
        self.assertEqual(self._row_state(Path(b)), (0, True))
        self.assertEqual(self._row_state(Path(h)), (0, True))
        # and an enumerated-but-gone row is flagged by `only`:
        self.connection.execute("DELETE FROM files WHERE path = ?", (a,))
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO files (path, mtime, size, name, category) "
            "VALUES (?, 0, 16, 'a.mkv', 'movies')",
            (a,),
        )
        self.connection.commit()
        result = db.prune_missing(self.connection, set(), scope=self.root, only={a})
        self.assertEqual(result.flagged, 1)

    def test_prune_missing_only_deletes_history_with_the_row(self) -> None:
        """db-level: when grace exhausts for an `only` row, its encodes go
        WITH it (the documented pairing) - and an unenumerated row's history
        is never touched.
        """
        a = str(self.root / "a.mkv")
        b = str(self.root / "b.mkv")
        for p in (a, b):
            self.connection.execute(
                "INSERT INTO files (path, mtime, size, name, category) "
                "VALUES (?, 0, 16, ?, 'movies')",
                (p, Path(p).name),
            )
            file_id = self.connection.execute(
                "SELECT id FROM files WHERE path = ?", (p,)
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, "
                "output_size, saved_percent) VALUES (?, '5', 'svt_av1', 16, 8, 50.0)",
                (file_id,),
            )
        self.connection.commit()
        # three reconciles of only={a} with a absent: grace 3 -> the row AND
        # its encodes are gone; b (unenumerated) keeps row + encodes
        for _ in range(3):
            db.prune_missing(self.connection, set(), scope=self.root, only={a})
        a_row = self.connection.execute(
            "SELECT COUNT(*) FROM files WHERE path = ?", (a,)
        ).fetchone()[0]
        a_hist = self.connection.execute(
            "SELECT COUNT(*) FROM encodes WHERE file_id = (SELECT id FROM files WHERE path = ?)",
            (a,),
        ).fetchone()[0]
        b_row = self.connection.execute(
            "SELECT COUNT(*) FROM files WHERE path = ?", (b,)
        ).fetchone()[0]
        b_hist = self.connection.execute(
            "SELECT COUNT(*) FROM encodes WHERE file_id = (SELECT id FROM files WHERE path = ?)",
            (b,),
        ).fetchone()[0]
        self.assertEqual((a_row, a_hist), (0, 0))
        self.assertEqual((b_row, b_hist), (1, 1))


if __name__ == "__main__":
    unittest.main()
