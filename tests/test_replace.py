"""Tests for swap-in-place with a retained original (issue #35).

The library is sacred: no code path in this module deletes a source. A
replaced original is renamed aside and recorded, and every crash state leaves
the source path holding either the original or the new file, never neither.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray import db, replace


class TestSwap(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"ORIGINAL" * 100)
        self.output = self.root / "enc" / "movie.mkv"
        self.output.parent.mkdir()
        self.output.write_bytes(b"NEW" * 10)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_source_path_holds_the_new_file(self) -> None:
        staged = replace.stage(self.output, self.source)
        replace.swap(self.source, staged)
        self.assertEqual(self.source.read_bytes(), b"NEW" * 10)

    def test_the_original_is_retained_not_deleted(self) -> None:
        staged = replace.stage(self.output, self.source)
        retained = replace.swap(self.source, staged)
        self.assertEqual(retained, replace.retained_path_for(self.source))
        self.assertEqual(retained.read_bytes(), b"ORIGINAL" * 100)

    def test_the_staging_copy_is_flushed_and_complete(self) -> None:
        staged = replace.stage(self.output, self.source)
        self.assertEqual(staged.stat().st_size, self.output.stat().st_size)
        self.assertEqual(staged.read_bytes(), self.output.read_bytes())

    def test_the_source_directory_holds_the_staging_file(self) -> None:
        """The staged copy must be a same-directory sibling: a cross-device
        rename is a copy, which cannot be atomic."""
        staged = replace.stage(self.output, self.source)
        self.assertEqual(staged.parent, self.source.parent)

    def test_swap_refuses_when_a_retained_original_exists(self) -> None:
        """Never overwrite a retained original: it is the operator's only copy
        of that file until they reclaim the space themselves."""
        replace.retained_path_for(self.source).write_bytes(b"OLDER")
        staged = replace.stage(self.output, self.source)
        with self.assertRaises(replace.SwapError):
            replace.swap(self.source, staged)
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL" * 100)

    def test_a_verification_finding_declines_the_swap(self):
        """Review finding 1: a swap on an artifact with a verification finding
        (a lost track, an unverifiable probe) would make the finding permanent:
        it is declined, the source is untouched, and the encode row is corrected
        to discarded (issue #232 contract)."""
        from shrinkray import db, service

        db_file = self.root / "metadata.db"
        connection = db.connect(db_file)
        outcome = service.swap_encode_into_place(
            connection,
            self.source,
            self.output,
            None,
            error="output lost 1 audio track(s)",
        )
        self.assertFalse(outcome.replaced)
        self.assertIn("failed verification", outcome.reason or "")
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL" * 100)
        connection.close()

    def test_a_failed_copy_raises_and_leaves_the_source_alone(self) -> None:
        missing = self.root / "enc" / "gone.mkv"
        with self.assertRaises(replace.SwapError):
            replace.stage(missing, self.source)
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL" * 100)


class TestRecovery(unittest.TestCase):
    """Every crash state leaves the source present, and recovery resolves it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"ORIGINAL")
        self.retained = replace.retained_path_for(self.source)
        self.staged = replace.staging_path_for(self.source)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clean_when_nothing_happened(self) -> None:
        self.assertEqual(replace.recover(self.source), "clean")
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL")

    def test_crash_after_the_first_rename_restores_the_original(self) -> None:
        """source moved aside, staging written, second rename never ran."""
        self.retained.write_bytes(b"ORIGINAL")
        self.staged.write_bytes(b"NEW")
        self.source.unlink()
        self.assertEqual(replace.recover(self.source), "restored the original")
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL")
        self.assertFalse(self.staged.exists())

    def test_an_orphaned_staging_copy_is_discarded(self) -> None:
        self.staged.write_bytes(b"NEW")
        self.assertEqual(replace.recover(self.source), "discarded an orphaned staging copy")
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL")

    def test_a_completed_swap_is_left_alone(self) -> None:
        """source and retained both present: the swap finished (or never
        started), so recovery must not touch either file."""
        self.retained.write_bytes(b"OLDER")
        self.assertEqual(replace.recover(self.source), "clean")
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL")
        self.assertEqual(self.retained.read_bytes(), b"OLDER")

    def test_a_missing_source_with_its_original_is_reported(self) -> None:
        self.retained.write_bytes(b"ORIGINAL")
        self.source.unlink()
        self.assertEqual(replace.recover(self.source), "source missing with its original retained")

    def test_recover_directory_resolves_every_interrupted_swap(self) -> None:
        second = self.root / "other.mkv"
        second.write_bytes(b"ORIGINAL")
        replace.retained_path_for(second).write_bytes(b"ORIGINAL")
        replace.staging_path_for(second).write_bytes(b"NEW")
        second.unlink()
        self.staged.write_bytes(b"NEW")
        outcomes = replace.recover_directory(self.root)
        self.assertEqual(outcomes["movie.mkv"], "discarded an orphaned staging copy")
        self.assertEqual(outcomes["other.mkv"], "restored the original")
        self.assertEqual(second.read_bytes(), b"ORIGINAL")


class TestLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "metadata.db")
        self.source = Path(self.tmp.name) / "movie.mkv"
        self.source.write_bytes(b"ORIGINAL" * 100)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_a_swapped_original_is_listed(self) -> None:
        retained = replace.retained_path_for(self.source)
        db.add_retained(self.connection, self.source, retained, None, 800, 30)
        rows = db.list_retained(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["retained_path"], str(retained))
        self.assertEqual(rows[0]["size_before"], 800)
        self.assertEqual(rows[0]["size_after"], 30)

    def test_the_ledger_links_the_encode(self) -> None:
        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=self.source,
                mtime=self.source.stat().st_mtime,
                size=self.source.stat().st_size,
                name="movie",
                category="Cat",
            ),
        )
        encode_id = db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "svt_av1",
            800,
            30,
            96.25,
            chosen_crf=30,
            achieved_vmaf=95.2,
        )
        retained = replace.retained_path_for(self.source)
        db.add_retained(self.connection, self.source, retained, encode_id, 800, 30)
        rows = db.list_retained(self.connection)
        self.assertEqual(rows[0]["encode_id"], encode_id)
        self.assertEqual(rows[0]["file_id"], file_id)

    def test_an_empty_ledger_lists_nothing(self) -> None:
        self.assertEqual(db.list_retained(self.connection), [])

    def test_the_retained_table_exists_from_v14_onwards(self) -> None:
        version = self.connection.execute("SELECT version FROM schema_version").fetchone()[0]
        # the ledger landed in migration 14; later migrations must not drop it
        self.assertGreaterEqual(version, 14)
        columns = [row[1] for row in self.connection.execute("PRAGMA table_info(retained)")]
        for expected in (
            "source_path",
            "retained_path",
            "encode_id",
            "size_before",
            "size_after",
            "created_at",
        ):
            self.assertIn(expected, columns)


class TestSwapGate(unittest.TestCase):
    """Issue #130: the swap gate is the last code before a file enters the
    library, so it re-checks size even though the engine already refused a grown
    encode. A bug upstream must not be able to make the library bigger."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"ORIGINAL" * 100)
        self.output = self.root / "enc" / "movie.mkv"
        self.output.parent.mkdir()
        self.connection = db.connect(self.root / "metadata.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _swap(self) -> None:
        from shrinkray.__main__ import _swap_into_place

        _swap_into_place(self.connection, self.source, self.output)

    def test_a_grown_output_is_refused(self) -> None:
        self.output.write_bytes(b"BIGGER" * 200)
        self._swap()
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL" * 100)
        self.assertFalse(replace.retained_path_for(self.source).exists())

    def test_an_identical_output_is_refused(self) -> None:
        """Equal size is not a shrink: nothing gained, a generation loss paid."""
        self.output.write_bytes(self.source.read_bytes())
        self._swap()
        self.assertEqual(self.source.read_bytes(), b"ORIGINAL" * 100)

    def test_a_smaller_output_is_swapped(self) -> None:
        """The regression guard: the gate must not block the normal case."""
        self.output.write_bytes(b"new" * 10)
        self._swap()
        self.assertEqual(self.source.read_bytes(), b"new" * 10)
        self.assertTrue(replace.retained_path_for(self.source).exists())
        rows = db.list_retained(self.connection)
        self.assertEqual(len(rows), 1)


class TestNoSourceDeletion(unittest.TestCase):
    """The product rule: no code path in ShrinkRay deletes a source."""

    def test_the_module_never_unlinks_a_source(self) -> None:
        import ast

        tree = ast.parse(Path(replace.__file__).read_text())
        unlinks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("unlink", "remove", "rmtree", "trash")
        ]
        self.assertEqual(len(unlinks), 1, "only _discard may remove a file")
        discard = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_discard"
        )
        self.assertIn(unlinks[0], list(ast.walk(discard)))


if __name__ == "__main__":
    unittest.main()
