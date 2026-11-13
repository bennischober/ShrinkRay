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
    crop_bars_or_fallback,
    detect_crop_rect,
    detect_crop_summary_cached,
)
from shrinkray.engine.encoders import (
    DEFAULT_ENCODER,
    encoder_for,
)
from shrinkray.engine.identity import (
    RunIdentity,
    capture_run_identity,
    ensure_unchanged,
    probe_scratch_prefix,
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
    DEFAULT_TARGET_VMAF,
    VMAF_STATISTIC,
    SampleProbe,
    find_optimal_crf_and_skip,
    sample_count_for,
    target_unreachable,
)
from shrinkray.logger import log
from shrinkray.tools.ffprobe import get_data, probe_duration_seconds
from shrinkray.tools.handbrake import HANDBRAKE_BINARY, find_handbrake
from shrinkray.tools.vmaf import vmaf_recipe

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
    vmaf_recipe: str = "",
    min_saving_percent: int = 0,
) -> sqlite3.Row | None:
    """Consults the plan cache for a valid plan of this movie+settings.

    Opens a short-lived read connection (the engine stays connection-free;
    db_file already locates the logs dir, so it already knows the db).
    Any mismatch, missing file row or vanished movie returns None — the
    caller re-probes, never guesses. So does a database that cannot be
    opened or read (logged): the plan cache only saves time.

    Args:
        db_file (Path): The database file.
        input_file (Path): The movie to encode.
        encoder (str): The pending encode's effective encoder.
        encoder_preset (str): The pending encode's preset.
        target_vmaf (float): The encode's target VMAF.
        min_saving_percent (int): The pending encode's minimum-saving
            threshold (issue #316; measured plans only).
        crf_range (tuple[int, int]): The encode's CRF range.
        sample_count (int): The pending encode's sample count.
        sample_seconds (float): The pending encode's sample length.
        crop_sample_count (int): The pending encode's crop sample count.
        vmaf_recipe (str): The crop-independent part of the measurement
            recipe (`VmafRecipe.fingerprint`, issue #308). An empty string is
            a stored plan measured before the recipe existed - never a match.

    Returns:
        sqlite3.Row | None: The valid plan row, or None.
    """
    from shrinkray import db as _db

    if not input_file.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = _db.connect(db_file)
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
            vmaf_recipe=vmaf_recipe,
            min_saving_percent=min_saving_percent,
        )
    except (OSError, sqlite3.Error) as e:
        # a damaged or unopenable metadata.db must not stop the job: no
        # reuse, the caller probes (issue #507, the #486 rule)
        log(f"The plan cache is unavailable ({e}); probing instead of reusing a plan.")
        return None
    finally:
        if connection is not None:
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
    the code-fixed transfer set, issue #518) so both paths always agree.

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
            the requested one.
        min_saving_percent (int): The minimum-saving threshold the verdict
            was decided under (issue #316; part of the plan fingerprint).
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
        input_mtime (float | None): The run-start st_mtime (issue #436): the
            plan fingerprint commits from the stat the run DECIDED about,
            not a commit-time re-read; None when the caller carries none
            (a result built outside the engine's capture).
    """

    input: Path
    output: Path
    success: bool
    target_vmaf: float = DEFAULT_TARGET_VMAF
    min_saving_percent: int = 0
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
    hdr: bool = False
    tool_versions: str | None = None
    input_mtime: float | None = None
    input_width: int | None = None
    input_height: int | None = None
    input_codec: str | None = None
    input_duration: float | None = None
    input_bitrate: float | None = None
    output_bitrate: float | None = None
    input_bit_depth: int | None = None
    # the probing pipeline's effective decision inputs (fingerprint of a
    # stored plan): the effective values the decision was made against
    crop: tuple[int, int, int, int] | None = None
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE
    sample_count: int = DEFAULT_SAMPLE_COUNT
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT
    # which aggregation produced achieved_vmaf: "mean" is the decision
    # statistic (issue #15), "min" marks a plan measured before it
    vmaf_statistic: str = "mean"
    # how the VMAF scores were measured (issue #308): model, scale, pixel
    # format and the recipe version. Part of the plan fingerprint, so
    # a changed measurement re-probes instead of reusing stored scores. None for
    # a plan that measured nothing (a gate skip).
    vmaf_recipe: str | None = None
    # provenance of the CRF decision ('plan' when reused from a stored
    # plan, 'probe' when measured now, None when not applicable)
    crf_source: str | None = None
    plan_created_at: str | None = None
    # the encoder the decision was made against - one source of truth for
    # the record and the plan cache
    encoder: str = DEFAULT_ENCODER

    @property
    def saved_percent(self) -> float:
        """Percentage of disk space saved by the encode."""
        if self.input_size <= 0 or self.output_size <= 0:
            return 0.0
        return round((1 - self.output_size / self.input_size) * 100, 2)


@dataclass
class Decision:
    """The eligibility gates' verdict for one file (issue #602).

    A decision is the first stage of the per-file pipeline and a job kind of
    its own: nothing here measures or encodes, so the Jobs page can show whether
    a file is a candidate before the expensive work starts.

    Attributes:
        input (Path): The file the decision is about.
        eligible (bool): True when every gate passed.
        skipped_reason (str | None): The gate that rejected the file, worded
            as plan_quality reported it before the split.
        error (str | None): The operator-facing explanation of a rejection.
        identity (RunIdentity | None): The run-start identity the search and
            the encode commit under (issue #436).
        hdr (bool): Whether the source carries an HDR transfer.
        encoder (str): The encoder the source's bit depth selects.
        duration (float | None): The probed duration in seconds.
        width (int | None): The probed width.
        height (int | None): The probed height.
        crf_range (tuple[int, int]): The search window the decision carries.
        sample_count (int): The sample count the decision carries.
        sample_seconds (float): The sample length the decision carries.
    """

    input: Path
    eligible: bool
    skipped_reason: str | None = None
    error: str | None = None
    identity: RunIdentity | None = None
    hdr: bool = False
    encoder: str = DEFAULT_ENCODER
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE
    sample_count: int = DEFAULT_SAMPLE_COUNT
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS


def decide_quality(
    input_file: Path,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,
    sample_count: int | None = None,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    sample_every: float | None = DEFAULT_SAMPLE_EVERY,
    min_source_height: int = DEFAULT_MIN_SOURCE_HEIGHT,
    min_source_width: int = DEFAULT_MIN_SOURCE_WIDTH,
    min_source_size_gb: float = 0.0,
    candidate_codecs: tuple[str, ...] = CANDIDATE_CODECS,
) -> Decision:
    """Runs the eligibility gates for one file, measuring nothing.

    The first stage of the per-file pipeline and a job kind of its own (issue
    #602): it captures the run identity and applies the gates in their
    cheapest-first order - HDR transfer, duration, resolution class, size floor,
    codec whitelist. A rejecting gate returns the same skipped_reason and error
    text plan_quality reported before the split; an eligible file returns the
    identity and the probe metadata the search and the encode carry.

    Args:
        input_file (Path): The file to decide.
        target_vmaf (float): The VMAF quality floor the decision records.
        crf_range (tuple[int, int]): The (lowest, highest) CRF the decision
            carries to the search.
        sample_count (int): The fixed sample count (None = derived from
            sample_every and the duration).
        sample_seconds (float): The length of each sample in seconds.
        sample_every (float | None): The sample density (None = the fixed
            count).
        min_source_height (int): Skip sources whose height AND width are both
            below these floors (issue #233: either dimension at class size
            qualifies).
        min_source_width (int): The width floor (see min_source_height).
        min_source_size_gb (float): Skip sources below this size (0 = off).
        candidate_codecs (tuple[str, ...]): Source codecs accepted beyond the
            product whitelist SOURCE_CODECS (issue #271).

    Returns:
        Decision: eligible=False with the gate's reason, or eligible=True
            carrying the captured identity and the probe metadata.

    Raises:
        FileNotFoundError: If the input file does not exist.
    """
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file '{input_file}' does not exist!")

    # the run's identity, captured ONCE before any gate or sample (issue
    # #436): every decision this run makes is about THESE bytes, and the
    # record-time rechecks below compare against this capture - not against
    # an ad hoc stat per stage
    identity = capture_run_identity(input_file)

    hdr = is_hdr_file(input_file)
    # The encoder follows the SOURCE's bit depth (issue #234): a 10-bit
    # SDR master keeps its depth. The choice runs for every file - the
    # skip result below still records it (issue #518).
    effective_encoder = encoder_for(source_bit_depth(input_file))

    duration = probe_duration_seconds(input_file)
    sample_count = sample_count_for(duration, sample_every, sample_count)
    if hdr:
        # the maintainer's decision (issue #340, 2026-10-31; issue #518): HDR
        # is not supported yet - the tone-mapped SDR measurement is not valid
        # for HDR (libvmaf's docs: an SDR model on HDR pictures "is not
        # meaningful"). Support returns with libvmaf's HDR models (#519).
        log(f"Plan for '{input_file.name}': skip (hdr not supported)")
        return Decision(
            input=input_file,
            eligible=False,
            error="HDR: not supported yet (no validated quality measurement); tracked in issue #519",
            skipped_reason="hdr not supported",
            identity=identity,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )
    if duration is not None and duration < MIN_ENCODE_SECONDS:
        # the plan's verdict line (issue #471): the decision exists in the
        # row and the dashboard, but docker logs must say it too
        log(f"Plan for '{input_file.name}': skip (too short)")
        # every return carries the run's identity (issue #436): a row that
        # commits a verdict must commit the facts of the bytes it decided
        # about, never a commit-time stat
        return Decision(
            input=input_file,
            eligible=False,
            error=f"duration {duration:.0f}s is below the {MIN_ENCODE_SECONDS:.0f}s minimum",
            skipped_reason="too short",
            identity=identity,
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
        log(f"Plan for '{input_file.name}': skip (resolution too low)")
        return Decision(
            input=input_file,
            eligible=False,
            error=(
                f"resolution {shown} is below the {min_source_width}x"
                f"{min_source_height} 1080p class minimum "
                "(a lower-resolution source saves too little to be worth the probe)"
            ),
            skipped_reason="resolution too low",
            identity=identity,
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
        log(f"Plan for '{input_file.name}': skip (below min size)")
        return Decision(
            input=input_file,
            eligible=False,
            error=(
                f"size {size_gb:.2f} GB is below the {min_source_size_gb:.2f} GB "
                "minimum (too little to save for the probe cost)"
            ),
            skipped_reason="below min size",
            identity=identity,
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
        log(f"Plan for '{input_file.name}': skip (source codec not a candidate)")
        return Decision(
            input=input_file,
            eligible=False,
            error=(
                f"source codec '{codec}' is not in the candidate codec list "
                f"({', '.join(allowed)}) - a source outside it is "
                "never re-encoded"
            ),
            skipped_reason="source codec not a candidate",
            identity=identity,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
        )

    return Decision(
        input=input_file,
        eligible=True,
        identity=identity,
        hdr=hdr,
        encoder=effective_encoder,
        duration=duration,
        width=width,
        height=height,
        crf_range=crf_range,
        sample_count=sample_count,
        sample_seconds=sample_seconds,
    )


def skipped_result(decision: Decision, target_vmaf: float) -> QualityResult:
    """Rebuilds a rejecting decision as the result plan_quality returns.

    Args:
        decision (Decision): The rejecting decision.
        target_vmaf (float): The requested VMAF floor.

    Returns:
        QualityResult: success=False carrying the gate's reason.
    """
    identity = decision.identity
    return QualityResult(
        input=decision.input,
        output=decision.input,
        success=False,
        target_vmaf=target_vmaf,
        error=decision.error,
        skipped_reason=decision.skipped_reason,
        input_size=identity.size if identity is not None else 0,
        input_mtime=identity.mtime if identity is not None else None,
        crf_range=decision.crf_range,
        sample_count=decision.sample_count,
        sample_seconds=decision.sample_seconds,
        hdr=decision.hdr,
        encoder=decision.encoder,
    )


def search_quality(
    input_file: Path,
    decision: Decision,
    *,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    min_saving_percent: int = 15,
    probe_encoder_preset: str = "5",
    encoder_preset: str = "3",
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT,
) -> QualityResult:
    """Runs the bar pass, the sample extraction and the CRF search.

    The second stage of the per-file pipeline and a job kind of its own (issue
    #602): starting from an eligible Decision it measures the bars, extracts the
    sample windows, bisects the CRF and stops before the full encode. The
    prediction it returns carries the chosen CRF and the size factor the encode
    re-validates at its boundary.

    Args:
        input_file (Path): The eligible file.
        decision (Decision): The eligibility verdict this search runs under
            (its identity is the run-start stat every verdict commits under).
        target_vmaf (float): The VMAF quality floor to meet.
        min_saving_percent (int): The minimum predicted saving the decision
            demands (issue #316; 0 = any real shrink).
        probe_encoder_preset (str): The PROBE's encoder speed preset (issue
            #22); the plan's fingerprint keys on this value.
        encoder_preset (str): The FINAL encode's speed preset, recorded for
            provenance only.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file, used to locate the logs
            directory for failure evidence.
        encoder_threads (int | None): Thread limit for the probe encodes.
        progress (callable | None): Callback progress(dict) for live updates.
        use_probe_cache (bool): Reuse stored probe scores (issue #29).
        crop_sample_count (int): cropdetect windows per movie; more samples
            can only shrink the crop (issue #181).

    Returns:
        QualityResult: The prediction (success=False with a skipped_reason
            for the search's own skip verdicts).

    Raises:
        RuntimeError: If HandBrakeCLI is not installed or probing failed.
        Cancelled: When the cancel event fires.
    """
    identity = decision.identity
    if identity is None:  # only an eligible decision reaches the search
        raise RuntimeError("search_quality needs an eligible decision")
    width, height = decision.width, decision.height
    hdr = decision.hdr
    effective_encoder = decision.encoder
    sample_count = decision.sample_count
    sample_seconds = decision.sample_seconds
    crf_range = decision.crf_range
    handbrake = find_handbrake()
    if handbrake is None:
        raise RuntimeError(
            f"'{HANDBRAKE_BINARY}' not found! Install HandBrake CLI "
            "(https://handbrake.fr/docs/en/latest/cli/cli-options.html)"
        )

    workdir = Path(tempfile.mkdtemp(prefix=probe_scratch_prefix(input_file)))
    try:
        # bar detection is part of every re-encode, never optional: a re-encode
        # is the only moment the picture can be corrected for free, and keeping
        # bars in a smaller file is strictly worse than the source
        # (docs/encode-decision.md). No bars is a normal outcome, not a skip.
        # The timeline detector is the one bar measurement (issue #181): it is
        # cached per file version in the db, so the keyframe-only pass runs
        # once per file. The sampled fallback runs only for a pass that
        # learned nothing (issue #525), stateless or not.
        summary = detect_crop_summary_cached(input_file, db_file, cancel=cancel)
        # the measured "no common bars" verdict stands (issue #525): the
        # sampled detector runs only for a pass that learned nothing
        crop = crop_bars_or_fallback(
            summary,
            lambda: detect_crop_rect(input_file, workdir, count=crop_sample_count, cancel=cancel),
        )
        # the measurement recipe follows the source's dimensions (for the model
        # and the scale, through the crop) and its bit depth (the analysis pixel
        # format). Derived once here so the probes and the recorded fingerprint
        # cannot disagree (issue #308).
        recipe = vmaf_recipe(
            crop=crop,
            width=width,
            height=height,
            bit_depth=source_bit_depth(input_file),
        )
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
        crf, achieved, probes, skip, factor = find_optimal_crf_and_skip(
            input_file,
            workdir,
            handbrake,
            target_vmaf=target_vmaf,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            encoder=effective_encoder,
            probe_encoder_preset=probe_encoder_preset,
            recipe=recipe,
            crop=crop,
            cancel=cancel,
            db_file=db_file,
            encoder_threads=encoder_threads,
            progress=progress,
            use_probe_cache=use_probe_cache,
            samples=samples,
        )

        if target_unreachable(crf, achieved, target_vmaf, crf_range):
            # gate 7 before gate 8 (issue #591): an unreachable search is
            # skipped for its quality, whatever the size at the floor CRF (the
            # search measured none). The verdict is recorded like a skip (issue
            # #436): the same record-time recheck applies
            ensure_unchanged(input_file, identity, "plan")
            log(f"Plan for '{input_file.name}': skip (target unreachable)")
            return QualityResult(
                input=input_file,
                output=input_file,
                success=False,
                target_vmaf=target_vmaf,
                min_saving_percent=min_saving_percent,
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probes=probes,
                input_size=identity.size,
                output_size=identity.size,
                error=(
                    f"skipped: target VMAF {target_vmaf} unreachable "
                    f"(best {achieved} at CRF {crf_range[0]})"
                ),
                skipped_reason="target unreachable",
                input_mtime=identity.mtime,
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crop=crop,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                hdr=hdr,
                encoder=effective_encoder,
                vmaf_recipe=recipe.fingerprint(),
            )

        # the minimum-saving threshold (issue #316, the maintainer's
        # 2026-10-31 decision): the decision to SPEND the encode demands more
        # than "smaller" - the all-window factor the search decided with (the
        # fifth element above) is the prediction, and a saving below the
        # configured threshold is not worth the encode's cost. 0 reproduces
        # the old rule. The search's own would-not-shrink skip (factor >= 1.0)
        # needs no threshold.
        saving_percent = (1.0 - factor) * 100.0
        if not skip and saving_percent < min_saving_percent:
            ensure_unchanged(input_file, identity, "plan")
            log(
                f"Plan for '{input_file.name}': skip (predicted saving "
                f"{saving_percent:.1f}% < required {min_saving_percent}%)"
            )
            return QualityResult(
                input=input_file,
                output=input_file,
                success=False,
                target_vmaf=target_vmaf,
                min_saving_percent=min_saving_percent,
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probes=probes,
                input_size=identity.size,
                output_size=identity.size,
                error=(
                    f"skipped: predicted saving {saving_percent:.1f}% "
                    f"< required {min_saving_percent}%"
                ),
                skipped_reason="saving below minimum",
                input_mtime=identity.mtime,
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crop=crop,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                hdr=hdr,
                encoder=effective_encoder,
                vmaf_recipe=recipe.fingerprint(),
            )

        if skip:
            # the record-time recheck (issue #436): the skip verdict rides the
            # plans row and is trusted without re-probing later, so it must
            # not commit for bytes the run no longer sees. Loud abort, the
            # caller reports it (the entry points catch RuntimeError).
            # NO verdict line here: the search layer already logged the
            # would-not-shrink skip (issue #471's no-duplicate rule)
            ensure_unchanged(input_file, identity, "plan")
            return QualityResult(
                input=input_file,
                output=input_file,
                success=False,
                target_vmaf=target_vmaf,
                min_saving_percent=min_saving_percent,
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probes=probes,
                input_size=identity.size,
                output_size=identity.size,
                error="skipped: re-encode would not shrink the file",
                skipped_reason="no size gain",
                input_mtime=identity.mtime,
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crop=crop,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                hdr=hdr,
                encoder=effective_encoder,
                vmaf_recipe=recipe.fingerprint(),
            )
        # the record-time recheck on the SUCCESS path too (issue #436): the
        # chosen CRF and the size estimate are decisions about these bytes,
        # and the fingerprint they commit under must be the run-start stat
        ensure_unchanged(input_file, identity, "plan")
        # worded as a prediction, never a promise (issue #471): the swap is
        # a later, separately-reported step
        log(
            f"Plan for '{input_file.name}': encode at CRF {crf} (predicted {factor:.2f}x of source)"
        )
        return QualityResult(
            input=input_file,
            output=input_file,
            success=True,
            target_vmaf=target_vmaf,
            min_saving_percent=min_saving_percent,
            chosen_crf=crf,
            achieved_vmaf=achieved,
            probes=probes,
            input_size=identity.size,
            output_size=int(identity.size * factor),
            input_mtime=identity.mtime,
            error=None,
            encoder_preset=encoder_preset,
            probe_encoder_preset=probe_encoder_preset,
            crop=crop,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            hdr=hdr,
            encoder=effective_encoder,
            vmaf_recipe=recipe.fingerprint(),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def plan_quality(
    input_file: Path,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    min_saving_percent: int = 15,
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
        min_saving_percent (int): The minimum predicted saving (percent)
            the decision demands (issue #316; 0 = any real shrink). Part of
            the stored plan fingerprint.
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
    decision = decide_quality(
        input_file,
        target_vmaf=target_vmaf,
        crf_range=crf_range,
        sample_count=sample_count,
        sample_seconds=sample_seconds,
        sample_every=sample_every,
        min_source_height=min_source_height,
        min_source_width=min_source_width,
        min_source_size_gb=min_source_size_gb,
        candidate_codecs=candidate_codecs,
    )
    if not decision.eligible:
        return skipped_result(decision, target_vmaf)
    return search_quality(
        input_file,
        decision,
        target_vmaf=target_vmaf,
        min_saving_percent=min_saving_percent,
        probe_encoder_preset=probe_encoder_preset,
        encoder_preset=encoder_preset,
        cancel=cancel,
        db_file=db_file,
        encoder_threads=encoder_threads,
        progress=progress,
        use_probe_cache=use_probe_cache,
        crop_sample_count=crop_sample_count,
    )


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
