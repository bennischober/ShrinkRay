"""Perceptual quality optimization for re-encoding (VMAF-driven CRF search).

Implements the 'per-title' idea popularized by Netflix: instead of a fixed
quality setting, short samples of the movie are probed, their perceptual
quality is measured with VMAF (Netflix's video quality metric, computed by
ffmpeg's libvmaf filter) and the largest HandBrake CRF that still meets the
target quality is chosen => lowest file size without visible quality loss.

The CRF search is a binary search ('bisect'): VMAF falls monotonically as
CRF rises, so ~5 probe encodes are enough to find the quality knee.

Note on score comparability: libvmaf versions measure slightly differently
(the same encode scored 95.76 on ffmpeg 5/libvmaf v2 and 94.40 on
ffmpeg 9/libvmaf v3). Decisions stay internally consistent because the
bisect measures and decides with the same local toolchain, but absolute
scores and chosen CRFs are only comparable between similar libvmaf builds.
"""

import json
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from shrinkray.encode import (
    HANDBRAKE_BINARY,
    _encode_with_command,
    find_handbrake,
)
from shrinkray.engine.crop import (
    CROP_DETECT_LIMIT as CROP_DETECT_LIMIT,
)
from shrinkray.engine.crop import (
    DEFAULT_CROP_SAMPLE_COUNT as DEFAULT_CROP_SAMPLE_COUNT,
)
from shrinkray.engine.crop import (
    MIN_BAR_PERCENT as MIN_BAR_PERCENT,
)
from shrinkray.engine.crop import (
    detect_crop_rect as detect_crop_rect,
)
from shrinkray.engine.crop import (
    intersect_crop_rects as intersect_crop_rects,
)
from shrinkray.engine.crop import (
    parse_crop_line as parse_crop_line,
)
from shrinkray.engine.encoders import (
    DEFAULT_ENCODER as DEFAULT_ENCODER,
)
from shrinkray.engine.encoders import (
    DEFAULT_HDR_TARGET_VMAF as DEFAULT_HDR_TARGET_VMAF,
)
from shrinkray.engine.encoders import (
    HDR_ENCODER as HDR_ENCODER,
)
from shrinkray.engine.encoders import (
    HDR_ESTIMATE_NOTE as HDR_ESTIMATE_NOTE,
)
from shrinkray.engine.encoders import (
    HDR_VMAF_OFFSET as HDR_VMAF_OFFSET,
)
from shrinkray.engine.encoders import (
    encoder_for as encoder_for,
)
from shrinkray.engine.plan import (
    CANDIDATE_CODECS as CANDIDATE_CODECS,
)
from shrinkray.engine.plan import (
    MIN_ENCODE_SECONDS as MIN_ENCODE_SECONDS,
)
from shrinkray.engine.plan import (
    MIN_SOURCE_CODEC as MIN_SOURCE_CODEC,
)
from shrinkray.engine.plan import (
    MODERN_CODECS as MODERN_CODECS,
)
from shrinkray.engine.plan import (
    QualityResult as QualityResult,
)
from shrinkray.engine.plan import (
    _lookup_plan as _lookup_plan,
)
from shrinkray.engine.plan import (
    _plan_crop as _plan_crop,
)
from shrinkray.engine.plan import (
    _plan_probes as _plan_probes,
)
from shrinkray.engine.plan import (
    is_hdr_file as is_hdr_file,
)
from shrinkray.engine.plan import (
    plan_quality as plan_quality,
)
from shrinkray.engine.plan import (
    source_codec as source_codec,
)
from shrinkray.engine.plan import (
    source_height as source_height,
)
from shrinkray.engine.samples import (
    DEFAULT_SAMPLE_COUNT as DEFAULT_SAMPLE_COUNT,
)
from shrinkray.engine.samples import (
    DEFAULT_SAMPLE_EVERY as DEFAULT_SAMPLE_EVERY,
)
from shrinkray.engine.samples import (
    DEFAULT_SAMPLE_SECONDS as DEFAULT_SAMPLE_SECONDS,
)
from shrinkray.engine.samples import (
    extract_samples as extract_samples,
)
from shrinkray.engine.samples import (
    sample_offsets as sample_offsets,
)
from shrinkray.engine.search import (
    DEFAULT_CRF_RANGE as DEFAULT_CRF_RANGE,
)
from shrinkray.engine.search import (
    DEFAULT_HDR_CRF_RANGE as DEFAULT_HDR_CRF_RANGE,
)
from shrinkray.engine.search import (
    DEFAULT_TARGET_VMAF as DEFAULT_TARGET_VMAF,
)
from shrinkray.engine.search import (
    VMAF_STATISTIC as VMAF_STATISTIC,
)
from shrinkray.engine.search import (
    SampleProbe as SampleProbe,
)
from shrinkray.engine.search import (
    SkipEncode as SkipEncode,
)
from shrinkray.engine.search import (
    _cached_or_none as _cached_or_none,
)
from shrinkray.engine.search import (
    _store_probe as _store_probe,
)
from shrinkray.engine.search import (
    bisect_crf as bisect_crf,
)
from shrinkray.engine.search import (
    check_chosen_crf_shrinks as check_chosen_crf_shrinks,
)
from shrinkray.engine.search import (
    estimate_size_factor as estimate_size_factor,
)
from shrinkray.engine.search import (
    find_optimal_crf as find_optimal_crf,
)
from shrinkray.engine.search import (
    find_optimal_crf_and_skip as find_optimal_crf_and_skip,
)
from shrinkray.engine.search import (
    probe_cache_key as probe_cache_key,
)
from shrinkray.engine.search import (
    probe_crf as probe_crf,
)
from shrinkray.engine.search import (
    sample_count_for as sample_count_for,
)
from shrinkray.logger import log, log_error
from shrinkray.tools.ffprobe import (
    probe_duration_seconds as probe_duration_seconds,
)
from shrinkray.tools.handbrake import (
    build_encode_command as build_encode_command,
)
from shrinkray.tools.process import (
    Cancelled as Cancelled,
)
from shrinkray.tools.process import (
    _run as _run,
)
from shrinkray.tools.vmaf import (
    MAX_VMAF_THREADS as MAX_VMAF_THREADS,
)
from shrinkray.tools.vmaf import (
    VMAF_MODEL as VMAF_MODEL,
)
from shrinkray.tools.vmaf import (
    VMAF_STYLE_LEGACY as VMAF_STYLE_LEGACY,
)
from shrinkray.tools.vmaf import (
    VMAF_STYLE_MODERN as VMAF_STYLE_MODERN,
)
from shrinkray.tools.vmaf import (
    _tool_versions as _tool_versions,
)
from shrinkray.tools.vmaf import (
    build_vmaf_command as build_vmaf_command,
)
from shrinkray.tools.vmaf import (
    detect_vmaf_style as detect_vmaf_style,
)
from shrinkray.tools.vmaf import (
    measure_vmaf as measure_vmaf,
)
from shrinkray.tools.vmaf import (
    parse_vmaf_log as parse_vmaf_log,
)

