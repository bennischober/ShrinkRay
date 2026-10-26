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
the CONTENDED directory makes the collision a clear "busy" instead of a
silent race.

Which directory is contended depends on the mode (issue #230):

- two-directory mode: the output directory — every writer writes into it.
- in place: the MOVIE'S OWN DIRECTORY. That is where `replace.stage` writes
  `<source>.shrinkray-staging` and where `replace.swap` performs its two
  renames, so it is the only key every writer can agree on. A key on a library
  root is NOT an identity: two writers given nested roots (`/media` and
  `/media/Movies`) take two different lock files for one file (#379). A key on
  each run's staging directory excluded no one at all (#336).

Staleness is a heartbeat, not a pid (issue #383). The holder touches the file
every minute and a lock silent for five minutes is reclaimed. Pids cannot answer
"is this still live?" in the target deployment: the server is pid 1 on every
container start, a one-shot `docker run` is pid 1 in its own namespace, and pids
are reused after a restart. The pid is written into the file only to name the
holder in the busy message.

The steal is a rename, which is atomic: of two claimants that both judged the
same lock stale, exactly one takes it. A rename cannot be made conditional on
the file still being silent, so a holder that beats in the microseconds between
the judgement and the rename has its lock renamed away and then restored; if a rival
has already claimed the path in that instant, the restore cannot win and both
runs proceed. That residual window is logged as an error rather than hidden, and
the holder's own heartbeat notices the loss at its next beat.
"""

import contextlib
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from shrinkray.logger import log_error, log_warning

# how often the holder proves it is still alive (a touch of the lock file)
_HEARTBEAT_SECONDS = 60.0

# five missed beats: the window measures SILENCE, not the age of the run, so a
# multi-hour encode keeps its slot and a killed one is reclaimed in minutes
_LOCK_STALE_SECONDS = 300.0

# bounded retries when the path is being stolen and re-claimed under us
_CLAIM_ATTEMPTS = 8

# the token this process wrote into each lock it holds, by lock path. The pid
# cannot answer "is this still my lock?" - the server and a one-shot container
# are both pid 1 - so ownership is compared by token (issue #383)
_OWNED: dict[Path, str] = {}


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


def _read_fields(lock: Path) -> list[str] | None:
    """Returns the lock file's whitespace-separated fields.

    Args:
        lock (Path): The lock file.

    Returns:
        list[str] | None: The stamp's fields (pid, holder, token), or None when
            the file could not be READ. A missing file and an unreadable one are
            different facts: only the first means the slot is free.
    """
    try:
        return lock.read_text(encoding="ascii").split()
    except FileNotFoundError:
        return []
    except OSError as e:
        log_warning(f"Could not read the encode lock '{lock}' ({e}).")
        return None


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
    fields = _read_fields(lock)
    pid = int(fields[0]) if fields and fields[0].isdigit() else 0
    label = fields[1] if fields and len(fields) > 1 else ""
    who = f"pid {pid}" if pid else "an unknown process"
    if label:
        who = f"{label}, {who}"
    return (
        f"another encoder is already using '{output_dir}' "
        f"({who}; last heartbeat {silence / 60:.0f} min ago)"
    )


def _claim(lock: Path, holder: str) -> bool:
    """Creates the lock file and stamps it, exclusively.

    Args:
        lock (Path): The lock file to create.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Returns:
        bool: True when this call created it; False when it already exists.
    """
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    token = uuid.uuid4().hex[:12]
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"{os.getpid()} {holder} {token}")
    _OWNED[lock] = token
    return True


def acquire_encode_slot(output_dir: Path, holder: str = "encode") -> Path:
    """Acquires the encode slot for a directory (blocking-free).

    Creates the directory if needed, then claims the lock file atomically
    (O_CREAT|O_EXCL). A lock that has been silent for the stale window is
    stolen with a warning. Concurrent claimants get the exception, never a wait -
    the product's answer to "already encoding" is a clear no, not a queue.

    Args:
        output_dir (Path): The directory to guard.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Returns:
        Path: The lock file path (release_encode_slot takes it back).

    Raises:
        EncodeBusyError: When another live encoder holds the slot, or the stale
            lock could not be taken.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # canonicalize BEFORE keying: a symlinked or relative path must not produce a
    # second lock file for one directory
    output_dir = output_dir.resolve()
    lock = _lock_path(output_dir)
    for _ in range(_CLAIM_ATTEMPTS):
        if _claim(lock, holder):
            return lock
        silence = _silence_seconds(lock)
        if silence is None:
            continue  # it vanished: the path is free again
        if silence < _LOCK_STALE_SECONDS:
            # the holder's heartbeat is recent: busy, whoever the pid names.
            # This also covers the window between O_CREAT and the stamp write,
            # where the file is still empty (issue #383): a fresh empty file is
            # a live claim, not a stale one.
            raise EncodeBusyError(_busy_message(output_dir, lock, silence)) from None
        log_warning(f"Stale encode lock in '{output_dir}' - stealing it.")
        outcome = _steal(lock)
        if outcome == "live":
            again = _silence_seconds(lock)
            raise EncodeBusyError(_busy_message(output_dir, lock, again or 0.0)) from None
        if outcome == "error":
            raise EncodeBusyError(
                f"could not take the stale encode lock in '{output_dir}' (see the log)"
            ) from None
        # "won" or "gone": the path is free, claim it again
    raise EncodeBusyError(
        f"could not claim the encode slot for '{output_dir}' after {_CLAIM_ATTEMPTS} attempts"
    )


def _steal(lock: Path) -> str:
    """Takes a stale lock out of the way, atomically, if it is still stale.

    The rename is the atomic step: of two claimants that both judged the same
    lock stale, exactly one renames it and the other finds nothing to take.

    Args:
        lock (Path): The lock file judged stale.

    Returns:
        str: "won" (the path is free), "gone" (someone else took it), "live"
            (the holder beat in the meantime and its lock was restored), or
            "error" (the filesystem refused the rename; it is logged).
    """
    taken = lock.with_name(f"{lock.name}.stale.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        os.rename(lock, taken)
    except FileNotFoundError:
        return "gone"
    except OSError as e:
        log_error(f"Could not take the stale encode lock '{lock}' ({e}).")
        return "error"
    silence = _silence_seconds(taken)
    if silence is not None and silence < _LOCK_STALE_SECONDS:
        # the holder beat between the judgement and the rename: put it back
        try:
            os.link(taken, lock)
        except FileExistsError:
            log_error(
                f"A rival claimed '{lock}' while a live holder's lock was being "
                f"restored; two encoders may now be running in '{lock.parent}'."
            )
            with contextlib.suppress(OSError):
                taken.unlink()
            return "live"
        except OSError as e:
            log_error(
                f"Could not restore the live encode lock '{lock}' ({e}); "
                f"it is at '{taken}'. Two encoders may now be running in '{lock.parent}'."
            )
            return "live"
        with contextlib.suppress(OSError):
            taken.unlink()
        return "live"
    with contextlib.suppress(OSError):
        taken.unlink()
    return "won"


def _beat(lock: Path, token: str) -> str:
    """Refreshes a held lock's mtime, if it is still ours.

    A holder that lost its slot must not refresh the new holder's file: that would
    keep the rival's lease alive and hide the loss from this run (PR #389's review
    finding 2). `os.utime` is used, never `Path.touch`, because touch CREATES a
    missing file and would leave a token-less lock behind.

    Args:
        lock (Path): The lock file.
        token (str): The token this process wrote when it acquired the slot.

    Returns:
        str: "ok" (refreshed), "lost" (no longer ours), or "failed" (the refresh
            could not be attempted or written; the slot is on a timer).
    """
    fields = _read_fields(lock)
    if fields is None:
        return "failed"
    if len(fields) < 3 or fields[2] != token:
        return "lost"
    try:
        os.utime(lock)
    except FileNotFoundError:
        return "lost"
    except OSError as e:
        log_warning(f"Could not refresh the encode lock '{lock}' ({e}).")
        return "failed"
    return "ok"


def release_encode_slot(lock: Path) -> None:
    """Releases a held encode slot.

    Only the lock this process wrote is removed: a holder that lost its slot must
    not delete the new holder's file on its way out, and the pid cannot tell the
    two apart (the server and a one-shot container are both pid 1), so the token
    written at acquisition decides (issue #383).

    Args:
        lock (Path): The lock file path returned by acquire_encode_slot.
    """
    token = _OWNED.pop(lock, None)
    if token is None:
        return
    fields = _read_fields(lock)
    if fields is None:
        # unreadable, so ownership is unknown: leaving the file alone is the
        # only safe choice, and it goes stale on its own
        return
    if len(fields) < 3 or fields[2] != token:
        return
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log_error(f"Could not release the encode lock '{lock}' ({e}).")


@contextlib.contextmanager
def guard_encodes(output_dir: Path, holder: str = "encode") -> Iterator[Path]:
    """Context manager around a whole encode run (all entry points).

    Keeps the slot alive for the run's whole duration by beating every
    `_HEARTBEAT_SECONDS`, so the stale window measures silence (issue #383).
    A beat that finds the lock taken over, or that cannot write, is reported
    once and stops: the run is then unguarded and the operator has to know.

    Args:
        output_dir (Path): The directory the run writes into.
        holder (str): A short label naming the encoder (job/watch/CLI).

    Yields:
        Path: The lock file path.
    """
    lock = acquire_encode_slot(output_dir, holder)
    token = _OWNED[lock]
    stop = threading.Event()

    def _refresh() -> None:
        reported: str | None = None
        while not stop.wait(_HEARTBEAT_SECONDS):
            status = _beat(lock, token)
            if status == "ok":
                reported = None
                continue
            if reported == status:
                continue
            reported = status
            if status == "lost":
                log_error(
                    f"Another encoder took the slot for '{output_dir}'; "
                    "this run is no longer exclusive."
                )
                return
            log_warning(
                f"Could not refresh the encode lock in '{output_dir}'; another "
                f"encoder may take the slot after {_LOCK_STALE_SECONDS / 60:.0f} min."
            )

    refresher = threading.Thread(target=_refresh, name="encode-lock-refresh", daemon=True)
    refresher.start()
    try:
        yield lock
    finally:
        stop.set()
        refresher.join(timeout=5)
        if refresher.is_alive():
            log_warning(f"The encode-lock heartbeat for '{output_dir}' did not stop.")
        release_encode_slot(lock)
