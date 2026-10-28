"""Plans table (schema v9): migration, upsert, fingerprint validation."""

import tempfile
import unittest
from pathlib import Path

from shrinkray import db, service


class TestPlans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _file(self, name: str = "movie.mkv") -> tuple[int, Path]:
        movie = Path(self.tmp.name) / name
        movie.write_bytes(b"x" * 16)
        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=name,
                category="Cat",
            ),
        )
        self.connection.commit()
        return file_id, movie

    def _save(self, file_id: int, **overrides) -> int:
        args: dict = {
            "file_id": file_id,
            "chosen_crf": 21,
            "achieved_vmaf": 95.47,
            "crop": None,
            "probes": [],
            "skipped_reason": None,
            "estimated_size": 8,
            "input_mtime": Path(self.tmp.name).joinpath("movie.mkv").stat().st_mtime,
            "input_size": 16,
            "encoder": "x265",
            "encoder_preset": "medium",
            "target_vmaf": 95.0,
            "crf_low": 14,
            "crf_high": 32,
            "sample_count": 7,
            "sample_seconds": 5.0,
        }
        args.update(overrides)
        return db.save_plan(self.connection, **args)

    def _lookup(self, movie: Path, **overrides):
        args: dict = {
            "encoder": "x265",
            "encoder_preset": "medium",
            "target_vmaf": 95.0,
            "crf_range": (14, 32),
            "sample_count": 7,
            "sample_seconds": 5.0,
        }
        args.update(overrides)
        return db.get_valid_plan(self.connection, movie, **args)

    def test_migrations_reach_v9_and_plans_table_exists(self):
        version = self.connection.execute("SELECT version FROM schema_version").fetchone()[0]
        self.assertEqual(version, db.SCHEMA_VERSION)
        tables = {
            r[0]
            for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("plans", tables)
        # provenance columns exist on encodes
        cols = {r[1] for r in self.connection.execute("PRAGMA table_info(encodes)")}
        self.assertIn("crf_source", cols)
        self.assertIn("plan_created_at", cols)

    def test_save_plan_upserts_one_row_per_file(self):
        file_id, _movie = self._file()
        self._save(file_id)
        self._save(file_id, chosen_crf=20)  # upsert: replaces, no duplicate
        rows = self.connection.execute("SELECT * FROM plans").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chosen_crf"], 20)

    def test_valid_plan_roundtrip(self):
        file_id, movie = self._file()
        self._save(file_id)
        plan = self._lookup(movie)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["chosen_crf"], 21)
        self.assertEqual(plan["achieved_vmaf"], 95.47)

    def test_mismatched_fingerprint_is_invalid(self):
        file_id, movie = self._file()
        self._save(file_id)
        # any changed decision input invalidates
        self.assertIsNone(self._lookup(movie, encoder_preset="6"))
        self.assertIsNone(self._lookup(movie, target_vmaf=93.0))
        self.assertIsNone(self._lookup(movie, crf_range=(16, 30)))
        self.assertIsNone(self._lookup(movie, sample_count=5))
        self.assertIsNone(self._lookup(movie, sample_seconds=4.0))
        # changed file (mtime+size) invalidates
        movie.write_bytes(b"x" * 32)
        self.assertIsNone(self._lookup(movie))
        # vanished file invalidates
        movie.unlink()
        self.assertIsNone(self._lookup(movie))

    def test_no_plan_row_is_none(self):
        _, movie = self._file()
        self.assertIsNone(self._lookup(movie))


class TestRecordPlan(unittest.TestCase):
    """The service write path (plan runs quicksave their decisions)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _store(self, size_change: bytes = 0):
        from types import SimpleNamespace

        movie = Path(self.tmp.name) / "movie.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name="movie.mkv",
                category="Cat",
            ),
        )
        self.connection.commit()
        if size_change:
            movie.write_bytes(b"x" * (16 + size_change))
        result = SimpleNamespace(
            chosen_crf=21,
            achieved_vmaf=95.47,
            crop=None,
            probes=[],
            skipped_reason=None,
            input_size=16,  # what was probed
            output_size=8,  # the estimate
            success=True,
            target_vmaf=95.0,
            crf_range=(14, 32),
            sample_count=3,
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            encoder_preset="5",
            probe_encoder_preset="5",
            hdr=False,
            encoder="x265",
            input_mtime=None,  # a duck-typed result without the #436 field
        )
        config = type("Conf", (), {"handbrake_encoder": "x265"})()
        plan_id = service.record_plan(self.connection, movie, result, config)
        return plan_id, movie

    def test_records_plan_for_scanned_movie(self):
        plan_id, _ = self._store()
        self.assertIsNotNone(plan_id)
        row = self.connection.execute("SELECT * FROM plans").fetchone()
        self.assertEqual(row["chosen_crf"], 21)

    def test_changed_file_is_not_recorded(self):
        """A file that changed between probing and recording gets no plan:
        storing would pin the NEW fingerprint to the OLD probes."""
        plan_id, _ = self._store(size_change=16)
        self.assertIsNone(plan_id)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)

    def test_unscanned_movie_is_not_recorded(self):
        from types import SimpleNamespace

        movie = Path(self.tmp.name) / "unscanned.mkv"
        movie.write_bytes(b"x" * 16)
        result = SimpleNamespace(
            chosen_crf=21,
            achieved_vmaf=95.47,
            crop=None,
            probes=[],
            skipped_reason=None,
            input_size=16,
            output_size=8,
            success=True,
            target_vmaf=95.0,
            crf_range=(14, 32),
            sample_count=3,
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            encoder_preset="5",
            probe_encoder_preset="5",
            hdr=False,
        )
        config = type("Conf", (), {"handbrake_encoder": "x265"})()
        self.assertIsNone(service.record_plan(self.connection, movie, result, config))


if __name__ == "__main__":
    unittest.main()
