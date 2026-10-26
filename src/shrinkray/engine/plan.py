"""plan: the stored plan's persisted shape (issue #97 phase 3, seam 4).

Moved verbatim from quality.py: the readers that rebuild a stored plan
row into live objects (the crop rect, the probe list, the fingerprint
lookup). plan_quality itself moves with the pipeline seam (it returns
QualityResult, which lands in pipeline).
"""

import json
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from shrinkray.config import DEFAULT_MIN_SOURCE_HEIGHT, DEFAULT_MIN_SOURCE_WIDTH
from shrinkray.engine.crop import (
    DEFAULT_CROP_SAMPLE_COUNT,
    detect_crop_rect,
    detect_crop_summary_cached,
)
from shrinkray.engine.encoders import (
    DEFAULT_ENCODER,
    DEFAULT_HDR_TARGET_VMAF,
    encoder_for,
)
from shrinkray.engine.samples import (
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SAMPLE_EVERY,
    DEFAULT_SAMPLE_SECONDS,
    ProgressCallback,
    extract_samples,
)
from shrinkray.engine.search import (
    DEFAULT_CRF_RANGE,
    DEFAULT_HDR_CRF_RANGE,
    DEFAULT_TARGET_VMAF,
    VMAF_STATISTIC,
    SampleProbe,
    estimate_size_factor,
    find_optimal_crf_and_skip,
    sample_count_for,
)
from shrinkray.logger import log
from shrinkray.tools.ffprobe import get_data, probe_duration_seconds
from shrinkray.tools.handbrake import HANDBRAKE_BINARY, find_handbrake

# the codec gate: which source codecs are candidates at all (issue #21).
# One whitelist, spelled out: an operator's config extends it (candidate_codecs),
# never replaces it - the product rules, not the config file, own the floor.
# VP9 is newer than H.264 in every sense that matters here (efficiency, decoder
# support) and was excluded only because the old two-list shape predates the
# decision that made this a whitelist (issue #233).
SOURCE_CODECS = ("h264", "hevc", "vp9", "av1")
CANDIDATE_CODECS = SOURCE_CODECS

# files shorter than this are not treated as encodable movies
MIN_ENCODE_SECONDS = 90.0


def _plan_crop(plan: sqlite3.Row) -> tuple[int, int, int, int] | None:
    """Parses a stored plan's crop rect ('top,bottom,left,right').

    Args:
        plan (sqlite3.Row): The plan row.

    Returns:
        tuple[int, int, int, int] | None: The crop rect, or None.
    """
    raw = plan["crop"]
    if not raw:
        return None
    parts = [int(v) for v in str(raw).split(",")]
    if len(parts) != 4:
        return None
    return (parts[0], parts[1], parts[2], parts[3])


def _plan_probes(plan: sqlite3.Row) -> list[SampleProbe]:
    """Restores a stored plan's probe list from its JSON column.

    Args:
        plan (sqlite3.Row): The plan row.

    Returns:
        list[SampleProbe]: The probes that backed the plan's decision.
    """
    try:
        pairs = json.loads(plan["probes"] or "[]")
    except (TypeError, ValueError):
        return []
    return [SampleProbe(crf=int(c), vmaf=float(v)) for c, v in pairs]


def _lookup_plan(
    db_file: Path,
    input_file: Path,
    *,
    encoder: str,
    encoder_preset: str,
    target_vmaf: float,
    crf_range: tuple[int, int],
    sample_count: int,
    sample_seconds: float,
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT,
) -> sqlite3.Row | None:
    """Consults the plan cache for a valid plan of this movie+settings.

    Opens a short-lived read connection (the engine stays connection-free;
    db_file already locates the logs dir, so it already knows the db).
    Any mismatch, missing file row or vanished movie returns None — the
    caller re-probes, never guesses.

    Args:
        db_file (Path): The database file.
        input_file (Path): The movie to encode.
        encoder (str): The pending encode's effective encoder.
        encoder_preset (str): The pending encode's preset.
        target_vmaf (float): The pending encode's (HDR-overridden) target.
        crf_range (tuple[int, int]): The pending encode's (HDR-overridden) range.
        sample_count (int): The pending encode's sample count.
        sample_seconds (float): The pending encode's sample length.
        crop_sample_count (int): The pending encode's crop sample count.

    Returns:
        sqlite3.Row | None: The valid plan row, or None.
    """
    from shrinkray import db as _db

    if not input_file.is_file():
        return None
    connection = _db.connect(db_file)
    try:
        return _db.get_valid_plan(
            connection,
            input_file,
            encoder=encoder,
            encoder_preset=encoder_preset,
            target_vmaf=target_vmaf,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            crop_sample_count=crop_sample_count,
            vmaf_statistic=VMAF_STATISTIC,
        )
    finally:
        connection.close()


