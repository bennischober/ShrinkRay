"""handbrake: the HandBrakeCLI invocation boundary (issue #97 phase 2).

Moved verbatim from encode.py and quality.py: binary discovery
(find_handbrake with its Windows install locations) and the two command
builders (fixed-preset and quality/CRF mode). The RUNNER
(_encode_with_command, progress parsing, disk watchdog) stays engine-side:
it is pipeline logic that USES these commands.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

HANDBRAKE_BINARY = "HandBrakeCLI"
PROBE_TIMEOUT = 60  # seconds


def find_handbrake() -> str | None:
    """Finds the HandBrakeCLI binary on this system.

    Returns:
        str | None: The path to the binary, or None if it was not found.
    """
    binary = shutil.which(HANDBRAKE_BINARY)
    if binary:
        return binary

    # common windows install locations
    for path in (
        r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
        r"C:\Program Files (x86)\HandBrake\HandBrakeCLI.exe",
    ):
        if Path(path).is_file():
            return path
    return None


def build_command(
    binary: str,
    input_file: Path,
    output_file: Path,
    preset: str,
    encoder: str,
    encoder_threads: int | None = None,
) -> list[str]:
    """Builds the HandBrakeCLI command for re-encoding a file.

    Args:
        binary (str): Path to the HandBrakeCLI binary.
        input_file (Path): The file to re-encode.
        output_file (Path): The target file.
        preset (str): The HandBrake preset, e.g. 'Fast 1080p30'.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        encoder_threads (int | None): Thread limit for the encoder
            (for parallel encoding across multiple movies). None = no limit.

    Returns:
        list[str]: The command as argument list.
    """
    command = [
        binary,
        "--input",
        str(input_file),
        "--output",
        str(output_file),
        "--preset",
        preset,
        "--encoder",
        encoder,
        # keep all audio and subtitle tracks, copy audio => no quality loss
        "--all-audio",
        "--all-subtitles",
        "--aencoder",
        "copy",
        "--optimize",  # web optimized => mp4/mkv file can start streaming
    ]
    if encoder_threads is not None:
        # HandBrakeCLI has no thread flag, and SVT-AV1 has no `threads`
        # option: --encopts pairs go to svt_av1_enc_parse_parameter, which
        # would silently ignore `threads=N` (issue #21). SVT-AV1's `pin`
        # restricts execution to the first N cores, which is what the cap
        # means here.
        command += ["--encopts", f"pin={encoder_threads}"]
    return command


def build_encode_command(
    binary: str,
    input_file: Path,
    output_file: Path,
    crf: int,
    encoder: str,
    encoder_preset: str,
    crop: tuple[int, int, int, int] | None = None,
    encoder_threads: int | None = None,
) -> list[str]:
    """Builds the HandBrakeCLI command for a quality-driven encode.

    Unlike the fixed preset in encode.py, the quality is set via --quality
    (CRF) and the speed via --encoder-preset.

    Args:
        binary (str): Path to the HandBrakeCLI binary.
        input_file (Path): The file to re-encode.
        output_file (Path): The target file.
        crf (int): The CRF quality value.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        encoder_preset (str): The encoder speed preset to encode at.
        crop (tuple | None): Crop bars (top, bottom, left, right) to remove
            baked-in black bars. None keeps the source resolution.
        encoder_threads (int | None): Thread limit for the encoder
            (None = no limit). Keeps cores free for other work.

    Returns:
        list[str]: The command as argument list.
    """
    command = [
        binary,
        "--input",
        str(input_file),
        "--output",
        str(output_file),
        "--encoder",
        encoder,
        "--encoder-preset",
        encoder_preset,
        "--quality",
        str(crf),
    ]
    if encoder_threads is not None:
        # HandBrakeCLI has no thread flag, and SVT-AV1 has no `threads`
        # option: --encopts pairs go to svt_av1_enc_parse_parameter, which
        # would silently ignore `threads=N` (issue #21). SVT-AV1's `pin`
        # restricts execution to the first N cores, which is what the cap
        # means here.
        command += ["--encopts", f"pin={encoder_threads}"]
    if crop is not None:
        top, bottom, left, right = crop
        # HandBrake crop order is top:bottom:left:right
        command += ["--crop", f"{top}:{bottom}:{left}:{right}"]
    else:
        # keep the source resolution: HandBrake's auto-crop would letterbox
        # crops break the VMAF comparison (dimension mismatch) and change
        # the output resolution of the library
        command += ["--crop", "0:0:0:0"]
    command += [
        # keep all audio and subtitle tracks, copy audio => no quality loss
        "--all-audio",
        "--all-subtitles",
        "--aencoder",
        "copy",
        "--optimize",  # web optimized => mp4/mkv file can start streaming
    ]
    return command


# HandBrake 1.6 is the first release whose SVT-AV1 encoder id is 'svt_av1';
# older builds only know 'svt_av1_10bit' / 'x265', so asking for svt_av1
# fails as an argument error mid-run instead of at preflight.
MIN_HANDBRAKE_VERSION = "1.6"


def handbrake_version(binary: str) -> str | None:
    """Returns the HandBrakeCLI version string, or None when unreadable.

    Args:
        binary (str): Path to the HandBrakeCLI binary.

    Returns:
        str | None: The version line, or None when the probe failed.
    """
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    return text.splitlines()[0] if text else None


def supports_svt_av1(binary: str) -> bool:
    """Reports whether this HandBrakeCLI knows the svt_av1 encoder id.

    The encoder list is the only honest test: a version string can be
    backported, while the list is what HandBrake will actually accept.

    Args:
        binary (str): Path to the HandBrakeCLI binary.

    Returns:
        bool: True when 'svt_av1' is among the available encoders.
    """
    try:
        proc = subprocess.run(
            [binary, "--encoder-preset-list", "svt_av1"],
            capture_output=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    # the preset list lands on stderr in real builds (libhb writes its banner
    # and the list to stderr; stdout stays empty — found by the container
    # smoke test, issue #165), so probe the MERGED output like the version
    # probe does
    presets = (proc.stdout + proc.stderr).decode(errors="replace").split()
    # an unknown encoder id answers with only the generic 'auto' preset
    return any(preset and preset != "auto" for preset in presets)
