"""search: the CRF search engine (issue #97 phase 3, seam 3).

Moved verbatim from quality.py: the probe CACHE (content-addressed probe
measurements), the sample planning helper, the bracketing CRF bisect, the
per-CRF probe (mean-of-means decision statistic), the size-estimate probe
and the two search drivers (find_optimal_crf, find_optimal_crf_and_skip).
SampleProbe carries one probe's measurement; SkipEncode carries a decided
skip's evidence.

The engine decides "is this file bigger than its quality requires?" —
this module IS that question's machinery.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shrinkray.engine.samples import (
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SAMPLE_EVERY,
    DEFAULT_SAMPLE_SECONDS,
    ProgressCallback,
    _check_cancel,
    extract_samples,
)
from shrinkray.logger import log
from shrinkray.tools.ffprobe import probe_duration_seconds
from shrinkray.tools.handbrake import build_encode_command
from shrinkray.tools.process import Cancelled, _run
from shrinkray.tools.vmaf import _tool_versions, measure_vmaf

FFMPEG_BINARY = "ffmpeg"

# the decision statistic (issue #15): the mean of the per-sample
# VMAF means, not the minimum. A minimum is monotone non-increasing in
# the sample count, which punishes denser sampling (#14).
VMAF_STATISTIC = "mean"

# VMAF 96 is the decided product default (Netflix's 'visually transparent'
# zone; 93 is their common top-tier floor). VMAF falls monotonically with
# rising CRF, so a higher target means a larger file.
DEFAULT_TARGET_VMAF = 96.0
# SVT-AV1 publishes 0-70 at 0.25 granularity (HandBrake's
# hb_video_quality_get_limits), a wider and finer scale than x265's.
# These bounds are NOT calibrated for SVT-AV1 yet: they are the
# x265-era ranges carried over, and the issue requires a measured
# SVT-AV1 CRF-to-VMAF curve on 3-5 titles before they can be
# trusted. The search clamps to this band, so an over-wide range only
# costs probes - it cannot make the decision wrong in the safe direction.
DEFAULT_CRF_RANGE = (14, 45)
DEFAULT_HDR_CRF_RANGE = (10, 45)


@dataclass
class SampleProbe:
    """One CRF probe: the measured quality across all samples."""

    crf: int
    vmaf: float


SAMPLE_EXTRACTION_TAG = "x264-crf12-veryfast"


def probe_cache_key(
    movie: Path,
    offset: float,
    seconds: float,
    crf: int,
    encoder: str,
    encoder_preset: str,
    crop: tuple[int, int, int, int] | None,
    hdr: bool,
    encoder_threads: int | None,
    tool_versions: str,
    kind: str = "vmaf",
) -> str:
    """Computes the content-addressed key of one probe measurement.

    Every input that can change the probe's encoded bytes or its VMAF
    score belongs in the key: the sample's content identity (source size
    + mtime, window offset/length, extraction recipe), the encode
    (encoder, preset, CRF, crop rect, thread cap) and the measurement
    (HDR tone-mapping, tool versions). The source PATH is deliberately
    not part of the key: a moved-but-unmodified file's probes stay valid
    (a move preserves mtime and size).

    Args:
        movie (Path): The source movie file (identity via size + mtime).
        offset (float): The sample window's start offset in seconds.
        seconds (float): The sample window's length in seconds.
        crf (int): The probed CRF.
        encoder (str): The effective video encoder.
        encoder_preset (str): The PROBE's speed preset (a measurement
            setting, issue #22).
        hdr (bool): Whether the VMAF measurement tone-maps both sides.
        encoder_threads (int | None): The thread cap of the probe encode.
        tool_versions (str): The tool version string (HandBrake, libvmaf).
        kind (str): 'vmaf' (a VMAF score) or 'size' (an encoded-size ratio).

    Returns:
        str: The sha256 hex digest of the joined key inputs.
    """
    stat = movie.stat()
    parts = [
        kind,
        str(stat.st_size),
        f"{stat.st_mtime:.6f}",
        f"{offset:.3f}",
        f"{seconds:.3f}",
        SAMPLE_EXTRACTION_TAG,
        crf,
        encoder,
        encoder_preset,
        "none" if crop is None else ",".join(str(v) for v in crop),
        "hdr" if hdr else "sdr",
        "threads:" + ("all" if encoder_threads is None else str(encoder_threads)),
        tool_versions,
    ]
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _cached_or_none(
    db_file: Path | None, key: str
) -> tuple[float | None, sqlite3.Connection | None]:
    """Reads a probe-cache entry, returning the connection to keep it open on.

    Opening one short-lived connection per probe is cheap (WAL, single
    statements) and keeps probe_crf/estimate_size_factor connection-free
    like the rest of the engine. Returns (None, None) without a db_file:
    probing then runs uncached (CLI one-shots without a database).

    Args:
        db_file (Path | None): The database file (None => no caching).
        key (str): The probe cache key.

    Returns:
        tuple: The cached value (None on miss) and the open connection
            to hand to the cache write, or (None, None).
    """
    if db_file is None:
        return None, None
    from shrinkray import db as _db

    connection = _db.connect(db_file)
    return _db.get_probe_score(connection, key), connection


def _store_probe(connection: sqlite3.Connection | None, key: str, value: float) -> None:
    """Stores one probe measurement (best-effort commit via db layer)."""
    if connection is None:
        return
    from shrinkray import db as _db

    _db.save_probe_score(connection, key, value)
    connection.close()


def sample_count_for(
    duration: float | None,
    sample_every: float | None,
    sample_count: int | None = None,
) -> int:
    """Derives the probe window count from the runtime and the density.

    A count-based knob makes coverage inverse to runtime: the longer the movie,
    the more scenes it has and the less of it gets probed. Deriving the count
    from `sample_every` keeps the probed fraction fixed at
    `sample_seconds / sample_every` regardless of length, which is the
    "consistent experience across the file" property worth having.

    An explicit `sample_count` wins when set, so a latency-sensitive caller
    (watch mode) can still pin a fixed probe budget.

    Args:
        duration (float | None): The movie's runtime in seconds.
        sample_every (float | None): Seconds of runtime per probe window
            (None or 0 disables the density knob).
        sample_count (int | None): An explicit count that overrides the density.

    Returns:
        int: The number of probe windows (at least 1).
    """
    if sample_count is not None and sample_count > 0:
        return sample_count
    if duration is None or duration <= 0 or not sample_every or sample_every <= 0:
        return DEFAULT_SAMPLE_COUNT
    return max(1, math.ceil(duration / sample_every))


def bisect_crf(score: Callable[[int], float], lo: int, hi: int, target_vmaf: float) -> int:
    """Finds the largest CRF whose probe still meets the quality target.

    The search is a bracketing interpolation (issue #23, the shape ab-av1's
    crf_search uses): each round measures a CRF between the current passing
    and failing brackets, guessed by linear interpolation in VMAF space
    (a typical VMAF curve reaches the target in fewer probes than a plain
    midpoint bisect). Two safeguards keep the worst case at the bisect's:

    - the guess is clamped strictly inside the bracket (interpolation can
      point outside on near-flat curves),
    - the guess must shrink the bracket by at least half — a strongly
      convex/step-like curve would make regula falsi crawl, so the search
      falls back to the midpoint when interpolation would not.

    Args:
        score (callable): Measurement score(crf) => float, the probe's VMAF
            at that CRF. Must be monotonic: high at low CRF, low at high
            CRF. Every CRF is measured at most once (cacheable inside the
            callable).
        lo (int): The lowest CRF to consider. Failing here is the "target
            unreachable" signal (callers skip the movie on it).
        hi (int): The highest CRF to consider (exclusive, never probed).

    Returns:
        int: The largest passing CRF in [lo, hi), or lo if even lo fails
            (the caller should use it anyway as the best available quality).
    """
    vmaf_lo = score(lo)
    if vmaf_lo < target_vmaf:
        return lo

    vmaf_hi: float | None = None  # measured at the current failing bound
    while hi - lo > 1:
        if vmaf_hi is None:
            # first round: no failing measurement yet — bisect (ab-av1's
            # first guess is the midpoint for the same reason)
            guess = (lo + hi) // 2
        else:
            # linear interpolation between the passing (vmaf_lo at lo) and
            # failing (vmaf_hi at hi) ends, in VMAF space; a flat segment
            # (equal scores) gives no direction => midpoint
            span = vmaf_lo - vmaf_hi
            if span > 0:
                frac = (vmaf_lo - target_vmaf) / span
                guess = round(lo + (hi - lo) * frac)
            else:
                guess = (lo + hi) // 2
            # safeguard: strictly inside the bracket, and the surviving
            # side after this probe must be no larger than bisection's
            # (ceil(w/2)) — on a convex or step-like curve regula falsi
            # crawls (its guesses hug the passing edge), so fall back to
            # the midpoint whenever interpolation would shrink the bracket
            # more slowly than the plain bisect would from here
            guess = max(lo + 1, min(hi - 1, guess))
            if max(guess - lo, hi - guess) > (hi - lo + 1) // 2:
                guess = (lo + hi) // 2
        value = score(guess)
        if value >= target_vmaf:
            lo, vmaf_lo = guess, value
        else:
            hi, vmaf_hi = guess, value
    return lo


def probe_crf(
    samples: list[tuple[Path, float]],
    crf: int,
    workdir: Path,
    handbrake: str,
    movie: Path,
    sample_seconds: float,
    encoder: str,
    probe_encoder_preset: str,
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    use_probe_cache: bool = True,
) -> float:
    """Encodes all samples at one CRF and returns their mean VMAF score.

    The MEAN of the per-sample means governs the decision. A minimum over
    samples is monotone non-increasing in the sample count: every window
    added can only lower it or leave it unchanged, which makes it
    structurally hostile to denser sampling (#14) - the better the
    coverage, the more likely one pathological window sinks the whole
    score and the film is reported 'unreachable' when it is encodable.

    What the mean gives up: 'every probed window meets the target' is no
    longer literally true. Denser sampling reduces how likely a genuinely
    bad scene hides behind good ones, but does not eliminate it.

    Per-sample measurements are cached (probe_cache table, content-addressed
    via probe_cache_key): a sample whose identity, encode settings and
    measurement mode all match a previous run is reused instead of being
    re-encoded. A cache hit IS a fresh measurement of unchanged inputs.

    Args:
        samples (list[tuple[Path, float]]): The sample files with their
            start offsets in seconds.
        crf (int): The CRF quality value to probe.
        workdir (Path): Working directory for probe files.
        handbrake (str): Path to the HandBrakeCLI binary.
        movie (Path): The source movie (cache identity: size + mtime).
        sample_seconds (float): The sample window's length in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's encoder speed preset (a
            measurement setting; the final encode's preset is a separate
            value, issue #22).
        crop (tuple | None): Crop bars applied to probes and measurements,
            so the decision matches the final encode.
        hdr (bool): Tone-map both sides for the VMAF measurement.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file (caching and failure
            evidence; None => uncached probing).
        encoder_threads (int | None): Thread limit for the probe encodes
            (None = no limit; must match the final encode's cap).
        use_probe_cache (bool): False bypasses the cache entirely
            (force_reprobe: the caller asked to measure everything fresh).

    Returns:
        float: The mean per-sample VMAF mean at this CRF.

    Raises:
        RuntimeError: If a sample encode or VMAF measurement failed.
        Cancelled: When the cancel event fires.
    """
    tool_versions = _tool_versions(handbrake)
    scores: list[float] = []
    for index, (sample, offset) in enumerate(samples):
        _check_cancel(cancel)
        key = probe_cache_key(
            movie,
            offset,
            sample_seconds,
            crf,
            encoder,
            probe_encoder_preset,
            crop,
            hdr,
            encoder_threads,
            tool_versions,
        )
        cached, connection = (None, None)
        if use_probe_cache:
            cached, connection = _cached_or_none(db_file, key)
        if cached is not None:
            log(f"probe cache hit (CRF {crf}, sample {index + 1}/{len(samples)})")
            scores.append(cached)
            if connection is not None:
                connection.close()
            continue
        encoded = workdir / f"probe_crf{crf}_{index}.mkv"
        command = build_encode_command(
            handbrake,
            sample,
            encoded,
            crf,
            encoder,
            probe_encoder_preset,
            crop=crop,
            encoder_threads=encoder_threads,
        )
        code, err = _run(command, cancel=cancel)
        if code == -1:
            if connection is not None:
                connection.close()
            raise Cancelled("cancelled during probe encode")
        if code != 0 or not encoded.is_file() or encoded.stat().st_size == 0:
            if connection is not None:
                connection.close()
            raise RuntimeError(f"probe encode failed (crf {crf}): {err[-400:]}")
        _check_cancel(cancel)
        score = measure_vmaf(
            encoded, sample, workdir, crop=crop, hdr=hdr, cancel=cancel, db_file=db_file
        )
        _store_probe(connection, key, score)
        encoded.unlink()
        scores.append(score)
    return sum(scores) / len(scores)


def estimate_size_factor(
    samples: list[tuple[Path, float]],
    workdir: Path,
    handbrake: str,
    crf: int,
    movie: Path,
    sample_seconds: float,
    encoder: str,
    probe_encoder_preset: str,
    crop: tuple[int, int, int, int] | None = None,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
) -> float:
    """Estimates the full-encode size relative to the source from one sample.

    Encodes the first sample at the given CRF and returns the size ratio
    (encoded / source). Values >= 1 mean the re-encode would not shrink the
    file => the movie is a skip candidate.

    The ratio is a byte count, so no VMAF measurement is made here: decoding
    both sides through libvmaf is the most expensive step in the probe pipeline
    and its result would be discarded. A missing or broken libvmaf must not stop
    a size-probe skip decision; movies that reach the CRF search still fail
    loudly in `probe_crf`.

    Args:
        samples (list[tuple[Path, float]]): The sample files with their
            start offsets in seconds.
        workdir (Path): Working directory for probe files.
        handbrake (str): Path to the HandBrakeCLI binary.
        crf (int): The CRF quality value to probe.
        movie (Path): The source movie (cache identity: size + mtime).
        sample_seconds (float): The sample window's length in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's encoder speed preset (a
            measurement setting, issue #22).
        crop (tuple | None): Crop bars applied to the probe encode.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file (caching; None => uncached).
        encoder_threads (int | None): Thread limit for the probe encode.
        progress (callable | None): Callback progress(dict) for live
            step updates (flow to the UI, never persisted).
        use_probe_cache (bool): False bypasses the probe cache.

    Returns:
        float: The size ratio of the probe encode (encoded / sample source).

    Raises:
        RuntimeError: If the probe encode failed.
        Cancelled: When the cancel event fires.
    """
    _check_cancel(cancel)
    sample, offset = samples[0]
    encoded = workdir / f"size_probe_crf{crf}.mkv"
    if progress:
        progress({"stage": "probe", "detail": f"size probe (CRF {crf})"})
    # the size ratio is measured without VMAF: the measurement inputs (hdr,
    # libvmaf version) cannot change it and stay out of the key
    key = probe_cache_key(
        movie,
        offset,
        sample_seconds,
        crf,
        encoder,
        probe_encoder_preset,
        crop,
        hdr=False,
        encoder_threads=encoder_threads,
        tool_versions=_tool_versions(handbrake),
        kind="size",
    )
    cached, connection = (None, None)
    if use_probe_cache:
        cached, connection = _cached_or_none(db_file, key)
    if cached is not None:
        log(f"probe cache hit (size probe, CRF {crf})")
        if connection is not None:
            connection.close()
        return cached
    command = build_encode_command(
        handbrake,
        sample,
        encoded,
        crf,
        encoder,
        probe_encoder_preset,
        crop=crop,
        encoder_threads=encoder_threads,
    )
    code, err = _run(command, cancel=cancel)
    if code == -1:
        if connection is not None:
            connection.close()
        raise Cancelled("cancelled during size probe")
    if code != 0 or not encoded.is_file() or encoded.stat().st_size == 0:
        if connection is not None:
            connection.close()
        raise RuntimeError(f"size probe encode failed (crf {crf}): {err[-400:]}")
    try:
        factor = encoded.stat().st_size / sample.stat().st_size
    finally:
        encoded.unlink()
    _store_probe(connection, key, factor)
    return factor


def find_optimal_crf(
    movie: Path,
    workdir: Path,
    handbrake: str,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,
    sample_count: int | None = None,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    sample_every: float | None = DEFAULT_SAMPLE_EVERY,
    encoder: str = "svt_av1",
    probe_encoder_preset: str = "5",
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
    samples: list[tuple[Path, float]] | None = None,
) -> tuple[int, float, list[SampleProbe]]:
    """Finds the largest CRF that still meets the VMAF target on samples.

    Args:
        movie (Path): The source movie file.
        workdir (Path): Working directory for samples and probes.
        handbrake (str): Path to the HandBrakeCLI binary.
        target_vmaf (float): The VMAF quality floor to meet.
        crf_range (tuple[int, int]): The (lowest, highest) CRF to search.
        sample_count (int): The number of samples to probe.
        sample_seconds (float): The length of each sample in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's encoder speed preset (a
            measurement setting, issue #22).
        crop (tuple | None): Crop bars (top, bottom, left, right) applied to
            probes and the decision, matching the final encode.
        hdr (bool): Tone-map both sides for the VMAF measurement.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file for failure-evidence logs.
        encoder_threads (int | None): Thread limit for the probe encodes
            (None = no limit; must match the final encode's cap).
        progress (callable | None): Callback progress(dict) for live
            step updates (flow to the UI, never persisted).

    Returns:
        tuple: The chosen CRF, the VMAF measured at that CRF and all probes.

    Raises:
        RuntimeError: If sampling or probing failed.
        Cancelled: When the cancel event fires.
    """
    if samples is None:
        samples = [
            (s.resolve(), offset)
            for s, offset in extract_samples(
                movie,
                workdir,
                sample_count_for(probe_duration_seconds(movie), sample_every, sample_count),
                sample_seconds,
                cancel=cancel,
                progress=progress,
            )
        ]

    probes: list[SampleProbe] = []

    def check(crf: int) -> float:
        _check_cancel(cancel)
        if progress:
            progress({"stage": "probe", "detail": f"probe encode (CRF {crf})"})
        vmaf = probe_crf(
            samples,
            crf,
            workdir,
            handbrake,
            movie,
            sample_seconds,
            encoder,
            probe_encoder_preset,
            crop=crop,
            hdr=hdr,
            cancel=cancel,
            db_file=db_file,
            encoder_threads=encoder_threads,
            use_probe_cache=use_probe_cache,
        )
        probes.append(SampleProbe(crf=crf, vmaf=round(vmaf, 2)))
        if progress:
            progress(
                {
                    "stage": "probe",
                    "detail": f"CRF {crf}: VMAF {round(vmaf, 2)}",
                    "probes_done": len(probes),
                }
            )
        return vmaf

    lo, hi = crf_range
    chosen = bisect_crf(check, lo, hi, target_vmaf)
    achieved = next(p.vmaf for p in reversed(probes) if p.crf == chosen)
    return chosen, achieved, probes


def find_optimal_crf_and_skip(
    movie: Path,
    workdir: Path,
    handbrake: str,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,
    sample_count: int | None = None,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    sample_every: float | None = DEFAULT_SAMPLE_EVERY,
    encoder: str = "svt_av1",
    probe_encoder_preset: str = "5",
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
    samples: list[tuple[Path, float]] | None = None,
) -> tuple[int, float, list[SampleProbe], bool]:
    """Finds the optimal CRF and predicts whether the encode is worthwhile.

    Before the quality bisect, a single size probe estimates whether the
    re-encode can shrink the file at all (e.g. an already-efficient HEVC
    source may grow). For such titles the expensive search is skipped.

    Args:
        movie (Path): The source movie file.
        workdir (Path): Working directory for samples and probes.
        handbrake (str): Path to the HandBrakeCLI binary.
        target_vmaf (float): The VMAF quality floor to meet.
        crf_range (tuple[int, int]): The (lowest, highest) CRF to search.
        sample_count (int): The number of samples to probe.
        sample_seconds (float): The length of each sample in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's encoder speed preset (a
            measurement setting, issue #22).
        crop (tuple | None): Crop bars (top, bottom, left, right) applied to
            probes and the decision, matching the final encode.
        hdr (bool): Tone-map both sides for the VMAF measurement.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file for failure-evidence logs.
        encoder_threads (int | None): Thread limit for the probe encodes
            (None = no limit; must match the final encode's cap).
        use_probe_cache (bool): False bypasses the probe-encode cache
            (force_reprobe: the caller asked to measure everything fresh).

    Returns:
        tuple: The chosen CRF, the VMAF at that CRF, all probes and a flag
            that is True when the movie should NOT be re-encoded.
    """
    if samples is None:
        samples = [
            (s.resolve(), offset)
            for s, offset in extract_samples(
                movie,
                workdir,
                sample_count_for(probe_duration_seconds(movie), sample_every, sample_count),
                sample_seconds,
                cancel=cancel,
                progress=progress,
            )
        ]

    # highest CRF (smallest size) of the search range as the pessimistic
    # bound: if even the most aggressive setting cannot shrink the file,
    # no CRF in the range can
    factor = estimate_size_factor(
        samples,
        workdir,
        handbrake,
        crf_range[1],
        movie,
        sample_seconds,
        encoder,
        probe_encoder_preset,
        crop=crop,
        cancel=cancel,
        db_file=db_file,
        encoder_threads=encoder_threads,
        progress=progress,
        use_probe_cache=use_probe_cache,
    )
    if factor >= 1.0:
        log(
            f"Skipping '{movie.name}': re-encode would not shrink it "
            f"(probe factor {factor:.2f} at CRF {crf_range[1]})."
        )
        return crf_range[1], 0.0, [], True

    chosen, achieved, probes = find_optimal_crf(
        movie,
        workdir,
        handbrake,
        target_vmaf=target_vmaf,
        crf_range=crf_range,
        sample_count=sample_count,
        sample_seconds=sample_seconds,
        encoder=encoder,
        probe_encoder_preset=probe_encoder_preset,
        samples=samples,
        crop=crop,
        hdr=hdr,
        cancel=cancel,
        db_file=db_file,
        encoder_threads=encoder_threads,
        progress=progress,
        use_probe_cache=use_probe_cache,
    )
    # the bound above answered "can ANY CRF in the range shrink this file?".
    # The product asks a narrower question: would THIS file, at the CRF the
    # search picked, actually get smaller? The chosen CRF sits far below the
    # range's top, so the file there is much bigger and can still grow.
    chosen_factor = check_chosen_crf_shrinks(
        samples,
        workdir,
        handbrake,
        chosen,
        movie,
        sample_seconds,
        encoder,
        probe_encoder_preset,
        crop=crop,
        cancel=cancel,
        db_file=db_file,
        encoder_threads=encoder_threads,
        progress=progress,
        use_probe_cache=use_probe_cache,
    )
    if chosen_factor >= 1.0:
        log(
            f"Skipping '{movie.name}': re-encode would not shrink it "
            f"(probe factor {chosen_factor:.2f} at the chosen CRF {chosen})."
        )
        return chosen, achieved, probes, True
    return chosen, achieved, probes, False


def check_chosen_crf_shrinks(
    samples: list[tuple[Path, float]],
    workdir: Path,
    handbrake: str,
    chosen: int,
    movie: Path,
    sample_seconds: float,
    encoder: str,
    probe_encoder_preset: str,
    crop: tuple[int, int, int, int] | None = None,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
) -> float:
    """Returns the size factor at the CRF the search actually chose.

    The top-of-range probe (crf_range[1]) is a *bound*: it answers "can any CRF
    in the range shrink this file?". It is not the question the product asks.
    crf_range[1] is far below the VMAF target, so the file there is tiny; the
    chosen CRF is much lower and the file much bigger. A title can pass the bound
    and still grow at the CRF that gets encoded.

    This costs one sample encode. It cannot reuse the search's probes: the VMAF
    cache is keyed kind="vmaf" and the size cache kind="size", so a VMAF probe's
    encoded bytes are not addressable as a size measurement. Folding the two into
    one cache entry is a separate optimization; the correctness check ships first.

    Args:
        samples (list[tuple[Path, float]]): The sample windows.
        workdir (Path): Working directory for probe files.
        handbrake (str): Path to the HandBrakeCLI binary.
        chosen (int): The CRF the search selected.
        movie (Path): The source movie (cache identity).
        sample_seconds (float): The sample window's length in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's speed preset.
        crop (tuple | None): Crop bars applied to the probe encode.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file for failure-evidence logs.
        encoder_threads (int | None): Thread limit for the probe encode.
        progress (ProgressCallback | None): Live-progress callback.
        use_probe_cache (bool): False bypasses the probe cache.

    Returns:
        float: The encoded/source size ratio at the chosen CRF.
    """
    return estimate_size_factor(
        samples,
        workdir,
        handbrake,
        chosen,
        movie,
        sample_seconds,
        encoder,
        probe_encoder_preset,
        crop=crop,
        cancel=cancel,
        db_file=db_file,
        encoder_threads=encoder_threads,
        progress=progress,
        use_probe_cache=use_probe_cache,
    )


class SkipEncode(Exception):
    """Raised when the probing decides a movie should not be encoded.

    The skip carries its evidence - the best CRF, the VMAF measured
    there and how many probes were spent - so a later run can tell
    'already probed at the floor and failed' from 'never probed'
    instead of re-deriving the same verdict every run.

    Attributes:
        chosen_crf (int | None): The best CRF the search reached.
        achieved_vmaf (float | None): The VMAF measured there.
        probe_count (int): How many CRF probes were spent.
    """

    def __init__(
        self,
        message: str,
        chosen_crf: int | None = None,
        achieved_vmaf: float | None = None,
        probe_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.chosen_crf = chosen_crf
        self.achieved_vmaf = achieved_vmaf
        self.probe_count = probe_count
