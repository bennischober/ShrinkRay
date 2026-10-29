"""Database layer tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from shrinkray import config as config_helper
from shrinkray import db


class TestDb(unittest.TestCase):
    def test_the_config_route_is_gone(self) -> None:
        """Issue #134: the dashboard is a status view, not a control surface.

        PUT /api/config let a browser rewrite config.json. The operator edits the
        file on the mounted volume instead, so the route is removed - and a removed
        route must answer 405, not silently accept the write.
        """
        from fastapi.testclient import TestClient

        from shrinkray.server import app as app_module

        client = TestClient(app_module.app)
        response = client.put("/api/config", json={"language": "de"})
        self.assertIn(response.status_code, (404, 405))

    def setUp(self):

        self.tmp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _record(self, path: str) -> db.MovieRecord:
        return db.MovieRecord(path=Path(path), mtime=100.0, size=1000, name="test", category="Cat")

    def test_connect_creates_schema(self):
        tables = {
            row["name"]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("files", tables)
        self.assertIn("encodes", tables)
        self.assertIn("schema_version", tables)
        version = self.connection.execute("SELECT version FROM schema_version").fetchone()
        self.assertEqual(version["version"], db.SCHEMA_VERSION)

    def test_migration_v1_to_v2(self):
        # simulate a v1 database: current schema without the quality columns

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v1.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (1);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL
                );
                CREATE TABLE encodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    preset TEXT NOT NULL,
                    encoder TEXT NOT NULL,
                    input_size INTEGER NOT NULL,
                    output_size INTEGER NOT NULL,
                    saved_percent REAL NOT NULL
                );
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect(db_path)
            version = migrated.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(version["version"], db.SCHEMA_VERSION)
            # the quality columns exist and are usable
            columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(encodes)").fetchall()
            }
            self.assertIn("chosen_crf", columns)
            self.assertIn("achieved_vmaf", columns)
            migrated.close()

    def test_migration_v2_to_v3_absolute_paths(self):
        # v2 database with a relative path => migrated to absolute (or dropped)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v2.db"
            # create a real file so the relative path can resolve to it
            existing = Path(tmp) / "movie.mkv"
            existing.write_bytes(b"x")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                connection = sqlite3.connect(db_path)
                connection.executescript(
                    """
                    CREATE TABLE schema_version (version INTEGER NOT NULL);
                    INSERT INTO schema_version (version) VALUES (2);
                    CREATE TABLE files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL UNIQUE,
                        mtime REAL NOT NULL,
                        size INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        category TEXT NOT NULL
                    );
                    CREATE TABLE encodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                        preset TEXT NOT NULL,
                        encoder TEXT NOT NULL,
                        input_size INTEGER NOT NULL,
                        output_size INTEGER NOT NULL,
                        saved_percent REAL NOT NULL
                    );
                    INSERT INTO files (path, mtime, size, name, category) VALUES
                        ('movie.mkv', 1.0, 1, 'movie', 'Cat'),
                        ('deleted.mkv', 1.0, 1, 'gone', 'Cat');
                    """
                )
                connection.commit()
                connection.close()

                migrated = db.connect(db_path)
                version = migrated.execute("SELECT version FROM schema_version").fetchone()
                self.assertEqual(version["version"], db.SCHEMA_VERSION)
                paths = {r["path"] for r in migrated.execute("SELECT path FROM files")}
                # existing file => absolute; deleted file => dropped
                self.assertEqual(len(paths), 1)
                self.assertIn(str(existing), paths)
                migrated.close()
            finally:
                os.chdir(cwd)

    def test_migration_v5_to_v6_missing_columns(self):
        # v5 database: files without the soft-missing columns => added with
        # defaults, existing rows are considered present (not missing)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v5.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (5);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL
                );
                CREATE TABLE encodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    preset TEXT NOT NULL,
                    encoder TEXT NOT NULL,
                    input_size INTEGER NOT NULL,
                    output_size INTEGER NOT NULL,
                    saved_percent REAL NOT NULL
                );
                INSERT INTO files (path, mtime, size, name, category) VALUES
                    ('D:/Movies/a.mkv', 1.0, 1, 'movie', 'Cat');
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect(db_path)
            version = migrated.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(version["version"], db.SCHEMA_VERSION)
            columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(files)").fetchall()
            }
            self.assertIn("missing_scans", columns)
            self.assertIn("missing_since", columns)
            row = migrated.execute("SELECT * FROM files").fetchone()
            self.assertEqual(row["missing_scans"], 0)
            self.assertIsNone(row["missing_since"])
            migrated.close()

    def test_migration_v7_generation_columns(self):
        # v6 database: encodes without the drift columns => added with
        # defaults, generation starts at 1

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v6.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (6);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL
                );
                CREATE TABLE encodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    preset TEXT NOT NULL,
                    encoder TEXT NOT NULL,
                    input_size INTEGER NOT NULL,
                    output_size INTEGER NOT NULL,
                    saved_percent REAL NOT NULL
                );
                INSERT INTO files (path, mtime, size, name, category) VALUES
                    ('D:/Movies/a.mkv', 1.0, 1, 'movie', 'Cat');
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect(db_path)
            version = migrated.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(version["version"], db.SCHEMA_VERSION)
            columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(encodes)").fetchall()
            }
            self.assertIn("input_bitrate", columns)
            self.assertIn("output_bitrate", columns)
            self.assertIn("source_file", columns)
            self.assertIn("generation", columns)
            migrated.close()

    def test_migration_v16_adds_the_crops_table(self):
        """A v15 database gains the timeline crop cache (issue #181).

        The crops table is new storage, so the migration only creates it: no
        rewrite of existing rows, no data loss. After the migration the table
        accepts the detector's summary and a v15-built database still passes
        its integrity check.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v15.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (15);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL
                );
                INSERT INTO files (path, mtime, size, name, category) VALUES
                    ('D:/Movies/a.mkv', 1.0, 1, 'movie', 'Cat');
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect(db_path)
            version = migrated.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(version["version"], db.SCHEMA_VERSION)
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(crops)")}
            self.assertIn("bars", columns)
            self.assertIn("input_mtime", columns)
            self.assertIn("input_size", columns)
            self.assertIn("ratios", columns)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM crops").fetchone()[0], 0)
            migrated.close()

    def test_migration_v18_adds_encoded_size(self):
        """A v17 database gains the probe cache's encoded_size column (issue
        #314).

        The column is new storage beside the score: legacy rows keep NULL,
        which the readers treat as a MISS for size purposes, never as zero.
        After the migration a v17-built database answers get_probe_entry with
        (score, None) and its integrity check passes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v17.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (17);
                CREATE TABLE probe_cache (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO probe_cache (key, value) VALUES ('abc', 95.5);
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect(db_path)
            version = migrated.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(version["version"], db.SCHEMA_VERSION)
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(probe_cache)")}
            self.assertIn("encoded_size", columns)
            # the legacy row survives and reads as (score, None): a size miss
            self.assertEqual(db.get_probe_entry(migrated, "abc"), (95.5, None))
            self.assertIsNone(db.get_probe_entry(migrated, "nope"))
            migrated.close()

    def test_migration_v15_drops_plans_probed_without_bars(self):
        """A pre-v15 plan is discarded, never reused (issue #128).

        Bar removal became part of every re-encode, so a plan stored while the
        flag existed was probed WITHOUT a crop when the flag was off. Reusing it
        would encode the film with its bars kept - the exact thing the rule
        forbids. Dropping the rows makes the next run re-probe, which is correct;
        keeping them would be silently wrong on a real library.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v14.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (14);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL
                );
                CREATE TABLE encodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    preset TEXT NOT NULL,
                    encoder TEXT NOT NULL,
                    input_size INTEGER NOT NULL,
                    output_size INTEGER NOT NULL,
                    saved_percent REAL NOT NULL
                );
                CREATE TABLE plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    chosen_crf INTEGER,
                    achieved_vmaf REAL,
                    crop TEXT,
                    probes TEXT,
                    skipped_reason TEXT,
                    estimated_size INTEGER,
                    input_mtime REAL NOT NULL,
                    input_size INTEGER NOT NULL,
                    encoder TEXT NOT NULL,
                    encoder_preset TEXT NOT NULL,
                    target_vmaf REAL NOT NULL,
                    crf_low INTEGER NOT NULL,
                    crf_high INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    sample_seconds REAL NOT NULL,
                    remove_bars INTEGER NOT NULL,
                    crop_sample_count INTEGER NOT NULL DEFAULT 3,
                    vmaf_statistic TEXT NOT NULL DEFAULT 'mean',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO files (path, mtime, size, name, category) VALUES
                    ('D:/Movies/a.mkv', 1.0, 1, 'movie', 'Cat');
                INSERT INTO plans (
                    file_id, chosen_crf, achieved_vmaf, input_mtime, input_size,
                    encoder, encoder_preset, target_vmaf, crf_low, crf_high,
                    sample_count, sample_seconds, remove_bars
                ) VALUES (1, 21, 95.4, 1.0, 1, 'svt_av1', '5', 95.0, 14, 32, 3, 10.0, 0);
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect(db_path)
            version = migrated.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(version["version"], db.SCHEMA_VERSION)
            # the stale plan is gone, so nothing can be reused uncropped
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(plans)")}
            self.assertNotIn("remove_bars", columns)
            migrated.close()

    def test_derive_generation_chain(self):
        """Encoding an encode chains generation N+1 via source_file."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            original = root / "movie.mkv"
            reencode = root / "movie_reencoded.mkv"

            file_id = db.upsert_file(self.connection, self._record(str(original)))
            db.add_encode(
                self.connection,
                file_id,
                "VMAF>95",
                "x265",
                1000,
                500,
                50.0,
                source_file=str(original),
                output_file=str(reencode),
                generation=1,
            )
            # the re-encode input is the output of generation 1
            self.assertEqual(service.derive_generation(self.connection, reencode), 2)
            # an unrelated file stays generation 1
            self.assertEqual(service.derive_generation(self.connection, root / "other.mkv"), 1)

    def test_add_encode_with_drift_fields(self):
        file_id = db.upsert_file(self.connection, self._record("a.mkv"))
        encode_id = db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "x265",
            1000,
            300,
            70.0,
            input_bitrate=18.4,
            output_bitrate=14.9,
            source_file="D:/Movies/a.mkv",
            output_file="D:/Out/a.mkv",
            generation=1,
        )
        row = self.connection.execute("SELECT * FROM encodes WHERE id = ?", (encode_id,)).fetchone()
        self.assertEqual(row["input_bitrate"], 18.4)
        self.assertEqual(row["output_bitrate"], 14.9)
        self.assertEqual(row["source_file"], "D:/Movies/a.mkv")
        self.assertEqual(row["output_file"], "D:/Out/a.mkv")
        self.assertEqual(row["generation"], 1)

    def test_add_encode_with_quality_fields(self):
        file_id = db.upsert_file(self.connection, self._record("a.mkv"))
        encode_id = db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "x265",
            1000,
            300,
            70.0,
            chosen_crf=22,
            achieved_vmaf=95.4,
        )
        row = self.connection.execute("SELECT * FROM encodes WHERE id = ?", (encode_id,)).fetchone()
        self.assertEqual(row["chosen_crf"], 22)
        self.assertAlmostEqual(row["achieved_vmaf"], 95.4)

    def test_library_api_exposes_benchmark_metrics(self):
        """The /api/library payload carries the schema-v5 encode metrics."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=Path("D:/x/metrics.mkv"),
                mtime=100.0,
                size=1000,
                name="metrics",
                category="Cat",
            ),
        )
        db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "x265",
            1000,
            300,
            70.0,
            chosen_crf=22,
            achieved_vmaf=95.4,
            encoder_preset="6",
            encode_seconds=1234.5,
            probe_count=3,
            probe_seconds=30.0,
            hdr=False,
            tool_versions="hb=1.9 libvmaf=2.3.1",
        )
        self.connection.commit()

        # AppState opens its own connection to the same database file the
        # test connection wrote to (committed rows are visible through WAL)
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            response = client.get("/api/library")
            self.assertEqual(response.status_code, 200)
            movie = next(m for m in response.json() if m["name"] == "metrics")
            encode = movie["encode"]
            self.assertEqual(encode["encoder_preset"], "6")
            self.assertAlmostEqual(encode["encode_seconds"], 1234.5)
            self.assertEqual(encode["probe_count"], 3)
            self.assertAlmostEqual(encode["probe_seconds"], 30.0)
            self.assertEqual(encode["tool_versions"], "hb=1.9 libvmaf=2.3.1")
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_library_etag_304(self):
        """An unchanged library answers If-None-Match with 304 (the poll
        becomes nearly free)."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            first = client.get("/api/library")
            self.assertEqual(first.status_code, 200)
            etag = first.headers["ETag"]
            # unchanged: 304 without a body
            cached = client.get("/api/library", headers={"If-None-Match": etag})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.content, b"")
            # a changed library (new encode row) invalidates the fingerprint
            file_id = db.upsert_file(
                self.connection,
                db.MovieRecord(
                    path=Path("D:/x/new.mkv"), mtime=99.0, size=5, name="new", category="Cat"
                ),
            )
            db.add_encode(self.connection, file_id, "VMAF>95", "x265", 5, 3, 40.0)
            self.connection.commit()
            fresh = client.get("/api/library", headers={"If-None-Match": etag})
            self.assertEqual(fresh.status_code, 200)
            self.assertNotEqual(fresh.headers["ETag"], etag)
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_movie_detail_endpoint(self):
        """GET /api/movie returns the file facts plus the COMPLETE encode
        history (the library list carries only the latest encode), including
        the schema-v7 chain: a re-encoded file's page must show the parent
        row whose output produced it (the round-5 finding — the old query
        filtered on file_id alone and the parent row vanished)."""
        # real temp dir: the endpoint allowlist-checks the path, so the
        # movie file must live under an allowed root (a fictional D:/x
        # would be rejected before the database is ever consulted)

        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        with tempfile.TemporaryDirectory() as media_tmp:
            movie_path = str(Path(media_tmp) / "detail-movie.mkv")
            Path(movie_path).write_bytes(b"x" * 1000)  # staleness stats it
            file_id = db.upsert_file(
                self.connection,
                db.MovieRecord(
                    path=Path(movie_path),
                    mtime=100.0,
                    size=1000,
                    name="detail-movie",
                    category="Cat",
                ),
            )
            db.add_encode(self.connection, file_id, "VMAF>95", "x265", 1000, 300, 70.0)
            db.add_encode(self.connection, file_id, "VMAF>90", "x265", 1000, 250, 75.0)
            self.connection.commit()

            app_module.state = AppState(Path(self.tmp.name) / "test.db")
            app_module.allow_roots([Path(media_tmp), Path(self.tmp.name)])
            try:
                client = TestClient(fast_app)
                response = client.get("/api/movie", params={"path": movie_path})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["file"]["name"], "detail-movie")
                self.assertEqual(body["file"]["generation"], 1)  # no ancestry
                self.assertEqual(len(body["history"]), 2)  # both encodes, oldest first
                self.assertEqual(body["history"][0]["preset"], "VMAF>95")
                self.assertEqual(body["history"][1]["preset"], "VMAF>90")
                # schema v7 provenance fields ride along (None for plain rows)
                self.assertIn("input_bitrate", body["history"][0])
                self.assertIn("generation", body["history"][0])
                self.assertIn("source_file", body["history"][0])
                self.assertIn("id", body["history"][0])  # each-key, unique per row
                # unknown path inside the allowlist: 404
                missing = client.get(
                    "/api/movie", params={"path": str(Path(self.tmp.name) / "unknown.mkv")}
                )
                self.assertEqual(missing.status_code, 404)

                # ── chain: a gen-2 file must see its parent row ──
                output_path = str(Path(media_tmp) / "detail-movie_t01.mkv")
                db.add_encode(
                    self.connection,
                    file_id,
                    "VMAF>95",
                    "x265",
                    300,
                    150,
                    50.0,
                    source_file=movie_path,
                    output_file=output_path,
                    generation=1,
                )
                child_id = db.upsert_file(
                    self.connection,
                    db.MovieRecord(
                        path=Path(output_path),
                        mtime=200.0,
                        size=150,
                        name="detail-movie_t01",
                        category="Cat",
                    ),
                )
                db.add_encode(
                    self.connection,
                    child_id,
                    "VMAF>95",
                    "x265",
                    150,
                    80,
                    46.7,
                    source_file=output_path,
                    output_file=str(Path(media_tmp) / "detail-movie_t02.mkv"),
                    generation=2,
                )
                self.connection.commit()
                child = client.get("/api/movie", params={"path": output_path})
                self.assertEqual(child.status_code, 200)
                child_body = child.json()
                self.assertEqual(child_body["file"]["generation"], 2)
                # the parent's gen-1 row rides in via output_file linkage
                self.assertEqual(len(child_body["history"]), 2)
                self.assertEqual(child_body["history"][0]["file_path"], movie_path)
                self.assertEqual(child_body["history"][0]["generation"], 1)
                self.assertEqual(child_body["history"][1]["generation"], 2)

                # ── plan: the prediction display's data ──
                # no plan recorded => the key exists, is None
                self.assertIsNone(child_body["plan"])
                db.save_plan(
                    self.connection,
                    file_id,
                    chosen_crf=21,
                    achieved_vmaf=95.47,
                    crop=None,
                    probes=[],
                    skipped_reason=None,
                    estimated_size=450,
                    input_mtime=Path(movie_path).stat().st_mtime,
                    input_size=Path(movie_path).stat().st_size,
                    encoder="x265",
                    encoder_preset="5",
                    target_vmaf=95.0,
                    crf_low=14,
                    crf_high=32,
                    sample_count=3,
                    sample_seconds=10.0,
                )
                self.connection.commit()
                planned = client.get("/api/movie", params={"path": movie_path})
                self.assertEqual(planned.status_code, 200)
                plan_body = planned.json()["plan"]
                self.assertEqual(plan_body["crf"], 21)
                self.assertEqual(plan_body["estimated_size"], 450)
                self.assertIsNone(plan_body["skipped_reason"])
                self.assertFalse(plan_body["stale"])
                self.assertEqual(plan_body["target_vmaf"], 95.0)
                # a changed file flips the staleness flag (mtime+size guard);
                # the file only exists physically from this write on
                Path(movie_path).write_bytes(b"x" * (1000 + 8))
                stale = client.get("/api/movie", params={"path": movie_path})
                self.assertTrue(stale.json()["plan"]["stale"])
                # a skip verdict rides the same shape
                db.save_plan(
                    self.connection,
                    file_id,
                    chosen_crf=14,
                    achieved_vmaf=92.8,
                    crop=None,
                    probes=[],
                    skipped_reason="target unreachable",
                    estimated_size=None,
                    input_mtime=Path(movie_path).stat().st_mtime,
                    input_size=Path(movie_path).stat().st_size,
                    encoder="x265",
                    encoder_preset="5",
                    target_vmaf=95.0,
                    crf_low=14,
                    crf_high=32,
                    sample_count=3,
                    sample_seconds=10.0,
                )
                self.connection.commit()
                skipped = client.get("/api/movie", params={"path": movie_path})
                skip_body = skipped.json()["plan"]
                self.assertEqual(skip_body["skipped_reason"], "target unreachable")
                self.assertIsNone(skip_body["estimated_size"])
            finally:
                app_module.state.shutdown()
                app_module.state = None
                app_module._allowed_roots = []

    def test_movie_detail_estimate_note(self):
        """The HDR estimate bias note (issue #98): /api/movie derives
        estimate_note for an HDR title with a live estimate, and omits it
        for SDR titles and skip verdicts — the estimate's provenance is
        visible wherever the estimate is shown."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.engine.encoders import HDR_ESTIMATE_NOTE
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        with tempfile.TemporaryDirectory() as media_tmp:
            app_module.state = AppState(Path(self.tmp.name) / "test.db")
            app_module.allow_roots([Path(media_tmp), Path(self.tmp.name)])
            try:
                client = TestClient(fast_app)
                for hdr_flag, label in ((1, "hdr-title"), (0, "sdr-title")):
                    movie_path = str(Path(media_tmp) / f"{label}.mkv")
                    Path(movie_path).write_bytes(b"x" * 1000)
                    file_id = db.upsert_file(
                        self.connection,
                        db.MovieRecord(
                            path=Path(movie_path),
                            mtime=100.0,
                            size=1000,
                            name=label,
                            category="Cat",
                            hdr=hdr_flag,
                        ),
                    )
                    db.save_plan(
                        self.connection,
                        file_id,
                        chosen_crf=21,
                        achieved_vmaf=95.0,
                        crop=None,
                        probes=[],
                        skipped_reason=None,
                        estimated_size=450,
                        input_mtime=Path(movie_path).stat().st_mtime,
                        input_size=Path(movie_path).stat().st_size,
                        encoder="svt_av1",
                        encoder_preset="5",
                        target_vmaf=95.0,
                        crf_low=14,
                        crf_high=32,
                        sample_count=3,
                        sample_seconds=10.0,
                    )
                    self.connection.commit()
                    body = client.get("/api/movie", params={"path": movie_path}).json()
                    note = body["plan"]["estimate_note"]
                    if hdr_flag:
                        self.assertEqual(note, HDR_ESTIMATE_NOTE)
                        # the note carries the measured evidence, not a guess
                        self.assertIn("19.1%", note)
                        self.assertIn("78%", note)
                    else:
                        self.assertIsNone(note)
            finally:
                app_module.state.shutdown()
                app_module.state = None
                app_module._allowed_roots = []

    def test_explicit_config_wins_over_db_dir(self):
        """Issue #58's resolution order: an explicit --config file beats the
        db-directory default, and $SHRINKRAY_CONFIG beats the db dir too
        (the container mount point)."""
        import os

        from shrinkray.config import resolve_config_path

        explicit = Path(self.tmp.name) / "explicit.json"
        db_dir = Path(self.tmp.name) / "data"
        db_dir.mkdir()
        self.assertEqual(resolve_config_path(config_file=explicit, db_dir=db_dir), explicit)
        old = os.environ.get("SHRINKRAY_CONFIG")
        os.environ["SHRINKRAY_CONFIG"] = str(Path(self.tmp.name) / "env.json")
        try:
            self.assertEqual(
                resolve_config_path(db_dir=db_dir),
                Path(self.tmp.name) / "env.json",
            )
        finally:
            if old is None:
                os.environ.pop("SHRINKRAY_CONFIG", None)
            else:
                os.environ["SHRINKRAY_CONFIG"] = old
        # and with nothing set, the db dir is the operator's chosen location
        self.assertEqual(resolve_config_path(db_dir=db_dir), db_dir / "config.json")

    def test_no_package_relative_config_lookup(self):
        """Issue #58: config resolution must not depend on the package's
        install location (in a wheel, the dir above site-packages is not a
        user-writable location)."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "shrinkray" / "config.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("__file__", source)

    def test_snapshot_endpoint(self):
        """One request returns status + jobs + library (ETag-aware), and an
        unchanged library comes back as null while the rest stays fresh."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=Path("D:/x/snap.mkv"), mtime=100.0, size=1000, name="snap", category="Cat"
            ),
        )
        db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "x265",
            1000,
            300,
            70.0,
            chosen_crf=22,
            achieved_vmaf=95.4,
        )
        self.connection.commit()

        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            first = client.get("/api/snapshot")
            self.assertEqual(first.status_code, 200)
            body = first.json()
            # status shape (same contract as /api/status)
            self.assertIn("watcher", body["status"])
            self.assertIn("queue", body["status"])
            self.assertIn("database", body["status"])
            # self-monitoring: the app reports its own memory
            self.assertIn("rss_mib", body["status"])
            # issue #323: the snapshot IS the UI's one state channel
            # (App.svelte reads snap.status alone), so its status must carry
            # EVERY key /api/status does - the config-error strip (#247),
            # the pause notice (#250) and the reveal button (#239) were
            # unreachable when these three were missing
            status_only = client.get("/api/status").json()
            for key in status_only:
                self.assertIn(key, body["status"], f"snapshot.status lost /api/status key {key!r}")
            # value parity for the three DERIVED fields (review finding 2,
            # PR #347): both handlers compute them from the same inputs, so
            # equality holds here and pins the handlers against drift.
            # rss_mib is excluded on purpose: it is a live measurement taken
            # milliseconds apart and will legitimately differ.
            for key in ("config_error", "paused", "reveal_available"):
                self.assertEqual(
                    status_only[key],
                    body["status"][key],
                    f"snapshot.status[{key!r}] disagrees with /api/status",
                )
            # the library is fully present on the first call
            self.assertIsInstance(body["library"], list)
            movie = next(m for m in body["library"] if m["name"] == "snap")
            # the encode sub-object carries the schema-v7 drift fields (NULL
            # for legacy rows, present as keys either way - non-breaking for
            # existing UI code that ignores them)
            self.assertIn("generation", movie["encode"])
            self.assertIn("input_bitrate", movie["encode"])
            self.assertIn("output_bitrate", movie["encode"])
            self.assertEqual(movie["encode"]["crf"], 22)
            # job history included
            self.assertIsInstance(body["jobs"], list)

            # second call with both ETags: library AND jobs null, status fresh
            second = client.get(
                "/api/snapshot",
                headers={"If-None-Match": f"{body['library_etag']}, {body['jobs_etag']}"},
            )
            self.assertEqual(second.status_code, 200)
            body2 = second.json()
            self.assertIsNone(body2["library"])
            self.assertIsNone(body2["jobs"])
            self.assertEqual(body2["library_etag"], body["library_etag"])
            self.assertEqual(body2["jobs_etag"], body["jobs_etag"])
            self.assertIn("status", body2)
            # only the status rides every tick
            self.assertIn("queue", body2["status"])

            # a state transition invalidates the jobs tag (a FINISHED job
            # row never mutates — (id, state) is the change signal).
            # Enqueue through the app's own queue object: direct SQL would
            # bypass its invariants.
            from shrinkray.server.jobs import Job, JobKind

            app_module.state.queue.enqueue(Job(kind=JobKind.SCAN, payload={"paths": ["D:/x"]}))
            fourth = client.get(
                "/api/snapshot",
                headers={"If-None-Match": f"{body['library_etag']}, {body['jobs_etag']}"},
            )
            body4 = fourth.json()
            self.assertIsNone(body4["library"])
            self.assertIsInstance(body4["jobs"], list)
            self.assertNotEqual(body4["jobs_etag"], body["jobs_etag"])

            # a RUNNING job's progress must NOT touch the tag anymore:
            # progress is flow on /api/events (SSE), never persisted — the
            # row keeps result = NULL and (id, state) stays a sound signal.
            # Regression for the DB-as-bus era, when every progress write
            # re-sent the whole jobs array mid-encode. (The queued->running
            # transition itself DOES invalidate — a real state change.)
            job = app_module.state.queue.take_next()
            self.assertIsNotNone(job)
            sixth = client.get(
                "/api/snapshot",
                headers={"If-None-Match": f"{body4['library_etag']}, {body4['jobs_etag']}"},
            )
            body6 = sixth.json()
            self.assertIsInstance(body6["jobs"], list)  # transition counted

            # now the pure-progress case: same (id, state), result written
            app_module.state.queue.report_progress(job, {"stage": "scan", "percent": 42})
            fifth = client.get(
                "/api/snapshot",
                headers={"If-None-Match": f"{body6['library_etag']}, {body6['jobs_etag']}"},
            )
            body5 = fifth.json()
            self.assertIsNone(body5["library"])
            self.assertIsNone(body5["jobs"])  # unchanged: progress did not count
            self.assertEqual(body5["jobs_etag"], body6["jobs_etag"])
            # the running row itself carries no live state on the wire
            self.assertTrue(all(j["result"] is None for j in body5["status"]["queue"]))
            # ...but the progress IS live on the bus (seed for SSE subs)
            self.assertEqual(
                app_module.state.bus.latest().get(job.id), {"stage": "scan", "percent": 42}
            )

            # a change re-validates the library in the next snapshot
            db.add_encode(self.connection, file_id, "Fast 1080p30", "x265", 1, 1, 0.0)
            self.connection.commit()
            third = client.get("/api/snapshot", headers={"If-None-Match": body["library_etag"]})
            body3 = third.json()
            self.assertIsInstance(body3["library"], list)
            self.assertNotEqual(body3["library_etag"], body["library_etag"])
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_events_stream_frames_and_unsubscribe(self):
        """The SSE frame generator seeds a hello on connect, streams
        published progress as data frames, pings when idle, and
        unregisters the subscriber when the stream ends (no leak)."""
        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState, _sse_stream

        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            # seed: publish BEFORE any subscriber connects
            app_module.state.queue.enqueue(
                __import__("shrinkray.server.jobs", fromlist=["Job"]).Job(
                    kind=__import__("shrinkray.server.jobs", fromlist=["JobKind"]).JobKind.SCAN,
                    payload={"paths": ["D:/x"]},
                )
            )
            job = app_module.state.queue.take_next()
            app_module.state.queue.report_progress(job, {"stage": "scan", "percent": 7})

            sid, box, seed = app_module.state.bus.subscribe()
            gen = _sse_stream(app_module.state.bus, sid, box, seed, ping_after=0.05)
            frames = [next(gen), next(gen), next(gen)]
            self.assertEqual(frames[0], "retry: 3000\n\n")
            self.assertEqual(frames[1], "event: hello\n")
            self.assertIn('"percent": 7', frames[2])
            # next frame: the ping (empty mailbox, 0.05s wait)
            self.assertEqual(next(gen), ": ping\n\n")
            # publish -> the event flows to THIS subscriber
            app_module.state.queue.report_progress(job, {"stage": "scan", "percent": 42})
            data_frame = next(gen)
            self.assertIn('"percent": 42', data_frame)
            self.assertIn(f'"job_id": {job.id}', data_frame)
            # closing the generator unregisters the subscriber (no leak)
            gen.close()
            self.assertEqual(len(app_module.state.bus._subs), 0)
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_events_stream_ends_when_shutdown_is_requested(self):
        """Issue #431 (review round 6, finding 3): the shutdown branch of the
        SSE generator was unexercised - the default `lambda: False` was the
        only path under test. With the flag set, the next mailbox wait must
        return the flag, not a ping frame, and the subscriber must still be
        unregistered (no leak)."""
        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState, _sse_stream

        app_module.state = AppState(Path(self.tmp.name) / "sse-shutdown.db")
        try:
            sid, box, seed = app_module.state.bus.subscribe()
            gen = _sse_stream(
                app_module.state.bus,
                sid,
                box,
                seed,
                ping_after=0.05,
                shutdown_requested=app_module.state.app_shutdown.is_set,
            )
            self.assertEqual(next(gen), "retry: 3000\n\n")
            self.assertEqual(next(gen), "event: hello\n")
            # iteration 1's loop-top check already ran: consume its frame
            self.assertIn('"jobs"', next(gen))
            # arm the flag: the next loop-top check ends the stream (a wait
            # already parked in the mailbox drains up to ping_after first -
            # bounded by uvicorn's timeout_graceful_shutdown, not zero)
            app_module.state.app_shutdown.set()
            with self.assertRaises(StopIteration):
                next(gen)
            self.assertEqual(len(app_module.state.bus._subs), 0)
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_reveal_rejects_outside_root(self):
        """The reveal endpoint uses the same path allowlist as everything
        else (a file manager window must never open outside the roots)."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        # an explicit real root: the reveal test must not depend on the
        # allowlist a previous test happened to leave behind
        root = Path(self.tmp.name).resolve()
        app_module.allow_roots([root])
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        try:
            client = TestClient(fast_app)
            # a real path outside the allowed root (not a drive-letter
            # literal: on posix that is relative and would reject for the
            # wrong reason - relativity, not containment)
            response = client.post("/api/reveal", json={"path": str(root.parent)})
            self.assertEqual(response.status_code, 403)
        finally:
            app_module.state.shutdown()
            app_module.state = None

    def test_add_skip_and_recognition(self):
        file_id = db.upsert_file(self.connection, self._record("b.mkv"))
        db.add_skip(self.connection, file_id, "no size gain")
        rows = db.get_encodes_for_file(self.connection, file_id)
        self.assertEqual(len(rows), 1)
        self.assertIn("skipped", rows[0]["preset"])
        self.assertIn("no size gain", rows[0]["preset"])
        self.assertEqual(rows[0]["input_size"], 0)

    def test_upsert_and_cache(self):

        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "test.mkv"
            file.write_bytes(b"x")
            stat = file.stat()
            record = db.MovieRecord(
                path=file, mtime=stat.st_mtime, size=stat.st_size, name="test", category="Cat"
            )
            db.upsert_file(self.connection, record)

            row = db.get_file_by_path(self.connection, file)
            self.assertIsNotNone(row)
            self.assertEqual(row["name"], "test")
            # unchanged => valid cache entry
            self.assertTrue(db.is_unchanged(row, file))
            # changed size => invalid
            file.write_bytes(b"more bytes than before")
            self.assertFalse(db.is_unchanged(row, file))

    def test_upsert_updates_existing(self):
        record = self._record("D:/Movies/test.mkv")
        file_id = db.upsert_file(self.connection, record)
        record.name = "updated"
        file_id2 = db.upsert_file(self.connection, record)
        self.assertEqual(file_id, file_id2)
        row = db.get_file_by_path(self.connection, Path("D:/Movies/test.mkv"))
        self.assertEqual(row["name"], "updated")

    def test_compute_hash(self):

        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "a.mkv"
            file.write_bytes(b"A" * (2 * 1024 * 1024))
            hash1 = db.compute_hash(file)
            self.assertEqual(hash1, db.compute_hash(file))
            # different content => different hash
            file.write_bytes(b"B" * (2 * 1024 * 1024))
            self.assertNotEqual(hash1, db.compute_hash(file))

    def test_compute_hash_ignores_interior_bytes(self):
        # pins the documented limit (issue #438): only the first and last MB
        # are hashed and the size is not mixed in, so neither an interior
        # change nor a longer interior is visible
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "a.mkv"
            mib = 1024 * 1024
            file.write_bytes(b"A" * mib + b"X" * mib + b"A" * mib)
            before = db.compute_hash(file)
            file.write_bytes(b"A" * mib + b"Y" * mib + b"A" * mib)
            self.assertEqual(before, db.compute_hash(file))
            file.write_bytes(b"A" * mib + b"Y" * (2 * mib) + b"A" * mib)
            self.assertEqual(before, db.compute_hash(file))

    def test_encode_history(self):
        record = self._record("D:/Movies/test.mkv")
        file_id = db.upsert_file(self.connection, record)
        db.add_encode(self.connection, file_id, "Fast 1080p30", "x265", 1000, 400, 60.0)

        encodes = db.get_encodes_for_file(self.connection, file_id)
        self.assertEqual(len(encodes), 1)
        self.assertEqual(encodes[0]["preset"], "Fast 1080p30")
        self.assertEqual(encodes[0]["saved_percent"], 60.0)

    def test_prune_missing(self):
        # real paths: fictional ones like 'D:/Movies/a.mkv' are absolute on
        # windows but RELATIVE on linux (CI) => scope containment diverges

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            record1 = self._record(str(root / "a.mkv"))
            record2 = self._record(str(root / "b.mkv"))
            id1 = db.upsert_file(self.connection, record1)
            db.upsert_file(self.connection, record2)
            db.add_encode(self.connection, id1, "p", "x265", 1, 1, 0.0)

            # prune with the stored path strings (Path normalizes separators)
            existing = {str(root / "a.mkv")}
            # first scan: b.mkv is flagged, not deleted (grace period)
            result = db.prune_missing(self.connection, existing, scope=str(root))
            self.assertEqual(result.flagged, 1)
            self.assertEqual(result.pruned, 0)
            self.assertEqual(result.missing_now, 1)
            self.assertIsNotNone(db.get_file_by_path(self.connection, root / "b.mkv"))
            self.assertEqual(len(db.get_encodes_for_file(self.connection, id1)), 1)
            row = db.get_file_by_path(self.connection, root / "b.mkv")
            self.assertEqual(row["missing_scans"], 1)
            self.assertIsNotNone(row["missing_since"])

            # grace exhausted: the row (and its encodes) is hard-deleted
            for _ in range(db.MISSING_GRACE - 1):
                result = db.prune_missing(self.connection, existing, scope=str(root))
            self.assertEqual(result.pruned, 1)
            self.assertIsNone(db.get_file_by_path(self.connection, root / "b.mkv"))
            # a.mkv was never missing => its encode history survives
            self.assertEqual(len(db.get_encodes_for_file(self.connection, id1)), 1)

    def test_prune_missing_recovers_reappeared_files(self):

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db.upsert_file(self.connection, self._record(str(root / "a.mkv")))
            # the file disappears => flagged
            result = db.prune_missing(self.connection, set(), scope=str(root))
            self.assertEqual(result.flagged, 1)
            # the file reappears (e.g. folder was moved back) => un-flagged
            result = db.prune_missing(self.connection, {str(root / "a.mkv")}, scope=str(root))
            self.assertEqual(result.recovered, 1)
            self.assertEqual(result.missing_now, 0)
            row = db.get_file_by_path(self.connection, root / "a.mkv")
            self.assertEqual(row["missing_scans"], 0)
            self.assertIsNone(row["missing_since"])

    def test_prune_missing_mass_flagging(self):
        # more than 20% of the scope flagged at once => the accident warning

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ("a", "b", "c", "d", "e", "f"):
                db.upsert_file(self.connection, self._record(str(root / f"{name}.mkv")))
            result = db.prune_missing(self.connection, set(), scope=str(root))
            self.assertEqual(result.flagged, 6)
            self.assertTrue(result.mass)

    def test_upsert_resets_missing_state(self):
        # a file flagged as missing is re-scanned (changed or rescan) =>
        # the upsert clears the missing state

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = str(root / "a.mkv")
            db.upsert_file(self.connection, self._record(path))
            db.prune_missing(self.connection, set(), scope=str(root))
            row = db.get_file_by_path(self.connection, root / "a.mkv")
            self.assertEqual(row["missing_scans"], 1)
            db.upsert_file(self.connection, self._record(path))
            row = db.get_file_by_path(self.connection, root / "a.mkv")
            self.assertEqual(row["missing_scans"], 0)
            self.assertIsNone(row["missing_since"])

    def test_has_output_for_latest_encode(self):

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            file_id = db.upsert_file(self.connection, self._record("c.mkv"))
            # no record at all => not finished
            self.assertFalse(
                db.has_output_for_latest_encode(self.connection, file_id, out / "c.mkv")
            )
            # skip marker (zero output size) => not finished
            db.add_skip(self.connection, file_id, "no size gain")
            self.assertFalse(
                db.has_output_for_latest_encode(self.connection, file_id, out / "c.mkv")
            )
            # real encode recorded but output missing => not finished (must re-encode)
            db.add_encode(self.connection, file_id, "VMAF>95", "x265", 100, 50, 50.0)
            self.assertFalse(
                db.has_output_for_latest_encode(self.connection, file_id, out / "c.mkv")
            )
            # output exists and is non-empty => finished
            (out / "c.mkv").write_bytes(b"encoded data")
            self.assertTrue(
                db.has_output_for_latest_encode(self.connection, file_id, out / "c.mkv")
            )
            # empty output file => not finished
            (out / "c.mkv").write_bytes(b"")
            self.assertFalse(
                db.has_output_for_latest_encode(self.connection, file_id, out / "c.mkv")
            )

    def test_finished_movies_excludes_truncated_output(self):
        """The quality-mode resume set excludes truncated outputs (debris):
        the db says encoded and the file is non-empty, but the duration does
        not verify => re-encode instead of skip-forever."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            out_dir = root / "enc"
            out_dir.mkdir()
            (out_dir / "movie.mkv").write_bytes(b"truncated debris")
            file_id = db.upsert_file(self.connection, self._record(str(movie)))
            db.add_encode(self.connection, file_id, "VMAF>95", "x265", 1000, 500, 50.0)
            with mock.patch("shrinkray.engine.pipeline.durations_match", return_value=False):
                finished = service.finished_movies(self.connection, [movie], out_dir)
            self.assertEqual(finished, set())
            with mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True):
                finished = service.finished_movies(self.connection, [movie], out_dir)
            self.assertEqual(finished, {str(movie)})

    def test_verification_recorded_in_db(self):
        """record_quality_results persists verified_vmaf/subsample on the
        encode row (schema v8 columns)."""

        from shrinkray import service
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            result = QualityResult(
                input=movie,
                output=root / "enc" / "movie.mkv",
                success=True,
                target_vmaf=95.0,
                chosen_crf=22,
                achieved_vmaf=95.4,
                input_size=1000,
                output_size=400,
                verified_vmaf=94.9,
                verified_subsample=10,
            )
            outcome = service.record_quality_results(
                self.connection, movie, result, config_helper.Conf()
            )
            self.assertTrue(outcome.encoded)
            file_id = self.connection.execute(
                "SELECT id FROM files WHERE path = ?", (str(movie),)
            ).fetchone()["id"]
            row = self.connection.execute(
                "SELECT verified_vmaf, verified_at, verified_subsample FROM encodes "
                "WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row["verified_vmaf"], 94.9)
            self.assertEqual(row["verified_subsample"], 10)
            self.assertIsNotNone(row["verified_at"])

    def test_verification_without_data_is_noop(self):
        """A result without verification data must not touch the row (every
        non-verify encode goes through the same record path)."""

        from shrinkray import service
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            result = QualityResult(
                input=movie,
                output=root / "enc" / "movie.mkv",
                success=True,
                input_size=1000,
                output_size=400,
            )
            service.record_quality_results(self.connection, movie, result, config_helper.Conf())
            row = self.connection.execute("SELECT verified_vmaf FROM encodes").fetchone()
            self.assertIsNone(row["verified_vmaf"])

    def test_records_effective_target_vmaf(self):
        """The preset column carries the EFFECTIVE target: for HDR the engine
        overrides the requested target (DEFAULT_HDR_TARGET_VMAF), and the row
        must record the target the encode was actually decided against —
        regression: both callers persisted the caller's value, so every HDR
        encode was recorded as VMAF>95.0 while decided against 94.5."""

        from shrinkray import service
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            hdr_result = QualityResult(
                input=movie,
                output=root / "enc" / "movie.mkv",
                success=True,
                target_vmaf=94.5,  # the engine's HDR override, not the caller's 95.0
                chosen_crf=22,
                achieved_vmaf=94.6,
                input_size=1000,
                output_size=400,
                hdr=True,
                # what the engine carries on an HDR result (the 10-bit switch)
                encoder="x265_10bit",
            )
            outcome = service.record_quality_results(
                self.connection, movie, hdr_result, config_helper.Conf()
            )
            self.assertTrue(outcome.encoded)
            row = self.connection.execute("SELECT preset, encoder FROM encodes").fetchone()
            self.assertEqual(row["preset"], "VMAF>94.5")
            self.assertEqual(row["encoder"], "x265_10bit")

    def test_records_config_encoder_for_non_hdr(self):
        """A non-HDR result records the config encoder (the default unchanged
        — the effective encoder field only overrides for HDR)."""

        from shrinkray import service
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            result = QualityResult(
                input=movie,
                output=root / "enc" / "movie.mkv",
                success=True,
                target_vmaf=95.0,
                chosen_crf=22,
                achieved_vmaf=95.4,
                input_size=1000,
                output_size=400,
                hdr=False,
            )
            outcome = service.record_quality_results(
                self.connection, movie, result, config_helper.Conf()
            )
            self.assertTrue(outcome.encoded)
            row = self.connection.execute("SELECT encoder FROM encodes").fetchone()
            self.assertEqual(row["encoder"], "svt_av1")  # config_helper.Conf() default

    def test_doctor_healthy_library(self):
        """A library whose rows all exist on disk and whose outputs are
        present produces no findings."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            output = root / "enc" / "movie.mkv"
            output.parent.mkdir()
            output.write_bytes(b"out")
            file_id = db.upsert_file(self.connection, self._record(str(movie)))
            db.add_encode(
                self.connection,
                file_id,
                "VMAF>95",
                "x265",
                1000,
                400,
                60.0,
                output_file=str(output),
            )
            findings = service.doctor(self.connection)
            self.assertEqual(findings, [])

    def test_doctor_reports_dead_paths(self):
        """A db row whose file is gone on disk is a finding (with the
        soft-missing counter as detail)."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "gone.mkv"  # never created
            db.upsert_file(self.connection, self._record(str(movie)))
            self.connection.execute(
                "UPDATE files SET missing_scans = 3 WHERE path = ?", (str(movie),)
            )
            self.connection.commit()
            findings = service.doctor(self.connection)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].issue, "dead path")
            self.assertIn("missing_scans=3", findings[0].detail or "")

    def test_doctor_reports_stale_scratch_workdirs(self):
        """Issue #440: crash debris in the configured scratch (stale probe
        workdirs) is a finding with its byte weight and age - and NOTHING
        is deleted (report-only, invariant 3).
        """

        from shrinkray import service
        from shrinkray.engine.identity import probe_scratch_prefix

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            # a healthy library (no db-backed findings)
            output = root / "enc" / "movie.mkv"
            output.parent.mkdir()
            output.write_bytes(b"out")
            file_id = db.upsert_file(self.connection, self._record(str(movie)))
            db.add_encode(
                self.connection,
                file_id,
                "VMAF>95",
                "x265",
                1000,
                400,
                60.0,
                output_file=str(output),
            )
            # the crash debris: killed runs' workdirs on the configured
            # scratch, with content. The names derive from the GRAMMAR OWNER
            # (identity.probe_scratch_prefix) - no literal re-spelled here -
            # plus the two older grammars: the anonymous pre-#435 name and
            # the verification workdir's own prefix.
            scratch = root / "scratch"
            scratch.mkdir()
            workdir = scratch / (probe_scratch_prefix(movie) + "x7y8z9a1")
            workdir.mkdir()
            (workdir / "sample_0.mkv").write_bytes(b"m" * 128)
            old_debris = scratch / "metadata_quality_deadbeef"
            old_debris.mkdir()
            (old_debris / "vmaf.json").write_bytes(b"{}")
            verify_debris = scratch / (probe_scratch_prefix(movie, kind="verify") + "x7y8z9a1")
            verify_debris.mkdir()
            (verify_debris / "vmaf.json").write_bytes(b"{}")
            # the debris must be STALE: age every workdir past the floor
            # (a fresh workdir is a LIVE probe's, never reported)
            # probe debris: past the 1 h probe floor. verify debris: past
            # its own 6 h floor (its floor differs - the measurement is
            # silent for its whole run)
            probe_old = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            verify_old = time.time() - 2 * service.SCRATCH_VERIFY_STALE_SECONDS
            for debris, old in (
                (workdir, probe_old),
                (old_debris, probe_old),
                (verify_debris, verify_old),
            ):
                os.utime(debris, (old, old))
                for child in debris.iterdir():
                    os.utime(child, (old, old))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                issues = [f.issue for f in findings]
                self.assertEqual(issues.count("stale probe scratch (crash debris?)"), 3)
                for finding in findings:
                    if finding.issue == "stale probe scratch (crash debris?)":
                        self.assertRegex(finding.detail or "", r"[0-9]+ bytes")
                        self.assertRegex(finding.detail or "", r"[0-9.]+ h ago")
                        self.assertGreaterEqual(
                            float((finding.detail or "0 h").split(" h")[0].split(", mtime ")[-1]),
                            1.0,
                        )
                # report-only: the debris and its content still exist after
                # the run (inside the fixture - the tempdir cleanup would
                # otherwise be the thing deleting them)
                self.assertTrue(workdir.is_dir())
                self.assertTrue((workdir / "sample_0.mkv").exists())
                self.assertTrue(old_debris.is_dir())
                self.assertTrue(verify_debris.is_dir())

    def test_doctor_ignores_a_live_probes_workdir(self):
        """A workdir younger than the staleness floor is a running probe's,
        not debris: it is never reported (reporting it would exit 1 on a
        healthy host mid-encode, issue #440's review finding 2)."""

        from shrinkray import service
        from shrinkray.engine.identity import probe_scratch_prefix

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            live = scratch / (probe_scratch_prefix(movie) + "x7y8z9a1")
            live.mkdir()
            (live / "sample_0.mkv").write_bytes(b"m" * 128)
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                self.assertEqual(
                    [f for f in findings if f.issue == "stale probe scratch (crash debris?)"],
                    [],
                )
                # report-only (inside the fixture: the tempdir cleanup is the
                # only thing that may remove it)
                self.assertTrue(live.is_dir(), "a live workdir is untouched")

    def test_doctor_ages_on_the_newest_member_not_the_directory(self):
        """A verification workdir writes its one child up front and nothing
        during a long measurement: a workdir whose own mtime has aged but
        whose newest member is fresh is a LIVE run, never debris (issue
        #440's review round 2)."""

        from shrinkray import service
        from shrinkray.engine.identity import probe_scratch_prefix

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            live = scratch / (probe_scratch_prefix(movie) + "x7y8z9a1")
            live.mkdir()
            fresh_child = live / "vmaf.json"
            fresh_child.write_bytes(b"{}")
            aged = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(live, (aged, aged))  # the DIRECTORY aged; the child did not
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                self.assertEqual(
                    [f for f in findings if f.issue == "stale probe scratch (crash debris?)"],
                    [],
                )
                self.assertTrue(fresh_child.exists())

    def test_doctor_never_walks_out_of_the_workdir_through_a_symlink(self):
        """A symlinked member inside a stale workdir contributes neither a
        walk outside the workdir nor its target's contents to the report:
        the guard keeps the contain-me guarantee explicit for this
        report-only check (rglob itself yields the link without descending
        on every Python this project supports - measured 3.10-3.15)."""

        from shrinkray import service
        from shrinkray.engine.identity import probe_scratch_prefix

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_bytes(b"do not read" * 10)
            debris = scratch / (probe_scratch_prefix(movie) + "x7y8z9a1")
            debris.mkdir()
            (debris / "sample_0.mkv").write_bytes(b"m" * 16)
            os.symlink(outside, debris / "linked")
            old = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(debris, (old, old))
            os.utime(debris / "sample_0.mkv", (old, old))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                debris_findings = [
                    f for f in findings if f.issue == "stale probe scratch (crash debris?)"
                ]
                # exactly one finding, and its byte weight counts ONLY the
                # workdir's real member (16 bytes) - the link's target
                # (110 bytes outside) is not in it
                self.assertEqual(len(debris_findings), 1)
                self.assertIn("16 bytes", debris_findings[0].detail or "")
                self.assertTrue((outside / "secret.txt").exists())

    def test_a_running_verification_is_not_debris_despite_its_silence(self):
        """A verification workdir writes nothing during the measurement (the
        model goes in up front, vmaf.json at the end), so its floor is 6 h:
        a workdir older than the probe floor but younger than the verify
        floor is a RUNNING verification, never debris (round-3 finding 1).
        """

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            from shrinkray.engine.identity import probe_scratch_prefix

            live_verify = scratch / (probe_scratch_prefix(movie, kind="verify") + "x7y8z9a1")
            live_verify.mkdir()
            (live_verify / "vmaf_model.json").write_bytes(b"{}")
            # older than the probe floor, younger than the verify floor
            aged = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(live_verify, (aged, aged))
            os.utime(live_verify / "vmaf_model.json", (aged, aged))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                self.assertEqual(
                    [f for f in findings if f.issue == "stale probe scratch (crash debris?)"],
                    [],
                )
                self.assertTrue(live_verify.is_dir())

    def test_the_verify_site_passes_its_kind_to_the_grammar_owner(self):
        """The producer pin: the pipeline's verification workdir must ask the
        grammar owner for the verify kind - deleting the argument keeps every
        behavioral test green (their fixtures pass it themselves) while the
        6 h floor silently stops applying to real runs (round-3's defect,
        closed only when THIS pin exists)."""

        import inspect

        from shrinkray.engine import pipeline as pipeline_mod

        source = inspect.getsource(pipeline_mod)
        # the verify site's call carries the kind argument
        self.assertIn('probe_scratch_prefix(input_file, kind="verify")', source)
        # ...exactly once (the probe sites pass no kind)
        self.assertEqual(source.count('kind="verify"'), 1)

    def test_the_historical_verify_grammar_keeps_its_floor(self):
        """The short-lived shrinkray_verify_ name (#435's first shape) still
        carries the verification floor: existing debris from an upgraded
        install stays floored like the current grammar's."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            historical = scratch / "shrinkray_verify_0b130ea"
            historical.mkdir()
            (historical / "vmaf.json").write_bytes(b"{}")
            aged = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(historical, (aged, aged))
            os.utime(historical / "vmaf.json", (aged, aged))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                self.assertEqual(
                    [f for f in findings if f.issue == "stale probe scratch (crash debris?)"],
                    [],
                )

    def test_the_verify_slot_is_anchored_at_the_grammar_tail(self):
        """The marker is the grammar's LAST slot before mkdtemp's suffix: a
        name whose stem ends '-<8hex>-verify-<something>' (an adversarial
        movie name, or a probe's name carrying a verify-shaped segment) must
        NOT read as a verify workdir - the tail anchor is what closes the
        slot (round-7 finding 1, measured)."""

        from shrinkray import service
        from shrinkray.engine.identity import probe_scratch_prefix

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            # the adversarial movie: its stem ends like a verify name
            movie = root / "film-1a2b3c4d-verify-cut.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            debris = scratch / (probe_scratch_prefix(movie) + "x7y8z9a1")
            debris.mkdir()
            (debris / "sample_0.mkv").write_bytes(b"m" * 16)
            aged = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(debris, (aged, aged))
            os.utime(debris / "sample_0.mkv", (aged, aged))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                debris_findings = [
                    f for f in findings if f.issue == "stale probe scratch (crash debris?)"
                ]
                self.assertEqual(len(debris_findings), 1, "probe debris reports at 1 h")
        # and the anchored regex rejects the truncated adversarial shape
        # directly (no mkdtemp suffix after the marker)
        self.assertIsNone(
            service._VERIFY_NAME_RE.match("shrinkray-probe-a-1a2b3c4d-verify-9f8e7d6c-extra")
        )
        self.assertIsNone(
            service._VERIFY_NAME_RE.match(
                "shrinkray-probe-film-1a2b3c4d-verify-cut-0f0e0d0c-x7y8z9a1"
            )
        )
        self.assertIsNotNone(
            service._VERIFY_NAME_RE.match("shrinkray-probe-movie-1a2b3c4d-verify-x7y8z9a1")
        )

    def test_a_stem_containing_verify_keeps_the_probe_floor(self):
        """The marker is the KIND slot of the grammar, anchored: a movie
        whose STEM contains '-verify-' is a probe workdir and gets the
        probe floor - its debris reports after 1 h, not 6 h (round-5
        finding 3, measured defect)."""

        from shrinkray import service
        from shrinkray.engine.identity import probe_scratch_prefix

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "my-verify-movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            debris = scratch / (probe_scratch_prefix(movie) + "x7y8z9a1")
            debris.mkdir()
            (debris / "sample_0.mkv").write_bytes(b"m" * 16)
            aged = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(debris, (aged, aged))
            os.utime(debris / "sample_0.mkv", (aged, aged))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                debris_findings = [
                    f for f in findings if f.issue == "stale probe scratch (crash debris?)"
                ]
                self.assertEqual(len(debris_findings), 1, "probe debris reports at 1 h")
                self.assertRegex(debris_findings[0].detail or "", r"[0-9.]+ h ago")

    def test_a_set_but_missing_scratch_variable_falls_through(self):
        """A SET tempfile variable naming a missing dir falls through to the
        next variable (tempfile's own shape); none usable means the check is
        skipped - never a silent nothing-report on a real scratch elsewhere
        (round-8 finding 5)."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            real_scratch = root / "real-scratch"
            real_scratch.mkdir()
            debris = real_scratch / "shrinkray-probe-movie-1a2b3c4d-x7y8z9a1"
            debris.mkdir()
            (debris / "sample_0.mkv").write_bytes(b"m" * 16)
            aged = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            os.utime(debris, (aged, aged))
            os.utime(debris / "sample_0.mkv", (aged, aged))
            env = {k: v for k, v in os.environ.items() if k not in ("TMPDIR", "TEMP", "TMP")}
            env["TMPDIR"] = str(root / "does-not-exist")  # set, unusable
            env["TEMP"] = str(real_scratch)  # usable - must win
            with patch.dict(os.environ, env, clear=True):
                findings = service.doctor(self.connection)
                debris_findings = [
                    f for f in findings if f.issue == "stale probe scratch (crash debris?)"
                ]
                self.assertEqual(len(debris_findings), 1, "the usable variable's scratch is read")

    def test_doctor_scratch_check_skips_without_a_configured_root(self):
        """A stateless CLI run has no configured scratch (no TMPDIR): the
        check is skipped, never a walk of the OS temp (issue #440)."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            env = {k: v for k, v in os.environ.items() if k not in ("TMPDIR", "TEMP", "TMP")}
            with patch.dict(os.environ, env, clear=True):
                findings = service.doctor(self.connection)
        self.assertEqual(findings, [])

    def test_doctor_scratch_check_ignores_unrelated_entries(self):
        """Only scratch-workdir grammars are findings; other scratch
        content (a user's own files) is never listed."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(self.connection, self._record(str(movie)))
            scratch = root / "scratch"
            scratch.mkdir()
            (scratch / "my-own-notes").mkdir()
            from shrinkray.engine.identity import probe_scratch_prefix

            probe_debris = scratch / "shrinkray-probe-x-abcdef12-x7y8z9a1"
            probe_debris.mkdir()
            verify_debris = scratch / (probe_scratch_prefix(movie, kind="verify") + "x7y8z9a1")
            verify_debris.mkdir()
            (scratch / "metadata_quality_1").mkdir()
            (scratch / "not_a_dir_shrinkray-probe-y").write_bytes(b"f")
            # age the workdirs past THEIR floors (a fresh one is a LIVE
            # run's workdir, correctly not reported; the verify floor is 6 h)
            probe_old = time.time() - 2 * service.SCRATCH_STALE_SECONDS
            verify_old = time.time() - 2 * service.SCRATCH_VERIFY_STALE_SECONDS
            for debris_dir, old in (
                (probe_debris, probe_old),
                (verify_debris, verify_old),
                (scratch / "metadata_quality_1", probe_old),
            ):
                os.utime(debris_dir, (old, old))
                for child in debris_dir.iterdir():
                    os.utime(child, (old, old))
            with patch.dict(os.environ, {"TMPDIR": str(scratch)}):
                findings = service.doctor(self.connection)
                debris = [f for f in findings if f.issue == "stale probe scratch (crash debris?)"]
                self.assertEqual(len(debris), 3)
                self.assertTrue(all("my-own-notes" not in f.movie for f in debris))
                self.assertTrue(all("not_a_dir" not in f.movie for f in debris))
                # the non-directory prefix match is not listed...
                self.assertFalse(
                    any(f.movie.endswith("not_a_dir_shrinkray-probe-y") for f in debris)
                )
                # ...and still exists (nothing was touched)
                self.assertTrue((scratch / "not_a_dir_shrinkray-probe-y").exists())

    def test_doctor_reports_vanished_outputs(self):
        """An encode row whose output_file no longer exists is a finding."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            file_id = db.upsert_file(self.connection, self._record(str(movie)))
            db.add_encode(
                self.connection,
                file_id,
                "VMAF>95",
                "x265",
                1000,
                400,
                60.0,
                output_file=str(root / "enc" / "deleted.mkv"),
            )
            findings = service.doctor(self.connection)
            issues = [f.issue for f in findings]
            self.assertIn("encode output vanished", issues)

    def test_doctor_probe_all_detects_corruption(self):
        """--probe-all ffprobes every row: a file that exists but does not
        probe is reported as unprobeable; a probeable one is silent."""

        from shrinkray import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            junk = root / "junk.mkv"
            junk.write_bytes(b"not a movie")
            db.upsert_file(self.connection, self._record(str(junk)))
            with mock.patch("shrinkray.service.probe_duration", return_value=None):
                findings = service.doctor(self.connection, probe_all=True)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].issue, "unprobeable (corrupt/truncated?)")

    def test_prune_missing_scoped(self):
        # a scan of one root must not wipe the history of files outside it
        # (real tmp paths: fictional drives are relative paths on linux/CI)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "library"
            other = Path(tmp).resolve() / "elsewhere"
            root.mkdir()
            other.mkdir()
            id_inside = db.upsert_file(self.connection, self._record(str(root / "a.mkv")))
            db.add_encode(self.connection, id_inside, "p", "x265", 1, 1, 0.0)
            id_outside = db.upsert_file(self.connection, self._record(str(other / "b.mkv")))
            db.add_encode(self.connection, id_outside, "p", "x265", 2, 1, 50.0)

            # scan of root finds only a.mkv, scope limits pruning to that root
            existing = {str(root / "a.mkv")}
            result = db.prune_missing(self.connection, existing, scope=str(root))
            self.assertEqual(result.pruned, 0)  # b.mkv is outside the scope => kept
            self.assertEqual(result.flagged, 0)  # ...and outside => not even flagged
            self.assertIsNotNone(db.get_file_by_path(self.connection, other / "b.mkv"))
            self.assertEqual(len(db.get_encodes_for_file(self.connection, id_outside)), 1)

            # a.mkv is inside the scope and on disk => kept as well
            self.assertIsNotNone(db.get_file_by_path(self.connection, root / "a.mkv"))

            # a file inside the scope that vanished is flagged first, then pruned
            db.upsert_file(self.connection, self._record(str(root / "gone.mkv")))
            result = db.prune_missing(self.connection, existing, scope=str(root))
            self.assertEqual(result.flagged, 1)
            for _ in range(db.MISSING_GRACE - 1):
                result = db.prune_missing(self.connection, existing, scope=str(root))
            self.assertEqual(result.pruned, 1)
            self.assertIsNone(db.get_file_by_path(self.connection, root / "gone.mkv"))


class TestInPlaceAuditTrail(unittest.TestCase):
    """An in-place encode row must describe the artifact's real location
    (issue #232): the row was written with the per-run staging path, which
    the run deletes on exit - so `doctor` reported every in-place encode as
    vanished, the generation chain never linked, and a batch never resumed.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"x" * 8)
        self.connection = db.connect(self.root / "meta.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _result(self, output: Path) -> "object":
        from shrinkray.engine.plan import QualityResult

        return QualityResult(
            input=self.movie,
            output=output,
            success=True,
            target_vmaf=96.0,
            chosen_crf=21,
            achieved_vmaf=95.5,
            input_size=800,
            output_size=400,
        )

    def test_the_row_records_the_artifacts_real_location(self) -> None:
        """In place, the row's output_file is the source's own path."""
        from shrinkray import service

        service.record_quality_results(
            self.connection,
            self.movie,
            self._result(self.root / ".shrinkray-x/movie.mkv"),
            config_helper.Conf(),
            final_output=self.movie,
        )
        row = self.connection.execute(
            "SELECT output_file FROM encodes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["output_file"], str(self.movie))

    def test_the_generation_chain_links_an_in_place_reencode(self) -> None:
        """A second in-place encode of the same path is generation 2."""
        from shrinkray import service

        service.record_quality_results(
            self.connection,
            self.movie,
            self._result(self.root / ".shrinkray-x/movie.mkv"),
            config_helper.Conf(),
            final_output=self.movie,
        )
        second = service.record_quality_results(
            self.connection,
            self.movie,
            self._result(self.root / ".shrinkray-y/movie.mkv"),
            config_helper.Conf(),
            final_output=self.movie,
        )
        row = self.connection.execute(
            "SELECT generation FROM encodes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["generation"], 2)
        self.assertTrue(second.encoded)

    def test_a_declined_swap_clears_the_row(self) -> None:
        """A swap that is refused leaves the row recording 'discarded'.

        The encode lives in a staging directory the run deletes on exit; a
        row pointing at it is the false 'encode output vanished' finding.
        """
        from shrinkray import service

        encode_id = service.save_encode(
            self.connection,
            self.movie,
            "VMAF>96",
            "svt_av1",
            800,
            400,
            output_file=str(self.movie),
        )
        db.set_encode_output(self.connection, encode_id, None)
        row = self.connection.execute(
            "SELECT output_file FROM encodes WHERE id = ?", (encode_id,)
        ).fetchone()
        self.assertIsNone(row["output_file"])


if __name__ == "__main__":
    unittest.main()
