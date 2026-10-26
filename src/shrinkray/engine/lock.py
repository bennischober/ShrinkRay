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
the CONTENDED directory (heartbeat-detected) makes the collision
impossible instead of unlikely.

Which directory is contended depends on the mode (issue #230):

- two-directory mode: the output directory — every writer writes into it.
- in place: the LIBRARY directory the swaps rename inside — the staging
  directory each process creates for itself is a per-run path nothing else
  ever locks, so a slot keyed on it excluded no one, and two encoders of one
  file raced on the deterministic swap staging name (`replace.staging_path_for`).

A caller that is unsure must pass the directory its writes ultimately land
in; the lock file lives inside it and is removed on release.

Staleness is a heartbeat, not a pid (issue #383): the holder touches the
file every minute, and a file that has been silent for the stale window is
reclaimed. Pids cannot answer the question in the target deployment — the
server is pid 1 in its container on every start, a one-shot `docker run`
is pid 1 in its own namespace, and pids are reused after a restart — so a
live holder's lock must be honoured without consulting them. The pid stays
in the file only to name the holder in the busy message.
"""

import contextlib
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from shrinkray.logger import log_warning

# how often the holder proves it is still alive (a touch of the lock file)
_HEARTBEAT_SECONDS = 60.0

# five missed beats: the window measures SILENCE, not the age of the run, so a
# multi-hour encode keeps its slot and a killed one is reclaimed in minutes
_LOCK_STALE_SECONDS = 300.0


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


def _read_holder(lock: Path) -> tuple[int, str]:
    """Returns the pid and label a lock names, best effort.

    The stamp is a diagnostic only: the staleness decision is the file's
    mtime, never the pid (issue #383). A half-written or garbage file
    yields pid 0 and an empty label.

    Args:
        lock (Path): The lock file.

    Returns:
        tuple[int, str]: The holder's pid (0 when unreadable) and its label.
    """
    fields = _read_fields(lock)
    pid = int(fields[0]) if fields and fields[0].isdigit() else 0
    return pid, fields[1] if len(fields) > 1 else ""


def _read_fields(lock: Path) -> list[str]:
    """Returns the lock file's whitespace-separated fields ([] when unreadable).

    Args:
        lock (Path): The lock file.

    Returns:
        list[str]: The stamp's fields.
    """
    try:
        return lock.read_text(encoding="ascii").split()
    except OSError:
        return []


# the token this process wrote into each lock it holds, by lock path. The pid
# cannot answer "is this still my lock?" - the server and a one-shot container
# are both pid 1 - so release compares the token it wrote (issue #383)
_OWNED: dict[Path, str] = {}


def _silence_seconds(lock: Path) -> float | None:
    """Returns how long the lock has been silent, or None when it is gone.

    Args:
        lock (Path): The lock file.

    Returns:
        float | None: Seconds since its last heartbeat (its mtime).
    """
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return None


def _busy_message(output_dir: Path, lock: Path, silence: float) -> str:
    """Builds the EncodeBusyError text naming the holder and its silence.

    Args:
        output_dir (Path): The guarded directory.
        lock (Path): The lock file.
        silence (float): Seconds since the holder's last heartbeat.

    Returns:
        str: The message.
    """
    pid, label = _read_holder(lock)
    who = f"pid {pid}" if pid else "an unknown process"
    if label:
        who = f"{label}, {who}"
    return (
        f"another encoder is already using '{output_dir}' "
        f"({who}; last heartbeat {silence / 60:.0f} min ago)"
    )


def acquire_encode_slot(output_dir: Path, holder: str = "encode") -> Path:
    """Acquires the encode slot for an output directory (blocking-free).

    Creates the output directory if needed, then claims the lock file
    atomically (O_CREAT|O_EXCL). A lock that has been silent for the stale
    window is stolen with a warning — and only if it is still silent when
    the steal re-checks it, so a heartbeat that lands in between wins.
    Concurrent claimants get the exception, never a wait - the product's
    answer to "already encoding" is a clear no, not a queue.

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
        silence = _silence_seconds(lock)
        if silence is None:
            # it vanished between the failed create and the stat: retry the
            # claim, which is now free
            return acquire_encode_slot(output_dir, holder)
        if silence < _LOCK_STALE_SECONDS:
            # the holder's heartbeat is recent: busy, whoever the pid names.
            # This also covers the window between O_CREAT and the stamp write,
            # where the file is still empty (issue #383): a fresh empty file
            # is a live claim, not a stale one.
            raise EncodeBusyError(_busy_message(output_dir, lock, silence)) from None
        # steal ONLY a lock that is still silent when we unlink it: between
        # the read above and this check, the holder may have beaten again or
        # another claimant may have stolen and re-created it (the review on
        # this PR, finding 4). The mtime - not the text - is the identity,
        # because a live holder's text never changes.
        log_warning(f"Stale encode lock in '{output_dir}' - stealing it.")
        again = _silence_seconds(lock)
        if again is not None and again < _LOCK_STALE_SECONDS:
            raise EncodeBusyError(_busy_message(output_dir, lock, again)) from None
        _release_path(lock)
        return acquire_encode_slot(output_dir, holder)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        token = uuid.uuid4().hex[:12]
        handle.write(f"{os.getpid()} {holder} {token}")
    _OWNED[lock] = token
    return lock


def release_encode_slot(lock: Path) -> None:
    """Releases a held encode slot.

    Only the lock this process wrote is removed: a holder that stalled long
    enough to lose its slot must not delete the new holder's file on its way
    out, and the pid cannot tell the two apart (the server and a one-shot
    container are both pid 1), so the token written at acquisition decides
    (issue #383).

    Args:
        lock (Path): The lock file path returned by acquire_encode_slot.
    """
    token = _OWNED.pop(lock, None)
    if token is None:
        return
    fields = _read_fields(lock)
    if len(fields) < 3 or fields[2] != token:
        return
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

    Keeps the slot alive for the run's whole duration by touching the lock
    every `_HEARTBEAT_SECONDS`, so the stale window measures silence
    (issue #383). A failed touch is reported, never swallowed: a slot whose
    heartbeat cannot be written will be reclaimed by the next claimant, and
    the operator has to know why.

    Args:
        output_dir (Path): The output directory the run writes into.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Yields:
        Path: The lock file path.
    """
    lock = acquire_encode_slot(output_dir, holder)
    stop = threading.Event()
    failing = False

    def _refresh() -> None:
        nonlocal failing
        while not stop.wait(_HEARTBEAT_SECONDS):
            try:
                lock.touch()
            except OSError as e:
                if not failing:
                    failing = True
                    log_warning(
                        f"Could not refresh the encode lock in '{output_dir}' ({e}); "
                        "another encoder may take the slot after "
                        f"{_LOCK_STALE_SECONDS / 60:.0f} min of silence."
                    )
            else:
                failing = False

    refresher = threading.Thread(target=_refresh, name="encode-lock-refresh", daemon=True)
    refresher.start()
    try:
        yield lock
    finally:
        stop.set()
        refresher.join(timeout=5)
        release_encode_slot(lock)