FFMPEG_BINARY = "ffmpeg"

# the decision statistic (issue #15) lives in engine/search.py; quality
# re-imports it below

# PRODUCT.md's source-codec rule: an H.264 source is a candidate on size
# alone, while a modern codec (HEVC/AV1) is already efficient and is only
# worth re-encoding when it has bars to remove. Anything older is never
# re-encoded. The names are ffprobe's own codec_name values.
# the codec-gate constants live in engine/plan.py (re-imported below)

# the engine's live-progress callback: stage detail and counters, never persisted
ProgressCallback = Callable[[dict], None]


# full-file verification sub-samples above this output size (n_subsample=10:
# every 10th frame measured) - full decode of a 2-hour 4K movie takes hours,
# a sub-sampled pass minutes, the score stays honest
VERIFY_SUBSAMPLE_THRESHOLD_BYTES = 2 * 1024**3


def _source_snapshot(movie: Path) -> dict:
    """Reads the source file's resolution/codec/duration/bitrate for encode records.

    The encode record must be self-contained: the files table row can change
    on a later re-scan, so the benchmark data snapshots what was true at
    encode time. The bitrate feeds the generation-drift documentation
    (input_bitrate/output_bitrate on the encodes row).

    Args:
        movie (Path): The source file.

    Returns:
        dict: width/height/codec/duration/bitrate (None where unknown).
    """
    from shrinkray.tools.ffprobe import probe_duration_seconds

    duration = probe_duration_seconds(movie)
    width = height = None
    codec = None
    bitrate = None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(movie),
            ],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout.decode())
            streams = data.get("streams", [])
            video: dict = next((s for s in streams if s.get("codec_type") == "video"), {})
            width = video.get("width")
            height = video.get("height")
            codec = video.get("codec_name")
            # the file bitrate (video+audio+containers); the video stream's
            # own bit_rate is often absent => fall back to the format level
            raw = video.get("bit_rate") or data.get("format", {}).get("bit_rate")
            if raw:
                bitrate = round(float(raw) / 1_000_000, 2)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {
        "width": width,
        "height": height,
        "codec": codec,
        "duration": duration,
        "bitrate": bitrate,
    }


