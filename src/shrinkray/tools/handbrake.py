"""handbrake: the HandBrakeCLI invocation boundary (issue #97 phase 2).

Moved verbatim from encode.py and quality.py: binary discovery
(find_handbrake with its Windows install locations) and the command
builder (quality/CRF mode, the only encode mode). The RUNNER
(_encode_with_command, progress parsing, disk watchdog) stays engine-side:
it is pipeline logic that USES this command.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

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


def build_encode_command(
    binary: str,
    input_file: Path,
    output_file: Path,
    crf: int,
    encoder: str,
    encoder_preset: str,
    crop: tuple[int, int, int, int] | None = None,
    encoder_threads: int | None = None,
    audio_languages: list[str] | None = None,
    subtitle_languages: list[str] | None = None,
) -> list[str]:
    """Builds the HandBrakeCLI command for a quality-driven encode.

    The quality is set via --quality (CRF) and the speed via --encoder-preset.

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
        audio_languages (list | None): ISO 639-2 codes to keep; None/empty
            keeps every language (the default). The flag pair travels
            together: --audio-lang-list alone sets selection to first.
        subtitle_languages (list | None): Same, for subtitle tracks.

    The container follows the source's extension (an .mkv source pins
    av_mkv; anything else is muxed by its own extension) so a file's bytes
    and its name never disagree; the 1:1 check verifies what the muxer kept.

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
    if input_file.suffix.lower() == ".mkv":
        # the container is PINNED for MKV sources: an explicit muxer cannot
        # drift from the output name (issue #240). A non-MKV source keeps
        # the extension-derived muxer (bytes and name must agree), and the
        # 1:1 check catches whatever that muxer drops silently - a dropped
        # track fails the encode's verification and blocks the swap
        # (review finding 3).
        command += ["-f", "av_mkv"]
    command += [
        # keep all audio and subtitle tracks, copy audio => no quality loss
        "--all-audio",
        "--all-subtitles",
        "--aencoder",
        "copy",
        # the passthru mask: every codec HandBrake 1.6-1.11 can pass through
        # untouched; a track outside it falls to the LOSSLESS fallback
        # (flac24 preserves the samples; aac would re-encode a track we
        # claim to copy; copy:pcm is deliberately absent - it exists only
        # in 1.11+ and an unknown name fails the whole job)
        "--audio-copy-mask",
        "copy:aac,copy:ac3,copy:dts,copy:dtshd,copy:eac3,copy:flac,copy:alac,"
        "copy:mp2,copy:mp3,copy:opus,copy:truehd,copy:vorbis",
        "--audio-fallback",
        "flac24",
        # chapters survive the encode
        "--markers",
        "--optimize",  # web optimized => mp4/mkv file can start streaming
    ]
    # language filters (issue #240): the reference preset's lists, now
    # config keys. The flags MUST travel together with --all-audio /
    # --all-subtitles: --audio-lang-list alone sets the selection
    # behaviour to 'first', which would drop every track after the first.
    if audio_languages:
        command += ["--audio-lang-list", ",".join(audio_languages)]
    if subtitle_languages:
        command += ["--subtitle-lang-list", ",".join(subtitle_languages)]
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


# HandBrake's progress line, e.g.
#   Encoding: task 1 of 1, 42.31 % (123.45 fps, avg 120.00 fps, ETA 00h12m34s)
# The percent, the two frame rates and the ETA are one external fact: the one
# parser lives here beside the command builder, and the runner in
# engine/pipeline.py only consumes it (issue #631).
_PROGRESS_LINE = re.compile(
    r"Encoding:.*?(?P<percent>\d{1,3}\.\d{2})\s*%"
    r"(?:.*?\((?P<fps>\d+(?:\.\d+)?)\s*fps,\s*avg\s+(?P<avg_fps>\d+(?:\.\d+)?)\s*fps"
    r"(?:,\s*ETA\s+(?P<eta_h>\d+)h(?P<eta_m>\d+)m(?P<eta_s>\d+)s)?)?"
)


class ProgressFacts(TypedDict):
    """The facts one HandBrake progress line reports (issue #631)."""

    percent: float
    fps: float | None
    avg_fps: float | None
    eta_seconds: int | None


def parse_progress_line(line: str) -> ProgressFacts | None:
    """Parses one HandBrake progress line into its reported facts.

    The line carries everything the dashboard shows for the final encode —
    percent, current fps, average fps and the ETA — and HandBrake omits the
    parenthesised block on some builds. A line with no percent is not a
    progress line (a banner or a log line), so it returns None; a line with a
    percent but no parenthesised block returns the percent with null fields
    rather than crashing or inventing numbers.

    Args:
        line (str): One stdout line (already split on ``\\r``/``\\n``).

    Returns:
        ProgressFacts | None: ``{"percent", "fps", "avg_fps", "eta_seconds"}``
            or None when the line carries no percent. ``fps``/``avg_fps``/
            ``eta_seconds`` are None when the line does not print them.
    """
    match = _PROGRESS_LINE.search(line)
    if match is None:
        return None
    eta: int | None = None
    if match.group("eta_h") is not None:
        eta = (
            int(match.group("eta_h")) * 3600
            + int(match.group("eta_m")) * 60
            + int(match.group("eta_s"))
        )
    return {
        "percent": float(match.group("percent")),
        "fps": float(match.group("fps")) if match.group("fps") is not None else None,
        "avg_fps": (float(match.group("avg_fps")) if match.group("avg_fps") is not None else None),
        "eta_seconds": eta,
    }
