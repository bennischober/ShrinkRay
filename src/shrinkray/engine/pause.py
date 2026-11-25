"""The resident app's shared control state: pause, manual encodes, cancel requests.

Two reasons make the resident app start no new probe or encode (issue #250):

- **The operator paused it.** A `paused` file in the data directory (the
  folder holding the database: `/data` in the container). It persists across
  restarts on purpose: a reboot must not silently resume a backlog the operator
  stopped. Create it to pause, remove it to resume.
- **A manual encode is running.** `shrinkray encode <path>` started next to
  the resident app (the container console's single-file case) leaves one marker
  per process in `manual-encodes/`. The CLI writes it and never calls the
  server, because the CLI must not require the resident process. A marker whose
  process is gone - a crash, a closed console, a container restart that reused
  its pid - is ignored and removed, so it can never pause the app for good.

A running encode always finishes; pausing only holds back the next file.

The third signal is a cancel request (issue #621): the terminal UI writes one
file per job in ``cancel-requests/``, and the resident app's poller applies it
through the job queue. The file stays until the job is terminal, so a crash during
a running job's unwind cannot lose the operator's intent.
"""

import contextlib
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from shrinkray.logger import log
from shrinkray.tools.process import process_exists, process_start_ticks

PAUSE_FLAG = "paused"
MANUAL_ENCODES = "manual-encodes"
CANCEL_REQUESTS = "cancel-requests"

# where the container keeps its config; its folder is the data directory a
# one-shot CLI run shares with the server (the image sets it, issue #247)
CONFIG_ENV_VAR = "SHRINKRAY_CONFIG"

# without a process start time (no /proc), a marker older than this is stale
# even when its pid is alive: the same rule as the encode lock's
_MARKER_STALE_SECONDS = 21600.0

# how often a paused job worker looks again
PAUSE_POLL_SECONDS = 5.0


def cancel_requests_dir(state_dir: Path) -> Path:
    """Returns the folder holding the terminal UI's cancel requests.

    Args:
        state_dir (Path): The data directory.

    Returns:
        Path: The ``cancel-requests`` folder under it.
    """
    return state_dir / CANCEL_REQUESTS


