from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

SCHEMA_VERSION = 19

if TYPE_CHECKING:
    # engine is ABOVE db in the layer contract; the summary type arrives as
    # an annotation only (a runtime import here would be an upward edge)
    from shrinkray.engine.crop import CropSummary

# how many consecutive scans a file may be missing before its row (and its
# encode history) is hard-deleted; the grace period also protects against
# accidental mass deletions (e.g. a renamed media folder)
MISSING_GRACE = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    hash TEXT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    duration TEXT,
    duration_raw REAL,
    fps REAL,
    codec TEXT,
    bitrate REAL,
    aspect_ratio TEXT,
    width INTEGER,
    height INTEGER,
    crop INTEGER,
    hdr INTEGER,
    other_codecs TEXT,
    audio_languages TEXT,
    missing_scans INTEGER NOT NULL DEFAULT 0,
    missing_since TEXT,
    scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);

CREATE TABLE IF NOT EXISTS encodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    preset TEXT NOT NULL,
    encoder TEXT NOT NULL,
    input_size INTEGER NOT NULL,
    output_size INTEGER NOT NULL,
    saved_percent REAL NOT NULL,
    chosen_crf INTEGER,
    achieved_vmaf REAL,
    encoder_preset TEXT,
    probe_encoder_preset TEXT,
    encode_seconds REAL,
    probe_count INTEGER,
    probe_seconds REAL,
    hdr INTEGER,
    source_width INTEGER,
    source_height INTEGER,
    source_codec TEXT,
    source_bit_depth INTEGER,
    source_duration REAL,
    input_bitrate REAL,
    output_bitrate REAL,
    source_file TEXT,
    output_file TEXT,
    generation INTEGER NOT NULL DEFAULT 1,
    tool_versions TEXT,
    verified_vmaf REAL,
    verified_at TEXT,
    verified_subsample INTEGER,
    crf_source TEXT,
    plan_created_at TEXT,
    encoded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plans (
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
    crop_sample_count INTEGER NOT NULL DEFAULT 3,
    vmaf_statistic TEXT NOT NULL DEFAULT 'min',
    vmaf_recipe TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_plans_file ON plans(file_id);

CREATE TABLE IF NOT EXISTS crops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    bars TEXT,
    frame_width INTEGER NOT NULL,
    frame_height INTEGER NOT NULL,
    readings INTEGER NOT NULL,
    sustained_readings INTEGER NOT NULL,
    ratios TEXT NOT NULL DEFAULT '{}',
    input_mtime REAL NOT NULL,
    input_size INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_crops_file ON crops(file_id);

CREATE INDEX IF NOT EXISTS idx_encodes_file ON encodes(file_id);

CREATE TABLE IF NOT EXISTS retained (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    retained_path TEXT NOT NULL,
    encode_id INTEGER REFERENCES encodes(id) ON DELETE SET NULL,
    size_before INTEGER NOT NULL,
    size_after INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_retained_file ON retained(file_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS probe_cache (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    encoded_size INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# migration v1 -> v2: record quality-mode decisions (chosen CRF, measured
# VMAF) and skipped movies so subsequent runs do not re-probe them
# migration v2 -> v3: stored paths must be absolute (relative paths created
# phantom duplicates when runs happened from different working directories)
def job_table_migration(connection: sqlite3.Connection) -> None:
    """Migration v3 -> v4: create the jobs table for the resident app."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            state TEXT NOT NULL,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            finished_at TEXT
        );
        """
    )


def plans_table_migration(connection: sqlite3.Connection) -> None:
    """Migration v8 -> v9: create the plans table (plan-result reuse)."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS plans (
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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_plans_file ON plans(file_id);
        """
    )


def probe_cache_table_migration(connection: sqlite3.Connection) -> None:
    """Migration v9 -> v10: create the probe-encode cache table."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS probe_cache (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )


MIGRATIONS = {
    2: [
        "ALTER TABLE encodes ADD COLUMN chosen_crf INTEGER",
        "ALTER TABLE encodes ADD COLUMN achieved_vmaf REAL",
    ],
    3: ["__absolute_paths__"],  # handled specially in _migrate (needs Path)
    4: ["__job_table__"],  # handled specially in _migrate (creates a table)
    5: [
        # benchmark-grade encode records (see TODO: library-wide data runs)
        "ALTER TABLE encodes ADD COLUMN encoder_preset TEXT",
        "ALTER TABLE encodes ADD COLUMN encode_seconds REAL",
        "ALTER TABLE encodes ADD COLUMN probe_count INTEGER",
        "ALTER TABLE encodes ADD COLUMN probe_seconds REAL",
        "ALTER TABLE encodes ADD COLUMN hdr INTEGER",
        "ALTER TABLE encodes ADD COLUMN source_width INTEGER",
        "ALTER TABLE encodes ADD COLUMN source_height INTEGER",
        "ALTER TABLE encodes ADD COLUMN source_codec TEXT",
        "ALTER TABLE encodes ADD COLUMN source_duration REAL",
        "ALTER TABLE encodes ADD COLUMN tool_versions TEXT",
    ],
    6: [
        # soft-missing: deleted files are flagged first (history retained),
        # hard-deleted only after MISSING_GRACE consecutive scans
        "ALTER TABLE files ADD COLUMN missing_scans INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE files ADD COLUMN missing_since TEXT",
    ],
    7: [
        # generation tracking: encodes rows chained via source_file/output_file
        # so the drift across re-encode generations is documented per title
        "ALTER TABLE encodes ADD COLUMN input_bitrate REAL",
        "ALTER TABLE encodes ADD COLUMN output_bitrate REAL",
        "ALTER TABLE encodes ADD COLUMN source_file TEXT",
        "ALTER TABLE encodes ADD COLUMN output_file TEXT",
        "ALTER TABLE encodes ADD COLUMN generation INTEGER NOT NULL DEFAULT 1",
    ],
    8: [
        # full-file VMAF verification (opt-in --verify): the ground truth
        # companion to the sample-based achieved_vmaf. subsample records the
        # ffmpeg n_subsample divisor (NULL = full-file, every frame measured)
        "ALTER TABLE encodes ADD COLUMN verified_vmaf REAL",
        "ALTER TABLE encodes ADD COLUMN verified_at TEXT",
        "ALTER TABLE encodes ADD COLUMN verified_subsample INTEGER",
    ],
    9: [
        # plan-result reuse ("quicksave"): plan_quality and the encode's
        # probe phase are the same computation - a valid plan lets the
        # encode skip probing entirely. The plans table stores one row per
        # file (upsert); crf_source/plan_created_at record whether an
        # encode's CRF decision was probed now or trusted from a plan.
        "__plans_table__",
        "ALTER TABLE encodes ADD COLUMN crf_source TEXT",
        "ALTER TABLE encodes ADD COLUMN plan_created_at TEXT",
    ],
    10: [
        # probe-encode cache (issue #24): individual probe measurements are
        # content-addressed (source identity, sample window, encoder settings,
        # crop, HDR measurement mode, tool versions), so a changed target
        # VMAF reuses the probes already measured and a re-run of the same
        # plan does not re-encode them. force_reprobe bypasses this cache.
        "__probe_cache_table__",
    ],
    11: [
        # two encoder presets (issue #22): the probe preset (a measurement
        # setting) and the final preset (an output setting) are recorded
        # separately, so a drift investigation can tell which number
        # produced which measurement. plans.encoder_preset keeps holding
        # the PROBE preset (a plan is a probe result; its fingerprint keys
        # on the probe preset only).
        "ALTER TABLE encodes ADD COLUMN probe_encoder_preset TEXT",
    ],
    12: [
        # the crop sample count is part of the decision: the crop is
        # monotone non-increasing in the sample count once the bars are the
        # ones common to every sample (issue #17), so a plan made with three
        # samples must not be reused by a run that samples ten
        "ALTER TABLE plans ADD COLUMN crop_sample_count INTEGER NOT NULL DEFAULT 3",
    ],
    13: [
        # the decision statistic is part of the fingerprint (issue #15):
        # a plan recorded under the min aggregation was a different
        # decision, so it must not be reused as if it were a mean one.
        # Existing rows default to 'min', which is what they measured.
        "ALTER TABLE plans ADD COLUMN vmaf_statistic TEXT NOT NULL DEFAULT 'min'",
    ],
    14: [
        # the retention ledger (issue #35): a source is never deleted, so a
        # swapped original is renamed aside and recorded here. The row is what
        # the operator reads to reclaim space manually; ShrinkRay itself never
        # deletes the retained file.
        """
        CREATE TABLE IF NOT EXISTS retained (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
            source_path TEXT NOT NULL,
            retained_path TEXT NOT NULL,
            encode_id INTEGER REFERENCES encodes(id) ON DELETE SET NULL,
            size_before INTEGER NOT NULL,
            size_after INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_retained_file ON retained(file_id)",
    ],
    15: [
        # bar removal is part of every re-encode, so remove_bars stopped being
        # a decision input (issue #128). A plan stored while the flag existed
        # was probed WITHOUT a crop when the flag was off, so reusing it would
        # encode the film with its bars kept - the exact thing the rule forbids.
        # Rebuilding the table drops every such row: the plans are re-probed on
        # the next run, which is the correct answer, not a guess.
        """
        CREATE TABLE plans_v15 (
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
            crop_sample_count INTEGER NOT NULL DEFAULT 3,
            vmaf_statistic TEXT NOT NULL DEFAULT 'min',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "DROP TABLE plans",
        "ALTER TABLE plans_v15 RENAME TO plans",
        "CREATE INDEX IF NOT EXISTS idx_plans_file ON plans(file_id)",
    ],
    16: [
        # the timeline crop detector's cache (issue #181): one keyframe-only
        # cropdetect pass per file VERSION, read by plan/encode and projected
        # into the scan's flag when present. A fresh table, no rewrites.
        """
        CREATE TABLE IF NOT EXISTS crops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            bars TEXT,
            frame_width INTEGER NOT NULL,
            frame_height INTEGER NOT NULL,
            readings INTEGER NOT NULL,
            sustained_readings INTEGER NOT NULL,
            ratios TEXT NOT NULL DEFAULT '{}',
            input_mtime REAL NOT NULL,
            input_size INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_crops_file ON crops(file_id)",
    ],
    17: [
        # the encoder choice follows the source's bit depth (issue #234):
        # the record snapshots what the decision was made from. A nullable
        # ADD COLUMN, no rewrites.
        "ALTER TABLE encodes ADD COLUMN source_bit_depth INTEGER",
    ],
    18: [
        # the probe cache records the encoded bytes next to the score (issue #314):
        # the chosen-CRF size decision reads the sizes the VMAF probes already
        # produced instead of re-encoding one window. Old rows keep NULL => a
        # size read misses rather than misleads.
        "ALTER TABLE probe_cache ADD COLUMN encoded_size INTEGER",
    ],
    19: [
        # the measurement recipe is part of the plan fingerprint (issue #308):
        # a plan measured with another model, scale, pixel format or tone-map is a
        # different measurement, so it must not be reused as if it were this one.
        # Old rows default to '' - a recipe the current code never produces, so
        # every pre-#308 plan misses and is re-probed. That is the correct
        # answer after a measurement change, not a guess.
        "ALTER TABLE plans ADD COLUMN vmaf_recipe TEXT NOT NULL DEFAULT ''",
    ],
}


def _migrate(connection: sqlite3.Connection) -> None:
    """Applies pending schema migrations and records the new version.

    Args:
        connection: The database connection.
    """
    stored = connection.execute("SELECT version FROM schema_version").fetchone()
    if stored is None:
        return
    current = int(stored[0])
    while current < SCHEMA_VERSION:
        current += 1
        for statement in MIGRATIONS.get(current, []):
            if statement == "__absolute_paths__":
                # rewrite relative file paths to absolute ones; a relative row
                # whose absolute twin already exists is merged into it (the
                # absolute row wins, the relative one is removed)
                rows = connection.execute("SELECT id, path FROM files").fetchall()
                for row in rows:
                    path = Path(row["path"])
                    if path.is_absolute():
                        continue
                    absolute = path.resolve()
                    twin = connection.execute(
                        "SELECT id FROM files WHERE path = ?", (str(absolute),)
                    ).fetchone()
                    if twin is not None:
                        # move encodes to the twin, then drop the relative row
                        connection.execute(
                            "UPDATE encodes SET file_id = ? WHERE file_id = ?",
                            (twin["id"], row["id"]),
                        )
                        connection.execute("DELETE FROM files WHERE id = ?", (row["id"],))
                    elif absolute.is_file():
                        connection.execute(
                            "UPDATE files SET path = ? WHERE id = ?",
                            (str(absolute), row["id"]),
                        )
                    else:
                        connection.execute("DELETE FROM files WHERE id = ?", (row["id"],))
            elif statement == "__job_table__":
                job_table_migration(connection)
            elif statement == "__plans_table__":
                plans_table_migration(connection)
            elif statement == "__probe_cache_table__":
                probe_cache_table_migration(connection)
            elif statement.startswith("ALTER TABLE") and " ADD COLUMN " in statement:
                # a column added by the current schema already exists on a
                # database created by this version: adding it twice is not
                # an error worth failing the open over
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
                        raise
            else:
                connection.execute(statement)
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (current,))
    connection.commit()


