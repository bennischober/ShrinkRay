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
"""

import contextlib
import os
import threading
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


def acquire_encode_slot(output_dir: Path, holder: str = "encode") -> Path:
    """Acquires the encode slot for an output directory (blocking-free).

    Creates the output directory if needed, then claims the lock file
    atomically (O_CREAT|O_EXCL). A stale lock (dead pid, a reused pid whose
    start stamp does not match, or one older than the stale window - which
    guard_encodes' refresher keeps touched, so the window measures silence,
    not age) is stolen with a warning, and only the exact file that was
    read. Concurrent claimants get the exception, never a wait - the
    product's answer to "already encoding" is a clear no, not a queue.

    Args:
        output_dir (Path): The output directory to guard.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Returns:
        Path: The lock file path (release_encode_slot takes it back).

    Raises:
        EncodeBusyError: When another live encoder holds the slot.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # canonicalize BEFORE keying: a symlinked or relative path must not
    # produce a second lock file for one directory (the review on this PR,
    # finding 3) - every writer resolves, so one directory is one lock
    output_dir = output_dir.resolve()
    lock = _lock_path(output_dir)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # someone holds it: stale check. The pid alone is not identity (a
        # container restarts pids from 1, and the server is pid 1 - a lock
        # left by a SIGKILLed run reads as live after restart): the holder
        # stamps its process start time beside the pid, and a recorded start
        # that does not match the living pid's start means the pid was
        # REUSED and the lock is stale regardless of age (issue #336).
        raw_text: str | None = None
        try:
            lock_stat = lock.stat()
            age = time.time() - lock_stat.st_mtime
            raw_text = lock.read_text(encoding="ascii").strip()
            raw = raw_text.split()
            pid = int(raw[0]) if raw and raw[0].isdigit() else 0
            recorded_start = int(raw[1]) if len(raw) > 1 and raw[1].isdigit() else None
        except (OSError, ValueError):
            lock_stat = None
            raw_text = None
            age, pid, recorded_start = _LOCK_STALE_SECONDS + 1, 0, None  # unreadable => stale
        if pid and _pid_alive(pid) and age < _LOCK_STALE_SECONDS:
            started = process_start_ticks(pid)
            if started is None or recorded_start is None or started == recorded_start:
                raise EncodeBusyError(
                    f"another encoder is already using '{output_dir}' "
                    f"(pid {pid}, started {age / 60:.0f} min ago)"
                ) from None
            # the pid is alive but is NOT the process that wrote the lock:
            # a reused pid after a crash/restart - the lock is stale
        # steal ONLY the file that was read: between the read and this
        # unlink, another claimant may have stolen and RE-CREATED the lock
        # (the review's finding 4) - an unconditional unlink would delete a
        # live holder's brand-new lock and let two writers run. Identity is
        # checked by CONTENT (an inode can be reused by a delete+create on
        # some filesystems): a lock whose text changed since the read is no
        # longer the stale one this claimant judged.
        log_warning(f"Stale encode lock in '{output_dir}' (pid {pid or '?'}) - stealing it.")
        try:
            current_text = lock.read_text(encoding="ascii").strip()
        except OSError:
            current_text = None
        if current_text is not None and current_text != (raw_text or ""):
            raise EncodeBusyError(
                f"another encoder is already using '{output_dir}' "
                "(it claimed the slot while this one read the stale lock)"
            ) from None
        _release_path(lock)
        return acquire_encode_slot(output_dir, holder)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        start = process_start_ticks(os.getpid())
        handle.write(str(os.getpid()) if start is None else f"{os.getpid()} {start}")
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
    stop = threading.Event()

    def _refresh() -> None:
        while not stop.wait(3600.0):
            with contextlib.suppress(OSError):
                lock.touch()

    refresher = threading.Thread(target=_refresh, name="encode-lock-refresh", daemon=True)
    refresher.start()
    try:
        yield lock
    finally:
        stop.set()
        refresher.join(timeout=5)
        release_encode_slot(lock)
