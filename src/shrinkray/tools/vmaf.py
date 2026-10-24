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
from pathlib import Path

from shrinkray.telemetry import save_failure
from shrinkray.tools.ffprobe import FFPROBE_TIMEOUT
from shrinkray.tools.process import Cancelled, _run, cpu_count

FFMPEG_BINARY = "ffmpeg"

# Netflix's reference model (https://github.com/Netflix/vmaf), shipped with the
# package because old ffmpeg/libvmaf builds cannot load embedded models.
VMAF_MODEL = Path(__file__).parent.parent / "models" / "vmaf_v0.6.1.json"

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
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(
            f"ffmpeg ('{binary}') not found or did not answer within "
            f"{FFPROBE_TIMEOUT}s! Install a build with libvmaf support. ({e})"
        ) from e
    options = proc.stdout.decode(errors="replace")
    if "libvmaf" not in options and "libvmaf" not in proc.stderr.decode(errors="replace"):
        raise RuntimeError("ffmpeg has no libvmaf filter! Install a build with libvmaf support.")
    _vmaf_style = VMAF_STYLE_MODERN if "model_path" not in options else VMAF_STYLE_LEGACY
    return _vmaf_style


# libvmaf v3 thread cap: values above ~8 crash the filter with EINVAL on
# some builds (frame queue limit), so stay on the safe side
MAX_VMAF_THREADS = 8


def build_vmaf_command(
    distorted: Path,
    reference: Path,
    threads: int = 0,
    style: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    subsample: int | None = None,
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
    would score misleadingly. Both sides get the identical chain, so the
    relative quality measurement stays honest.

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

    Returns:
        list[str]: The command as argument list (run with cwd=workdir).
    """
    if style is None:
        style = detect_vmaf_style()
    if style == VMAF_STYLE_MODERN:
        model_option = "model=path=vmaf_v0.6.1.json"
    else:
        model_option = "model_path=vmaf_v0.6.1.json"

    vmaf = f"libvmaf=log_path=vmaf.json:log_fmt=json:{model_option}:n_threads={threads}"
    if subsample and subsample > 1:
        vmaf += f":n_subsample={int(subsample)}"

    # the tone-map chain: linear light -> bt709 primaries -> hable tonemap
    # -> back to video levels (validated on real HDR10 content)
    tonemap = (
        "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
        "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
        "format=yuv420p10le"
    )

    if hdr:
        # tone-map both sides to their SDR twins before comparing
        filtergraph = f"[0:v]{tonemap}[d];[1:v]{tonemap}[r];[d][r]{vmaf}"
    elif crop is not None:
        top, bottom, left, right = crop
        filtergraph = (
            f"[1:v]crop=iw-{left + right}:ih-{top + bottom}:{left}:{top}[r];[0:v][r]{vmaf}"
        )
    else:
        filtergraph = f"[0:v][1:v]{vmaf}"
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


def measure_vmaf(
    distorted: Path,
    reference: Path,
    workdir: Path,
    crop: tuple[int, int, int, int] | None = None,
    hdr: bool = False,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
    subsample: int | None = None,
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

    Returns:
        float: The mean VMAF score.

    Raises:
        RuntimeError: If ffmpeg/libvmaf failed or produced no score.
        Cancelled: When the cancel event fires.
    """
    distorted = distorted.resolve()
    reference = reference.resolve()
    model = workdir / VMAF_MODEL.name
    shutil.copyfile(VMAF_MODEL, model)

    threads = min(cpu_count(), MAX_VMAF_THREADS)
    if detect_vmaf_style() != VMAF_STYLE_MODERN:
        # legacy libvmaf (ffmpeg < 6) is single-threaded regardless of the option
        threads = 0
    command = build_vmaf_command(
        distorted, reference, threads, crop=crop, hdr=hdr, subsample=subsample
    )
    code, err = _run(
        command,
        cwd=workdir,
        cancel=cancel,
    )
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