def compute_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Computes a fast hash of the first and last MB of a file.

    This is not a checksum of the whole file: above two chunks
    (``chunk_size``, 1 MB by default) only the first and last chunk are
    hashed, and the size is not mixed in, so two files that agree at
    both ends hash alike whatever lies between them. File identity stays
    size + mtime; a same-size, mtime-preserved byte swap is an accepted
    residual risk (issue #438).

    Args:
        path (Path): The file to hash.
        chunk_size (int): The size of the chunks to read.

    Returns:
        str: The hash as hex string.
    """
    hasher = hashlib.sha256()
    size = path.stat().st_size
    with open(path, "rb") as file:
        hasher.update(file.read(min(chunk_size, size)))
        if size > chunk_size:
            file.seek(max(0, size - chunk_size))
            hasher.update(file.read(chunk_size))
    return hasher.hexdigest()


@dataclass
class MovieRecord:
    """A movie record as stored in the database.

    Attributes:
        path (Path): Path of the movie file.
        mtime (float): Modification time as unix timestamp.
        size (int): Size in bytes.
        hash (str | None): Fast content hash (first/last MB), if computed.
        name (str): Movie name.
        category (str): Category name.
        duration (str | None): Duration as 'HH:MM:SS' string.
        duration_raw (float | None): Duration in seconds.
        fps (float | None): Frames per second.
        codec (str | None): Video codec name.
        bitrate (float | None): Bitrate in MBit/s.
        aspect_ratio (str | None): Aspect ratio as 'WxH' string.
        width (int | None): Width in pixels.
        height (int | None): Height in pixels.
        crop (int): 1 if black bars were detected.
        hdr (int): 1 if the video is HDR.
        other_codecs (str): Comma separated other codec names.
        audio_languages (str): Comma separated audio languages.
        scanned_at (str | None): ISO timestamp of the last scan.
    """

    path: Path
    mtime: float
    size: int
    name: str
    category: str
    hash: str | None = None
    duration: str | None = None
    duration_raw: float | None = None
    fps: float | None = None
    codec: str | None = None
    bitrate: float | None = None
    aspect_ratio: str | None = None
    width: int | None = None
    height: int | None = None
    crop: int = 0
    hdr: int = 0
    other_codecs: str = ""
    audio_languages: str = ""
    scanned_at: str | None = None


@dataclass
class PruneResult:
    """Outcome of a disk/database reconciliation.

    Attributes:
        flagged (int): Rows newly flagged as missing (still within grace).
        recovered (int): Rows that reappeared on disk and were un-flagged.
        pruned (int): Rows hard-deleted (grace exhausted) with their encodes.
        missing_now (int): Total rows currently flagged as missing.
        mass (bool): True when an implausibly large share of the scope was
            flagged in one pass (likely a renamed folder / detached drive).
    """

    flagged: int = 0
    recovered: int = 0
    pruned: int = 0
    missing_now: int = 0
    mass: bool = False


def connect(db_path: Path) -> sqlite3.Connection:
    """Opens a connection to the database and ensures the schema.

    The connection is usable from any thread (check_same_thread=False) and
    the journal mode is WAL so that the resident app and CLI processes can
    access the database concurrently. Writes stay short (single statements
    with commit) so readers never block meaningfully.

    Args:
        db_path (Path): Path of the sqlite database file.

    Returns:
        sqlite3.Connection: The connection (row_factory set to sqlite3.Row).
    """
    connection = sqlite3.connect(db_path, check_same_thread=False)
    # the caller only owns the connection once it is returned: a schema or
    # migration failure must not leave it to the garbage collector (issue #480)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        # ensure the version row exists
        if connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        _migrate(connection)
        connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def is_unchanged(row: sqlite3.Row, path: Path) -> bool:
    """Checks if a stored file entry is still valid (mtime and size match).

    Args:
        row (sqlite3.Row): The stored file row.
        path (Path): The current file on disk.

    Returns:
        bool: True if the file did not change since the last scan.
    """
    stat = path.stat()
    return bool(row["mtime"] == stat.st_mtime and row["size"] == stat.st_size)


def upsert_file(connection: sqlite3.Connection, record: MovieRecord) -> int:
    """Inserts or updates a movie record and returns the file id.

    Args:
        connection (sqlite3.Connection): The database connection.
        record (MovieRecord): The record to store.

    Returns:
        int: The file id.
    """
    cursor = connection.execute(
        """
        INSERT INTO files (
            path, mtime, size, hash, name, category, duration, duration_raw, fps,
            codec, bitrate, aspect_ratio, width, height, crop, hdr,
            other_codecs, audio_languages, scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
            mtime = excluded.mtime,
            size = excluded.size,
            hash = excluded.hash,
            name = excluded.name,
            category = excluded.category,
            duration = excluded.duration,
            duration_raw = excluded.duration_raw,
            fps = excluded.fps,
            codec = excluded.codec,
            bitrate = excluded.bitrate,
            aspect_ratio = excluded.aspect_ratio,
            width = excluded.width,
            height = excluded.height,
            crop = excluded.crop,
            hdr = excluded.hdr,
            other_codecs = excluded.other_codecs,
            audio_languages = excluded.audio_languages,
            missing_scans = 0,
            missing_since = NULL,
            scanned_at = datetime('now')
        """,
        (
            str(record.path),
            record.mtime,
            record.size,
            record.hash,
            record.name,
            record.category,
            record.duration,
            record.duration_raw,
            record.fps,
            record.codec,
            record.bitrate,
            record.aspect_ratio,
            record.width,
            record.height,
            record.crop,
            record.hdr,
            record.other_codecs,
            record.audio_languages,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def get_file_by_path(connection: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    """Fetches a file row by path.

    Args:
        connection (sqlite3.Connection): The database connection.
        path (Path): The movie file path.

    Returns:
        sqlite3.Row | None: The row, or None if not stored.
    """
    return cast(
        "sqlite3.Row | None",
        connection.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone(),
    )


def save_plan(
    connection: sqlite3.Connection,
    file_id: int,
    chosen_crf: int | None,
    achieved_vmaf: float | None,
    crop: tuple[int, int, int, int] | None,
    probes: list,
    skipped_reason: str | None,
    estimated_size: int | None,
    input_mtime: float,
    input_size: int,
    encoder: str,
    encoder_preset: str,
    target_vmaf: float,
    crf_low: int,
    crf_high: int,
    sample_count: int,
    sample_seconds: float,
    crop_sample_count: int = 3,
    vmaf_statistic: str = "mean",
    vmaf_recipe: str = "",
) -> int:
    """Stores (upserts) the plan decision for one file — one row per file.

    Plan history stays in the job payloads; this table holds only the
    LATEST decision per file, the thing an encode consults before probing.

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id of the planned movie.
        chosen_crf (int | None): The planned CRF (None for some skips).
        achieved_vmaf (float | None): The VMAF measured at that CRF.
        crop (tuple | None): The crop rect (None when no bars removed).
        probes (list): The sample probes ([SampleProbe], JSON-serialized).
        skipped_reason (str | None): Skip verdict, e.g. 'no size gain'.
        estimated_size (int | None): The predicted output size (the plan
            run's size estimate; None for skips without one).
        input_mtime (float): The file's mtime at plan time (fingerprint).
        input_size (int): The file's size at plan time (fingerprint).
        encoder (str): The encoder used (fingerprint).
        encoder_preset (str): The PROBE's speed preset (fingerprint; a plan
            is a probe result, issue #22).
        target_vmaf (float): The VMAF target (fingerprint).
        crf_low (int): The search range's low bound (fingerprint).
        crf_high (int): The search range's high bound (fingerprint).
        sample_count (int): Samples per probe (fingerprint).
        sample_seconds (float): Sample length (fingerprint).
        crop_sample_count (int): The crop detector's sample count
            (fingerprint: more samples can only shrink the crop).
        vmaf_recipe (str): The measurement recipe's crop-independent part
            (fingerprint, issue #308: a plan measured another way is a
            different measurement). Empty means a plan stored before the
            recipe existed.

    Returns:
        int: The plan row id.
    """
    connection.execute("DELETE FROM plans WHERE file_id = ?", (file_id,))
    cursor = connection.execute(
        """
        INSERT INTO plans (
            file_id, chosen_crf, achieved_vmaf, crop, probes, skipped_reason,
            estimated_size, input_mtime, input_size, encoder, encoder_preset,
            target_vmaf, crf_low, crf_high, sample_count, sample_seconds,
            crop_sample_count, vmaf_statistic, vmaf_recipe
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            chosen_crf,
            achieved_vmaf,
            None if crop is None else ",".join(str(v) for v in crop),
            json.dumps([(p.crf, p.vmaf) for p in probes]),
            skipped_reason,
            estimated_size,
            input_mtime,
            input_size,
            encoder,
            encoder_preset,
            target_vmaf,
            crf_low,
            crf_high,
            sample_count,
            sample_seconds,
            crop_sample_count,
            vmaf_statistic,
            vmaf_recipe,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def save_probe_score(
    connection: sqlite3.Connection, key: str, value: float, encoded_size: int | None = None
) -> None:
    """Stores one probe measurement under its content-addressed key.

    The cache is an optimization, never a decision input on its own: a row
    is only read back when EVERY input that can change the measurement
    matches (see probe_cache_key in quality.py). Upsert: a re-measured probe
    overwrites its own entry.

    Args:
        connection (sqlite3.Connection): The database connection.
        key (str): The sha256 cache key (probe_cache_key).
        value (float): The measured VMAF score or size factor.
        encoded_size (int | None): The probe encode's byte size, recorded
            beside the score (issue #314): the chosen-CRF size decision
            reads these instead of re-encoding a window. None for the
            size-factor entries (they have no VMAF score, and their
            single-window size is not a total).
    """
    connection.execute(
        """
        INSERT INTO probe_cache (key, value, encoded_size) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       encoded_size = excluded.encoded_size,
                                       created_at = datetime('now')
        """,
        (key, value, encoded_size),
    )
    connection.commit()


def get_probe_entry(connection: sqlite3.Connection, key: str) -> tuple[float, int | None] | None:
    """Returns a cache entry's (score, encoded_size), or None on a miss.

    Args:
        connection (sqlite3.Connection): The database connection.
        key (str): The sha256 cache key (probe_cache_key).

    Returns:
        tuple | None: (value, encoded_size) — the size is None for old rows
            (pre-#314) and for size-factor entries; the caller treats a None
            size as a MISS for size purposes, never as zero.
    """
    row = connection.execute(
        "SELECT value, encoded_size FROM probe_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    size = int(row["encoded_size"]) if row["encoded_size"] is not None else None
    return float(row["value"]), size


def get_valid_plan(
    connection: sqlite3.Connection,
    path: Path,
    *,
    encoder: str,
    encoder_preset: str,
    target_vmaf: float,
    crf_range: tuple[int, int],
    sample_count: int,
    sample_seconds: float,
    crop_sample_count: int = 3,
    vmaf_statistic: str = "mean",
    vmaf_recipe: str = "",
) -> sqlite3.Row | None:
    """Returns the file's stored plan when the fingerprint matches.

    A plan is valid ONLY when every decision input matches: the file
    itself (mtime + size), the encoder settings, the quality target, the
    search range and the sampling parameters. ANY mismatch returns None —
    fail-safe to re-probe, never guess.

    Args:
        connection (sqlite3.Connection): The database connection.
        path (Path): The movie file path.
        encoder (str): The encoder of the pending encode.
        encoder_preset (str): The PROBE's speed preset of the pending
            encode (a plan is a probe result, issue #22).
        target_vmaf (float): The VMAF target of the pending encode.
        crf_range (tuple[int, int]): The pending encode's search range.
        sample_count (int): The pending encode's sample count.
        sample_seconds (float): The pending encode's sample length.
        crop_sample_count (int): The pending encode's crop sample count.
        vmaf_recipe (str): The pending encode's measurement recipe
            (crop-independent part, issue #308). An empty string never
            matches: a plan stored before the recipe existed is re-probed.

    Returns:
        sqlite3.Row | None: The valid plan row, or None.
    """
    if not path.is_file():
        return None
    row = connection.execute(
        """
        SELECT p.*, f.mtime AS files_mtime, f.size AS files_size
        FROM plans p JOIN files f ON f.id = p.file_id
        WHERE f.path = ?
        """,
        (str(path),),
    ).fetchone()
    if row is None:
        return None
    stat = path.stat()
    if (
        abs(stat.st_mtime - row["input_mtime"]) > 1.0
        or stat.st_size != row["input_size"]
        or row["encoder"] != encoder
        or row["encoder_preset"] != encoder_preset
        or abs(row["target_vmaf"] - target_vmaf) > 0.01
        or row["crf_low"] != crf_range[0]
        or row["crf_high"] != crf_range[1]
        or row["sample_count"] != sample_count
        or abs(row["sample_seconds"] - sample_seconds) > 0.01
        or int(row["crop_sample_count"]) != crop_sample_count
        or str(row["vmaf_statistic"]) != vmaf_statistic
        # the recipe only matters when the plan MEASURED something: a gate skip
        # (too short, resolution, size, codec) never ran a VMAF measurement, so
        # its row carries '' and must stay reusable - requiring a recipe there
        # would re-derive every gate skip on every run (issue #308's review).
        or (row["achieved_vmaf"] is not None and str(row["vmaf_recipe"]) != vmaf_recipe)
    ):
        return None
    return cast("sqlite3.Row", row)


def save_crop_summary(
    connection: sqlite3.Connection,
    movie: Path,
    summary: CropSummary,
) -> None:
    """Stores (upserts) the timeline detector's summary for one file version.

    One row per file: the LATEST summary replaces the previous one, keyed on
    the file's identity at write time (size + mtime). The read side validates
    the identity exactly like the plan cache — a changed file is a miss.

    Args:
        connection: The database connection.
        movie (Path): The source movie file.
        summary (crop.CropSummary): The timeline detector's result.
    """
    row = connection.execute("SELECT id FROM files WHERE path = ?", (str(movie),)).fetchone()
    if row is None:
        # never scanned: the summary has no file to hang on (the scan owns
        # the file rows); compute-uncached callers lose the cache this run
        return
    file_id = int(row["id"])
    stat = movie.stat()
    bars = None if summary.bars is None else ",".join(str(v) for v in summary.bars)
    ratios = json.dumps([[list(rect), share] for rect, share in summary.ratios.items()])
    connection.execute("DELETE FROM crops WHERE file_id = ?", (file_id,))
    connection.execute(
        """
        INSERT INTO crops (
            file_id, bars, frame_width, frame_height, readings,
            sustained_readings, ratios, input_mtime, input_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            bars,
            summary.frame_width,
            summary.frame_height,
            summary.readings,
            summary.sustained_readings,
            ratios,
            stat.st_mtime,
            stat.st_size,
        ),
    )
    connection.commit()


def get_crop_row(connection: sqlite3.Connection, movie: Path) -> sqlite3.Row | None:
    """Returns the file's cached crop row when the file is unchanged.

    Identity is size + mtime (the scan cache's rule): ANY change is a miss,
    fail-safe to recompute. The ROW is returned raw: interpreting it into a
    summary (bars, ratios) is the engine's job — db stores, engine decides.

    Args:
        connection: The database connection.
        movie (Path): The source movie file.

    Returns:
        sqlite3.Row | None: The raw crops row, or None.
    """
    if not movie.is_file():
        return None
    stat = movie.stat()
    row = connection.execute(
        """
        SELECT c.bars, c.frame_width, c.frame_height, c.readings,
               c.sustained_readings, c.ratios, c.input_mtime, c.input_size
        FROM crops c JOIN files f ON f.id = c.file_id
        WHERE f.path = ?
        """,
        (str(movie),),
    ).fetchone()
    if row is None:
        return None
    if abs(stat.st_mtime - row["input_mtime"]) > 1.0 or stat.st_size != row["input_size"]:
        return None
    return cast(sqlite3.Row, row)


def get_cached_records(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Returns all stored file rows keyed by their path.

    Args:
        connection (sqlite3.Connection): The database connection.

    Returns:
        dict: Mapping of path string to file row.
    """
    rows = connection.execute("SELECT * FROM files").fetchall()
    return {row["path"]: row for row in rows}


def add_encode(
    connection: sqlite3.Connection,
    file_id: int,
    preset: str,
    encoder: str,
    input_size: int,
    output_size: int,
    saved_percent: float,
    chosen_crf: int | None = None,
    achieved_vmaf: float | None = None,
    encoder_preset: str | None = None,
    probe_encoder_preset: str | None = None,
    encode_seconds: float | None = None,
    probe_count: int | None = None,
    probe_seconds: float | None = None,
    hdr: bool | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
    source_codec: str | None = None,
    source_bit_depth: int | None = None,
    source_duration: float | None = None,
    tool_versions: str | None = None,
    input_bitrate: float | None = None,
    output_bitrate: float | None = None,
    source_file: str | None = None,
    output_file: str | None = None,
    generation: int = 1,
    crf_source: str | None = None,
    plan_created_at: str | None = None,
) -> int:
    """Records an encode in the history table.

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id of the input movie.
        preset (str): The HandBrake preset used (or the quality target label).
        encoder (str): The encoder used.
        input_size (int): Input size in bytes.
        output_size (int): Output size in bytes.
        saved_percent (float): Saved percentage.
        chosen_crf (int | None): The CRF chosen by the quality search, if any.
        achieved_vmaf (float | None): The VMAF measured at the chosen CRF.
        encoder_preset (str | None): The FINAL encode's speed preset
            (an output setting, issue #22).
        probe_encoder_preset (str | None): The PROBE's speed preset (a
            measurement setting, issue #22) — which preset produced the
            measurements this encode's CRF decision rests on.
        input_bitrate (float | None): Source bitrate in MBit/s (drift data).
        output_bitrate (float | None): Encode bitrate in MBit/s (drift data).
        source_file (str | None): The absolute path of the encode's input.
        output_file (str | None): The absolute path of the encode's output —
            the chain link: a later encode of that file derives generation
            N+1 from this row.
        generation (int): 1 for an encode of an original source, N+1 when
            the input was itself produced by a previous encode row.
        crf_source (str | None): Provenance of the CRF decision: 'plan'
            when reused from a stored plan, 'probe' when measured now.
        plan_created_at (str | None): When the trusted plan was probed
            ('plan' provenance only).

    Returns:
        int: The encode id.
    """
    cursor = connection.execute(
        """
        INSERT INTO encodes (
            file_id, preset, encoder, input_size, output_size, saved_percent,
            chosen_crf, achieved_vmaf, encoder_preset, probe_encoder_preset,
            encode_seconds,
            probe_count, probe_seconds, hdr, source_width, source_height,
            source_codec, source_bit_depth, source_duration, tool_versions,
            input_bitrate, output_bitrate, source_file, output_file, generation,
            crf_source, plan_created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            preset,
            encoder,
            input_size,
            output_size,
            saved_percent,
            chosen_crf,
            achieved_vmaf,
            encoder_preset,
            probe_encoder_preset,
            encode_seconds,
            probe_count,
            probe_seconds,
            hdr,
            source_width,
            source_height,
            source_codec,
            source_bit_depth,
            source_duration,
            tool_versions,
            input_bitrate,
            output_bitrate,
            source_file,
            output_file,
            generation,
            crf_source,
            plan_created_at,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def set_encode_output(
    connection: sqlite3.Connection, encode_id: int, output_file: str | None
) -> None:
    """Sets (or clears) an encode row's output_file after the fact.

    The row is written before the swap that decides where the artifact ends
    up (issue #232): a swap that succeeds leaves the row's path standing, one
    that is declined discards the encode with its staging directory, and the
    row must then record that the artifact no longer exists - otherwise
    `doctor` reports a vanished file for every declined swap and the
    generation chain links through a path that never held the artifact.

    Args:
        connection (sqlite3.Connection): The database connection.
        encode_id (int): The encode row to update.
        output_file (str | None): The path that now holds the artifact, or
            None to record that the encode was discarded.
    """
    connection.execute(
        "UPDATE encodes SET output_file = ? WHERE id = ?",
        (output_file, encode_id),
    )
    connection.commit()


def record_verification(
    connection: sqlite3.Connection,
    encode_id: int,
    verified_vmaf: float,
    subsample: int | None,
) -> None:
    """Attaches a full-file VMAF verification result to an encode row.

    Args:
        connection (sqlite3.Connection): The database connection.
        encode_id (int): The encode row the verification belongs to.
        verified_vmaf (float): The measured full-file (or sub-sampled) VMAF.
        subsample (int | None): The ffmpeg n_subsample divisor used
            (None = every frame measured).
    """
    connection.execute(
        """
        UPDATE encodes
        SET verified_vmaf = ?, verified_at = datetime('now'), verified_subsample = ?
        WHERE id = ?
        """,
        (verified_vmaf, subsample, encode_id),
    )
    connection.commit()


def get_verification(
    connection: sqlite3.Connection,
    encode_id: int,
) -> sqlite3.Row | None:
    """Returns the verification columns of an encode row.

    Args:
        connection (sqlite3.Connection): The database connection.
        encode_id (int): The encode row id.

    Returns:
        sqlite3.Row | None: The row (verified_vmaf/verified_at/
            verified_subsample), None when the encode id is unknown.
    """
    row = connection.execute(
        "SELECT verified_vmaf, verified_at, verified_subsample FROM encodes WHERE id = ?",
        (encode_id,),
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def add_skip(
    connection: sqlite3.Connection,
    file_id: int,
    reason: str,
    chosen_crf: int | None = None,
    achieved_vmaf: float | None = None,
) -> int:
    """Records a skipped movie in the encode history.

    A skip is stored as an encode row with zero sizes and a 'skipped (...)'
    preset label so subsequent runs can recognize it (and e.g. not re-probe
    a movie known not to shrink).

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id of the input movie.
        reason (str): Why the movie was skipped, e.g. 'no size gain'.
        chosen_crf (int | None): The CRF used in the probe, if any.
        achieved_vmaf (float | None): The VMAF measured in the probe, if any.

    Returns:
        int: The encode row id.
    """
    return add_encode(
        connection,
        file_id,
        f"skipped ({reason})",
        "none",
        0,
        0,
        0.0,
        chosen_crf=chosen_crf,
        achieved_vmaf=achieved_vmaf,
    )


def get_latest_encode(connection: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    """Returns the most recent encode (or skip) row of a file.

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id.

    Returns:
        sqlite3.Row | None: The newest row, or None if never encoded.
    """
    rows = get_encodes_for_file(connection, file_id)
    return rows[0] if rows else None


def get_latest_encode_output(connection: sqlite3.Connection, file_id: int) -> Path | None:
    """Returns the latest encode row's output_file as a path (None when unset).

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id.

    Returns:
        Path | None: The recorded artifact path, or None when the latest row
            is a skip marker or records no output.
    """
    latest = get_latest_encode(connection, file_id)
    if latest is None or latest["output_size"] <= 0 or not latest["output_file"]:
        return None
    return Path(latest["output_file"])


def has_output_for_latest_encode(
    connection: sqlite3.Connection,
    file_id: int,
    output_file: Path,
) -> bool:
    """Checks whether the latest encode entry produced a still-existing output.

    Used for batch resume: a movie whose encode was recorded AND whose output
    file still exists is considered finished; anything else (no record,
    deleted output) must be (re-)encoded.

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id.
        output_file (Path): The output path this run would write.

    Returns:
        bool: True if the movie can be considered already encoded.
    """
    latest = get_latest_encode(connection, file_id)
    if latest is None:
        return False
    if latest["output_size"] <= 0:
        return False  # a skip marker, not a real encode
    return output_file.is_file() and output_file.stat().st_size > 0


def get_encodes_for_file(connection: sqlite3.Connection, file_id: int) -> list[sqlite3.Row]:
    """Returns all encodes for a file, newest first.

    Args:
        connection (sqlite3.Connection): The database connection.
        file_id (int): The file id.

    Returns:
        list: The encode rows.
    """
    return connection.execute(
        "SELECT * FROM encodes WHERE file_id = ? ORDER BY id DESC",
        (file_id,),
    ).fetchall()


def add_retained(
    connection: sqlite3.Connection,
    source_path: Path,
    retained_path: Path,
    encode_id: int | None,
    size_before: int,
    size_after: int,
) -> int:
    """Records a swapped original in the retention ledger (issue #35).

    A source is never deleted: a swapped original is renamed aside and this
    row is what the operator reads to reclaim the space. ShrinkRay itself
    never removes the retained file.

    Args:
        connection (sqlite3.Connection): The database connection.
        source_path (Path): The path that now holds the new file.
        retained_path (Path): Where the original was moved.
        encode_id (int | None): The encode that superseded it.
        size_before (int): The original's size in bytes.
        size_after (int): The new file's size in bytes.

    Returns:
        int: The new ledger row's id.
    """
    row = connection.execute("SELECT id FROM files WHERE path = ?", (str(source_path),)).fetchone()
    file_id = row["id"] if row is not None else None
    cursor = connection.execute(
        """
        INSERT INTO retained (
            file_id, source_path, retained_path, encode_id, size_before, size_after
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (file_id, str(source_path), str(retained_path), encode_id, size_before, size_after),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def list_retained(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Returns every retained original, newest first.

    Args:
        connection (sqlite3.Connection): The database connection.

    Returns:
        list[sqlite3.Row]: The ledger rows.
    """
    return connection.execute("SELECT * FROM retained ORDER BY id DESC").fetchall()


def prune_missing(
    connection: sqlite3.Connection,
    existing_paths: set[str],
    scope: str | None = None,
) -> PruneResult:
    """Reconciles the database with what is actually on disk.

    Files of the scanned scope that are no longer on disk are not deleted
    immediately: they are flagged (missing_scans incremented, missing_since
    recorded) and hard-deleted — including their encode history — only after
    MISSING_GRACE consecutive scans still do not see them. Files that
    reappear while flagged are reset to normal. The grace period also acts
    as a guard against accidents (e.g. a renamed media folder flags instead
    of wiping the whole scope).

    Without a scope, only rows inside the given scope directory are
    considered: a scan of one folder must never touch the history of files
    stored elsewhere (e.g. a scan of test_media/movies_two must leave the
    records of test_media/movies alone).

    Args:
        connection (sqlite3.Connection): The database connection.
        existing_paths (set[str]): Paths that are still on disk.
        scope (str | None): Only reconcile rows under this directory path.

    Returns:
        PruneResult: What was flagged, recovered and hard-deleted.
    """
    rows = connection.execute("SELECT id, path, missing_scans FROM files").fetchall()
    # the db stores absolute paths; a relative scan root must resolve or the
    # prefix match would silently never fire (no flagging, no pruning)
    scope_normalized = str(Path(scope).resolve()) if scope is not None else None
    result = PruneResult()
    in_scope = 0
    for row in rows:
        path = row["path"]
        if scope_normalized is not None and not path.startswith(scope_normalized):
            continue
        in_scope += 1
        if path in existing_paths:
            if row["missing_scans"]:
                connection.execute(
                    "UPDATE files SET missing_scans = 0, missing_since = NULL WHERE id = ?",
                    (row["id"],),
                )
                result.recovered += 1
            continue
        scans = int(row["missing_scans"] or 0) + 1
        if scans >= MISSING_GRACE:
            connection.execute("DELETE FROM encodes WHERE file_id = ?", (row["id"],))
            connection.execute("DELETE FROM files WHERE id = ?", (row["id"],))
            result.pruned += 1
        else:
            connection.execute(
                """
                UPDATE files
                SET missing_scans = ?,
                    missing_since = COALESCE(missing_since, datetime('now'))
                WHERE id = ?
                """,
                (scans, row["id"]),
            )
            result.flagged += 1
    result.missing_now = int(
        connection.execute("SELECT COUNT(*) FROM files WHERE missing_scans > 0").fetchone()[0]
    )
    # many flags in one pass look like an accident (renamed folder, detached
    # drive) — the scanner turns this into a loud warning
    result.mass = in_scope > 0 and result.flagged >= max(5, in_scope * 0.2)
    connection.commit()
    return result
