"""vmaf: the libvmaf measurement boundary (issue #97 phase 2).

Moved verbatim from quality.py: the ffmpeg/libvmaf invocation (style
detection for the legacy/modern filter API, the filtergraph builder,
the log parser) and the mean-score measurement the whole engine
decides on. The Netflix model json is shipped data, referenced from
the package's models/ directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from shrinkray.telemetry import save_failure
from shrinkray.tools.ffprobe import FFPROBE_TIMEOUT
from shrinkray.tools.process import Cancelled, _run, cpu_count

FFMPEG_BINARY = "ffmpeg"

MODELS_DIR = Path(__file__).parent.parent / "models"

# Netflix's reference models (https://github.com/Netflix/vmaf), shipped with the
# package because old ffmpeg/libvmaf builds cannot load embedded models. The 4K
# model is the one ab-av1 picks above 2560x1440 (issue #309).
VMAF_MODEL = MODELS_DIR / "vmaf_v0.6.1.json"
VMAF_4K_MODEL = MODELS_DIR / "vmaf_4k_v0.6.1.json"

# Bumped whenever the filtergraph or a model changes in a way that moves scores:
# the identifier below is part of the probe cache key and the plan fingerprint, so
# a bump re-measures stored results instead of reusing them (issue #308).
VMAF_RECIPE_VERSION = 2

# ab-av1's thresholds (src/command/args/vmaf.rs at ed5f5461f0aa, v0.11.7): the 4K
# model above 2560x1440, and each model's auto scale below the size it was
# trained for. Both read the DISTORTED (post-crop) dimensions, as ab-av1 does.
VMAF_4K_MODEL_MIN_WIDTH = 2560
VMAF_4K_MODEL_MIN_HEIGHT = 1440
VMAF_1080_SCALE_BELOW = (1728, 972)
VMAF_4K_SCALE_BELOW = (3456, 1944)

# libvmaf filter API changed with modern ffmpeg: old builds (ffmpeg < 6) take
# an explicit 'model_path', new builds take 'model=path=...' and ship embedded
# models. Detected once via the filter's option list.
_vmaf_style: str | None = None
VMAF_STYLE_LEGACY = "legacy"  # model_path=...
VMAF_STYLE_MODERN = "modern"  # model=path=...


def detect_vmaf_style(binary: str = FFMPEG_BINARY) -> str:
    """Detects which libvmaf filter API the local ffmpeg supports.

    Args:
        binary (str): The ffmpeg binary to probe.

    Returns:
        str: VMAF_STYLE_LEGACY or VMAF_STYLE_MODERN.

    Raises:
        RuntimeError: If ffmpeg is not installed or has no libvmaf filter.
    """
    global _vmaf_style
    if _vmaf_style is not None and binary == FFMPEG_BINARY:
        return _vmaf_style

    try:
        proc = subprocess.run(
            [binary, "-hide_banner", "-h", "filter=libvmaf"],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
        )
    except FileNotFoundError:
        # errno-13 on WSL (its /init interop turns the failed execve into
        # EACCES): naming the raw OSError sent the operator to chmod a file
        # that does not exist (issue #195)
        raise RuntimeError(
            f"ffmpeg ('{binary}') not found! Install a build with libvmaf support "
            f"(https://ffmpeg.org/download.html)."
        ) from None
    except PermissionError:
        raise RuntimeError(
            f"ffmpeg ('{binary}') could not be executed: check that the binary "
            f"is executable, or install a build with libvmaf support."
        ) from None
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(
            f"ffmpeg ('{binary}') did not answer within {FFPROBE_TIMEOUT}s! "
            f"Install a build with libvmaf support. ({e})"
        ) from e
    options = proc.stdout.decode(errors="replace")
    if "libvmaf" not in options and "libvmaf" not in proc.stderr.decode(errors="replace"):
        raise RuntimeError("ffmpeg has no libvmaf filter! Install a build with libvmaf support.")
    _vmaf_style = VMAF_STYLE_MODERN if "model_path" not in options else VMAF_STYLE_LEGACY
    return _vmaf_style


# libvmaf v3 thread cap: values above ~8 crash the filter with EINVAL on
# some builds (frame queue limit), so stay on the safe side
MAX_VMAF_THREADS = 8


def vmaf_pix_fmt(bit_depth: int | None) -> str:
    """Returns the analysis pixel format for a source's bit depth.

    One explicit format for both inputs, so libvmaf compares like with like: a
    10-bit source stays 10-bit (dropping the two extra bits would score a
    conversion error the encode never made), an 8-bit or unknown source is
    yuv420p. ab-av1 takes the deeper of the encoded and source formats
    (issue #308).

    Args:
        bit_depth (int | None): The source's bit depth, None when unknown.

    Returns:
        str: 'yuv420p10le' for 10-bit and deeper, else 'yuv420p'.
    """
    return "yuv420p10le" if bit_depth is not None and bit_depth > 8 else "yuv420p"


def vmaf_model(width: int | None, height: int | None) -> str:
    """Picks the VMAF model for the analysis dimensions.

    ab-av1's rule (issue #309): the 4K model when the distorted frame is
    wider than 2560 AND taller than 1440, the 1080p model otherwise. The
    dimensions are the POST-CROP ones - a 4K scope film cropped to 3840x1390
    stays on the 1080p model at native size, exactly as ab-av1 would score it.

    Args:
        width (int | None): The distorted frame width, None when unknown.
        height (int | None): The distorted frame height, None when unknown.

    Returns:
        str: The model's file name.
    """
    if (
        width is not None
        and height is not None
        and width > VMAF_4K_MODEL_MIN_WIDTH
        and height > VMAF_4K_MODEL_MIN_HEIGHT
    ):
        return VMAF_4K_MODEL.name
    return VMAF_MODEL.name


def _minimal_scale(width: int, height: int, target: tuple[int, int]) -> tuple[int, int] | None:
    """Returns ab-av1's minimal bicubic upscale towards a target, or None.

    Only one axis is given and the other is left to ffmpeg's -1, so the aspect
    ratio is kept; the axis that is proportionally further from the target is
    the one scaled (ab-av1's minimally_scale).

    Args:
        width (int): The frame width.
        height (int): The frame height.
        target (tuple[int, int]): The target (width, height).

    Returns:
        tuple[int, int] | None: (w, h) with -1 on one axis, or None when the
            frame already reaches the target on at least one axis.
    """
    if width >= target[0] or height >= target[1]:
        return None
    w_factor = width / target[0]
    h_factor = height / target[1]
    if h_factor > w_factor:
        return (-1, target[1])
    return (target[0], -1)


def vmaf_auto_scale(model: str, width: int | None, height: int | None) -> tuple[int, int] | None:
    """Returns ab-av1's `--vmaf-scale auto` target for the analysis.

    ab-av1 scales only when BOTH axes sit below the model's threshold (1728x972
    for the 1080p model, 3456x1944 for the 4K one), then upscales minimally
    towards 1920x1080 or 3840x2160. A model is defined for a viewing distance
    that assumes a frame size, so scoring a much smaller frame at native size
    measures something the model was not trained for (issue #309).

    Args:
        model (str): The model's file name (from vmaf_model).
        width (int | None): The distorted frame width, None when unknown.
        height (int | None): The distorted frame height, None when unknown.

    Returns:
        tuple[int, int] | None: The scale target, or None for no scaling.
    """
    if width is None or height is None:
        return None
    if model == VMAF_4K_MODEL.name:
        threshold, target = VMAF_4K_SCALE_BELOW, (3840, 2160)
    else:
        threshold, target = VMAF_1080_SCALE_BELOW, (1920, 1080)
    if width >= threshold[0] or height >= threshold[1]:
        return None
    return _minimal_scale(width, height, target)


@dataclass(frozen=True)
class VmafRecipe:
    """How one VMAF score is measured - every input that moves the number.

    The measurement itself, not the encode: the model, the analysis scale, the
    pixel format both inputs are converted to, the sync options and the HDR
    tone-map. It is the identifier the probe cache and the plan fingerprint key
    on (issue #308), so a change to any of these re-measures instead of
    reusing a score the old recipe produced.

    Attributes:
        model (str): The model's file name, e.g. 'vmaf_v0.6.1.json'.
        scale (tuple[int, int] | None): The bicubic upscale target, or None.
        pix_fmt (str): The explicit pixel format for both inputs.
        hdr (bool): Whether both inputs are tone-mapped to SDR first.
    """

    model: str
    scale: tuple[int, int] | None
    pix_fmt: str
    hdr: bool

    def identifier(self) -> str:
        """Returns the stable string the probe cache keys on.

        Every field that moves a score is in it, so a bump of any of them (or of
        VMAF_RECIPE_VERSION) misses every stored probe.

        Returns:
            str: e.g. 'vmaf=2;model=vmaf_v0.6.1.json;scale=none;fmt=yuv420p;hdr=no'.
        """
        scale = "none" if self.scale is None else f"{self.scale[0]}x{self.scale[1]}"
        return (
            f"vmaf={VMAF_RECIPE_VERSION};model={self.model};scale={scale};"
            f"fmt={self.pix_fmt};hdr={'hable' if self.hdr else 'no'}"
        )

    def fingerprint(self) -> str:
        """Returns the crop-independent part, for the PLAN fingerprint.

        The plan lookup runs BEFORE bar detection (a valid plan skips the
        keyframe pass entirely), so it cannot know the post-crop dimensions the
        model and scale follow. Those two are still covered: the model and scale
        depend only on the source dimensions (the file's identity, already in the
        fingerprint) and the crop (a function of the file and the crop sample
        count, also already there). What is left is the recipe version, the pixel
        format and the tone-map.

        Returns:
            str: e.g. 'vmaf=2;fmt=yuv420p;hdr=no'.
        """
        return f"vmaf={VMAF_RECIPE_VERSION};fmt={self.pix_fmt};hdr={'hable' if self.hdr else 'no'}"


def vmaf_recipe(
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    width: int | None = None,
    height: int | None = None,
    bit_depth: int | None = None,
) -> VmafRecipe:
    """Derives the measurement recipe for one source and its crop.

    Args:
        crop (tuple | None): Crop bars (top, bottom, left, right) the encode
            applies, so the analysis dimensions are the cropped ones.
        hdr (bool): Whether both inputs are tone-mapped to SDR.
        width (int | None): The SOURCE frame width, None when unknown.
        height (int | None): The SOURCE frame height, None when unknown.
        bit_depth (int | None): The source's bit depth, None when unknown.

    Returns:
        VmafRecipe: The recipe build_vmaf_command and the caches use.
    """
    cropped_width = cropped_height = None
    if width is not None and height is not None:
        if crop is None:
            cropped_width, cropped_height = width, height
        else:
            top, bottom, left, right = crop
            cropped_width = width - left - right
            cropped_height = height - top - bottom
    model = vmaf_model(cropped_width, cropped_height)
    return VmafRecipe(
        model=model,
        scale=vmaf_auto_scale(model, cropped_width, cropped_height),
        # PQ content is 10-bit by definition, so a tone-mapped comparison is
        # analysed at 10 bits whatever the source's depth reads (a 10-bit
        # intermediate flattened into 8 bits would score the flattening).
        pix_fmt="yuv420p10le" if hdr else vmaf_pix_fmt(bit_depth),
        hdr=hdr,
    )


def build_vmaf_command(
    distorted: Path,
    reference: Path,
    threads: int = 0,
    style: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    subsample: int | None = None,
    recipe: VmafRecipe | None = None,
) -> list[str]:
    """Builds the ffmpeg command measuring the VMAF of an encode.

    The model file is copied to the working directory by measure_vmaf and
    referenced by bare filename => no Windows path escaping in the
    filtergraph (colons in drive letters break filter parsing). Works with
    both the legacy (<6) and modern libvmaf filter API.

    With a crop, the reference is cropped to the same geometry as the
    distorted input, otherwise libvmaf fails on the dimension/SAR mismatch
    (e.g. PAL DVD content with uneven side bars).

    With HDR, both inputs are tone-mapped to SDR inside the filtergraph
    before the comparison: the VMAF model is SDR-trained and PQ content
    would score misleadingly. Both sides get the identical chain; what the
    number then measures is quality-within-this-chain, not PQ-direct
    quality - see the tone-map chain's note below.

    Args:
        distorted (Path): The encoded file (libvmaf 'main' input).
        reference (Path): The pristine source file (libvmaf 'reference').
        threads (int): libvmaf worker threads (0 = automatic). Meaningful
            only on modern libvmaf (v3+); legacy builds score single-threaded.
        style (str | None): Force a filter API style (legacy/modern); used by
            tests. None detects the local ffmpeg.
        crop (tuple | None): Crop bars (top, bottom, left, right) applied to
            the reference input to match the distorted geometry.
        hdr (bool): Tone-map both inputs to SDR (PQ/bt2020 sources).
        subsample (int | None): libvmaf n_subsample divisor: measure only
            every Nth frame (None/0/1 = every frame). Full-file verification
            of a 2-hour movie decodes EVERYTHING (hours); a sub-sampled pass
            (e.g. 10) cuts it to minutes while keeping the score honest
            (the sampled frames stay the SAME ones the probes never saw).
        recipe (VmafRecipe | None): The measurement recipe (model, scale,
            pixel format). None derives one with no dimensions known, which is
            the 1080p model at native size in the source's own format - the
            pre-#308 behaviour, kept for callers that have no dimensions.

    Returns:
        list[str]: The command as argument list (run with cwd=workdir).
    """
    if recipe is None:
        recipe = vmaf_recipe(crop=crop, hdr=hdr)
    if style is None:
        style = detect_vmaf_style()
    if style == VMAF_STYLE_MODERN:
        model_option = f"model=path={recipe.model}"
    else:
        model_option = f"model_path={recipe.model}"

    # ab-av1's sync options (issue #308): nearest-timestamp pairing rather than
    # framesync's 'nearest lower or equal', and 'shortest' so a sample window
    # whose streams end at different frames stops at the shorter one instead of
    # repeating the last frame.
    vmaf = (
        "libvmaf=shortest=true:ts_sync_mode=nearest:"
        f"log_path=vmaf.json:log_fmt=json:{model_option}:n_threads={threads}"
    )
    if subsample and subsample > 1:
        vmaf += f":n_subsample={int(subsample)}"

    # the analysis suffix, in ab-av1's order: one explicit pixel format, the
    # optional auto upscale, then the timestamps rebased to zero with a common
    # timebase. A probe sample is a stream copy cut before -i, so an open-GOP
    # source can start at a non-zero PTS while HandBrake's output starts at 0;
    # without the rebase the two sides pair frames one or two apart and every
    # score drops (issue #308). It follows the tone-map so the tone-mapped
    # intermediate is not converted back to 8-bit before the model sees it.
    suffix = f"format={recipe.pix_fmt},"
    if recipe.scale is not None:
        suffix += f"scale={recipe.scale[0]}:{recipe.scale[1]}:flags=bicubic,"
    suffix += "setpts=PTS-STARTPTS,settb=AVTB"

    # the tone-map chain: linear light -> bt709 primaries -> hable tonemap
    # -> back to video levels. libvmaf's models are SDR-trained, so HDR is
    # flattened into a comparable space before scoring; the chain is an
    # approximation, not a validated reference - #340 measured its footprint
    # at ~2.9 VMAF on one real HDR10 pair (ShrinkRay scores lower than
    # ab-av1's PQ-direct path, which errs safe: quality is overshot, never
    # undershot). No end-to-end HDR verification against reference tooling
    # exists yet; until one does, treat HDR scores as chain-relative.
    tonemap = (
        "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
        "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
    )

    # Both sides get the same chain, in one order: the reference's crop first
    # (issue #307: the bars must not influence the tone-map, and the analysis
    # dimensions are the post-crop ones), then the tone-map when HDR, then the
    # shared analysis prefix. The distorted input arrives already cropped by
    # HandBrake (--crop), so only the reference is cropped here.
    ref_chain: list[str] = []
    if crop is not None:
        top, bottom, left, right = crop
        ref_chain.append(f"crop=iw-{left + right}:ih-{top + bottom}:{left}:{top}")
    if hdr:
        ref_chain.append(tonemap)
    ref_chain.append(suffix)

    dis_chain = [tonemap, suffix] if hdr else [suffix]
    filtergraph = f"[0:v]{','.join(dis_chain)}[d];[1:v]{','.join(ref_chain)}[r];[d][r]{vmaf}"
    return [
        FFMPEG_BINARY,
        "-y",
        "-v",
        "error",
        "-i",
        str(distorted),
        "-i",
        str(reference),
        "-lavfi",
        filtergraph,
        "-f",
        "null",
        "-",
    ]


def parse_vmaf_log(text: str) -> float:
    """Extracts the pooled mean VMAF score from a libvmaf json log.

    Args:
        text (str): The content of the libvmaf json log file.

    Returns:
        float: The mean VMAF score.

    Raises:
        ValueError: If the log has no pooled vmaf score.
    """
    pooled = json.loads(text).get("pooled_metrics", {}).get("vmaf")
    if pooled is None or "mean" not in pooled:
        raise ValueError("libvmaf log has no pooled vmaf score")
    return float(pooled["mean"])


def _recipe_for_file(
    distorted: Path,
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
) -> VmafRecipe:
    """Derives the recipe from a file's own geometry and bit depth.

    The fallback for a caller that knows nothing about the source (the parity
    harness, a one-off measurement): the DISTORTED input already has the final
    geometry, so its dimensions are the analysis dimensions. An unreadable probe
    degrades to no dimensions known, which is the 1080p model at native size -
    the same measurement as before, never a guess at 4K.

    Args:
        distorted (Path): The encoded file to measure.
        crop (tuple | None): Crop bars applied to the REFERENCE (the distorted
            side is already cropped).
        hdr (bool): Whether both inputs are tone-mapped.

    Returns:
        VmafRecipe: The derived recipe.
    """
    from shrinkray.tools.ffprobe import bit_depth, run_ffprobe, try_get_height, try_get_width

    width = height = None
    depth = None
    try:
        probe = run_ffprobe(distorted)
        video = next(
            (s for s in probe.get("streams") or [] if s.get("codec_type") == "video"), None
        )
        if video is not None:
            width = try_get_width(video)
            height = try_get_height(video)
            depth = bit_depth(video)
    except (RuntimeError, OSError, ValueError):
        # an unreadable probe is not a measurement failure: it only means the
        # model cannot be chosen, and the 1080p model at native size is the
        # documented fallback (the pre-#308 behaviour)
        pass
    return vmaf_recipe(crop=crop, hdr=hdr, width=width, height=height, bit_depth=depth)


def measure_vmaf(
    distorted: Path,
    reference: Path,
    workdir: Path,
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    subsample: int | None = None,
    recipe: VmafRecipe | None = None,
) -> float:
    """Measures the mean VMAF of an encode against its source.

    The main/reference input order matters: the distorted stream must be the
    first input, otherwise scores are inflated. Both input paths are resolved
    to absolute form because ffmpeg runs with cwd=workdir.

    Args:
        distorted (Path): The encoded file.
        reference (Path): The pristine source file.
        workdir (Path): Working directory for the model and log files.
        crop (tuple | None): Crop bars (top, bottom, left, right) applied to
            the reference so it matches the distorted geometry.
        hdr (bool): Tone-map both sides for the measurement.
        cancel (threading.Event | None): Cooperative cancellation.
        db_file (Path | None): The database file, used to locate the logs
            directory for failure evidence (None => no failure bundle; the
            caller decides where telemetry lands).
        subsample (int | None): libvmaf n_subsample divisor (None = every
            frame). Used by the post-encode verification for large files.
        recipe (VmafRecipe | None): The measurement recipe (model, scale,
            pixel format). None derives one from the DISTORTED file's own
            dimensions and bit depth, so the model and the scale always follow
            the geometry being scored (issue #309) - a caller cannot silently
            measure a 4K file with the 1080p model. The engine passes an
            explicit one so the probe and the verification agree.

    Returns:
        float: The mean VMAF score.

    Raises:
        RuntimeError: If ffmpeg/libvmaf failed or produced no score.
        Cancelled: When the cancel event fires.
    """
    if recipe is None:
        recipe = _recipe_for_file(distorted, crop=crop, hdr=hdr)
    distorted = distorted.resolve()
    reference = reference.resolve()
    shutil.copyfile(MODELS_DIR / recipe.model, workdir / recipe.model)

    threads = min(cpu_count(), MAX_VMAF_THREADS)
    if detect_vmaf_style() != VMAF_STYLE_MODERN:
        # legacy libvmaf (ffmpeg < 6) is single-threaded regardless of the option
        threads = 0
    command = build_vmaf_command(
        distorted, reference, threads, crop=crop, hdr=hdr, subsample=subsample, recipe=recipe
    )
    # watchdog=False (issue #431): libvmaf is silent by design until the whole
    # file is scored - a full-precision measurement of a large movie produces
    # no output for its entire run, so the stall watchdog kills a healthy
    # measurement as hung. The cancel event remains the kill path.
    try:
        code, err = _run(
            command,
            cwd=workdir,
            cancel=cancel,
            watchdog=False,
        )
    except OSError as e:
        # the binary cannot spawn (missing after startup, EMFILE/ENOMEM):
        # the measurement's documented failure shape is RuntimeError (issue
        # #458; the runner stays unguarded per #401's record)
        raise RuntimeError(f"VMAF measurement cannot run '{command[0]}': {e}") from e
    if code == -1:
        raise Cancelled("cancelled during vmaf measurement")
    log_file = workdir / "vmaf.json"
    if code != 0 or not log_file.is_file():
        # preserve the evidence (a failure with an EMPTY stderr is the
        # worst case: the bundle keeps stderr, command and any partial
        # vmaf.json alive for diagnosis)
        save_failure(
            db_file=db_file,
            kind="vmaf-measure",
            command=command,
            stderr=err,
            workdir=workdir,
            artifacts=["vmaf.json"],
            context={
                "distorted": str(distorted),
                "reference": str(reference),
                "hdr": hdr,
                "crop": crop,
                "subsample": subsample,
                "recipe": recipe.identifier(),
                "returncode": code,
            },
        )
        raise RuntimeError(f"libvmaf failed: {err[-400:]}")

    try:
        return parse_vmaf_log(log_file.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"libvmaf log could not be parsed: {e}") from e


def _tool_versions(handbrake: str) -> str:
    """Reads the tool versions relevant for benchmark records.

    Args:
        handbrake (str): Path to the HandBrakeCLI binary.

    Returns:
        str: A compact version string, e.g. 'hb=1.11.2;libvmaf=v3'.
    """
    import re as _re
    import subprocess as _subprocess

    versions = []
    try:
        proc = _subprocess.run([handbrake, "--version"], capture_output=True, timeout=15)
        match = _re.search(r"HandBrake\s+([0-9.]+)", proc.stdout.decode(errors="replace"))
        if match:
            versions.append(f"hb={match.group(1)}")
    except (OSError, _subprocess.SubprocessError):
        pass
    try:
        style = detect_vmaf_style()
    except RuntimeError:
        # ffmpeg absent (CI runners, minimal NAS installs): version
        # metadata must never fail the encode result - the tool_versions
        # string is documentation, not function. Omit the libvmaf token.
        return ";".join(versions)
    versions.append("libvmaf=v3" if style == VMAF_STYLE_MODERN else "libvmaf=v2")
    return ";".join(versions)