def write_cancel_request(state_dir: Path, job_id: int) -> Path:
    """Writes a cancel request for one job, atomically.

    The request is a file named after the job id in ``cancel-requests/``. It
    stays until the job reaches a terminal state; the resident app's poller reads
    it and applies it through the job queue. The atomic write follows the
    config save's pattern: a crash leaves the old file or the new one, never a
    half-written request.

    Args:
        state_dir (Path): The data directory shared with the resident app.
        job_id (int): The job to cancel (a positive integer).

    Returns:
        Path: The request file.

    Raises:
        ValueError: When ``job_id`` is not a positive integer.
    """
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise ValueError(f"job id must be a positive integer, got {job_id!r}")
    folder = cancel_requests_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / str(job_id)
    fd, temp_name = tempfile.mkstemp(dir=folder, prefix=f".{job_id}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{job_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return target


def read_pending_cancel_requests(state_dir: Path) -> list[int]:
    """Returns the pending cancel requests, sorted by job id.

    Only a canonical positive-integer filename is a request: a stray file, a
    temporary file and a non-integer name are ignored without raising. A
    missing folder has no requests.

    Args:
        state_dir (Path): The data directory.

    Returns:
        list[int]: The requested job ids, oldest id first.
    """
    folder = cancel_requests_dir(state_dir)
    if not folder.is_dir():
        return []
    ids: list[int] = []
    for entry in folder.iterdir():
        try:
            job_id = int(entry.name)
        except ValueError:
            continue
        if job_id <= 0 or entry.name != str(job_id):
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        ids.append(job_id)
    return sorted(ids)


def remove_cancel_request(state_dir: Path, job_id: int) -> None:
    """Removes a consumed cancel request (idempotent).

    Args:
        state_dir (Path): The data directory.
        job_id (int): The request's job id.
    """
    cancel_requests_dir(state_dir).joinpath(str(job_id)).unlink(missing_ok=True)


def state_dir_for_cli(db_file: Path | None) -> Path | None:
    """Returns the data directory a CLI run shares with a resident app.

    Args:
        db_file (Path | None): The run's --db, when given.

    Returns:
        Path | None: The --db folder; else, in the container, the folder of
            $SHRINKRAY_CONFIG (/data); else None: a native stateless run has no
            resident app to coordinate with.
    """
    if db_file is not None:
        return db_file.resolve().parent
    config = os.environ.get(CONFIG_ENV_VAR, "")
    return Path(config).resolve().parent if config else None


def operator_paused(state_dir: Path) -> bool:
    """Tells whether the operator paused the resident app."""
    return (state_dir / PAUSE_FLAG).exists()


def set_operator_paused(state_dir: Path, paused: bool) -> None:
    """Pauses or resumes the resident app (the terminal UI's switch).

    Args:
        state_dir (Path): The data directory.
        paused (bool): True to pause, False to resume.
    """
    flag = state_dir / PAUSE_FLAG
    if paused:
        flag.touch(exist_ok=True)
    else:
        flag.unlink(missing_ok=True)


def _marker_alive(marker: Path) -> bool:
    """Tells whether a manual-encode marker's process still runs.

    Args:
        marker (Path): A marker file named after its pid, holding the
            process's start time (empty where the platform has none).

    Returns:
        bool: True when the process that wrote it is still running.
    """
    try:
        pid = int(marker.name)
        recorded = marker.read_text(encoding="ascii").strip()
        age = time.time() - marker.stat().st_mtime
    except (OSError, ValueError):
        return False
    if not process_exists(pid):
        return False
    started = process_start_ticks(pid)
    if started is not None and recorded:
        return str(started) == recorded
    return age < _MARKER_STALE_SECONDS


def running_manual_encodes(state_dir: Path) -> list[int]:
    """Returns the pids of the manual encodes running now; drops dead markers.

    Args:
        state_dir (Path): The data directory.

    Returns:
        list[int]: The live markers' pids, sorted.
    """
    folder = state_dir / MANUAL_ENCODES
    if not folder.is_dir():
        return []
    live: list[int] = []
    # an unreadable or half-unmounted data folder must not take the caller
    # down: this now runs on the dashboard's poll path (the snapshot's
    # status carries pause_reason, issue #323's review finding 3) — report
    # unmeasured ([]) rather than raise
    try:
        markers = list(folder.iterdir())
    except OSError:
        return []
    for marker in markers:
        if _marker_alive(marker):
            live.append(int(marker.name))
        else:
            # our own marker of a process that is gone: removing it is safe
            with contextlib.suppress(OSError):
                marker.unlink()
    return sorted(live)


def pause_reason(state_dir: Path | None) -> str | None:
    """Says why the resident app must not start the next file, if it must not.

    Args:
        state_dir (Path | None): The data directory (None: never paused).

    Returns:
        str | None: The reason, or None when the app may go on.
    """
    if state_dir is None:
        return None
    if operator_paused(state_dir):
        return f"paused by the operator (remove '{state_dir / PAUSE_FLAG}' to resume)"
    manual = running_manual_encodes(state_dir)
    if manual:
        return f"a manual encode is running (pid {', '.join(map(str, manual))})"
    return None


@contextlib.contextmanager
def manual_encode(state_dir: Path | None) -> Iterator[None]:
    """Marks a manual encode for its whole run, so a resident watcher yields.

    Args:
        state_dir (Path | None): The data directory shared with the resident
            app, or None (nothing to coordinate with: the marker is skipped).

    Yields:
        None: While the marker is in place.
    """
    if state_dir is None:
        yield
        return
    folder = state_dir / MANUAL_ENCODES
    marker = folder / str(os.getpid())
    try:
        folder.mkdir(parents=True, exist_ok=True)
        started = process_start_ticks(os.getpid())
        marker.write_text("" if started is None else str(started), encoding="ascii")
    except OSError as e:
        # the encode itself must not fail over the courtesy signal
        log(f"Could not tell a resident watcher to pause ({e}); it may encode alongside.")
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            marker.unlink()


def wait_while_paused(
    state_dir: Path | None,
    cancel: threading.Event,
    on_pause: Callable[[str], None] | None = None,
) -> None:
    """Blocks a job between two files while the app is paused.

    Args:
        state_dir (Path | None): The data directory.
        cancel (threading.Event): Returns early when set.
        on_pause (Callable[[str], None] | None): Told the reason whenever it
            changes, so the job can report it.
    """
    reported: str | None = None
    while not cancel.is_set():
        reason = pause_reason(state_dir)
        if reason is None:
            if reported is not None:
                log("Resumed: the next file starts now.")
            return
        if reason != reported:
            log(f"Waiting before the next file: {reason}.")
            if on_pause is not None:
                on_pause(reason)
            reported = reason
        cancel.wait(PAUSE_POLL_SECONDS)