def _output_bitrate(output_file: Path, duration: float | None) -> float | None:
    """Computes the output bitrate in MBit/s from size and duration.

    Size/duration is the honest number for the drift chain (a ffprobe of
    the output would re-read the container and return the same sum).

    Args:
        output_file (Path): The encoded file.
        duration (float | None): The duration in seconds (shared by both
            sides — the truncation check above verified equality).

    Returns:
        float | None: Bitrate in MBit/s, None when unknowable.
    """
    if not duration or duration <= 0 or not output_file.is_file():
        return None
    return round(output_file.stat().st_size * 8 / duration / 1_000_000, 2)


def _probe_movie(
    input_file: Path,
    target_vmaf: float,
    crf_range: tuple[int, int],
    sample_count: int,
    sample_seconds: float,
    encoder: str,
    probe_encoder_preset: str,
    hdr: bool = False,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    progress: ProgressCallback | None = None,
    use_probe_cache: bool = True,
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT,
) -> tuple[int, float, list[SampleProbe], tuple[int, int, int, int] | None]:
    """Shared probing pipeline of plan_quality and encode_with_quality.

    Args:
        input_file (Path): The file to probe.
        target_vmaf (float): The VMAF quality floor to meet.
        crf_range (tuple[int, int]): The (lowest, highest) CRF to search.
        sample_count (int): The number of samples to probe.
        sample_seconds (float): The length of each sample in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's encoder speed preset (a
            measurement setting, issue #22).
        crop_sample_count (int): cropdetect windows per movie.
        hdr (bool): Tone-map both sides for the VMAF measurement.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file for failure-evidence logs.
        encoder_threads (int | None): Thread limit for the probe encodes
            (None = no limit; must match the final encode's cap).
        progress (callable | None): Callback progress(dict) for live step
            updates of the probing pipeline (flow to the UI, never
            persisted).

    Returns:
        tuple: The chosen CRF, its VMAF, all probes and the crop bars
            (None when no crop is applied).

    Raises:
        RuntimeError: If HandBrakeCLI is not installed or probing failed.
        Cancelled: When the cancel event fires.
    """
    _check_cancel(cancel)
    handbrake = find_handbrake()
    if handbrake is None:
        raise RuntimeError(
            f"'{HANDBRAKE_BINARY}' not found! Install HandBrake CLI "
            "(https://handbrake.fr/docs/en/latest/cli/cli-options.html)"
        )

    workdir = Path(tempfile.mkdtemp(prefix="metadata_quality_"))
    try:
        # bar detection is part of every re-encode, never a flag: the encoder
        # is already re-encoding every frame, so the crop is free here and a
        # file with no bars simply proceeds uncropped
        crop = detect_crop_rect(input_file, workdir, count=crop_sample_count)
        if crop is not None:
            log(
                f"Removing stable black bars of '{input_file.name}': "
                f"top {crop[0]}, bottom {crop[1]}, left {crop[2]}, right {crop[3]}"
            )
        log(f"Probing '{input_file.name}' (target VMAF {target_vmaf})...")
        crf, achieved, probes, skip = find_optimal_crf_and_skip(
            input_file,
            workdir,
            handbrake,
            target_vmaf=target_vmaf,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            encoder=encoder,
            probe_encoder_preset=probe_encoder_preset,
            crop=crop,
            hdr=hdr,
            cancel=cancel,
            db_file=db_file,
            encoder_threads=encoder_threads,
            progress=progress,
            use_probe_cache=use_probe_cache,
        )
        if skip:
            raise SkipEncode(
                "re-encode would not shrink the file",
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probe_count=len(probes),
            )
        if achieved < target_vmaf and crf == crf_range[0]:
            raise SkipEncode(
                f"target VMAF {target_vmaf} unreachable (best {achieved} at CRF {crf_range[0]})",
                chosen_crf=crf,
                achieved_vmaf=achieved,
                probe_count=len(probes),
            )
        return crf, achieved, probes, crop
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _check_cancel(cancel: threading.Event | None) -> None:
    """Raises Cancelled when the cancel event is set.

    Args:
        cancel (threading.Event | None): The job's cancel event (None
            disables the check, e.g. for CLI one-shots).

    Raises:
        Cancelled: When the event is set.
    """
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled by user")


