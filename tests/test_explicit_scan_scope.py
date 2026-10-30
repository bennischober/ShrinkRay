"""Tests for an explicit file/glob scan never pruning beyond its files (issue #453).

Before the fix, `scan_movies(root, config, files=[a], walked=False)`
reconciled the database through `prune_missing(..., scope=str(root))`: every
catalogued row under the root that the explicit call did not enumerate got
flagged as missing and, after MISSING_GRACE scans, hard-deleted together
with its encode history - although the explicit scan never inspected those
files. An explicit scan inspects exactly its files and has no opinion about
the rest of the scope.
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
                walked=False,
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
                walked=False,
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
                walked=False,
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

    def test_a_walked_folder_scan_still_reconciles_the_scope(self) -> None:
        """A WALKED folder scan is the operator's full-library statement:
        `shrinkray plan <folder>` passes the walk's result as `files` WITH
        `walked=True`, and the reconcile must flag/grace-delete an
        unenumerated gone row exactly as before the explicit-scope fix
        (deleting the `and not walked` clause must turn this test red, so
        the discriminator is load-bearing).
        """
        self._catalogue_all()
        (self.root / "b.mkv").unlink()
        # the CLI's directory shape: the walked list (a + h, b is gone)
        # arrives as `files`, walked=True
        walked_files = engine_scan.find_movies(self.root, [".mkv"])
        self.assertEqual(
            sorted(p.name for p in walked_files),
            ["a.mkv", "h.mkv"],
        )
        for _ in range(3):
            engine_scan.scan_movies(
                self.root,
                _make_scan_config(),
                connection=self.connection,
                workers=1,
                files=walked_files,
                walked=True,
            )
        # b was not enumerated (the walk found only a + h now) and is gone:
        # after 3 walked scans the grace is exhausted, the row is deleted
        self.assertEqual(self._row_state(self.root / "b.mkv"), (-1, False))
        self.assertEqual(self._row_state(self.root / "a.mkv"), (0, True))

    def test_the_cli_forwards_the_walked_pair(self) -> None:
        """The resolver's `walked` fact must REACH scan_movies through
        BOTH CLI call sites (plan and encode): a CLI command that resolves a
        walk's result but drops the flag reconciles the WRONG scope -
        deleting `walked=walked` at either site leaves every test green
        while silently removing `plan <folder>`'s deletion path. The real
        CLI is driven and the recorded kwargs pin the forwarding (the engine
        itself is mocked).
        """
        import os
        from contextlib import contextmanager
        from unittest.mock import patch

        from shrinkray.__main__ import app

        recorded: list[dict] = []

        def fake_scan(root, config, **kwargs):
            recorded.append(kwargs)
            return {}, [], []

        # the CLI binds scan_movies into its own namespace: the patch target
        # is the LOOKUP home (the suite's convention), not the engine module

        @contextmanager
        def toolchain_present():
            from shrinkray.engine import watch as watch_mod

            with (
                patch("shrinkray.service.shutil.which", return_value="ffprobe"),
                patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
                patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
                patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value="modern"),
                patch.object(watch_mod, "STABLE_MIN_AGE", 0.0),
                patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
            ):
                yield

        previous = os.getcwd()
        os.chdir(self.root)
        try:
            from typer.testing import CliRunner

            runner = CliRunner()
            with (
                toolchain_present(),
                patch("shrinkray.__main__.scan_movies", side_effect=fake_scan),
            ):
                runner.invoke(app, ["plan", str(self.root)])
                folder_kwargs = dict(recorded[-1])
                runner.invoke(app, ["plan", "a.mkv"])
                file_kwargs = dict(recorded[-1])
                # the ENCODE site has its own resolve_input_paths +
                # scan_movies call - drive it too (the plan pin alone leaves
                # an encode-site regression green)
                runner.invoke(app, ["encode", str(self.root)])
                encode_folder_kwargs = dict(recorded[-1])
                runner.invoke(app, ["encode", "a.mkv"])
                encode_file_kwargs = dict(recorded[-1])
        finally:
            os.chdir(previous)

        self.assertTrue(folder_kwargs.get("walked"), "plan <folder> lost walked")
        self.assertIsNotNone(folder_kwargs.get("files"))
        self.assertFalse(file_kwargs.get("walked"), "plan <file> lost walked=False")
        self.assertIsNotNone(file_kwargs.get("files"))
        self.assertTrue(encode_folder_kwargs.get("walked"), "encode <folder> lost walked")
        self.assertIsNotNone(encode_folder_kwargs.get("files"))
        self.assertFalse(encode_file_kwargs.get("walked"), "encode <file> lost walked=False")
        self.assertIsNotNone(encode_file_kwargs.get("files"))

    def test_the_walked_pair_is_refused_half_supplied(self) -> None:
        """The files/walked pair is contracted in SOURCE: a half-supplied
        pair (files without walked, walked without files) is refused with a
        ValueError instead of silently reconciling the wrong scope - a
        caller supplying a walk's result while omitting the flag would
        otherwise stop flagging deletions without any error.
        """
        with self.assertRaises(ValueError):
            engine_scan.scan_movies(self.root, _make_scan_config(), files=[self.root / "a.mkv"])
        with self.assertRaises(ValueError):
            engine_scan.scan_movies(self.root, _make_scan_config(), walked=True)

    def test_resolve_input_paths_reports_the_walked_fact(self) -> None:
        """The resolver returns the discriminator: a directory argument is a
        walk (True), a named file and a glob are enumerations (False) - the
        scan's reconciliation keys on exactly this.
        """
        from shrinkray.engine.scan import resolve_input_paths

        _root, _files, walked = resolve_input_paths(str(self.root), [".mkv"])
        self.assertTrue(walked)
        _root, _files, walked = resolve_input_paths(str(self.root / "a.mkv"), [".mkv"])
        self.assertFalse(walked)
        _root, _files, walked = resolve_input_paths(str(self.root / "*.mkv"), [".mkv"])
        self.assertFalse(walked)

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
