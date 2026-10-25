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

# the codec gate: which source codecs are candidates at all (issue #21)
CANDIDATE_CODECS = ("h264",)
MODERN_CODECS = ("hevc", "av1")
MIN_SOURCE_CODEC = "h264"

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


def source_height(movie: Path) -> int | None:
    """Reads the source video height for the resolution gate.

    Returns None when the height cannot be determined, so the caller can treat
    "unknown" as "do not skip": an unreadable height must never silently
    exclude a file the operator expected to be considered.

    Args:
        movie (Path): The movie file.

    Returns:
        int | None: The video stream's height in pixels, or None.
    """
    from shrinkray.config import load_config

    try:
        data = get_data(movie, load_config())
    except (RuntimeError, OSError, ValueError):
        return None
    dimensions = data.get("dimensions") or {}
    height = dimensions.get("height")
    if height is None:
        return None
    return int(height)


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
    min_source_height: int = 0,
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
        min_source_height (int): Skip sources below this height (0 = gate
            off). A lower-resolution source has too few pixels to save
            meaningfully, so the probe cost buys nothing.
        min_source_size_gb (float): Skip sources below this size (0 = gate
            off). Probe cost is roughly constant per file while the payoff
            scales with size, so the smallest files have the worst
            cost/benefit ratio.
        candidate_codecs (tuple[str, ...]): Source codecs that are candidates
            on size alone. A modern codec (HEVC/AV1) is already efficient,
            so it is a candidate only when it has bars to remove; anything
            older is never re-encoded.
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
    effective_encoder = encoder_for(hdr)
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

    height = source_height(input_file)
    if height is not None and height < min_source_height:
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"height {height}p is below the {min_source_height}p minimum "
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
    # alone. The codec-age gate below is the only codec rule left: an older
    # codec is never re-encoded, while H.264 and newer all take the same path
    # (docs/encode-decision.md). An unknown codec is probed, never excluded -
    # an unreadable value must not silently drop a file.
    codec = source_codec(input_file)

    if codec is not None and codec not in candidate_codecs and codec not in MODERN_CODECS:
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"source codec '{codec}' is below the {MIN_SOURCE_CODEC} minimum "
                "(an older codec is not re-encoded)"
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
        summary = detect_crop_summary_cached(input_file, db_file)
        if summary is None or summary.bars is None:
            crop = detect_crop_rect(input_file, workdir, count=crop_sample_count)
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