def source_codec(movie: Path) -> str | None:
    """Reads the source's video codec name for the codec gate.

    The value is ffprobe's own codec_name (h264, hevc, mpeg2video, vc1,
    av1, ...), so the gate compares against what ffprobe reports rather
    than a normalized spelling.

    Returns None when the codec cannot be determined, so the gate can treat
    "unknown" as "do not skip": an unreadable codec must never silently
    exclude a file the operator expected to be considered.

    Args:
        movie (Path): The movie file.

    Returns:
        str | None: The video stream's codec name, or None.
    """
    from shrinkray.config import load_config

    try:
        data = get_data(movie, load_config())
    except (RuntimeError, OSError, ValueError):
        return None
    codec = data.get("codec")
    if codec is None:
        return None
    return str(codec)


def _source_dimensions(movie: Path) -> dict | None:
    """Reads the source's width/height once, for both resolution helpers.

    Args:
        movie (Path): The movie file.

    Returns:
        dict | None: The dimensions dict ("width"/"height" keys), or None
            when the probe fails.
    """
    from shrinkray.config import load_config

    try:
        data = get_data(movie, load_config())
    except (RuntimeError, OSError, ValueError):
        return None
    dimensions = data.get("dimensions") or {}
    return dimensions or None


def is_1080p_class(
    width: int | None,
    height: int | None,
    min_width: int | None = None,
    min_height: int | None = None,
) -> bool:
    """Decides whether a resolution is at least the 1080p class (issue #233).

    The product rule is "1080p", not "1080 tall": a 2.39:1 scope film is a
    1080p-class master at 1920x800, and a height-only floor skipped exactly
    the widescreen sources this tool exists to shrink. Either dimension at
    class size qualifies. The floors default to the product's (1920/1080) and
    may be raised by a caller; they can never be lowered below it, which the
    config validators enforce and this signature cannot bypass.

    Args:
        width (int | None): The source's width in pixels (None when unknown).
        height (int | None): The source's height in pixels (None when unknown).
        min_width (int | None): The width floor to apply (None: the product's).
        min_height (int | None): The height floor to apply (None: the product's).

    Returns:
        bool: True when either dimension meets the class floor.
    """
    width_floor = (
        DEFAULT_MIN_SOURCE_WIDTH if min_width is None else max(min_width, DEFAULT_MIN_SOURCE_WIDTH)
    )
    height_floor = (
        DEFAULT_MIN_SOURCE_HEIGHT
        if min_height is None
        else max(min_height, DEFAULT_MIN_SOURCE_HEIGHT)
    )
    if width is not None and width >= width_floor:
        return True
    return height is not None and height >= height_floor


def is_hdr_file(movie: Path) -> bool:
    """Checks whether a movie file is HDR.

    Reuses the scan's detection (media.get_data computes the hdr flag from
    the configured codec.hdr markers) so both paths always agree.

    Args:
        movie (Path): The movie file.

    Returns:
        bool: True if the video stream is detected as HDR.
    """
    from shrinkray.config import load_config

    try:
        data = get_data(movie, load_config())
    except (RuntimeError, OSError, ValueError):
        return False
    return bool(data.get("hdr"))


def source_bit_depth(movie: Path) -> int | None:
    """Reads the source's bit depth for the encoder choice (issue #234).

    Reuses the scan's probe (get_data computes bit_depth from
    bits_per_raw_sample / pix_fmt) so both paths always agree. None when
    the depth cannot be determined — the encoder choice then falls back to
    the HDR answer instead of silently truncating an unreadable depth.

    Args:
        movie (Path): The movie file.

    Returns:
        int | None: The video stream's bit depth, or None.
    """
    from shrinkray.config import load_config

    try:
        data = get_data(movie, load_config())
    except (RuntimeError, OSError, ValueError):
        return None
    return data.get("bit_depth")


