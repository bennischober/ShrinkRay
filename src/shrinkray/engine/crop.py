"""crop: baked-in black-bar detection (issue #97 phase 3, engine seam 1).

The one detector is the keyframe TIMELINE pass (issue #181, the #131 spike's
follow-up): cropdetect over the whole runtime decoding only keyframes
(`-skip_frame nokey`, `reset=1` so every keyframe reports its own rectangle),
readings filtered to SUSTAINED runs (a run-length in seconds - never
readings, the keyframe interval varies by encoder - with a floor of 3
readings so a sparse keyframe stream cannot satisfy it by accident), then
the SAFE union rule: crop only the bars common to every sustained reading
(docs/black-bar-spike.md Q4 - the naive "largest bars" combine measured at
keeping 0% of the frame; it must never ship).

The summary is cached in the database (the `crops` table, keyed by size and
mtime), so the expensive pass runs once per file version: plan/encode read
the cache, and the scan's coarse flag stays a separate cheap measurement
(scan never runs the timeline pass itself).
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any

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

# a sustained run's minimum length in SECONDS of runtime (never readings:
# the keyframe interval varies by encoder, seconds are invariant — spike Q2,
# measured median 1 s, worst case 3 s). A shorter run is dark-scene noise,
# not a segment of the film
MIN_RUN_SECONDS = 30.0
# the run's minimum READING count so a sparse keyframe stream cannot satisfy
# the seconds rule by accident (spike Q2's floor)
MIN_RUN_READINGS = 3


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


# the timeline pass's per-reading shape: the picture rectangle and when it
# was read (seconds into the file)
class CropReading:
    """One cropdetect reading from the keyframe timeline.

    Attributes:
        timestamp (float): Seconds into the file where the keyframe sits.
        width (int): The detected picture width.
        height (int): The detected picture height.
        x (int): The picture's left offset.
        y (int): The picture's top offset.
    """

    __slots__ = ("height", "timestamp", "width", "x", "y")

    def __init__(self, timestamp: float, width: int, height: int, x: int, y: int) -> None:
        self.timestamp = timestamp
        self.width = width
        self.height = height
        self.x = x
        self.y = y

    def rect(self) -> tuple[int, int, int, int]:
        """The reading as a (width, height, x, y) picture rectangle."""
        return (self.width, self.height, self.x, self.y)


# cropdetect's timeline line format (ffmpeg stderr, one line per keyframe)
_TIMELINE_RE = re.compile(r"x1:(-?\d+) x2:(-?\d+) y1:(-?\d+) y2:(-?\d+) w:(\d+) h:(\d+)")
_STAMP_RE = re.compile(r"t:([0-9.]+)")


def parse_crop_timeline(text: str) -> list[CropReading]:
    """Extracts every cropdetect reading from the keyframe-timeline output.

    Args:
        text (str): The ffmpeg stderr of the timeline pass (one cropdetect
            line per decoded keyframe).

    Returns:
        list[CropReading]: The readings in file order (one per keyframe).
    """
    readings: list[CropReading] = []
    for line in text.splitlines():
        if "Parsed_cropdetect" not in line:
            continue
        match = _TIMELINE_RE.search(line)
        if match is None:
            continue
        x1, _x2, y1, _y2, w, h = (int(g) for g in match.groups())
        stamp = _STAMP_RE.search(line)
        timestamp = float(stamp.group(1)) if stamp else 0.0
        readings.append(CropReading(timestamp, w, h, x1, y1))
    return readings


def sustained_runs(
    readings: list[CropReading],
    min_seconds: float = MIN_RUN_SECONDS,
    min_readings: int = MIN_RUN_READINGS,
) -> list[CropReading]:
    """Keeps the readings of every SUSTAINED rectangle run.

    A run is a maximal sequence of consecutive readings with the identical
    picture rectangle; it is sustained when it spans at least min_seconds of
    runtime AND has at least min_readings readings. A short run is dark-scene
    noise (the spike's dominant noise source, 18-26% of readings on the SDR
    corpus), not a segment of the film.

    Args:
        readings (list[CropReading]): The timeline pass's readings, in file
            order.
        min_seconds (float): The minimum run length in seconds of runtime.
        min_readings (int): The minimum readings per run (a floor so a file
            with sparse keyframes cannot satisfy the seconds rule by
            accident).

    Returns:
        list[CropReading]: The sustained readings, in file order.
    """
    sustained: list[CropReading] = []
    run: list[CropReading] = []

    def flush(current: list[CropReading]) -> None:
        if not current:
            return
        span = current[-1].timestamp - current[0].timestamp
        # a single reading spans 0 s; use the KEYFRAME INTERVAL as the span
        # for runs of one-two readings (they can never be sustained unless
        # the intervals themselves are huge - the readings floor catches that)
        if len(current) >= 2 and span < min_seconds:
            return
        if len(current) < min_readings:
            return
        sustained.extend(current)

    for reading in readings:
        if run and (run[-1].width, run[-1].height, run[-1].x, run[-1].y) != (
            reading.width,
            reading.height,
            reading.x,
            reading.y,
        ):
            flush(run)
            run = []
        run.append(reading)
    flush(run)
    return sustained


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


def run_crop_timeline(
    movie: Path,
    limit: int = CROP_DETECT_LIMIT,
    cancel: threading.Event | None = None,
) -> tuple[list[CropReading], tuple[int, int] | None]:
    """Runs the keyframe-only cropdetect pass over the whole runtime.

    One ffmpeg decode of every keyframe (the spike's measured cost:
    ~3 min/hour of runtime on 1080p, ~3.5 on 4K HDR). `reset=1` makes every
    keyframe report its own rectangle, so the readings form a timeline.

    Args:
        movie (Path): The source movie file.
        limit (int): The cropdetect blackness threshold (0-255).
        cancel (threading.Event | None): Cooperative cancellation (issue
            #384): a set event kills the pass within a poll tick and the
            caller caches nothing.

    Returns:
        tuple: The readings in file order and the (width, height) of the
            video frame. The frame is None when the stream could not be read
            or the pass did not complete: an ffmpeg that exits early (killed
            by ctrl+c or a closed terminal, a decode error, the timeout) has
            measured only part of the runtime, and a crop from part of the
            runtime can cut into picture a later segment shows.
    """
    from shrinkray.tools.ffprobe import probe_duration_seconds

    frame = source_frame(movie)
    if frame is None:
        return [], None
    if cancel is not None and cancel.is_set():
        return [], None
    # the pass's duration-derived ceiling (issue #62's protection, kept through
    # the runner: issue #384 adds the cancel event beside it)
    ceiling = max(int((probe_duration_seconds(movie) or 0) * 4) + 600, FFPROBE_TIMEOUT)
    try:
        code, err = _run(
            [
                FFMPEG_BINARY,
                "-hide_banner",
                "-nostats",
                "-skip_frame",
                "nokey",
                "-i",
                str(movie),
                "-map",
                "0:v:0",
                "-vf",
                f"cropdetect=limit={limit}:round=2:reset=1",
                "-an",
                "-sn",
                "-f",
                "null",
                "-",
            ],
            cancel=cancel,
            timeout=ceiling,
        )
    except OSError as e:
        # ffmpeg missing or not spawnable (EMFILE, ENOMEM): no measurement,
        # the caller falls back like any failed pass
        log(f"The crop timeline pass could not start for '{movie.name}': {e}")
        return [], None
    if code == -1 and cancel is not None and cancel.is_set():
        # cancelled (issue #384): a cancel caches nothing. Without the event a
        # -1 is a child that died of SIGHUP and is logged as a failure below
        return [], None
    if code == -2 and err.startswith(("timed out after", "stalled:")):
        # terminated at the ceiling or by the stall watchdog: the runner's
        # message says which. A child that died of SIGINT also returns -2,
        # but its stderr is ffmpeg's own and is logged as an exit code below
        log(f"The crop timeline pass failed for '{movie.name}': {err}")
        return [], None
    if code != 0:
        log(f"The crop timeline pass failed for '{movie.name}': ffmpeg exited {code}")
        return [], None
    return parse_crop_timeline(err), frame


def source_frame(movie: Path) -> tuple[int, int] | None:
    """Reads the video stream's (width, height), None when unreadable."""
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
        streams = json.loads(proc.stdout.decode())["streams"]
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as e:
        # OSError: ffprobe missing. TimeoutExpired: a stale mount blocking the
        # read (issue #62). ValueError covers unparsable output (-v quiet
        # leaves stdout empty when ffprobe fails). No frame size => no crop
        log(f"ffprobe could not read the frame size of '{movie.name}': {e}")
        return None
    try:
        stream = next(s for s in streams if s.get("codec_type") == "video")
        return (int(stream["width"]), int(stream["height"]))
    except (StopIteration, KeyError, ValueError, TypeError):
        log(f"'{movie.name}' has no video stream with a frame size: no crop")
        return None


def crop_summary_from_timeline(
    readings: list[CropReading],
    frame: tuple[int, int],
    min_seconds: float = MIN_RUN_SECONDS,
    min_readings: int = MIN_RUN_READINGS,
) -> CropSummary:
    """Reduces a timeline to the crop summary (the spike's Q4 reduction).

    The safe crop is the union of the SUSTAINED readings' picture areas (the
    bars common to every sustained reading); the aspect-ratio table lists
    every sustained rectangle's share of runtime, for the report only - the
    crop decision never uses it.

    Args:
        readings (list[CropReading]): The timeline pass's readings.
        frame (tuple): The (width, height) of the video frame.
        min_seconds (float): The sustained run's minimum length.
        min_readings (int): The sustained run's minimum reading count.

    Returns:
        CropSummary: The cacheable summary.
    """
    sustained = sustained_runs(readings, min_seconds, min_readings)
    bars = intersect_crop_rects([r.rect() for r in sustained], frame)

    # the aspect table: one entry per sustained rectangle, share of the
    # readings' runtime span (for the report's "variable aspect ratio" line)
    ratios: dict[tuple[int, int, int, int], float] = {}
    if sustained:
        run_start = sustained[0].timestamp
        current_rect = sustained[0].rect()
        spans: dict[tuple[int, int, int, int], float] = {}
        for reading in sustained[1:]:
            rect = reading.rect()
            if rect != current_rect:
                spans[current_rect] = spans.get(current_rect, 0.0) + (reading.timestamp - run_start)
                run_start = reading.timestamp
                current_rect = rect
        spans[current_rect] = spans.get(current_rect, 0.0) + (sustained[-1].timestamp - run_start)
        total = sum(spans.values())
        if total > 0:
            for rect, span in spans.items():
                ratios[rect] = span / total

    return CropSummary(
        bars=bars,
        frame_width=frame[0],
        frame_height=frame[1],
        readings=len(readings),
        sustained_readings=len(sustained),
        ratios=ratios,
    )


class CropSummary:
    """The timeline detector's cacheable result for one file version.

    Attributes:
        bars (tuple | None): The (top, bottom, left, right) bars to crop
            (None: no stable bars), the SAFE union over sustained readings.
        frame_width (int): The source frame's width.
        frame_height (int): The source frame's height.
        readings (int): Total readings the pass produced.
        sustained_readings (int): Readings that survived the run filter.
        ratios (dict): Picture rectangle -> share of sustained runtime
            (for the report only, never the crop decision).
    """

    __slots__ = (
        "bars",
        "frame_height",
        "frame_width",
        "ratios",
        "readings",
        "sustained_readings",
    )

    def __init__(
        self,
        bars: tuple[int, int, int, int] | None,
        frame_width: int,
        frame_height: int,
        readings: int,
        sustained_readings: int,
        ratios: dict[tuple[int, int, int, int], float],
    ) -> None:
        self.bars = bars
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.readings = readings
        self.sustained_readings = sustained_readings
        self.ratios = ratios

    @property
    def flag(self) -> int:
        """The scan-side 1/0 flag projection of this summary."""
        if self.bars is None or self.frame_width <= 0 or self.frame_height <= 0:
            return 0
        bar_w = self.frame_width - (self.frame_width - self.bars[3] - self.bars[2])
        bar_h = self.frame_height - (self.frame_height - self.bars[0] - self.bars[1])
        return 1 if bar_w > 0 or bar_h > 0 else 0


def get_crop_summary(connection: sqlite3.Connection, movie: Path) -> CropSummary | None:
    """Reads the file's cached summary from the raw crops row.

    db stores; the engine interprets: bars back to a tuple, the ratio JSON
    back to a dict. A garbled row (hand-edited db) is a miss, not a crash.

    Args:
        connection: The database connection.
        movie (Path): The source movie file.

    Returns:
        CropSummary | None: The cached summary, or None when absent/stale.
    """
    from shrinkray import db

    row = db.get_crop_row(connection, movie)
    if row is None:
        return None
    bars = None
    if row["bars"]:
        parts = [int(v) for v in str(row["bars"]).split(",")]
        if len(parts) == 4:
            bars = (parts[0], parts[1], parts[2], parts[3])
    try:
        pairs = json.loads(row["ratios"] or "[]")
        ratios = {
            (int(r[0][0]), int(r[0][1]), int(r[0][2]), int(r[0][3])): float(r[1]) for r in pairs
        }
    except (TypeError, ValueError):
        ratios = {}
    return CropSummary(
        bars=bars,
        frame_width=int(row["frame_width"]),
        frame_height=int(row["frame_height"]),
        readings=int(row["readings"]),
        sustained_readings=int(row["sustained_readings"]),
        ratios=ratios,
    )


def detect_crop_summary_cached(
    movie: Path,
    db_file: Path | None,
    cancel: threading.Event | None = None,
) -> CropSummary | None:
    """Returns the file's crop summary, computing and caching it on miss.

    The cache is keyed by (path, size, mtime) — the same identity rule the
    scan cache uses. db_file None (a stateless run) computes without caching.
    A failed pass returns None and caches nothing: the next run tries again,
    and the caller falls back to the sampled detector. A database that cannot
    be opened, read or written is logged and costs only the cache, never the
    encode.

    Args:
        movie (Path): The source movie file.
        db_file (Path | None): The database file (None: compute uncached).
        cancel (threading.Event | None): Cooperative cancellation (issue
            #384): a set event ends the timeline pass within a poll tick and
            caches nothing.

    Returns:
        CropSummary | None: The summary, or None when the pass failed.
    """
    if not movie.is_file():
        return None
    connection: sqlite3.Connection | None = None
    # one finally owns the connection on every exit, the cache hit included
    # (issue #480: the hit returned before the old finally and leaked it)
    try:
        if db_file is not None:
            try:
                from shrinkray import db

                connection = db.connect(db_file)
                cached = get_crop_summary(connection, movie)
                if cached is not None:
                    return cached
            except (OSError, RuntimeError, sqlite3.Error) as e:
                # a broken database must not stop the encode: compute uncached
                # (sqlite3.Error: a damaged or foreign metadata.db, issue #486)
                log(f"The crop cache is unavailable ({e}); computing uncached.")
                if connection is not None:
                    connection.close()
                    connection = None
        readings, frame = run_crop_timeline(movie, cancel=cancel)
        if frame is None:
            return None
        summary = crop_summary_from_timeline(readings, frame)
        if connection is not None:
            from shrinkray import db

            try:
                db.save_crop_summary(connection, movie, summary)
            except (OSError, sqlite3.Error) as e:
                # the measurement stands; only the next run's shortcut is lost
                # (the uncommitted upsert rolls back when the finally closes)
                log(f"The crop summary could not be cached ({e}); using it uncached.")
        return summary
    finally:
        if connection is not None:
            connection.close()


def detect_crop_rect(
    movie: Path,
    workdir: Path,
    count: int = DEFAULT_CROP_SAMPLE_COUNT,
    cancel: threading.Event | None = None,
) -> tuple[int, int, int, int] | None:
    """Detects baked-in black bars (letterbox) of a movie via cropdetect.

    Runs cropdetect on several segments and keeps the bars common to all of
    them, so only bars that are present throughout the movie are removed.
    The FALLBACK detector: the timeline pass (detect_crop_summary_cached) is
    the primary, this one keeps runs without a database working.

    Args:
        movie (Path): The source movie file.
        workdir (Path): Working directory (unused, kept for symmetry).
        count (int): The number of segments to probe.
        cancel (threading.Event | None): Cooperative cancellation (issue
            #384): a set event ends the sampled pass within a poll tick.

    Returns:
        tuple | None: The (top, bottom, left, right) bars to crop, or None
            if no stable bars were detected.

    Raises:
        RuntimeError: When ffmpeg cannot start (missing from PATH, EMFILE,
            ENOMEM): an unmeasured file must never read as "no bars"
            (issue #401).
    """
    from shrinkray.tools.ffprobe import probe_duration_seconds

    duration = probe_duration_seconds(movie)
    if duration is None:
        return None
    if cancel is not None and cancel.is_set():
        return None

    frame = source_frame(movie)
    if frame is None:
        return None

    from shrinkray.engine.samples import sample_offsets

    rects: list[tuple[int, int, int, int]] = []
    for offset in sample_offsets(duration, count):
        if cancel is not None and cancel.is_set():
            return None
        try:
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
                ],
                cancel=cancel,
            )
        except OSError as e:
            # ffmpeg missing from PATH or not spawnable (EMFILE/ENOMEM): the
            # probe step fails with the documented shape instead of a
            # traceback (issue #401). The timeline pass has its own #398
            # guard: DEGRADING here would misreport an unmeasured file as
            # "no bars" (invariant 8) - the run cannot proceed anyway, and
            # the entry points (CLI encode/plan, job worker, watcher) catch
            # RuntimeError and report it (the CLI encode aborts the batch:
            # the binary is gone for the rest of it too).
            raise RuntimeError(
                f"the sampled crop pass could not start ffmpeg for '{movie.name}': {e}"
            ) from e
        if code == -1 and cancel is not None and cancel.is_set():
            # cancelled (issue #384): the sampled pass measures nothing
            return None
        if code < 0:
            # the child died of a signal (the runner's stall watchdog, SIGKILL,
            # a crash): a crop from the windows left would lose its veto. A
            # SIGHUP/SIGINT/SIGTERM is caught by ffmpeg, which exits 255:
            # that is a failed window like any positive exit (issue #403)
            log(f"The sampled crop pass failed for '{movie.name}': ffmpeg exited {code}")
            return None
        if code != 0:
            # a positive exit (a failed decode, or 255 from a caught outside
            # signal - believed from fftools/ffmpeg.c, not run, issue #403's
            # comment) measured nothing: the veto rule (bars common to every
            # sampled frame) cannot be evaluated for this window, so the whole
            # file is left uncropped - the safe direction (a wrong crop cuts
            # into picture; a missed crop only costs space) and the same
            # outcome a signal-killed window gets above. INTERIM RULE: the
            # issue carries needs-decision and the maintainer's call is open -
            # this is the reading the repo's own documents argue for, applied
            # so the veto cannot be lost silently; a flip to skip-with-log
            # needs a documented safety argument (issue #403)
            log(f"The sampled crop pass failed for '{movie.name}': ffmpeg exited {code}")
            return None
        rect = parse_crop_line(err)
        if rect is None:
            # exit 0 but no crop line (cropdetect prints one per frame with
            # bars; a window past the stream's end prints none): this window
            # measured nothing either - the same veto consequence as the
            # failed window above, reviewed in PR #476 (finding 3)
            log(
                f"The sampled crop pass measured no bars in a window of '{movie.name}'; the file is left uncropped"
            )
            return None
        rects.append(rect)

    return intersect_crop_rects(rects, frame)


