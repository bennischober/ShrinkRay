"""samples: probe-window computation and extraction (issue #97 phase 3, seam 2).

Moved verbatim from quality.py: the offset computation (evenly spread,
credits avoided) and the stream-copy extraction (a lossy extraction would
bias every measurement; the copy fallback to ffv1 keeps the reference
pristine).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from shrinkray.logger import log_warning
from shrinkray.tools.ffprobe import FFPROBE_TIMEOUT
from shrinkray.tools.process import Cancelled, _run

FFMPEG_BINARY = "ffmpeg"

DEFAULT_SAMPLE_COUNT = 3
DEFAULT_SAMPLE_SECONDS = 10.0
# probe density as a duration: one window per N seconds of runtime, so
# coverage is a fixed fraction (sample_seconds / sample_every) instead of
# shrinking as the movie gets longer. None keeps the count-based default.
DEFAULT_SAMPLE_EVERY = 600.0

# the engine's live-progress callback: stage detail and counters, never persisted
# (quality.py holds the same alias; the two merge when the engine split finishes)
ProgressCallback = Callable[[dict], None]


def _check_cancel(cancel: threading.Event | None) -> None:
    """Raises Cancelled when the cancel event is set."""
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled")


def sample_offsets(duration: float, count: int) -> list[float]:
    """Computes the sample start offsets evenly spread over the duration.

    The offsets avoid the first and last seconds (credits/lead-in) and are
    spread between 10% and 90% of the duration.

    Args:
        duration (float): The duration in seconds.
        count (int): The number of samples.

    Returns:
        list[float]: The start offsets in seconds.
    """
    if duration <= 0 or count <= 0:
        return []
    start, end = duration * 0.1, duration * 0.9
    if count == 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + i * step for i in range(count)]


def extract_samples(
    movie: Path,
    workdir: Path,
    count: int,
    seconds: float,
    cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> list[tuple[Path, float]]:
    """Extracts sample clips for probing by stream-copying the source.

    The samples are the VMAF *reference* and the probe encode's input, so a
    re-encode here would bias both sides of every measurement: a lossy pass
    smooths grain and fine detail, making the probe source easier than the real
    one and the chosen CRF too high. A stream copy keeps the reference
    pristine, and it is I/O-bound rather than CPU-bound.

    `-ss` before `-i` with `-c:v copy` starts the clip at the keyframe at or
    before the offset, so the clip is slightly longer than `seconds`. Both the
    probe encode and the VMAF reference read the same file, so the comparison
    stays frame-aligned; `-avoid_negative_ts make_zero` normalises the
    timestamps open-GOP sources otherwise carry into the clip.

    If the container cannot hold the source codec the copy fails and the clip is
    re-extracted losslessly (ffv1), which keeps the reference pristine but
    costs a decode + encode; that fallback is logged so the operator knows the
    extraction was expensive.

    Args:
        movie (Path): The source movie file.
        workdir (Path): Directory for the sample files.
        count (int): The number of samples.
        seconds (float): The length of each sample in seconds.
        cancel (threading.Event | None): Cooperative cancellation.
        progress (callable | None): Callback progress(dict) for live
            step updates (flow to the UI, never persisted).

    Returns:
        list[tuple[Path, float]]: The sample files with their start offsets
            in seconds (the offset is part of a probe's cache identity).

    Raises:
        RuntimeError: If a sample could not be extracted.
        Cancelled: When the cancel event fires.
    """
    _check_cancel(cancel)
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(movie),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT,
        )
    except FileNotFoundError as e:
        # ffprobe missing after startup (issue #458): the step's documented
        # failure shape with the binary named, not a traceback
        raise RuntimeError(f"sample extraction could not start: ffprobe ({e})") from e
    except subprocess.SubprocessError as e:
        # TimeoutExpired: a stale mount (NFS/SMB that went away) blocks the
        # read in the kernel — without a timeout this call would hang the
        # job worker/watch loop forever with no error (issue #62).
        raise RuntimeError(f"ffprobe timed out after {FFPROBE_TIMEOUT}s for '{movie}': {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for '{movie}'")
    duration = float(json.loads(proc.stdout.decode())["format"]["duration"])

    samples: list[tuple[Path, float]] = []
    for index, offset in enumerate(sample_offsets(duration, count)):
        _check_cancel(cancel)
        if progress:
            progress(
                {
                    "stage": "probe",
                    "detail": f"extracting sample {index + 1}/{count}",
                    "done": index,
                    "total": count,
                }
            )
        sample = workdir / f"sample_{index}.mkv"
        try:
            code, err = _run(
                [
                    FFMPEG_BINARY,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{offset:.3f}",
                    "-t",
                    f"{seconds:.3f}",
                    "-i",
                    str(movie),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-an",
                    str(sample),
                ],
                cancel=cancel,
            )
        except OSError as e:
            # ffmpeg missing or not spawnable (EMFILE, ENOMEM): the step's
            # documented failure shape, not a traceback (issue #458)
            raise RuntimeError(
                f"sample extraction could not start at {offset:.0f}s: ffmpeg ({e})"
            ) from e
        if code != 0 or not sample.is_file() or sample.stat().st_size == 0:
            # the container cannot hold the source codec: re-extract losslessly
            # so the reference stays pristine, and say so - this path costs a
            # full decode and encode instead of a copy
            log_warning(
                f"Sample extraction could not stream-copy '{movie.name}' "
                f"at {offset:.0f}s; re-extracting losslessly (slower)."
            )
            try:
                code, err = _run(
                    [
                        FFMPEG_BINARY,
                        "-y",
                        "-v",
                        "error",
                        "-ss",
                        f"{offset:.3f}",
                        "-t",
                        f"{seconds:.3f}",
                        "-i",
                        str(movie),
                        "-map",
                        "0:v:0",
                        "-c:v",
                        "ffv1",
                        "-avoid_negative_ts",
                        "make_zero",
                        "-an",
                        str(sample),
                    ],
                    cancel=cancel,
                )
            except OSError as e:
                # same failure class for the lossless re-extract (issue #458)
                raise RuntimeError(
                    f"sample extraction could not start at {offset:.0f}s: {e}"
                ) from e
        if code != 0 or not sample.is_file() or sample.stat().st_size == 0:
            raise RuntimeError(f"sample extraction failed at {offset:.0f}s: {err[-400:]}")
        samples.append((sample, offset))
    return samples