def encode_with_quality(
    input_file: Path,
    output_file: Path,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,
    sample_count: int | None = None,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    sample_every: float | None = DEFAULT_SAMPLE_EVERY,
    encoder: str = "svt_av1",
    probe_encoder_preset: str = "5",
    encoder_preset: str = "3",
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    encoder_threads: int | None = None,
    verify: bool = False,
    force_reprobe: bool = False,
    use_probe_cache: bool = True,
    min_source_height: int = 0,
    min_source_size_gb: float = 0.0,
    candidate_codecs: tuple[str, ...] = CANDIDATE_CODECS,
    crop_sample_count: int = DEFAULT_CROP_SAMPLE_COUNT,
) -> QualityResult:
    """Re-encodes a movie with the largest CRF that meets the VMAF target.

    Short samples of the movie are probed first (bisect search), then the
    whole movie is encoded once with the winning CRF. Stable baked-in black
    bars (letterbox) are detected and cropped away as part of every encode.

    Args:
        input_file (Path): The file to re-encode.
        output_file (Path): The target file (must not exist).
        target_vmaf (float): The VMAF quality floor to meet.
        crf_range (tuple[int, int]): The (lowest, highest) CRF to search.
        sample_count (int): The number of samples to probe.
        sample_seconds (float): The length of each sample in seconds.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        probe_encoder_preset (str): The PROBE's speed preset (a measurement
            setting, issue #22); the probes and the plan fingerprint use it.
        encoder_preset (str): The FINAL encode's speed preset (an output
            setting, issue #22).
        progress (callable | None): Callback progress(dict) for live updates
            of the running encode's percentage.
        cancel (threading.Event | None): Cooperative cancellation (checked
            between probe steps; the final encode aborts by termination).
        db_file (Path | None): The database file, used to locate the logs
            directory for failure evidence (None => cwd metadata.db).
        encoder_threads (int | None): Thread limit for the probe encodes
            AND the final encode (None = no limit).
        min_source_height (int): Skip sources below this height (0 = gate
            off).
        min_source_size_gb (float): Skip sources below this size (0 = gate
            off).
        candidate_codecs (tuple[str, ...]): Source codecs that are
            candidates on size alone.
        crop_sample_count (int): cropdetect windows per movie.
        verify (bool): Run a full-file VMAF verification after a successful
            encode (opt-in: hours of decode for a large movie; sub-sampled
            1/10 above VERIFY_SUBSAMPLE_THRESHOLD_BYTES).
        force_reprobe (bool): Bypass the plan lookup entirely and run the
            normal probing pipeline even when a valid plan exists (CLI:
            --replan). It also bypasses the probe-encode cache: everything
            is measured fresh, nothing is reused. Plans are only consulted
            for explicit encode runs; watch mode passes True to stay
            independent of plan staleness (and therefore also re-probes
            fresh).

    Returns:
        QualityResult: The result of the encode.

    Raises:
        FileNotFoundError: If the input file does not exist.
        RuntimeError: If HandBrakeCLI is not installed.
        Cancelled: When the cancel event fires.
    """
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file '{input_file}' does not exist!")

    import time as _time

    run_started = _time.perf_counter()
    snapshot = _source_snapshot(input_file)
    hdr = is_hdr_file(input_file)
    # the effective decision inputs: for HDR the engine overrides the
    # target/range and switches to the 10-bit encoder (same rule as
    # plan_quality, so a stored HDR plan is found under its real encoder)
    effective_encoder = encoder_for(hdr)
    if hdr:
        target_vmaf = DEFAULT_HDR_TARGET_VMAF
        crf_range = DEFAULT_HDR_CRF_RANGE

    duration = probe_duration_seconds(input_file)
    sample_count = sample_count_for(duration, sample_every, sample_count)
    if duration is not None and duration < MIN_ENCODE_SECONDS:
        return QualityResult(
            input=input_file,
            output=output_file,
            success=False,
            target_vmaf=target_vmaf,
            error=f"duration {duration:.0f}s is below the {MIN_ENCODE_SECONDS:.0f}s minimum",
            skipped_reason="too short",
            hdr=hdr,
            encoder=effective_encoder,
        )

    # the resolution gate decides from cached metadata, before the size probe
    # and the CRF search spend minutes on a source whose pixels bound the saving
    height = (snapshot or {}).get("height")
    if height is not None and height < min_source_height:
        return QualityResult(
            input=input_file,
            output=output_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"height {height}p is below the {min_source_height}p minimum "
                "(a lower-resolution source saves too little to be worth the probe)"
            ),
            skipped_reason="resolution too low",
            hdr=hdr,
            encoder=effective_encoder,
        )

    size_gb = input_file.stat().st_size / (1024**3)
    if min_source_size_gb > 0 and size_gb < min_source_size_gb:
        return QualityResult(
            input=input_file,
            output=output_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"size {size_gb:.2f} GB is below the {min_source_size_gb:.2f} GB "
                "minimum (too little to save for the probe cost)"
            ),
            skipped_reason="below min size",
            hdr=hdr,
            encoder=effective_encoder,
        )

    # every eligible file is probed; size alone decides the encode. The
    # codec-age gate below is the only codec rule left.
    codec = source_codec(input_file)

    if codec is not None and codec not in candidate_codecs and codec not in MODERN_CODECS:
        return QualityResult(
            input=input_file,
            output=output_file,
            success=False,
            target_vmaf=target_vmaf,
            error=(
                f"source codec '{codec}' is below the {MIN_SOURCE_CODEC} minimum "
                "(an older codec is not re-encoded)"
            ),
            skipped_reason="source codec not a candidate",
            hdr=hdr,
            encoder=effective_encoder,
        )

    # disk-space preflight: the honest worst case for one encode is its
    # input (CRF 0 can grow slightly above it); failing 90 minutes into a
    # 4-hour encode on a full drive is the failure mode this prevents
    from shrinkray.encode import check_disk_space

    ok, space_message = check_disk_space(output_file.parent, input_file.stat().st_size)
    if not ok:
        return QualityResult(
            input=input_file,
            output=output_file,
            success=False,
            target_vmaf=target_vmaf,
            error=space_message,
            hdr=hdr,
            encoder=effective_encoder,
        )
    if space_message:
        log(f"Warning: {space_message}")

    # plan-result reuse ("quicksave"): plan_quality and the probe pipeline
    # are the same deterministic computation - a valid stored plan replaces
    # probing entirely. ANY fingerprint mismatch (file changed, different
    # settings) fails safe to the normal probing path. The lookup uses the
    # OVERRIDDEN target/range so HDR consistency falls out of the same rule.
    plan = None
    if db_file is not None and not force_reprobe:
        # the plan fingerprint keys on the PROBE preset (issue #22 decision):
        # a stored plan is a probe result, its CRF depends on the probe
        # preset only - changing the final preset reuses the plan
        plan = _lookup_plan(
            db_file,
            input_file,
            encoder=effective_encoder,
            encoder_preset=probe_encoder_preset,
            target_vmaf=target_vmaf,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            crop_sample_count=crop_sample_count,
        )
    crf_source: str | None = None
    plan_created_at: str | None = None
    if plan is not None:
        if plan["skipped_reason"]:
            # the plan already rejected this movie - trust the verdict,
            # no probing, no encode. The skip decision is as valuable
            # as the CRF.
            reason = str(plan["skipped_reason"])
            if progress is not None:
                progress(
                    {
                        "stage": "probe",
                        "detail": f"using plan (probed {plan['created_at']}) · {reason}",
                    }
                )
            log(f"Using plan for '{input_file.name}' (probed {plan['created_at']}): {reason}")
            return QualityResult(
                input=input_file,
                output=output_file,
                success=False,
                target_vmaf=target_vmaf,
                chosen_crf=plan["chosen_crf"],
                achieved_vmaf=plan["achieved_vmaf"],
                probes=_plan_probes(plan),
                input_size=input_file.stat().st_size,
                output_size=input_file.stat().st_size,
                error=f"skipped: {reason}",
                skipped_reason=reason,
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crop=None,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                crf_source="plan",
                plan_created_at=plan["created_at"],
                hdr=hdr,
                encoder=effective_encoder,
            )
        crf = int(plan["chosen_crf"])
        achieved = float(plan["achieved_vmaf"] or 0.0)
        probes = _plan_probes(plan)
        crop = _plan_crop(plan)
        probe_seconds = 0.0
        crf_source = "plan"
        plan_created_at = plan["created_at"]
        if progress is not None:
            progress(
                {
                    "stage": "probe",
                    "detail": f"using plan (probed {plan['created_at']}) · CRF {crf}",
                }
            )
        log(f"Using plan for '{input_file.name}' (probed {plan['created_at']}): CRF {crf}")
    else:
        try:
            crf, achieved, probes, crop = _probe_movie(
                input_file,
                target_vmaf,
                crf_range,
                sample_count,
                sample_seconds,
                effective_encoder,
                probe_encoder_preset,
                hdr=hdr,
                cancel=cancel,
                db_file=db_file,
                encoder_threads=encoder_threads,
                progress=progress,
                # force_reprobe means EVERYTHING is measured fresh: the plan
                # cache above is bypassed, and so is the probe-encode cache
                use_probe_cache=not force_reprobe,
            )
        except SkipEncode as e:
            return QualityResult(
                input=input_file,
                output=output_file,
                success=False,
                target_vmaf=target_vmaf,
                error=f"skipped: {e}",
                chosen_crf=e.chosen_crf,
                achieved_vmaf=e.achieved_vmaf,
                probes=[],
                input_size=input_file.stat().st_size,
                output_size=input_file.stat().st_size,
                skipped_reason=(
                    "target unreachable" if "unreachable" in str(e) else "no size gain"
                ),
                encoder_preset=encoder_preset,
                probe_encoder_preset=probe_encoder_preset,
                crf_range=crf_range,
                sample_count=sample_count,
                sample_seconds=sample_seconds,
                crf_source="probe",
                hdr=hdr,
                encoder=effective_encoder,
            )
        crf_source = "probe"
        probe_seconds = _time.perf_counter() - run_started

    log(f"Chosen CRF {crf} (VMAF {achieved})")
    handbrake = find_handbrake()
    if handbrake is None:  # already checked in _probe_movie, for mypy
        raise RuntimeError(f"'{HANDBRAKE_BINARY}' not found!")
    # the FINAL encode runs the final preset (an output setting, issue #22):
    # the artifact is what gets kept, so it spends the time; the probes ran
    # at the probe preset (a measurement setting)
    command = build_encode_command(
        handbrake,
        input_file,
        output_file,
        crf,
        effective_encoder,
        encoder_preset,
        crop=crop,
        encoder_threads=encoder_threads,
    )
    # always stream the final encode: the streamed loop is the disk watchdog
    # (free-space floor => cancel instead of filling the drive). Callers
    # without a progress interest get a no-op callback; the events are
    # identical either way.
    if progress is None:
        progress = lambda data: None  # noqa: E731
    encode_result = _encode_with_command(
        input_file, output_file, command, progress=progress, cancel=cancel, db_file=db_file
    )
    if encode_result.cancelled:
        # _encode_with_command already removed the partial output; only the
        # engine-level skip-vs-fail classification remains
        raise Cancelled(encode_result.error or "cancelled during final encode")
    if not encode_result.success:
        return QualityResult(
            input=input_file,
            output=output_file,
            success=False,
            target_vmaf=target_vmaf,
            chosen_crf=crf,
            achieved_vmaf=achieved,
            probes=probes,
            error=encode_result.error,
        )

    # the size gate that matters: the probe estimate is a prediction from a few
    # samples, this is the ground truth. The product rule is that a re-encode
    # happens only when the file gets smaller, so a grown output is a failure to
    # report - not a success with a negative saved_percent. Reported before the
    # verification pass: measuring a file that is about to be discarded would
    # spend minutes on a number nobody keeps.
    final_input_size = input_file.stat().st_size
    final_output_size = output_file.stat().st_size
    if final_output_size >= final_input_size:
        output_file.unlink(missing_ok=True)
        return QualityResult(
            input=input_file,
            output=input_file,
            success=False,
            target_vmaf=target_vmaf,
            chosen_crf=crf,
            achieved_vmaf=achieved,
            probes=probes,
            error=(
                f"skipped: the encode did not shrink the file "
                f"({final_input_size} -> {final_output_size} bytes)"
            ),
            skipped_reason="output not smaller",
            encoder_preset=encoder_preset,
            probe_encoder_preset=probe_encoder_preset,
            crop=crop,
            crf_range=crf_range,
            sample_count=sample_count,
            sample_seconds=sample_seconds,
            crf_source=crf_source,
            plan_created_at=plan_created_at,
            hdr=hdr,
            encoder=effective_encoder,
        )
    # success path: skip the verification below (already done)
    verified_vmaf: float | None = None
    verified_subsample: int | None = None
    if verify:
        # full-file verification: the ground-truth companion to the
        # sample-based probes. Sub-sample very large files (n_subsample
        # measures every Nth frame - hours of full decode become minutes)
        # while keeping the score honest: the sampled frames are still the
        # COMPLETE movie's frames, not the probe windows the CRF search
        # already saw. A verification failure does NOT invalidate the
        # encode (it measured fine at probe time) - it surfaces in the
        # result and the record, never as a hard error.
        output_size = output_file.stat().st_size
        verified_subsample = 10 if output_size > VERIFY_SUBSAMPLE_THRESHOLD_BYTES else None
        try:
            verify_workdir = Path(tempfile.mkdtemp(prefix="shrinkray_verify_"))
            try:
                verified_vmaf = measure_vmaf(
                    output_file,
                    input_file,
                    verify_workdir,
                    crop=crop,
                    hdr=hdr,
                    cancel=cancel,
                    db_file=db_file,
                    subsample=verified_subsample,
                )
            finally:
                shutil.rmtree(verify_workdir, ignore_errors=True)
            log(
                f"Verified '{input_file.name}': full-file VMAF "
                f"{verified_vmaf:.2f}"
                + (f" (sub-sampled 1/{verified_subsample})" if verified_subsample else "")
                + f" vs target {target_vmaf}"
            )
        except Cancelled:
            raise
        except (RuntimeError, ValueError) as e:
            log_error(f"Verification of '{input_file.name}' failed (non-fatal): {e}")
            verified_vmaf = None
            verified_subsample = None

    return QualityResult(
        input=input_file,
        output=output_file,
        success=True,
        target_vmaf=target_vmaf,
        chosen_crf=crf,
        achieved_vmaf=achieved,
        probes=probes,
        verified_vmaf=verified_vmaf,
        verified_subsample=verified_subsample,
        input_size=input_file.stat().st_size,
        output_size=output_file.stat().st_size,
        encoder_preset=encoder_preset,
        probe_encoder_preset=probe_encoder_preset,
        encode_seconds=_time.perf_counter() - run_started,
        probe_seconds=probe_seconds,
        hdr=hdr,
        encoder=effective_encoder,
        tool_versions=_tool_versions(handbrake),
        input_width=snapshot["width"],
        input_height=snapshot["height"],
        input_codec=snapshot["codec"],
        input_duration=snapshot["duration"],
        input_bitrate=snapshot["bitrate"],
        output_bitrate=_output_bitrate(output_file, snapshot["duration"]),
        crf_source=crf_source,
        plan_created_at=plan_created_at,
    )