@dataclass
class QualityResult:
    """Result of a quality-optimized encode.

    Attributes:
        input (Path): The input file.
        output (Path): The output file.
        success (bool): True if the encode succeeded.
        target_vmaf (float): The effective VMAF quality floor that was met:
            the requested one, or the HDR override when the movie is HDR.
        chosen_crf (int | None): The CRF selected by the bisect.
        achieved_vmaf (float | None): The mean per-sample VMAF measured at
            the chosen CRF (None if no probe was possible). Not the worst
            window: a mean is robust against a single pathological sample,
            which is why the decision statistic is the mean (issue #15).
        probes (list[SampleProbe]): All CRF probes performed during the search.
        input_size (int): Size of the input file in bytes.
        output_size (int): Size of the output file in bytes.
        error (str | None): The error message if the encode failed.
        input_bit_depth (int | None): The source's bit depth (issue #234).
    """

    input: Path
    output: Path
    success: bool
    target_vmaf: float = DEFAULT_TARGET_VMAF
    chosen_crf: int | None = None
    achieved_vmaf: float | None = None
    probes: list[SampleProbe] = field(default_factory=list)
    input_size: int = 0
    output_size: int = 0
    error: str | None = None
    skipped_reason: str | None = None
    encoder_preset: str | None = None
    probe_encoder_preset: str | None = None
    encode_seconds: float | None = None
    probe_seconds: float | None = None
    verified_vmaf: float | None = None
    verified_subsample: int | None = None
    hdr: bool = False
    tool_versions: str | None = None
    input_width: int | None = None
    input_height: int | None = None
    input_codec: str | None = None
    input_duration: float | None = None
    input_bitrate: float | None = None
    output_bitrate: float | None = None
    input_bit_depth: int | None = None
    # the probing pipeline's effective decision inputs (fingerprint of a
    # stored plan): HDR overrides target/range before probing, so these
    # carry the OVERRIDDEN values
    crop: tuple[int, int, int, int] | None = None
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE
    sample_count: int = DEFAULT_SAMPLE_COUNT
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT
    # which aggregation produced achieved_vmaf: "mean" is the decision
    # statistic (issue #15), "min" marks a plan measured before it
    vmaf_statistic: str = "mean"
    # provenance of the CRF decision ('plan' when reused from a stored
    # plan, 'probe' when measured now, None when not applicable)
    crf_source: str | None = None
    plan_created_at: str | None = None
    # the encoder the decision was made against: the requested one, or the
    # HDR override (svt_av1_10bit) - one source of truth for the record and
    # the plan cache (a plan of an HDR movie is only valid under 10-bit)
    encoder: str = DEFAULT_ENCODER

    @property
    def saved_percent(self) -> float:
        """Percentage of disk space saved by the encode."""
        if self.input_size <= 0 or self.output_size <= 0:
            return 0.0
        return round((1 - self.output_size / self.input_size) * 100, 2)