def has_black_bars(
    file: Path,
    dimensions: dict[str, Any],
    min_width: int = 100,
    min_height: int = 100,
) -> int:
    """Checks if a movie has (relevant) black bars by using the ffmpeg cropdetect filter.

    Some frames in the middle of the movie are checked against the codec data
    (using a threshold to be sure its not just some pixels that can be cut).

    Args:
        file (Path): Path to the movie file.
        dimensions (dict): Dictionary with the 'width' and 'height' of the video.
        min_width (int): Minimum width difference to detect black bars.
        min_height (int): Minimum height difference to detect black bars.

    Returns:
        int: 1 if black bars were detected, 0 if not (or it could not be checked).
    """
    if dimensions["width"] is None or dimensions["height"] is None:
        return 0

    try:
        pres = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                "90",
                "-i",
                str(file),
                "-vframes",
                "10",
                "-vf",
                "cropdetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=60,
        )
        cdetect = str(pres.stderr)
    except (OSError, subprocess.SubprocessError):
        return 0

    # cropdetect may not find any crop => no black bars
    rect = parse_crop_line(cdetect)
    if rect is None:
        return 0
    w, h = rect[0], rect[1]

    if w + min_width < dimensions["width"]:
        return 1
    if h + min_height < dimensions["height"]:
        return 1
    return 0
