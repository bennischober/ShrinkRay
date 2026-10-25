"""Encode orchestration (the service layer between CLI and engine).

Contains the logic that drives multiple encode engine calls for a whole
movie set: batch resume, skip recording and result persistence. It has no
presentation logic — the CLI prints, this module decides and records.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shrinkray import db
from shrinkray.config import DEFAULT_PROBE_ENCODER_PRESET
from shrinkray.db import MovieRecord
from shrinkray.tools.ffprobe import probe_duration_seconds as probe_duration

if TYPE_CHECKING:
    from shrinkray.config import Conf
    from shrinkray.engine.pipeline import EncodeResult
    from shrinkray.engine.plan import QualityResult


@dataclass
class EncodeOutcome:
    """The outcome of processing one movie in an encode run.

    Attributes:
        movie (str): The movie file path as string.
        encoded (bool): True if an encode was performed and succeeded.
        skipped (bool): True if a guardrail or the resume check skipped it.
        error (str | None): The failure message for failed encodes.
    """

    movie: str
    encoded: bool = False
    skipped: bool = False
    error: str | None = None


def ensure_file_row(connection: sqlite3.Connection, movie: Path) -> int:
    """Returns the file id for the movie, storing a minimal row if needed.

    Args:
        connection: The database connection.
        movie (Path): The movie file.

    Returns:
        int: The file id.
    """
    movie = movie.resolve()
    file_row = db.get_file_by_path(connection, movie)
    if file_row is not None:
        return int(file_row["id"])
    record = MovieRecord(
        path=movie,
        mtime=movie.stat().st_mtime,
        size=movie.stat().st_size,
        name=movie.stem,
        category=movie.parent.name,
    )
    return db.upsert_file(connection, record)


def derive_generation(connection: sqlite3.Connection, movie: Path) -> int:
    """Determines the generation of a movie by its encode ancestry.

    The encodes table chains generations via output_file: a row's
    output_file is the path of the file it produced. When the movie's path
    appears as a previous row's output_file, the movie IS that encode's
    output — generation N+1. A file with no ancestry is generation 1.

    Args:
        connection: The database connection.
        movie (Path): The movie file to classify.

    Returns:
        int: The generation number (>= 1).
    """
    path_str = str(movie.resolve())
    row = connection.execute(
        """
        SELECT generation FROM encodes
        WHERE output_file = ?
        ORDER BY id DESC LIMIT 1
        """,
        (path_str,),
    ).fetchone()
    if row is not None:
        return int(row["generation"]) + 1
    return 1


def record_plan(
    connection: sqlite3.Connection, movie: Path, result: QualityResult, config: Conf
) -> int | None:
    """Persists a plan decision for one movie (the encode-path "quicksave").

    Stores the LATEST decision per file (upsert): the chosen CRF with its
    sample-based VMAF, or the skip verdict — both short-circuit a later
    encode's probing when the fingerprint matches.

    Args:
        connection: The database connection.
        movie (Path): The input movie file.
        result (QualityResult): The plan's engine result.
        config (Conf): The config settings.

    Returns:
        int | None: The plan row id, or None when the movie has no file
        row yet (plans are keyed by the scanned library; an unscanned
        movie's plan is not reusable anyway).
    """
    row = db.get_file_by_path(connection, movie)
    if row is None:
        return None
    stat = movie.stat()
    if result.input_size and stat.st_size != result.input_size:
        # the file changed between probing and recording (replace while
        # planning): storing the plan would pin the NEW fingerprint to the
        # OLD probes - fail safe, the next plan run re-records it
        return None
    # the fingerprint stores the EFFECTIVE decision inputs: for HDR the
    # engine overrides target/range and switches to the 10-bit encoder,
    # so a plan of an HDR movie must be recorded under those values. The
    # result is the single source of truth for all of them (the engine's
    # plan-reuse lookup matches on exactly this fingerprint)
    encoder = result.encoder or config.handbrake_encoder
    # the plan fingerprint keys on the PROBE preset (issue #22 decision): a
    # stored plan is a probe result, its CRF depends on the probe preset
    # only - the plans.encoder_preset column holds the probe preset
    probe_preset = (
        result.probe_encoder_preset or result.encoder_preset or DEFAULT_PROBE_ENCODER_PRESET
    )
    return db.save_plan(
        connection,
        row["id"],
        result.chosen_crf,
        result.achieved_vmaf,
        result.crop,
        result.probes,
        result.skipped_reason,
        # on a plan result, output_size IS the size estimate (a plan never
        # writes a file); the estimate is the UI's prediction number
        result.output_size if result.success else None,
        stat.st_mtime,
        stat.st_size,
        encoder,
        probe_preset,
        result.target_vmaf,
        result.crf_range[0],
        result.crf_range[1],
        result.sample_count,
        result.sample_seconds,
        result.crop_sample_count,
        result.vmaf_statistic,
    )


def save_encode(
    connection: sqlite3.Connection,
    movie: Path,
    preset: str,
    encoder: str,
    input_size: int,
    output_size: int,
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
    source_duration: float | None = None,
    tool_versions: str | None = None,
    input_bitrate: float | None = None,
    output_bitrate: float | None = None,
    source_file: str | None = None,
    output_file: str | None = None,
    generation: int | None = None,
    crf_source: str | None = None,
    plan_created_at: str | None = None,
) -> int:
    """Stores an encode record for the given movie in the database.

    Args:
        connection: The database connection.
        movie (Path): The input movie file.
        preset (str): The preset (or quality target) used.
        encoder (str): The encoder used.
        input_size (int): Input size in bytes.
        output_size (int): Output size in bytes.
        chosen_crf (int | None): The CRF chosen by the quality search, if any.
        achieved_vmaf (float | None): The VMAF measured at the chosen CRF.
        encoder_preset (str | None): The FINAL encode's speed preset (an
            output setting, issue #22).
        probe_encoder_preset (str | None): The PROBE's speed preset (a
            measurement setting, issue #22).
        encode_seconds (float | None): Wall time of the encode (benchmark).
        probe_count (int | None): Number of probes in the search (benchmark).
        probe_seconds (float | None): Wall time of the probing (benchmark).
        hdr (bool | None): Whether the source was HDR (benchmark).
        source_width (int | None): Source resolution snapshot (benchmark).
        source_height (int | None): Source resolution snapshot (benchmark).
        source_codec (str | None): Source codec snapshot (benchmark).
        source_duration (float | None): Source duration snapshot (benchmark).
        tool_versions (str | None): Tool version string (benchmark).
        input_bitrate (float | None): Source bitrate in MBit/s (drift data).
        output_bitrate (float | None): Encode bitrate in MBit/s (drift data).
        source_file (str | None): The input path (provenance).
        output_file (str | None): The output path (generation chain link).
        generation (int | None): Derived when None (ancestry lookup).
        crf_source (str | None): Provenance of the CRF decision: 'plan'
            when reused from a stored plan, 'probe' when measured now.
        plan_created_at (str | None): When the trusted plan was probed
            ('plan' provenance only).
    """
    file_id = ensure_file_row(connection, movie)
    saved_percent = round((1 - output_size / input_size) * 100, 2) if input_size else 0.0
    if generation is None:
        generation = derive_generation(connection, movie)
    return db.add_encode(
        connection,
        file_id,
        preset,
        encoder,
        input_size,
        output_size,
        saved_percent,
        chosen_crf=chosen_crf,
        achieved_vmaf=achieved_vmaf,
        encoder_preset=encoder_preset,
        probe_encoder_preset=probe_encoder_preset,
        encode_seconds=encode_seconds,
        probe_count=probe_count,
        probe_seconds=probe_seconds,
        hdr=hdr,
        source_width=source_width,
        source_height=source_height,
        source_codec=source_codec,
        source_duration=source_duration,
        tool_versions=tool_versions,
        input_bitrate=input_bitrate,
        output_bitrate=output_bitrate,
        source_file=source_file,
        output_file=output_file,
        generation=generation,
        crf_source=crf_source,
        plan_created_at=plan_created_at,
    )


def save_verification(
    connection: sqlite3.Connection, encode_id: int, result: QualityResult
) -> None:
    """Attaches a quality result's verification data to its encode row.

    Args:
        connection: The database connection.
        encode_id (int): The encode row id (from save_encode).
        result (QualityResult): The engine result (verified_vmaf and
            verified_subsample are read from it; None => no-op).
    """
    if result.verified_vmaf is None:
        return
    db.record_verification(
        connection,
        encode_id,
        float(result.verified_vmaf),
        result.verified_subsample,
    )


def save_skip(
    connection: sqlite3.Connection,
    movie: Path,
    reason: str,
    chosen_crf: int | None = None,
    achieved_vmaf: float | None = None,
) -> None:
    """Stores a skip record for the given movie in the database.

    The skip keeps its evidence (the best CRF and the VMAF measured there),
    so a later run can recognize 'already probed at the floor and failed'
    instead of re-deriving the same verdict.

    Args:
        connection: The database connection.
        movie (Path): The input movie file.
        reason (str): Why the movie was skipped.
        chosen_crf (int | None): The best CRF the search reached.
        achieved_vmaf (float | None): The VMAF measured there.
    """
    file_id = ensure_file_row(connection, movie)
    db.add_skip(connection, file_id, reason, chosen_crf, achieved_vmaf)


def finished_movies(
    connection: sqlite3.Connection, movies: list[Path], output_dir: Path
) -> set[str]:
    """Determines which movies are already encoded (batch resume check).

    A movie counts as finished when the database holds a real encode (not a
    skip marker) AND its output file still exists, is non-empty AND its
    duration verifies against the source. The duration check catches debris
    from interrupted encodes: a crashed HandBrake leaves a non-empty but
    truncated file which must be re-encoded, not skipped (an unverifiable
    file stays conservatively "finished" - never delete on a guess).

    Args:
        connection: The database connection.
        movies (list[Path]): The movie files of the run.
        output_dir (Path): The output directory of the run.

    Returns:
        set[str]: The movie paths (as strings) that can be skipped.
    """
    from shrinkray.engine.pipeline import durations_match

    finished: set[str] = set()
    for movie in movies:
        file_row = db.get_file_by_path(connection, movie)
        if file_row is None:
            continue
        output_file = output_dir / movie.name
        if not db.has_output_for_latest_encode(connection, int(file_row["id"]), output_file):
            continue
        # the db says encoded and the file is non-empty: verify it is whole
        if durations_match(movie, output_file):
            finished.add(str(movie))
        else:
            from shrinkray.logger import log_warning

            log_warning(
                f"Output of '{movie.name}' exists but is truncated (interrupted "
                "encode?) => it will be re-encoded."
            )
    return finished


def record_quality_results(
    connection: sqlite3.Connection, movie: Path, result: QualityResult, config: Conf
) -> EncodeOutcome:
    """Persists the result of one quality-mode encode (or skip).

    Args:
        connection: The database connection.
        movie (Path): The input movie file.
        result (QualityResult): The engine result.
        config (Conf): The config settings.

    Returns:
        EncodeOutcome: What happened (encoded / skipped / failed).
    """
    if result.success:
        encode_id = save_encode(
            connection,
            movie,
            # the EFFECTIVE target, not the caller's: for HDR the engine
            # overrides it (DEFAULT_HDR_TARGET_VMAF), and the row must record
            # the target the encode was actually decided against
            f"VMAF>{result.target_vmaf}",
            # the EFFECTIVE encoder, not the config's: for HDR the engine
            # switched to the 10-bit variant, and a per-encoder drift or
            # benchmark analysis would mix 8-bit and 10-bit rows otherwise
            result.encoder or config.handbrake_encoder,
            result.input_size,
            result.output_size,
            chosen_crf=result.chosen_crf,
            achieved_vmaf=result.achieved_vmaf,
            encoder_preset=result.encoder_preset,
            probe_encoder_preset=result.probe_encoder_preset,
            encode_seconds=result.encode_seconds,
            probe_count=len(result.probes),
            probe_seconds=result.probe_seconds,
            hdr=result.hdr,
            source_width=result.input_width,
            source_height=result.input_height,
            source_codec=result.input_codec,
            source_duration=result.input_duration,
            tool_versions=result.tool_versions,
            input_bitrate=result.input_bitrate,
            output_bitrate=result.output_bitrate,
            source_file=str(movie.resolve()),
            output_file=str(result.output.resolve()) if result.output else None,
            crf_source=result.crf_source or "probe",
            plan_created_at=result.plan_created_at,
        )
        save_verification(connection, encode_id, result)
        return EncodeOutcome(movie=str(movie), encoded=True)
    if result.skipped_reason is not None:
        save_skip(
            connection,
            movie,
            result.skipped_reason,
            result.chosen_crf,
            result.achieved_vmaf,
        )
        return EncodeOutcome(movie=str(movie), skipped=True, error=result.error)
    return EncodeOutcome(movie=str(movie), error=result.error)


def record_fixed_results(
    connection: sqlite3.Connection, results: list[EncodeResult], config: Conf
) -> list[EncodeOutcome]:
    """Persists the results of a fixed-preset run.

    An encode that did NOT shrink the file is recorded as a skip ('no size
    gain') instead of a success: a pointless encode should never wear a
    green chip in the UI.

    Args:
        connection: The database connection.
        results (list[EncodeResult]): The engine results.
        config (Conf): The config settings.

    Returns:
        list[EncodeOutcome]: One outcome per result.
    """
    outcomes = []
    for encode_result in results:
        if encode_result.success and encode_result.output_size >= encode_result.input_size:
            save_skip(connection, encode_result.input, "no size gain")
            outcomes.append(EncodeOutcome(movie=str(encode_result.input), skipped=True))
            continue
        if encode_result.success:
            save_encode(
                connection,
                encode_result.input,
                config.handbrake_preset,
                config.handbrake_encoder,
                encode_result.input_size,
                encode_result.output_size,
                encoder_preset=config.handbrake_preset,
                source_width=encode_result.input_width,
                source_height=encode_result.input_height,
                source_codec=encode_result.input_codec,
                source_duration=encode_result.input_duration,
            )
            outcomes.append(EncodeOutcome(movie=str(encode_result.input), encoded=True))
        else:
            outcomes.append(
                EncodeOutcome(movie=str(encode_result.input), error=encode_result.error)
            )
    return outcomes


@dataclass
class DoctorFinding:
    """One health finding of the doctor check.

    Attributes:
        movie (str): The affected file path (as stored in the db).
        issue (str): What is wrong (short, human-readable).
        detail (str | None): Extra context (e.g. the expected vs actual
            state, or the error ffprobe produced).
    """

    movie: str
    issue: str
    detail: str | None = None


def doctor(connection: sqlite3.Connection, probe_all: bool = False) -> list[DoctorFinding]:
    """Checks the library's health: db reality vs disk reality.

    Composes existing primitives into one report (B6): dead paths,
    soft-missing counters, encode rows whose output vanished, sources that
    no longer probe cleanly, and orphaned outputs (an encode row's output
    file whose source row is gone). Read-only: doctor never repairs,
    delete-flagging stays the scan's job (grace period semantics).

    Args:
        connection: The database connection.
        probe_all (bool): When True, ffprobe EVERY file row (slow: ~50ms
            per file) to detect truncated/corrupt files; the default
            checks existence and mtime sanity only.

    Returns:
        list[DoctorFinding]: All findings, empty when the library is healthy.
    """
    findings: list[DoctorFinding] = []
    rows = db.get_cached_records(connection)
    for row in rows.values():
        path = Path(row["path"])
        if not path.exists():
            findings.append(
                DoctorFinding(
                    movie=row["path"],
                    issue="dead path",
                    detail=f"missing_scans={row['missing_scans']}"
                    if row["missing_scans"]
                    else None,
                )
            )
            continue
        if not probe_all:
            continue
        duration = probe_duration(path)
        if duration is None:
            findings.append(
                DoctorFinding(movie=row["path"], issue="unprobeable (corrupt/truncated?)")
            )
    # encode rows: does the recorded output still exist?
    for path_str in connection.execute(
        "SELECT DISTINCT output_file FROM encodes WHERE output_file IS NOT NULL"
    ).fetchall():
        output = Path(path_str["output_file"])
        if not output.exists():
            findings.append(
                DoctorFinding(
                    movie=path_str["output_file"],
                    issue="encode output vanished",
                )
            )
    return findings


def toolchain_problems() -> dict[str, list[str]]:
    """Returns the missing toolchain capabilities, empty when all are present.

    The quality mode is the whole product, so a host that cannot measure VMAF or
    cannot encode AV1 cannot do the one thing this tool does. Checking here turns a
    confusing mid-run failure into a clear refusal before any work starts.

    The entries are grouped by prerequisite so a caller can gate on what IT needs:
    `scan` reads media with ffprobe only (HandBrake is not a scan prerequisite),
    `plan`/`encode` need everything. Callers that want the flat list take
    `all_problems()`.

    Returns:
        dict[str, list[str]]: Missing capabilities keyed by prerequisite
            ('ffprobe', 'handbrake', 'vmaf'); a key with an empty list means
            that prerequisite is satisfied.
    """
    from shrinkray.tools import handbrake as handbrake_module
    from shrinkray.tools.vmaf import detect_vmaf_style

    problems: dict[str, list[str]] = {"ffprobe": [], "handbrake": [], "vmaf": []}
    if shutil.which("ffprobe") is None:
        problems["ffprobe"].append(
            "'ffprobe' not found on PATH: install ffmpeg (ffprobe ships with it)"
        )

    binary = handbrake_module.find_handbrake()
    minimum = handbrake_module.MIN_HANDBRAKE_VERSION
    if binary is None:
        problems["handbrake"].append(
            f"'{handbrake_module.HANDBRAKE_BINARY}' not found: install HandBrake "
            f">= {minimum} (AV1 output needs svt_av1)"
        )
    elif not handbrake_module.supports_svt_av1(binary):
        version = handbrake_module.handbrake_version(binary) or "unknown version"
        problems["handbrake"].append(
            f"HandBrakeCLI ({version}) does not know the 'svt_av1' encoder: "
            f"install >= {minimum} (AV1 output needs svt_av1)"
        )

    try:
        detect_vmaf_style()
    except RuntimeError as e:
        problems["vmaf"].append(f"VMAF cannot be measured: {e}")
    return problems


def all_problems() -> list[str]:
    """Flattens `toolchain_problems()` into one message list (doctor's view).

    Returns:
        list[str]: Every missing capability's message, in prerequisite order.
    """
    grouped = toolchain_problems()
    return grouped["ffprobe"] + grouped["handbrake"] + grouped["vmaf"]
