"""ffprobe: the one place that runs and parses ffprobe (issue #97 phase 2).

Moved verbatim from media.py: the subprocess boundary for the external
ffprobe binary (run_ffprobe with its timeout) plus the pure parse helpers
that turn its output into typed facts (durations, resolution, HDR markers,
codec info).
"""

import json
import math
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from shrinkray.config import Conf, load_config
from shrinkray.logger import log

FFPROBE_TIMEOUT = 60  # seconds


def probe_duration_seconds(path: Path) -> float | None:
    """Reads the duration of a media file via ffprobe.

    Args:
        path (Path): The media file.

    Returns:
        float | None: The duration in seconds, or None if it could not be
            determined (e.g. missing ffprobe, truncated/unfinalized file).
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # OSError: ffprobe not installed (e.g. CI). TimeoutExpired: the file
        # sits behind a mount that went stale (NFS/SMB that went away) and
        # ffprobe blocks in the kernel — without a timeout the calling
        # thread would hang forever and the watch loop/job worker would
        # stall silently (issue #62). Duration unknown => caller skips.
        log(f"ffprobe could not read '{path}' within {FFPROBE_TIMEOUT}s: {e}")
        return None
    if proc.returncode != 0:
        return None
    try:
        duration = float(json.loads(proc.stdout.decode())["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if duration <= 0:
        return None
    return duration


def run_ffprobe(path: Path) -> dict:
    """Runs ffprobe on a media file and returns the parsed json output.

    Args:
        path (Path): Path to the media file.

    Returns:
        dict: The parsed ffprobe output with 'streams' and 'format'.

    Raises:
        RuntimeError: If ffprobe fails or returns invalid json.
    """
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
                str(path),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
        )
    except FileNotFoundError:
        # the interpreter's error for an absent binary is errno-13 on WSL
        # (its /init interop turns the failed execve into EACCES) - naming
        # the raw OSError sent the operator to chmod a file that does not
        # exist (issue #195)
        raise RuntimeError(
            f"ffprobe not found! Install ffmpeg (https://ffmpeg.org/download.html) "
            f"so '{path}' can be probed."
        ) from None
    except PermissionError:
        raise RuntimeError(
            "ffprobe could not be executed: check that the binary is executable, or install ffmpeg"
        ) from None
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"ffprobe failed for '{path}': {e}") from e

    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ffprobe error for '{path}': {stderr}")

    try:
        return cast("dict[str, Any]", json.loads(proc.stdout))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe returned invalid json for '{path}'") from e


def get_aspect_ratio(width: int, height: int) -> str:
    gcd = math.gcd(width, height)
    lhs = int(width / gcd)
    rhs = int(height / gcd)
    return f"{lhs}x{rhs}"


def parse_frame_rate(rate: str | None) -> float | None:
    """Parses a ffmpeg frame rate string like '25/1' or '29.97' into a float.

    Args:
        rate (str | None): Frame rate string, e.g. '25/1'.

    Returns:
        float | None: The frame rate as float, or None if it cannot be parsed.
    """
    if not rate or rate == "0/0":
        return None
    try:
        if "/" in rate:
            numerator_str, denominator_str = rate.split("/", 1)
            numerator = float(numerator_str)
            denominator = float(denominator_str)
            if denominator == 0:
                return None
            return numerator / denominator
        return float(rate)
        # division by zero / invalid strings are caught below
    except (ValueError, ZeroDivisionError):
        return None


def get_raw_duration(video: dict) -> float | None:
    """Calculates the raw duration from the frame count and frame rate.

    Args:
        video (dict): The video stream data from ffprobe.

    Returns:
        float | None: The duration in seconds, or None if it cannot be calculated.
    """
    duration_raw = None
    tags = video.get("tags") or {}
    frame_rate = parse_frame_rate(video.get("avg_frame_rate"))

    # frame count can be stored in different tags depending on the file/muxer
    frame_count = None
    for key in ("NUMBER_OF_FRAMES-eng", "NUMBER_OF_FRAMES", "nb_frames"):
        if key in tags:
            frame_count = tags[key]
            break

    if frame_count is None and "nb_frames" in video:
        frame_count = video["nb_frames"]

    if frame_count is not None and frame_rate:
        try:
            duration_raw = float(frame_count) / frame_rate
        except (TypeError, ValueError):
            return None
    return duration_raw


def get_duration(video: dict, language: str = "en") -> str | None:
    """Gets the duration from the stream tags, preferring the configured language.

    Args:
        video (dict): The video stream data from ffprobe.
        language (str): Preferred language for the DURATION tags, e.g. 'en'.

    Returns:
        str | None: The duration as 'HH:MM:SS' string, or None if not present.
    """
    duration = None
    tags = video.get("tags") or {}
    try:
        # search for the configured language first, then fallbacks
        keys = [f"DURATION-{language}", "DURATION-eng", "DURATION-de", "DURATION"]
        for key in keys:
            if key in tags:
                duration = str(tags[key].split(".")[0])
                break
        else:
            # no duration tag => fallback to the format duration
            return None
    except (AttributeError, KeyError, TypeError):
        return duration
    return duration


def seconds_to_timestr(seconds: float) -> str:
    """Converts seconds into a 'HH:MM:SS' string.

    Args:
        seconds (float): The duration in seconds.

    Returns:
        str: The duration as 'HH:MM:SS' string.
    """
    seconds = round(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def try_get_width(video: dict) -> int | None:
    width = None
    if "width" in video:
        width = video["width"]
    elif "coded_width" in video:
        width = video["coded_width"]
    return width


def try_get_height(video: dict) -> int | None:
    height = None
    if "height" in video:
        height = video["height"]
    elif "coded_height" in video:
        height = video["coded_height"]
    return height


def get_video_stream(streams: list) -> dict | None:
    """Finds the first video stream in the probe data.

    Args:
        streams (list): The streams list from ffprobe.

    Returns:
        dict | None: The video stream, or None if there is none.
    """
    for stream in streams:
        if stream.get("codec_type") == "video":
            return cast("dict[str, Any]", stream)
    return None


def get_codec_info(stream: dict) -> dict:
    """Extracts codec name, type, language and title from a stream.

    Args:
        stream (dict): A stream from ffprobe.

    Returns:
        dict: Dictionary with 'name', 'type', 'language' and 'title'.
    """
    tags = stream.get("tags") or {}
    return {
        "name": str(stream.get("codec_name", "")),
        "type": stream.get("codec_type", ""),
        "language": tags.get("language", ""),
        "title": tags.get("title", ""),
    }


def get_audio_languages(streams: list) -> str:
    """Collects the languages of all audio streams as comma separated string.

    Args:
        streams (list): The streams list from ffprobe.

    Returns:
        str: Comma separated languages, e.g. 'de, en'. Empty if there are none.
    """
    languages: list[str] = []
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        language = (stream.get("tags") or {}).get("language", "")
        if language and language not in languages:
            languages.append(language)
    return ", ".join(languages)


def is_hdr(video: dict, config: Conf) -> bool:
    """Checks if the video stream is HDR by comparing its color data with the config.

    Args:
        video (dict): The video stream data from ffprobe.
        config (Conf): The config settings.

    Returns:
        bool: True if the video is HDR.
    """
    color_space = str(video.get("color_space") or "")
    color_transfer = str(video.get("color_transfer") or "")

    for space in config.hdr_color_spaces:
        if space and space in color_space:
            return True
    return any(transfer and transfer in color_transfer for transfer in config.hdr_color_transfers)


def get_bitrate(probe: dict) -> float:
    """Gets the bitrate of the file in MBit/s.

    Args:
        probe (dict): The ffprobe output.

    Returns:
        float: The bitrate in MBit/s, 0 if not present.
    """
    bit_rate = probe.get("format", {}).get("bit_rate")
    if not bit_rate:
        return 0.00
    try:
        return float(round(Decimal(bit_rate) / (1024 * 1024), 2))
    except (InvalidOperation, ValueError):
        return 0.00


def get_data(path: Path, config: Conf | None = None) -> dict:
    """Reads the metadata of a media file.

    Args:
        path (Path): Path to the media file.
        config (Conf | None): The config settings. Loaded if not given.

    Returns:
        dict: Dictionary with the metadata of the file.
    """
    if config is None:
        config = load_config()

    # read the audio/video file => uses ffprobe command to extract all possible metadata
    probe = run_ffprobe(path)

    bitrate = get_bitrate(probe)
    streams = probe.get("streams") or []
    video = get_video_stream(streams)
    if video is None:
        raise TypeError(f"No video stream found in '{path}'!")

    codec = video.get("codec_name", "unknown")

    # for other codecs => needs to be included in the output file!
    other_codecs = [get_codec_info(stream) for stream in streams if stream is not video]

    # FPS (rounded to 2 decimals), '25' instead of '25.0'
    fps = parse_frame_rate(video.get("avg_frame_rate"))
    if fps:
        fps = round(fps, 2)
        fps = int(fps) if fps.is_integer() else fps

    duration = get_duration(video, config.language)
    duration_raw = get_raw_duration(video)

    # fallback: no duration tag => calculate 'HH:MM:SS' from the raw duration
    if duration is None and duration_raw is not None:
        duration = seconds_to_timestr(duration_raw)

    height = try_get_height(video)
    width = try_get_width(video)

    # might look for a better option => 16:9 - excel will convert this to datetime
    aspect_ratio = "0x0"
    if width is not None and height is not None:
        aspect_ratio = get_aspect_ratio(width, height)

    audio_languages = get_audio_languages(streams)
    hdr = is_hdr(video, config)

    return {
        "codec": codec,
        "other_codecs": other_codecs,
        "bitrate": bitrate,
        "duration": duration,
        "duration_raw": duration_raw,
        "fps": fps,
        "aspect_ratio": aspect_ratio,
        "dimensions": {"width": width, "height": height},
        "audio_languages": audio_languages,
        "hdr": hdr,
    }
