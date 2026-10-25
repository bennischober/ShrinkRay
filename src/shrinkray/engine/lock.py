"""Encode mutual exclusion: one encoder at a time per contended directory.

The job queue serializes jobs (one worker thread), but encoders can still
collide from three independent entry points:

- the job worker (UI-initiated encodes)
- the watch loop (in-box automation, runs in its own thread)
- any CLI process (`shrinkray encode ...`) running next to the app

Two encoders writing the same output filename (the watch loop and a job
both encoding an inbox title that also lives in the library produce the
same `output_dir / movie.name`) clobber each other's output — a corrupt
archive that passes the duration check only by luck. A lock file inside
the CONTENDED directory (pid-stamped, stale-detecting) makes the
collision impossible instead of unlikely.

Which directory is contended depends on the mode (issue #230):

- two-directory mode: the output directory — every writer writes into it.
- in place: the LIBRARY directory the swaps rename inside — the staging
directory each process creates for itself is a per-run path nothing else
ever locks, so a slot keyed on it excluded no one, and two encoders of one
file raced on the deterministic swap staging name (`replace.staging_path_for`).

A caller that is unsure must pass the directory its writes ultimately land
in; the lock file lives inside it and is removed on release.

The lock records the holder's pid and, where /proc exists, its start time. A
lock a killed process left behind can sit in a library folder that the next
run locks again, and in the container the server is pid 1 on every start: a
live pid alone would make the leftover look held until the stale window ran
out, naming the new process as the busy holder.
"""

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

from shrinkray.logger import log_warning
from shrinkray.tools.process import process_exists, process_start_ticks

_LOCK_STALE_SECONDS = 21600.0  # 6 h: long encodes are normal, crashes are not


class EncodeBusyError(RuntimeError):
    """Raised when another encoder already owns the output directory.

    The message names the holder so the user (or the job outcome) can say
    who is encoding and since when.
    """


def _lock_path(output_dir: Path) -> Path:
    """Returns the lock file path for an output directory.

    Args:
        output_dir (Path): The output directory being guarded.

    Returns:
        Path: The lock file inside the output directory.
    """
    return output_dir / ".shrinkray_encode.lock"


def _pid_alive(pid: int) -> bool:
    """Delegates to tools.process.process_exists (issue #97: subprocess
    calls live in tools/ only)."""
    return process_exists(pid)


def _holder_alive(raw: str) -> bool:
    """Tells whether the process a lock file names still holds it.

    Args:
        raw (str): The lock file's content: "<pid>" or "<pid> <start ticks>".

    Returns:
        bool: True when that pid is alive and, if the lock recorded a start
            time, it is still the same process (not a reused pid).
    """
    parts = raw.split()
    if not parts or not parts[0].isdigit():
        return False
    pid = int(parts[0])
    if not _pid_alive(pid):
        return False
    if len(parts) > 1 and parts[1].isdigit():
        return process_start_ticks(pid) == int(parts[1])
    return True  # a pid-only lock (an older version, or no /proc)


def acquire_encode_slot(output_dir: Path, holder: str = "encode") -> Path:
    """Acquires the encode slot for an output directory (blocking-free).

    Creates the output directory if needed, then claims the lock file
    atomically (O_CREAT|O_EXCL). A stale lock (dead pid or older than the
    stale window) is stolen with a warning. Concurrent claimants get the
    exception, never a wait - the product's answer to "already encoding"
    is a clear no, not a queue.

    Args:
        output_dir (Path): The output directory to guard.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Returns:
        Path: The lock file path (release_encode_slot takes it back).

    Raises:
        EncodeBusyError: When another live encoder holds the slot.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(output_dir)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # someone holds it: stale check
        try:
            age = time.time() - lock.stat().st_mtime
            raw = lock.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            age, raw = _LOCK_STALE_SECONDS + 1, ""  # unreadable => treat stale
        pid = raw.split()[0] if raw else "?"
        if _holder_alive(raw) and age < _LOCK_STALE_SECONDS:
            raise EncodeBusyError(
                f"another encoder is already using '{output_dir}' "
                f"(pid {pid}, started {age / 60:.0f} min ago)"
            ) from None
        log_warning(f"Stale encode lock in '{output_dir}' (pid {pid}) - stealing it.")
        _release_path(lock)
        return acquire_encode_slot(output_dir, holder)
    ticks = process_start_ticks(os.getpid())
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(str(os.getpid()) if ticks is None else f"{os.getpid()} {ticks}")
    return lock


def release_encode_slot(lock: Path) -> None:
    """Releases a held encode slot.

    Args:
        lock (Path): The lock file path returned by acquire_encode_slot.
    """
    _release_path(lock)


def _release_path(lock: Path) -> None:
    """Removes a lock file, tolerating races and missing files.

    Args:
        lock (Path): The lock file to remove.
    """
    with contextlib.suppress(OSError):
        lock.unlink()


@contextlib.contextmanager
def guard_encodes(output_dir: Path, holder: str = "encode") -> Iterator[Path]:
    """Context manager around a whole encode run (all entry points).

    Args:
        output_dir (Path): The output directory the run writes into.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Yields:
        Path: The lock file path.
    """
    lock = acquire_encode_slot(output_dir, holder)
    try:
        yield lock
    finally:
        release_encode_slot(lock)
