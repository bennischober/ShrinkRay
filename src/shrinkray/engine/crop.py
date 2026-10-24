"""crop: baked-in black-bar detection (issue #97 phase 3, engine seam 1).

Moved verbatim from quality.py: the ffmpeg cropdetect runner and the
bar-decision logic (only bars every sample agrees on are removed; noise
bars below MIN_BAR_PERCENT are ignored). helpers.check_black_bars is the
scan-side detector and stays put until phase 5 merges the two detectors
(the record: one detector, one home).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from shrinkray.logger import log
from shrinkray.tools.ffprobe import FFPROBE_TIMEOUT
from shrinkray.tools.process import _run

FFMPEG_BINARY = "ffmpeg"

# a detected bar smaller than this percentage of the dimension is noise
MIN_BAR_PERCENT = 2.0
# blackness threshold for cropdetect (0-255); 24 matches ffmpeg default
CROP_DETECT_LIMIT = 24

# cropdetect windows per movie: with the bars common to every sample the
# crop is monotone non-increasing in the sample count, so more samples only
# ever shrink it. That is the safe direction for an irreversible crop, and it
# makes an aspect-ratio switch much less likely to be missed (issue #17).
DEFAULT_CROP_SAMPLE_COUNT = 10


def parse_crop_line(text: str) -> tuple[int, int, int, int] | None:
    """Extracts the last cropdetect crop rectangle from ffmpeg output.

    Args:
        text (str): The ffmpeg stderr output containing cropdetect lines.

    Returns:
        tuple | None: The (width, height, x, y) crop rectangle, or None if
            no crop was detected.
    """
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", text)
    if not matches:
        return None
    last = matches[-1]
    return tuple(int(value) for value in last)  # type: ignore[return-value]


def intersect_crop_rects(
    rects: list[tuple[int, int, int, int]], frame: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """Intersects picture rectangles => only bars present in ALL samples.

    cropdetect reports the *picture* rectangle of each sample, so keeping the
    picture area common to every sample means cropping only the bars that every
    sample agrees on. A sample without bars keeps the full frame, which vetoes
    the crop entirely - no picture content can be removed.

    Args:
        rects (list): Detected (width, height, x, y) picture rectangles.
        frame (tuple): The (width, height) of the source frame.

    Returns:
        tuple | None: The (top, bottom, left, right) bars to crop, or None
            if no stable bars were detected or they are too small to matter.
    """
    if not rects:
        return None
    frame_w, frame_h = frame

    # union of the picture areas = the bars common to every sample
    left = min(x for _, _, x, _ in rects)
    top = min(y for _, _, _, y in rects)
    right = max(x + w for w, _, x, _ in rects)
    bottom = max(y + h for _, h, _, y in rects)

    width = max(0, right - left)
    height = max(0, bottom - top)

    # the removed BAR sizes must be significant (2% of the dimension), noise
    # sized bars are ignored; keep even dimensions (encoder requirement)
    bar_w = frame_w - width
    bar_h = frame_h - height
    min_w = frame_w * MIN_BAR_PERCENT / 100
    min_h = frame_h * MIN_BAR_PERCENT / 100
    if (bar_w > 0 and bar_w < min_w) or (bar_h > 0 and bar_h < min_h):
        return None
    if bar_w == 0 and bar_h == 0:
        return None
    width -= width % 2
    height -= height % 2

    if width >= frame_w and height >= frame_h:
        return None
    # convert the picture rectangle into HandBrake's bar order
    bars_left = left - left % 2
    bars_top = top - top % 2
    return (
        bars_top,
        max(0, frame_h - bars_top - height),
        bars_left,
        max(0, frame_w - bars_left - width),
    )


def detect_crop_rect(
    movie: Path, workdir: Path, count: int = DEFAULT_CROP_SAMPLE_COUNT
) -> tuple[int, int, int, int] | None:
    """Detects baked-in black bars (letterbox) of a movie via cropdetect.

    Runs cropdetect on several segments and keeps the bars common to all of
    them, so only bars that are present throughout the movie are removed.

    Args:
        movie (Path): The source movie file.
        workdir (Path): Working directory (unused, kept for symmetry).
        count (int): The number of segments to probe.

    Returns:
        tuple | None: The (top, bottom, left, right) bars to crop, or None
            if no stable bars were detected.
    """
    from shrinkray.tools.ffprobe import probe_duration_seconds

    duration = probe_duration_seconds(movie)
    if duration is None:
        return None

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                str(movie),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # OSError: ffprobe missing. TimeoutExpired: stale mount blocking the
        # kernel read — without a timeout the watch loop/job worker stalls
        # forever with no error (issue #62). No frame size => no crop.
        log(f"ffprobe could not read '{movie}' within {FFPROBE_TIMEOUT}s: {e}")
        return None
    try:
        stream = next(
            s for s in json.loads(proc.stdout.decode())["streams"] if s.get("codec_type") == "video"
        )
        frame = (int(stream["width"]), int(stream["height"]))
    except (StopIteration, KeyError, ValueError, json.JSONDecodeError):
        return None

    from shrinkray.engine.samples import sample_offsets

    rects: list[tuple[int, int, int, int]] = []
    for offset in sample_offsets(duration, count):
        code, err = _run(
            [
                FFMPEG_BINARY,
                "-y",
                "-v",
                "info",
                "-ss",
                f"{offset:.3f}",
                "-t",
                "2",
                "-i",
                str(movie),
                "-vf",
                f"cropdetect=limit={CROP_DETECT_LIMIT}:round=2:reset=0",
                "-f",
                "null",
                "-",
            ]
        )
        if code != 0:
            continue
        rect = parse_crop_line(err)
        if rect is not None:
            rects.append(rect)

    return intersect_crop_rects(rects, frame)