def plan_quality(
    input_file: Path,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,
    sample_count: int | None = None,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    sample_every: float | None = DEFAULT_SAMPLE_EVERY,
    encoder: str = "svt_av1",
    probe_encoder_preset: str = "5",
    encoder_preset: str = "3",
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
    samples: list[tuple[Path, float]] | None = None,
    min_source_height: int = DEFAULT_MIN_SOURCE_HEIGHT,
    min_source_width: int = DEFAULT_MIN_SOURCE_WIDTH,
    min_source_size_gb: float = 0.0,
    candidate_codecs: tuple[str, ...] = CANDIDATE_CODECS,
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT,
) -> QualityResult:
    """Probes a movie and predicts the outcome without encoding it.

    Runs the exact same probing pipeline as encode_with_quality (samples,
    size estimate, CRF bisect) but stops before the full encode. The result
    carries the predicted CRF, the measured VMAF and a size estimate based
    on the sample probes.

    Args:
        input_file (Path): The file to analyze.
        target_vmaf (float): The VMAF quality floor to meet.
        crf_range (tuple[int, int]): The (lowest, highest) CRF to search.
        sample_count (int): The number of samples to probe.
        sample_seconds (float): The length of each sample in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'. for HDR the engine
            switches to the 10-bit variant and the result carries it.
        probe_encoder_preset (str): The PROBE's encoder speed preset (a
            measurement setting, issue #22); the plan's fingerprint keys
            on this value.
        encoder_preset (str): The FINAL encode's speed preset (an output
            setting, issue #22); plan never encodes, so this is recorded
            on the result for provenance only.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file, used to locate the logs
            directory for failure evidence (None => cwd metadata.db).
        encoder_threads (int | None): Thread limit for the probe encodes
            (None = no limit; must match a later encode's cap).
        progress (callable | None): Callback progress(dict) for live step
            updates of the probing pipeline (flow to the UI, never
            persisted — the same events the console prints as log lines).
        min_source_height (int): Skip sources whose height AND width are both
            below the 1080p-class floors (1080/1920 by default; a caller may
            raise them, never lower them). Either dimension at class size
            qualifies: a 1920x800 scope film is a 1080p-class master
            (issue #233). A lower-resolution source has too few pixels to
            save meaningfully, so the probe cost buys nothing.
        min_source_size_gb (float): Skip sources below this size (0 = gate
            off). Probe cost is roughly constant per file while the payoff
            scales with size, so the smallest files have the worst
            cost/benefit ratio.
        candidate_codecs (tuple[str, ...]): Source codecs accepted beyond the
            product whitelist SOURCE_CODECS (the config's extras). The gate
            always applies the whitelist too, so this extends it, never
            replaces it (issue #271).
        crop_sample_count (int): cropdetect windows per movie. More
            samples can only shrink the crop once the bars are the ones
            common to every sample, the safe direction for an
            irreversible crop.

    Returns:
        QualityResult: The prediction (success=False with a skipped_reason
            for skip decisions; input/output sizes carry the size estimate
            as output_size = input_size * estimated factor).

    Raises:
        FileNotFoundError: If the input file does not exist.
        RuntimeError: If HandBrakeCLI is not installed or probing failed.
        Cancelled: When the cancel event fires.
    """
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file '{input_file}' does not exist!")

    hdr = is_hdr_file(input_file)
    # the effective decision inputs: for HDR the engine overrides the
    # target/range and switches to the 10-bit encoder — every downstream
    # call and the result's fingerprint fields carry these OVERRIDDEN values
    # The encoder follows the SOURCE's bit depth now (issue #234): a 10-bit
    # SDR master keeps its depth; HDR alone no longer decides it.
    effective_encoder = encoder_for(source_bit_depth(input_file), hdr)
    if hdr:
        # the tone-mapped measurement scale sits slightly lower; use the
        # calibrated target and the deeper CRF range for 4K headroom
        target_vmaf = DEFAULT_HDR_TARGET_VMAF
        crf_range = DEFAULT_HDR_CRF_RANGE

    duration = probe_duration_seconds(input_file)
    sample_count = sample_count_for(duration, sample_every, sample_count)
    if duration is not None and duration < MIN_ENCODE_SECONDS:
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            error=f"duration {duration:.0f}s is below the {MIN_ENCODE_SECONDS:.0f}s minimum",
            skipped_reason="too short",
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )

    dimensions = _source_dimensions(input_file)
    width = (dimensions or {}).get("width")
    height = (dimensions or {}).get("height")
    known_below_class = (width is not None or height is not None) and not is_1080p_class(
        width, height, min_width=min_source_width, min_height=min_source_height
    )
    if known_below_class:
        # at least one dimension was read and neither reaches the class floor;
        # an unreadable probe stays eligible (the "unknown is not a skip" rule).
        # The message names the caller's floors, not the product defaults: an
        # operator who raised them to 4K must read "4K" in the skip reason.
        shown = (
            f"{width}x{height}"
            if width is not None and height is not None
            else (f"width {width}px" if width is not None else f"height {height}px")
        )
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"resolution {shown} is below the {min_source_width}x"
                f"{min_source_height} 1080p class minimum "
                "(a lower-resolution source saves too little to be worth the probe)"
            ),
            skipped_reason="resolution too low",
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )

    # the size gate is the cheapest of the remaining checks: it reads one
    # stat, and probe cost is roughly constant while the payoff scales with
    # the file's size
    size_gb = input_file.stat().st_size / (1024**3)
    if min_source_size_gb > 0 and size_gb < min_source_size_gb:
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"size {size_gb:.2f} GB is below the {min_source_size_gb:.2f} GB "
                "minimum (too little to save for the probe cost)"
            ),
            skipped_reason="below min size",
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )

    # every eligible file is probed; whether to encode is decided by size
    # alone. The codec whitelist below is the only codec rule left: a source
    # codec outside it is never re-encoded, and the operator's config extends
    # the product list but never shrinks it (docs/encode-decision.md). An
    # unknown codec is probed, never excluded - an unreadable value must not
    # silently drop a file.
    codec = source_codec(input_file)

    allowed = allowed_codecs(candidate_codecs)
    if codec is not None and codec not in allowed:
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"source codec '{codec}' is not in the candidate codec list "
                f"({', '.join(allowed)}) - a source outside it is "
                "never re-encoded"
            ),
            skipped_reason="source codec not a candidate",
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )

    handbrake = find_handbrake()
    if handbrake is None:
        raise RuntimeError(
            f"'{HANDBRAKE_BINARY}' not found! Install HandBrake CLI "
            "(https://handbrake.fr/docs/en/latest/cli/cli-options.html)"
        )

    workdir = Path(tempfile.mkdtemp(prefix="metadata_quality_"))
    try:
        # bar detection is part of every re-encode, never optional: a re-encode
        # is the only moment the picture can be corrected for free, and keeping
        # bars in a smaller file is strictly worse than the source
        # (docs/encode-decision.md). No bars is a normal outcome, not a skip.
        # The timeline detector is the one bar measurement (issue #181): it is
        # cached per file version in the db, so the keyframe-only pass runs
        # once per file. Without a database (stateless runs) the sampled
        # fallback keeps bar detection working.
        summary = detect_crop_summary_cached(input_file, db_file, cancel=cancel)
        if summary is None or summary.bars is None:
            crop = detect_crop_rect(input_file, workdir, count=crop_sample_count, cancel=cancel)
        else:
            crop = summary.bars
        log(f"Probing '{input_file.name}' (target VMAF {target_vmaf})...")
        # extract the windows once and hand the same list to the search and
        # the reported size factor: each extraction decodes and writes every
        # sample, so doing it per phase encoded each window three times
        samples = [
            (s.resolve(), offset)
            for s, offset in extract_samples(
                input_file, workdir, sample_count, sample_seconds, cancel=cancel, progress=progress
            )
        ]
        crf, achieved, probes, skip = find_optimal_crf_and_skip(
            input_file,
            workdir,
            handbrake,
            target_vmaf=target_vmaf,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            encoder=effective_encoder,
            probe_encoder_preset=probe_encoder_preset,
            crop=crop,
            hdr=hdr,
            cancel=cancel,
            db_file=db_file,
            encoder_threads=encoder_threads,
            progress=progress,
            use_probe_cache=use_probe_cache,
            samples=samples,
        )

        input_size = input_file.stat().st_size
        factor = estimate_size_factor(
            samples,
            workdir,
            handbrake,
            crf,
            input_file,
            sample_seconds,
            effective_encoder,
            probe_encoder_preset,
            crop=crop,
            cancel=cancel,
            db_file=db_file,
            encoder_threads=encoder_threads,
            progress=progress,
            use_probe_cache=use_probe_cache,
        )

        if skip:
            return QualityResult(
                input=input_file,
                output=input_file,
                success=False,
                target_vmaf=target_vmaf,
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probes=probes,
                input_size=input_size,
                output_size=input_size,
                error="skipped: re-encode would not shrink the file",
                skipped_reason="no size gain",
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crop=crop,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                hdr=hdr,
                encoder=effective_encoder,
            )
        if achieved < target_vmaf and crf == crf_range[0]:
            return QualityResult(
                input=input_file,
                output=input_file,
                success=False,
                target_vmaf=target_vmaf,
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probes=probes,
                input_size=input_size,
                output_size=input_size,
                error=(
                    f"skipped: target VMAF {target_vmaf} unreachable "
                    f"(best {achieved} at CRF {crf_range[0]})"
                ),
                skipped_reason="target unreachable",
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crop=crop,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                hdr=hdr,
                encoder=effective_encoder,
            )

        return QualityResult(
            input=input_file,
            output=input_file,
            success=True,
            target_vmaf=target_vmaf,
            chosen_crf=crf,
            achieved_vmaf=achieved,
            probes=probes,
            input_size=input_size,
            output_size=int(input_size * factor),
            error=None,
            encoder_preset=encoder_preset,
            probe_encoder_preset=probe_encoder_preset,
            crop=crop,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def allowed_codecs(extra: tuple[str, ...]) -> tuple[str, ...]:
    """Returns the codec whitelist with an operator's extras appended.

    The gate itself applies the product whitelist, so a caller can only ever
    extend it: passing the config's `candidate_codecs` (the operator's EXTRA
    codecs, empty by default) once replaced the whole list and made every
    source ineligible (issue #271).

    Args:
        extra (tuple[str, ...]): Codecs to accept beyond SOURCE_CODECS (a list
            that repeats the whitelist is fine).

    Returns:
        tuple[str, ...]: SOURCE_CODECS followed by the extras not already in it.
    """
    return SOURCE_CODECS + tuple(c for c in dict.fromkeys(extra) if c not in SOURCE_CODECS)
