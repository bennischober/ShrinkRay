"""Encode orchestration (the service layer between CLI and engine).

Contains the logic that drives multiple encode engine calls for a whole
movie set: batch resume, skip recording and result persistence. It has no
presentation logic — the CLI prints, this module decides and records.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shrinkray import db
from shrinkray.config import DEFAULT_PROBE_ENCODER_PRESET
from shrinkray.db import MovieRecord
from shrinkray.logger import log_error
from shrinkray.replace import SwapError
from shrinkray.replace import stage as stage_for_swap
from shrinkray.replace import swap as swap_into_library
from shrinkray.tools.ffprobe import probe_duration_seconds as probe_duration

if TYPE_CHECKING:
    from shrinkray.config import Conf
    from shrinkray.engine.plan import QualityResult


@dataclass
class EncodeOutcome:
    """The outcome of processing one movie in an encode run.

    Attributes:
        movie (str): The movie file path as string.
        encoded (bool): True if an encode was performed and succeeded.
        skipped (bool): True if a guardrail or the resume check skipped it.
        error (str | None): The failure message for failed encodes.
        encode_id (int | None): The encode row's id (issue #232) - a swap
            that is declined must clear that row's output_file, because the
            encode is discarded with its staging directory; a caller that
            swaps in place reads it from here instead of re-deriving it.
    """

    movie: str
    encoded: bool = False
    skipped: bool = False
    error: str | None = None
    encode_id: int | None = None


@dataclass
class SwapOutcome:
    """The outcome of one swap-in-place attempt.

    Attributes:
        replaced (bool): True when the encoded file took the source's path.
        retained_path (Path | None): Where the original was moved (None when
            the swap was declined and the source was never touched).
        reason (str | None): Why the swap was declined (None on success).
        size_before (int | None): The source's size before the swap (None
            when declined); the caller's reporting needs it AFTER the swap,
            when the staged file no longer exists.
        size_after (int | None): The new file's size (None when declined).
    """

    replaced: bool
    retained_path: Path | None = None
    reason: str | None = None
    size_before: int | None = None
    size_after: int | None = None


def swap_encode_into_place(
    connection: sqlite3.Connection | None,
    source: Path,
    output: Path,
    encode_id: int | None = None,
    error: str | None = None,
) -> SwapOutcome:
    """Swaps a verified encode into the source's path, retaining the original.

    Shared by the CLI and the job worker (issue #192): the in-place encode
    contract is one code path, not one per caller. Size IS a gate here even
    though the engine already refused a grown encode - this is the last code
    before a file enters the library, and a bug upstream must not be able to
    make the library bigger.

    A swap failure never loses the source: `replace.swap` renames the original
    back when the second rename fails, and recovery handles a crash. A DECLINED
    swap clears the encode row's output_file (issue #232): the encode is
    discarded with its staging directory, so the row must not point at a path
    that is about to vanish - that lie is what made `doctor` report every
    in-place encode as vanished. A swap is also declined when the caller
    reports a verification finding (the `error` argument): the artifact stays
    whole and the source is never touched.

    Args:
        connection: The database connection, or None for a stateless run (the
            swap still happens, but no ledger row is written).
        source: The library file the output replaces.
        output: The verified encoded file.
        encode_id: The encode row the caller just wrote (None: stateless).
        error: A non-fatal verification finding from the encode (e.g. issue
            #240's 1:1 check, where the artifact itself is fine but tracks
            were lost). A declined swap keeps the artifact, never touches
            the source.

    Returns:
        SwapOutcome: What happened (replaced / declined, and why).
    """
    if error is not None:
        # the artifact is fine (the picture measured at target), but the
        # encode broke a promise the swap would make permanent: a lost track
        # cannot be recovered from the swapped-in file, and the operator
        # never asked for a library file with fewer tracks. Decline, keep
        # the original, and say why (issue #240, review finding 1). The
        # encode row is corrected like any declined swap (issue #232): the
        # artifact is discarded with its staging directory.
        reason = f"the encode failed verification: {error}"
        log_error(f"Not replacing '{source.name}': {reason}")
        _encode_discarded(connection, encode_id)
        return SwapOutcome(replaced=False, reason=reason)
    size_before = source.stat().st_size
    size_after = output.stat().st_size
    if size_after >= size_before:
        reason = f"the output is not smaller ({size_before} -> {size_after} bytes)"
        log_error(f"Not replacing '{source.name}': {reason}")
        _encode_discarded(connection, encode_id)
        return SwapOutcome(replaced=False, reason=reason)
    try:
        staged = stage_for_swap(output, source)
        retained = swap_into_library(source, staged)
    except SwapError as error:
        log_error(f"Not replacing '{source.name}': {error}")
        _encode_discarded(connection, encode_id)
        return SwapOutcome(replaced=False, reason=str(error))
    if connection is not None:
        db.add_retained(
            connection,
            source,
            retained,
            encode_id if encode_id is not None else _latest_encode_id(connection, source),
            size_before,
            size_after,
        )
    output.unlink(missing_ok=True)
    return SwapOutcome(
        replaced=True,
        retained_path=retained,
        size_before=size_before,
        size_after=size_after,
    )


def _encode_discarded(connection: sqlite3.Connection | None, encode_id: int | None) -> None:
    """Records that a written encode's artifact was discarded (issue #232).

    A declined swap leaves the encode in its staging directory, which the run
    deletes on exit. The row written a moment earlier points at that staging
    path; it must record the discard instead, or `doctor` reports a vanished
    output for a swap that was REFUSED - the healthy case.

    Args:
        connection: The database connection, or None for a stateless run.
        encode_id: The encode row to correct (None: nothing to correct).
    """
    if connection is None or encode_id is None:
        return
    db.set_encode_output(connection, encode_id, None)


def _latest_encode_id(connection: sqlite3.Connection, source: Path) -> int | None:
    """Returns the encode id that most recently superseded a source.

    The ledger row points at the encode it replaced, so the operator can trace
    a retained original back to the decision that produced its replacement.

    Args:
        connection: The database connection.
        source: The library file.

    Returns:
        int | None: The newest encode's id, or None when the file has no row.
    """
    row = connection.execute("SELECT id FROM files WHERE path = ?", (str(source),)).fetchone()
    if row is None:
        return None
    latest = db.get_latest_encode(connection, int(row["id"]))
    return int(latest["id"]) if latest is not None else None


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
    run_mtime = getattr(result, "input_mtime", None)
    if (result.input_size and stat.st_size != result.input_size) or (
        run_mtime is not None and stat.st_mtime != run_mtime
    ):
        # the file changed between deciding and recording (replace while
        # planning): storing the plan would pin the NEW fingerprint to the
        # OLD probes - a verdict about bytes that no longer exist. Loud
        # abort (issue #436, criterion 3; the pre-#436 size branch returned
        # None silently - a silent None reads as "nothing to record", and
        # the callers already report a RuntimeError per file). The getattr
        # keeps the duck-typed results the tests build (no field) on that
        # older behavior.
        if run_mtime is not None:
            raise RuntimeError(
                f"Not recording the plan for '{movie.name}': the file changed "
                "while the run was deciding it - the verdict is about bytes "
                "that no longer exist; the file is re-decided on the next run"
            )
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
    if run_mtime is None:
        # a duck-typed result without the #436 field (the tests' SimpleNamespace
        # fakes): the commit-time stat is the only identity available
        run_mtime = stat.st_mtime
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
        # the fingerprint's identity is the RUN-START capture (issue #436):
        # the commit-time stat is only the change-gate comparison above, never
        # what the row claims the probes were taken from
        run_mtime,
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
        result.vmaf_recipe or "",
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
    source_bit_depth: int | None = None,
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
        source_bit_depth (int | None): Source bit depth snapshot (issue #234).
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
        source_bit_depth=source_bit_depth,
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
        # in place the artifact IS the source path once the swap succeeded
        # (issue #232): the row's output_file carries it, and a run that
        # replaced the source finds its own work by comparing the row against
        # the movie's path - not against a per-run staging directory, which
        # never matched anything and made every in-place batch re-encode.
        recorded = db.get_latest_encode_output(connection, int(file_row["id"]))
        output_file = recorded if recorded is not None else output_dir / movie.name
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
    connection: sqlite3.Connection,
    movie: Path,
    result: QualityResult,
    config: Conf,
    final_output: Path | None = None,
) -> EncodeOutcome:
    """Persists the result of one quality-mode encode (or skip).

    Args:
        connection: The database connection.
        movie (Path): The input movie file.
        result (QualityResult): The engine result.
        config (Conf): The config settings.
        final_output (Path | None): The path the artifact will occupy when the
            run replaces sources in place (issue #232) - the source's own
            path, which a successful swap gives the encoded file. The row must
            describe the artifact's REAL location: a row pointing at the
            per-run staging path made `doctor` report every in-place encode
            as vanished and broke the generation chain. None when the run
            writes into a real output directory (the engine's output path IS
            the artifact's location).

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
            source_bit_depth=result.input_bit_depth,
            source_duration=result.input_duration,
            tool_versions=result.tool_versions,
            input_bitrate=result.input_bitrate,
            output_bitrate=result.output_bitrate,
            source_file=str(movie.resolve()),
            output_file=str(final_output.resolve())
            if final_output
            else (str(result.output.resolve()) if result.output else None),
            crf_source=result.crf_source or "probe",
            plan_created_at=result.plan_created_at,
        )
        save_verification(connection, encode_id, result)
        return EncodeOutcome(movie=str(movie), encoded=True, encode_id=encode_id)
    if result.skipped_reason is not None:
        # the record-time recheck (issue #436): the skip verdict rides the
        # plans row (skipped_reason beside the fingerprint) and is trusted
        # without re-probing later, so a verdict derived from bytes-A must
        # not commit under bytes-B's identity. Compared against the result's
        # run-start facts (the same pattern as record_plan): a changed size
        # or mtime fails safe - no row, the file is left alone and re-decided
        # on the next run (the watcher's per-file failure path continues).
        stat = movie.stat()
        if (result.input_size and stat.st_size != result.input_size) or (
            getattr(result, "input_mtime", None) is not None and stat.st_mtime != result.input_mtime
        ):
            # loud abort, not log-and-continue (issue #436, invariant 5): a
            # verdict about bytes that no longer exist is not recordable; the
            # entry points catch RuntimeError and report it per file
            raise RuntimeError(
                f"Not recording the skip verdict for '{movie.name}': the file "
                "changed while the run was deciding it - the verdict is about "
                "bytes that no longer exist; the file is re-decided on the next run"
            )
        save_skip(
            connection,
            movie,
            result.skipped_reason,
            result.chosen_crf,
            result.achieved_vmaf,
        )
        return EncodeOutcome(movie=str(movie), skipped=True, error=result.error)
    return EncodeOutcome(movie=str(movie), error=result.error)


@dataclass
class DoctorFinding:
    """One health finding of the doctor check.

    Attributes:
        movie (str): The affected file path (as stored in the db), or the
            scratch workdir's path for the scratch-debris finding.
        issue (str): What is wrong (short, human-readable).
        detail (str | None): Extra context (e.g. the expected vs actual
            state, or the error ffprobe produced).
    """

    movie: str
    issue: str
    detail: str | None = None


# scratch workdir name grammars doctor recognizes: the current one (with the
# verify marker #440 added when the verification workdir needed its own floor)
# and the two historical ones (the anonymous pre-#435 name and the short-lived
# shrinkray_verify_ prefix that #435 replaced) - existing debris stays
# attributable:
SCRATCH_WORKDIR_PREFIXES = ("shrinkray-probe-", "shrinkray_verify_", "metadata_quality_")
SCRATCH_VERIFY_MARKER = "-verify-"

# a workdir younger than its floor is a LIVE run's workdir, not crash debris:
# reporting it would exit 1 on a healthy host. The floors differ by workdir kind
# because they write differently (the issue's review rounds 2-3):
# - probe dirs (a bisect): minutes of work, new dirs appear as the queue moves,
#   so one hour of silence is the conservative "nobody is coming back" line
#   (lock.py's silence-window shape).
# - verify dirs: the measurement copies the model in up front and writes
#   vmaf.json only at the END - nothing is touched in between (tools/vmaf.py),
#   so a full-file measurement of a large movie is silent for its whole run
#   (tens of minutes). Its floor is 6 h: an 18x margin over the longest
#   supported measurement, while overnight debris (the case this check exists
#   for) still reports.
SCRATCH_STALE_SECONDS = 3600.0
SCRATCH_VERIFY_STALE_SECONDS = 6 * 3600.0


def doctor(connection: sqlite3.Connection, probe_all: bool = False) -> list[DoctorFinding]:
    """Checks the library's health: db reality vs disk reality.

    Composes existing primitives into one report (B6): dead paths,
    soft-missing counters, encode rows whose output vanished, sources that
    no longer probe cleanly, orphaned outputs (an encode row's output
    file whose source row is gone), and stale probe-scratch workdirs on
    the configured scratch root (crash debris; ``stale_scratch_findings``).
    Read-only: doctor never repairs, delete-flagging stays the scan's job
    (grace period semantics).

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
    findings.extend(stale_scratch_findings())
    return findings


def scratch_root() -> Path | None:
    """Returns the scratch root the probe workdirs live under, when known.

    The probe scratch is wherever ``tempfile.mkdtemp`` puts its workdirs:
    the container's ``$TMPDIR`` (shipped: ``/tmp/shrinkray``). The same
    variables tempfile consults are honored (``TMPDIR``, then ``TEMP``,
    then ``TMP`` - Windows names TEMP); when none is set (a stateless CLI
    run on a developer host) doctor skips the check rather than scanning
    the OS temp root (issue #440's criterion 1).

    Returns:
        Path | None: The configured scratch root, or None when the
        environment does not name one.
    """
    for var in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    return None


def stale_scratch_findings(root: Path | None = None) -> list[DoctorFinding]:
    """Reports crash debris in the probe scratch: report-only, never a sweep.

    A probe run's workdir is removed in a ``finally`` (plan.py, pipeline.py),
    so a hard kill - SIGKILL, power loss, container restart - leaves it on
    the scratch forever. The debris is invisible to every db-backed finding
    class (no row ever pointed there). This check lists each stale workdir
    with its age and recursive byte weight so the operator can decide; it
    does NOT delete anything (invariant 3: destructive on user data stays
    opt-in and recoverable), and it walks only the configured scratch root -
    never the filesystem (issue #440).

    Args:
        root (Path | None): The scratch root to inspect (defaults to
            ``scratch_root()``; None or a missing root reports nothing).

    Returns:
        list[DoctorFinding]: One finding per stale workdir, empty when the
        scratch is clean or unknown.
    """
    if root is None:
        root = scratch_root()
    if root is None or not root.is_dir():
        return []
    findings: list[DoctorFinding] = []
    now = time.time()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        # the scratch volume vanished between the is_dir check and the walk
        return []
    for entry in entries:
        if entry.is_symlink():
            # never followed: a symlinked entry would make rglob walk OUT of
            # the scratch root (rglob follows directory symlinks), and this
            # report-only check reads only the configured scratch itself
            continue
        if not entry.name.startswith(SCRATCH_WORKDIR_PREFIXES) or not entry.is_dir():
            continue
        try:
            items = [entry, *entry.rglob("*")]
        except OSError:
            # the workdir vanished or is unreadable mid-report: skip it
            # (gone or unreadable is healthy for this report-only check)
            continue
        newest = 0.0
        total = 0
        for item in items:
            if item.is_symlink():
                # never followed: a symlinked member would walk OUT of the
                # scratch root (pre-3.13 rglob follows directory symlinks),
                # and this report-only check reads only the workdir itself
                continue
            try:
                item_stat = item.stat()
            except OSError:
                # a member vanishing mid-walk is skipped: the size is
                # decoration for the operator, never a decision input
                continue
            newest = max(newest, item_stat.st_mtime)
            if item.is_file():
                total += item_stat.st_size
        age_hours = max((now - newest) / 3600.0, 0.0)
        # the floor follows the workdir kind (see the constants' comment):
        # a verification's silence is the measurement running, not a death.
        # The kind is read from the grammar's verify marker - the CURRENT
        # builds' name (identity.probe_scratch_prefix(kind="verify")) and the
        # short-lived shrinkray_verify_ name both carry it
        is_verify = SCRATCH_VERIFY_MARKER in entry.name or entry.name.startswith(
            "shrinkray_verify_"
        )
        floor = SCRATCH_VERIFY_STALE_SECONDS if is_verify else SCRATCH_STALE_SECONDS
        if now - newest < floor:
            # a live run's workdir: the verification writes its one direct
            # child up front and nothing during a long measurement, so the
            # DIRECTORY's own mtime would report a still-running run as
            # debris - the age reads the NEWEST mtime under it instead
            continue
        findings.append(
            DoctorFinding(
                movie=str(entry),
                issue="stale probe scratch (crash debris?)",
                detail=f"{total} bytes, mtime {age_hours:.1f} h ago",
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
