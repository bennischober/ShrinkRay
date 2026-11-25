"""The dashboard's reclaimed figure (issue #646).

The dashboard reports the true per-title library delta for an in-place chain
(the chain's first applied input minus the file's current size) plus the
recorded input-minus-output of applied output-directory/legacy rows, clamped at
0. The /metrics gauge keeps its own corroborated row-sum rule (pinned in
tests/test_metrics.py); the two intentionally differ on a multi-hop chain.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from shrinkray import db


class TestReclaimedBytes(unittest.TestCase):
    """Issue #646: one set-based field, consistent on both library surfaces."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.media = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.media.cleanup()
        self.tmp.cleanup()

    def _file(self, name: str, size: int) -> tuple[str, int]:
        path = str(Path(self.media.name) / name)
        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(path=Path(path), mtime=100.0, size=size, name=name, category="Cat"),
        )
        return path, file_id

    def _encode(
        self,
        file_id: int,
        input_size: int,
        output_size: int,
        output_file: str,
        *,
        outcome: str = "applied",
        generation: int = 1,
    ) -> None:
        db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "svt_av1",
            input_size,
            output_size,
            0.0,
            output_file=output_file,
            generation=generation,
            outcome=outcome,
        )

    def _read(self, name: str) -> tuple[dict, dict]:
        """Returns the title's snapshot and /api/library entries."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        self.connection.commit()
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        app_module.allow_roots([Path(self.media.name), Path(self.tmp.name)])
        try:
            client = TestClient(fast_app)
            snapshot = client.get("/api/snapshot").json()["library"]
            library = client.get("/api/library").json()
        finally:
            app_module.state.shutdown()
            app_module.state = None
        snapshot_entry = next(entry for entry in snapshot if entry["name"] == name)
        library_entry = next(entry for entry in library if entry["name"] == name)
        return snapshot_entry, library_entry

    def _assert_reclaimed(self, name: str, expected: int) -> None:
        snapshot_entry, library_entry = self._read(name)
        self.assertEqual(snapshot_entry["reclaimed_bytes"], expected)
        self.assertEqual(library_entry["reclaimed_bytes"], expected)

    def test_a_chain_reports_the_whole_library_delta(self) -> None:
        """A 2-hop in-place chain 1000->500->400 reports 600, not the
        latest hop's 100 (issue #646's defect)."""
        path, file_id = self._file("chained.mkv", 400)
        self._encode(file_id, 1000, 500, path, generation=1)
        self._encode(file_id, 500, 400, path, generation=2)
        self._assert_reclaimed("chained.mkv", 600)

    def test_an_output_dir_row_counts_its_own_saving(self) -> None:
        """An output-directory artifact lives outside the library: its
        recorded input-minus-output is the title's saving."""
        _, file_id = self._file("out.mkv", 1000)
        self._encode(file_id, 1000, 400, str(Path(self.media.name) / "encoded.mkv"))
        self._assert_reclaimed("out.mkv", 600)

    def test_an_output_dir_row_adds_to_the_library_delta(self) -> None:
        """The owner's definition is a sum: an in-place chain's library
        delta plus the savings of the title's output-directory rows."""
        path, file_id = self._file("mixed.mkv", 500)
        self._encode(file_id, 1000, 400, str(Path(self.media.name) / "encoded.mkv"))
        self._encode(file_id, 1000, 500, path, generation=1)
        self._assert_reclaimed("mixed.mkv", 1100)

    def test_an_untouched_title_contributes_nothing(self) -> None:
        """A never-encoded title still carries the field, as 0."""
        self._file("untouched.mkv", 1000)
        self._assert_reclaimed("untouched.mkv", 0)

    def test_a_restored_original_contributes_nothing(self) -> None:
        """The #647 predicate: the in-place row's recorded output is not
        the file the path now carries, so the title reclaims nothing."""
        path, file_id = self._file("restored.mkv", 1000)
        self._encode(file_id, 1000, 500, path)
        self._assert_reclaimed("restored.mkv", 0)

    def test_a_discarded_attempt_contributes_nothing(self) -> None:
        """The #625 rule: a discarded attempt's artifact was thrown away."""
        path, file_id = self._file("discarded.mkv", 1000)
        self._encode(file_id, 1000, 500, path, outcome="discarded")
        self._assert_reclaimed("discarded.mkv", 0)

    def test_a_missing_file_contributes_nothing(self) -> None:
        """The #647 rule: a missing file has nothing on disk."""
        path, file_id = self._file("missing.mkv", 400)
        self._encode(file_id, 1000, 400, path)
        self.connection.execute("UPDATE files SET missing_scans = 2 WHERE id = ?", (file_id,))
        self._assert_reclaimed("missing.mkv", 0)

    def test_a_size_mismatched_in_place_row_contributes_nothing(self) -> None:
        """The #647 predicate: the file changed since the encode, so the
        row is history and no library delta is claimed."""
        path, file_id = self._file("mismatch.mkv", 900)
        self._encode(file_id, 1000, 500, path)
        self._assert_reclaimed("mismatch.mkv", 0)

    def test_a_grown_file_clamps_at_zero(self) -> None:
        """The owner's 2026-11-23 decision: a negative delta is 0."""
        path, file_id = self._file("grown.mkv", 1200)
        self._encode(file_id, 1000, 1200, path)
        self._assert_reclaimed("grown.mkv", 0)

    def test_a_null_first_input_clamps_at_zero(self) -> None:
        """A malformed chain whose first applied row has a NULL input_size
        contributes 0 rather than a null or negative figure."""
        path, file_id = self._file("null.mkv", 400)
        self._encode(file_id, 1000, 500, path, generation=1)
        self._encode(file_id, 500, 400, path, generation=2)
        self.connection.commit()
        self.connection.close()
        raw = sqlite3.connect(Path(self.tmp.name) / "test.db")
        try:
            raw.execute("PRAGMA writable_schema = ON")
            raw.execute(
                "UPDATE sqlite_master SET sql = replace(sql,"
                " 'input_size INTEGER NOT NULL', 'input_size INTEGER')"
                " WHERE name = 'encodes' AND type = 'table'"
            )
            raw.execute("PRAGMA writable_schema = RESET")
            raw.execute(
                "UPDATE encodes SET input_size = NULL WHERE id = (SELECT MIN(id) FROM encodes)"
            )
            raw.commit()
        finally:
            raw.close()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")
        self._assert_reclaimed("null.mkv", 0)


if __name__ == "__main__":
    unittest.main()
