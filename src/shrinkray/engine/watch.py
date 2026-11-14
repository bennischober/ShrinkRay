"""Watch mode: automatically encode new movies arriving in a folder.

Polls an inbox directory for new movie files, waits until they are stable
(no longer being written => the download/copy finished), applies the same skip
logic as the encode/plan commands and encodes them (source stays untouched
=> the human stays in the loop for replacing library files).
"""

import contextlib
import ctypes
import math
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from shrinkray.config import DEFAULT_MIN_SOURCE_HEIGHT, DEFAULT_MIN_SOURCE_WIDTH, encode_window_open
from shrinkray.config import Conf as Conf
from shrinkray.engine.lock import EncodeBusyError, guard_encodes
from shrinkray.engine.pause import pause_reason
from shrinkray.engine.pipeline import (
    DISK_FREE_FLOOR_BYTES,
    cap_threads,
    disk_free_bytes,
    encode_with_quality,
)
from shrinkray.engine.plan import MIN_ENCODE_SECONDS, QualityResult
from shrinkray.engine.samples import ProgressCallback
from shrinkray.engine.scan import process_movie
from shrinkray.engine.search import DEFAULT_TARGET_VMAF
from shrinkray.logger import log, log_error
from shrinkray.replace import STAGING_DIR_PREFIX, is_staging_path
from shrinkray.tools.ffprobe import probe_duration_seconds
from shrinkray.tools.handbrake import HANDBRAKE_BINARY, find_handbrake
from shrinkray.tools.process import Cancelled


# a file is stable when its last write is at least STABLE_MIN_AGE old and it is
# not sparse (issue #479, `check_stability`); STABLE_CHECK_INTERVAL paces the
# one-shot wait's retries of a file it cannot stat. The env seam below can
# override both (test-only, #537).
# Test-only env overrides (issue #537): the SIGHUP test spawns the CLI as a
# SUBPROCESS, so the suite's in-process patches (tests/test_cli.py's
# `_toolchain_ok`, the inline block in `test_a_signal_after_the_final_encode_*
# and the `_encode` helper, which zero both constants) cannot reach it, and its
# 60 s spawn deadline must not measure a 30 s sleep its subject never
# exercises. This is deliberately NOT the shape of an operator knob: it is read
# once at import, has no config key and no CLI flag, and exists so a test can
# reach a constant that lives in the child process (issue #537's review finding
# 2 — the earlier comment wrongly cited $SHRINKRAY_WATCH_OUTPUT, which is
# documented operator surface, and $SHRINKRAY_SMOKE, which no product module
# reads).
#
# The guard is TOTAL (review findings 1 and 2): a value that `float()` accepts
# but that is not a usable duration — `nan`, `inf`, negative, or too large for
# `time.sleep` — must fall back to the default, because `STABLE_MIN_AGE = -5`
# would make `check_stability()` accept a file still being written (the exact
# #437 defect this witness exists to prevent), and `nan`/`inf`/negative/1e10
# make `time.sleep`/`st_mtime` arithmetic raise where no caller expects it.
def _duration_env(var: str, default: float) -> float:
    """Reads a non-negative, finite duration from the environment.

    Args:
        var (str): The environment variable name.
        default (float): The value when unset, unparsable, or unusable.

    Returns:
        float: The override, or the default.
    """
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # `float()` accepts nan/inf/negatives; a duration must be finite, >= 0, and
    # small enough that `time.sleep`/`st_mtime` arithmetic cannot overflow
    # (1e10 passes isfinite and overflows sleep — round-2 review finding 1).
    # The cap bounds OVERFLOW ONLY, not the wait: `wait_until_settled`'s cancel
    # event is observed only BETWEEN sleeps, so a legal value up to a day
    # delays a cancel by up to one sleep of that length. At the defaults the
    # #326 contract's "never absorbs a signal for minutes" holds; the seam's
    # values are a test's choice, never an operator's. Larger values are a
    # mistake or an attempt to disable the witness, and fall back to the
    # default.
    if not math.isfinite(value) or value < 0 or value > 86400.0:
        return default
    return value


STABLE_CHECK_INTERVAL = _duration_env("SHRINKRAY_STABLE_CHECK_INTERVAL", 30.0)
STABLE_MIN_AGE = _duration_env("SHRINKRAY_STABLE_MIN_AGE", 60.0)
# below this share of its size allocated on disk, a file is sparse: a download
# that wrote its full size up front and has not filled it (issue #479). Video
# is already compressed, so filesystem compression is expected to keep a
# finished file well above it (reasoned, not measured); the margin is generous
# so that a finished file does not trip it
SPARSE_ALLOCATED_FLOOR = 0.9
# the bounded wait's ceiling for the one-shot CLI (issue #437): the watcher
# has no ceiling (it reconsiders on its next pass); a one-shot run reports
# and skips instead of waiting forever on a file that never settles
STABLE_WAIT_LIMIT = 600.0
POLL_INTERVAL = 60.0
# adaptive polling: while the inbox stays idle the sleep between polls grows
# up to this multiple of POLL_INTERVAL (any new candidate resets it back)
MAX_POLL_MULTIPLIER = 4.0


def next_poll_interval(current: float, base: float, found_new: bool) -> float:
    """Computes the next poll interval for the watch loop.

    The interval doubles while the inbox stays idle (fewer checks when
    nothing happens) and resets to the base interval as soon as a poll
    finds new candidates (activity suggests more may be coming).

    Args:
        current (float): The interval that was just used (seconds).
        base (float): The configured base interval (seconds).
        found_new (bool): Whether the last poll found new candidate files.

    Returns:
        float: The next interval in seconds: double the current one, capped
        at base * MAX_POLL_MULTIPLIER, or the base interval on activity.
    """
    if found_new:
        return base
    return min(current * 2.0, base * MAX_POLL_MULTIPLIER)


def _scan_into_db(movie: Path, root: Path, db_file: Path | None) -> bool:
    """Scans one file into the database before its encode job runs (issue #231).

    PRODUCT.md's watchdog contract is two halves: a stable new or changed file
    is (1) scanned into the database and (2) re-decided from that fresh scan.
    The engine owns the scan (`engine.scan.process_movie`), so the watcher can
    call it without crossing a layer; the DECISION belongs to the encode job
    the queue runs (`submit`). The scan RECORDS the row here (issue #574):
    process_movie computes and never writes, so the write-back is this
    function's job - a file the watchdog saw but whose encode job never ran
    is still catalogued, under the watch root's category. A failed scan
    returns False and the caller leaves the file eligible (it is already in
    `known`, so the retry happens when the file changes).

    Args:
        movie (Path): The file to scan.
        root (Path): The watch root (the scan's category root).
        db_file (Path | None): The database file; None records nothing.

    Returns:
        bool: True when the scan succeeded (the row recorded, or - on a
            recording failure - logged and left to the encode job's
            recording, `service.ensure_file_row`'s minimal row), False when
            the scan itself failed.
    """
    if db_file is None:
        return True
    from shrinkray import db
    from shrinkray.config import load_config
    from shrinkray.engine.scan import movie_record_from_data

    try:
        data, error = process_movie(movie, root, load_config(), db_file=db_file)
    except OSError as e:
        # the scan's own filesystem reads (getsize/stat) raise on a file
        # that vanished or a mount that dropped (issue #427): a per-file
        # failure, retried when the file changes - never a thread kill
        log_error(f"Scanning '{movie.name}' failed: {e} => retried when the file changes.")
        return False
    if data is None:
        log_error(f"Scanning '{movie.name}' failed: {error} => retried when the file changes.")
        return False
    # record the scan's result (issue #574): the row is the catalog's memory
    # of the watchdog's scan. It also preempts the encode path's minimal row
    # (service.ensure_file_row writes category=parent.name when no row
    # exists) - the recorded category stays the watch root's. A recording
    # failure must not fail the scan (the #507/#517 rule: a failed write
    # costs the shortcut, never the measurement); the encode path's own
    # recording then writes its minimal row.
    try:
        connection = db.connect(db_file)
        try:
            db.upsert_file(connection, movie_record_from_data(movie, data))
            connection.commit()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as e:
        log(
            f"Recording '{movie.name}'s scan failed ({e}); the encode path's own recording writes the row."
        )
    return True


def _known_identities_from_db(db_file: Path) -> set[str]:
    """Builds the watcher's change-tracking keys from the scanned library.

    Every row in the files table records the mtime and size its scan decided
    from, which is exactly the watcher's identity key - so the database, not
    an in-process set, is the memory (issue #231). A restart seeds from here
    and resumes; a file that changed since its scan is NOT in the set (its
    mtime/size differ) and is re-decided, which is the contract's "a changed
    file is a new question".

    Args:
        db_file (Path): The sqlite database file (caller checked not-None).

    Returns:
        set[str]: "path:mtime:size" keys, one per scanned file.
    """
    from shrinkray import db

    connection = db.connect(db_file)
    try:
        rows = connection.execute("SELECT path, mtime, size FROM files").fetchall()
    finally:
        connection.close()
    return {f"{Path(row['path']).resolve()}:{row['mtime']}:{row['size']}" for row in rows}


def find_new_movies(inbox: Path, extensions: list[str], known: set[str]) -> list[Path]:
    """Finds movie files under the roots that are not in the known set.

    The tree is walked RECURSIVELY, not one directory: Mode B's watchdog is
    specified over the mounted library, where a new file can appear in any
    subdirectory. An inbox only ever catches what someone drops in one folder.

    A path is "known" when it is in `known` at the same mtime and size, so a
    CHANGED file (same path, new content) is a new question and gets
    re-decided. A file that is still being written is filtered by the caller's
    stability check, not here.

    Args:
        inbox (Path): The root directory to watch (walked recursively).
        extensions (list[str]): The extensions to search for.
        known (set[str]): "path:mtime:size" keys that were already decided.

    Returns:
        list[Path]: The new or changed movie files.
    """
    movies: list[Path] = []
    for extension in extensions:
        extension = extension.lower().lstrip(".")
        # recursive: a new file anywhere in the tree must be picked up
        movies.extend(inbox.rglob(f"*.{extension}"))
        movies.extend(inbox.rglob(f"*.{extension.upper()}"))
    # windows glob is case-insensitive => deduplicate, then filter known.
    # the retained originals and staging copies are NEVER movies: in place the
    # library holds <name> and <name>.shrinkray-original side by side, and
    # re-encoding a retained original would double the library on every pass.
    # The staging DIRECTORY (the in-place working dir) and everything inside it
    # is the same non-movie class: an in-flight or crashed run's encode must
    # not be picked up as a new arrival (issue #237).
    unique = list(dict.fromkeys(movies))
    kept = []
    for m in unique:
        try:
            identity = _identity(m)
        except OSError:
            # a dangling entry: the file vanished between the listing and
            # this stat (issue #427's discovery-class escapes) - skip it;
            # it is gone, there is nothing to watch
            continue
        if any(part.startswith(".") for part in m.relative_to(inbox).parts[:-1]):
            # a dot-DIRECTORY inside the watched root is not library
            # content (issue #439) - the same segment rule find_movies
            # applies, so the two listings share one membership rule: the
            # watcher must not encode what the scanner would not catalogue
            continue
        if identity not in known and not is_staging_path(m, inbox):
            kept.append(m)
    return kept


def _identity(path: Path) -> str:
    """Returns the change-tracking key of one movie file.

    mtime and size are part of the key so a CHANGED file at the same path is a
    new question, not a file the watcher remembers deciding. Without them a
    replaced file would never be re-scanned.

    Args:
        path (Path): The movie file.

    Returns:
        str: "absolute path:mtime:size".
    """
    stat = path.stat()
    return f"{path.resolve()}:{stat.st_mtime}:{stat.st_size}"


@dataclass(frozen=True)
class Stability:
    """One `stat()`'s verdict on whether a file is complete (issue #479).

    Attributes:
        young_for (float): Seconds until the file's last write is
            STABLE_MIN_AGE old; 0.0 once it is. Positive means a writer may
            still be at work: wait that long, then ask again.
        incomplete (str | None): Why a file old enough for the age check is
            still not complete, worded for the operator; None when it is
            complete or its allocation cannot be judged.
    """

    young_for: float
    incomplete: str | None

    @property
    def settled(self) -> bool:
        """True when the file may be decided on now."""
        return self.young_for == 0.0 and self.incomplete is None


def _allocated_fraction(stat: os.stat_result) -> float | None:
    """The share of a file's size that is allocated on disk.

    `st_blocks` counts 512-byte units on every POSIX platform. Windows has no
    `st_blocks`, and some FUSE/SMB filesystems report 0 for a complete file:
    both mean "unknown", never "sparse" (the maintainer's #479 refinement A).
    A file with nothing allocated at all is all zeros, which no probe reads
    as a video stream, so treating it as unknown cannot lead to an encode.

    Args:
        stat (os.stat_result): The file's stat.

    Returns:
        float | None: allocated bytes / size, or None when it cannot be judged.
    """
    blocks: int | None = getattr(stat, "st_blocks", None)
    if not blocks or stat.st_size == 0:
        return None
    return blocks * 512 / stat.st_size


def check_stability(path: Path) -> Stability:
    """Judges from a single `stat()` whether a file has finished arriving.

    The witness (issue #479, replacing #437's size re-check): the file's last
    write is at least STABLE_MIN_AGE old, and it is not sparse. Every write
    updates mtime, so a file still being written always looks young; an old
    mtime means nothing wrote for STABLE_MIN_AGE. That made the old 30 s size
    comparison a second, shorter copy of the age question, paid by every
    settled file. What the age check cannot see is a sparse download that
    stalled: it reports its full size from the start, and only its allocated
    blocks show the holes. Video is already compressed, so a finished file on
    a compressing filesystem (ZFS, btrfs) is expected to stay well above
    SPARSE_ALLOCATED_FLOOR (reasoned, not measured). Accepted gaps: a fully preallocated (fallocate)
    download that stalls, and a network mount serving stale metadata. #436's
    run identity aborts a run whose file changes while it is measured, up to
    the start of the final encode - not during or after that encode.

    Args:
        path (Path): The file to judge.

    Returns:
        Stability: The verdict; it never sleeps.

    Raises:
        OSError: When the file cannot be stat'ed (vanished, unreadable).
    """
    stat = path.stat()
    young_for = STABLE_MIN_AGE - (time.time() - stat.st_mtime)
    if young_for > 0:
        return Stability(young_for=young_for, incomplete=None)
    allocated = _allocated_fraction(stat)
    if allocated is not None and allocated < SPARSE_ALLOCATED_FLOOR:
        # floor, not round: 89.6 % must not read as the 90 % that passes
        return Stability(
            young_for=0.0,
            incomplete=(
                f"appears incomplete: only {math.floor(allocated * 100)} % of its size "
                "is allocated on disk (a stalled download?)"
            ),
        )
    return Stability(young_for=0.0, incomplete=None)


def wait_until_settled(
    path: Path,
    limit: float = STABLE_WAIT_LIMIT,
    cancel: threading.Event | None = None,
) -> str | None:
    """Waits, bounded and cancel-aware, until the file may be decided on; the
    one-shot CLI's entry to the witness the watcher uses (issues #437, #479).

    A settled file returns at once, with no sleep. A young file sleeps its
    remaining age and is judged again, so a file written meanwhile waits
    again from its new mtime. An old file that is still incomplete (sparse)
    is not waited on: it is a stalled download, and waiting would not finish
    it - the reason is returned at once and the caller skips the file. The
    watcher calls `check_stability` directly once per candidate per pass; it
    needs no bound because it reconsiders on its next pass.

    Args:
        path (Path): The file to wait for.
        limit (float): Total seconds to wait before giving up. A wait that
            would end past the limit gives up before sleeping.
        cancel (threading.Event | None): Cooperative cancellation, checked
            after every sleep (the #326 contract: closing the terminal
            cancels the wait too). A cancel that arrives DURING a sleep is
            honoured at the next check, so the worst case is one sleep: the
            remaining age (up to STABLE_MIN_AGE, 60 s at the default) or,
            on the unreadable-file retry, STABLE_CHECK_INTERVAL (30 s).

    Returns:
        str | None: None when the file settled; otherwise why it is skipped,
            worded for the operator. The file stays eligible: a later run
            decides it again.

    Raises:
        Cancelled: When the cancel event fires during the wait.
    """
    deadline = time.monotonic() + limit
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled while waiting for the file to settle")
        try:
            verdict = check_stability(path)
        except OSError as e:
            # vanished or unreadable: retried on the interval until the limit
            if time.monotonic() + STABLE_CHECK_INTERVAL >= deadline:
                return f"could not be read within {int(limit)} s ({e})"
            time.sleep(STABLE_CHECK_INTERVAL)
            continue
        if verdict.incomplete is not None:
            return verdict.incomplete
        if verdict.settled:
            return None
        if time.monotonic() + verdict.young_for >= deadline:
            return f"did not settle within {int(limit)} s (still copying?)"
        time.sleep(verdict.young_for)


def decide(movie: Path, target_vmaf: float, encoder: str) -> str:
    """Decides what to do with a movie without encoding it.

    Applies only the CHEAP pre-checks (duration minimum, binary presence);
    the skip decisions that need probing (no size gain, target unreachable)
    happen inside encode_with_quality, which reports them via
    skipped_reason. This used to run the full probe here and then discard
    the CRF - double probe cost for nothing.

    Args:
        movie (Path): The movie file.
        target_vmaf (float): The VMAF quality floor (unused here since the
            quality search owns the skip rules; kept for signature
            compatibility).
        encoder (str): The video encoder (unused here, same reason).

    Returns:
        str: 'encode' or a cheap skip reason.
    """
    duration = probe_duration_seconds(movie)
    if duration is None:
        # unreadable/corrupt file (or missing ffprobe): let the encode path
        # decide => it reports the error properly instead of looping forever
        return "encode"
    if duration < MIN_ENCODE_SECONDS:
        return "too short"

    handbrake = find_handbrake()
    if handbrake is None:
        return f"'{HANDBRAKE_BINARY}' not found"
    return "encode"


def acquire_lock(lock_file: Path) -> bool:
    """Tries to acquire the single-instance lock.

    Args:
        lock_file (Path): The lock file path.

    Returns:
        bool: True if the lock was acquired (this is the only watcher).
    """
    try:
        if lock_file.exists():
            # stale lock from a crashed run? a pid that does not exist anymore
            # frees the lock
            pid = int(lock_file.read_text(encoding="utf-8").strip() or 0)
            try:
                os.kill(pid, 0)
                return False  # process exists => another watcher is running
            except (OSError, ValueError):
                pass  # dead process or unparsable => take over
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


class _FsWaiter:
    """Wakes the watch loop early when the filesystem changes (issue #176).

    The waiter replaces the loop's plain sleep: `wait()` returns on an fs
    event or the stop event, whichever comes first, and otherwise after the
    full interval. It never bypasses the stability check (a still-writing
    file is deferred exactly as before). Platforms without a supported event
    API (and every error path) fall back to the plain sleep, which is
    today's behavior.

    Linux: inotify via ctypes (recursive watches on each root; a watch
    descriptor budget caps the recursion). Windows: ReadDirectoryChangesW
    via ctypes on a background thread per root. Everywhere else: disabled —
    `wait()` just sleeps.
    """

    _IN_CLOSE_WRITE = 0x00000008
    _IN_MOVED_TO = 0x00000080
    _IN_CREATE = 0x00000100
    _IN_IGNORED = 0x00000040
    _IN_MOVED_FROM = 0x00000040  # same value family as IN_IGNORED; both end a watch

    def __init__(self, roots: list[Path]) -> None:
        self._roots = roots
        self._wake: threading.Event | None = None
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._wd_paths: dict[int, Path] = {}
        self._failed = False
        self._supported = sys.platform in ("linux", "win32")
        self._libc: ctypes.CDLL | None = None

    def start(self) -> None:
        """Sets up the watches; errors disable the waiter (poll fallback)."""
        if not self._supported:
            return
        try:
            if sys.platform == "linux":
                self._start_linux()
            else:
                self._start_windows()
        except (OSError, AttributeError):
            self._failed = True
            self.stop()

    def is_active(self) -> bool:
        """Reports whether fs events are wired (False: the caller sleeps)."""
        return not self._failed and self._wake is not None

    def _start_linux(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        # inotify_init1(IN_NONBLOCK) keeps the read from blocking so the
        # stop event stays responsive
        IN_NONBLOCK = 0x800
        self._fd = libc.inotify_init1(IN_NONBLOCK)
        if self._fd < 0:
            raise OSError(ctypes.get_errno())
        self._libc = libc
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        for root in self._roots:
            self._add_recursive(root)
        self._thread.start()

    def _add_recursive(self, root: Path) -> None:
        assert self._libc is not None and self._fd is not None  # set by _start_linux
        IN_CLOSE_WRITE = self._IN_CLOSE_WRITE
        IN_MOVED_TO = self._IN_MOVED_TO
        IN_CREATE = self._IN_CREATE
        mask = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE
        for dirpath, _dirnames, _files in os.walk(root):
            wd = self._libc.inotify_add_watch(self._fd, os.fsencode(dirpath), mask)
            if wd < 0:
                # too many watches (the descriptor budget): disable quietly
                self._failed = True
                return
            self._wd_paths[wd] = Path(dirpath)

    def _drain(self) -> None:
        import ctypes
        import select

        assert self._libc is not None and self._fd is not None and self._wake is not None
        buf = ctypes.create_string_buffer(4096)
        fd = self._fd
        libc = self._libc
        wake = self._wake
        while not wake.is_set():
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            n = libc.read(fd, buf, len(buf))
            if n <= 0:
                # EINTR or the fd closed by stop(): both mean "no events"
                continue
            offset = 0
            while offset + 16 <= n:
                wd, mask, _cookie, _name_len = struct.unpack_from("iIII", buf, offset)
                offset += 16 + _name_len
                if mask & self._IN_IGNORED:
                    self._wd_paths.pop(wd, None)
                    continue
                if wd in self._wd_paths:
                    wake.set()

    def _start_windows(self) -> None:
        # ReadDirectoryChangesW needs a pumping thread per handle; the poll
        # fallback is the honest behavior on this port until someone needs it
        raise NotImplementedError

    def wait(self, seconds: float, stop_event: threading.Event | None) -> None:
        """Sleeps up to seconds, early-woken by an fs event or the stop event.

        Args:
            seconds (float): The poll interval (the backoff value).
            stop_event (threading.Event | None): Cooperative stop.
        """
        if self._failed or self._wake is None:
            _sleep_plain(seconds, stop_event)
            return
        wake = self._wake
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # whichever fires first: the stop event or an fs wake; the fs
            # wake is cleared immediately so a stale event cannot shorten
            # the NEXT sleep too
            if stop_event is not None and stop_event.wait(min(0.5, remaining)):
                break
            if wake.wait(min(0.5, remaining)):
                wake.clear()
                break
            wake.clear()

    def stop(self) -> None:
        """Tears the watches down (the loop's finally)."""
        if self._wake is not None:
            self._wake.set()
        # the drain must be out of select() before the descriptor closes
        # (issue #642): its select is bounded at 0.5 s, so the join is too,
        # and closing first raced the drain into EBADF - or onto a reused
        # descriptor number under a still-running read. A drain that never
        # started (a failed _start_linux) is not alive and needs no join.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        if self._fd is not None and self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


def _sleep_plain(seconds: float, stop_event: threading.Event | None) -> None:
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def _wait_or_stop(
    seconds: float,
    stop_event: threading.Event | None,
    waiter: "_FsWaiter | None" = None,
) -> None:
    """Sleeps between two watch passes, early-woken by fs changes or stop.

    The watch loop's ONLY sleep call. With an active fs waiter the sleep is
    interruptible by filesystem events (issue #176); the plain sleep stays
    the fallback, so behavior without events is exactly what it was.

    Args:
        seconds (float): The poll interval (the backoff value).
        stop_event (threading.Event | None): Cooperative stop; when set,
            this returns immediately (the caller re-checks it).
        waiter (._FsWaiter | None): The watcher's fs-event waiter.
    """
    if waiter is not None and waiter.is_active():
        waiter.wait(seconds, stop_event)
        return
    _sleep_plain(seconds, stop_event)


def release_lock(lock_file: Path) -> None:
    """Releases the single-instance lock if this process holds it.

    Args:
        lock_file (Path): The lock file path.
    """
    try:
        if lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_file.unlink()
    except OSError:
        pass


# the single-instance lock's path: one watcher per machine. A module-level
# constant so tests can point it at a per-test temp file - a shared /tmp
# path plus pid reuse made watch() refuse to run in a test run (issue #176
# follow-up: six watch tests failed with empty results because a leftover
# lock's pid happened to be alive again).
WATCH_LOCK_FILE = Path(tempfile.gettempdir()) / "shrinkray_watch.lock"


@dataclass(frozen=True)
class WatchSettings:
    """The settings the watcher applies to one file (issue #247).

    A resident watcher runs for months; the operator changes settings while it
    runs. The watcher asks for these before every file, so a saved change
    applies at the next file and a running encode keeps what it started with.
    """

    max_threads: int | None
    target_vmaf: float
    min_source_height: int
    min_source_width: int
    min_source_size_gb: float
    candidate_codecs: tuple[str, ...]
    crop_sample_count: int
    min_saving_percent: int = 15
    audio_languages: list[str] | None = None
    subtitle_languages: list[str] | None = None
    # the daily encode window (issue #434): resident automation holds back
    # new files while outside it (an empty pair = no window; a stateless
    # run constructs without these and is never gated)
    encode_window_start: str = ""
    encode_window_end: str = ""


def watch(
    roots: list[Path],
    output_dir: Path | None = None,
    target_vmaf: float = DEFAULT_TARGET_VMAF,
    encoder: str = "svt_av1",
    probe_encoder_preset: str = "5",
    encoder_preset: str = "3",
    poll_interval: float = POLL_INTERVAL,
    once: bool = False,
    max_threads: int | None = None,
    stop_event: threading.Event | None = None,
    db_file: Path | None = None,
    min_source_height: int = DEFAULT_MIN_SOURCE_HEIGHT,
    min_source_width: int = DEFAULT_MIN_SOURCE_WIDTH,
    min_saving_percent: int = 15,
    min_source_size_gb: float = 0.0,
    candidate_codecs: tuple[str, ...] = ("h264",),
    crop_sample_count: int = 10,
    audio_languages: list[str] | None = None,
    subtitle_languages: list[str] | None = None,
    progress: ProgressCallback | None = None,
    settings: Callable[[], WatchSettings] | None = None,
    load_config: Callable[[], "Conf"] | None = None,
    record: Callable[[Path, QualityResult, Path | None], int | None] | None = None,
    submit: Callable[[Path], bool] | None = None,
    extensions: list[str] | None = None,
) -> None:
    """Watches the inbox and encodes newly arrived movies.

    This function blocks (unless once=True); stop it with Ctrl+C - the stop
    event ends the pass loop and terminates the running encode child
    on the direct-encode route (issue #431; the submit route's running job
    is terminated by the worker's stop), and the process exits cleanly.

    Args:
        roots (list[Path]): The folders to watch, each walked recursively
            so a new or changed file anywhere under one is picked up.
        output_dir (Path | None): Directory for the encoded files, or None to
            encode IN PLACE: each verified output takes the source's own path
            and the original is renamed aside and kept.
        target_vmaf (float): The VMAF quality floor.
        min_saving_percent (int): The minimum predicted saving (percent)
            the decision demands (issue #316; 0 = any real shrink).
        encoder (str): The video encoder.
        probe_encoder_preset (str): The PROBE's speed preset (a measurement
            setting, issue #22).
        encoder_preset (str): The FINAL encode's speed preset (an output
            setting, issue #22).
        poll_interval (float): Seconds between inbox checks (grows while the
            inbox stays idle, resets on any new candidate).
        once (bool): One pass and return (for tests).
        max_threads (int | None): Per-encode thread cap (None/0 = all cores).
        stop_event (threading.Event | None): Cooperative stop for the resident
            app: checked between passes AND passed to encode_with_quality as
            its cancel event, so a stop terminates the running encode
            child on the direct-encode route (the submit route's running job
            is terminated by the worker's stop, issue #431).
        db_file (Path | None): The sqlite database file - enables the stall
            watchdog's failure-bundle telemetry (None: no telemetry; the
            CLI one-shot watch prints the reason instead).
        progress (ProgressCallback | None): Live-update callback receiving the
            engine's event dicts ({'stage': 'encode', 'percent': ..., 'movie':
            ...} and {'stage': 'probe', 'detail': ...}); the CLI's watch display
            renders these. None (default): the events are dropped.
        settings (Callable[[], WatchSettings] | None): Asked at the start of
            every pass and before every file for the settings to apply, so a
            changed config takes effect at the next file and an idle watcher
            still notices it (the resident app passes one, issue #247). None:
            the fixed arguments above apply to every file.
        load_config (Callable[[], Conf] | None): The operator's configuration
            provider, used ONLY to fill the language filters when the caller
            passed none (review finding 6: the built-in defaults must not be
            a silent narrowing filter; the resident app passes its loader so
            a saved change applies at the next file). None (default): the
            built-in defaults - no filtering, the 1:1 behaviour.
        record (Callable[[Path, QualityResult, Path | None], int | None] | None):
            Persisted one encode's (or skip's) outcome (issue #232): called as
            record(movie, result, final_output) where final_output is the path
            the artifact occupies when the run replaces sources in place, and
            returns the encode row's id so a DECLINED swap can correct the row
            (`_swap`). The engine layer cannot import the service layer (the
            import-linter contract), so the caller above passes this in - the
            resident app passes one closure, a stateless run passes None and
            records nothing, exactly as db_file=None does.
    """

    # no provider: no language filter, the strict 1:1 behaviour the docstring
    # promises (review finding 6) - never the built-in narrowing lists
    if load_config is None:
        default_audio_languages: list[str] = []
        default_subtitle_languages: list[str] = []
    else:
        _conf = load_config()
        default_audio_languages = _conf.handbrake_audio_languages
        default_subtitle_languages = _conf.handbrake_subtitle_languages
    current = WatchSettings(
        max_threads=max_threads,
        target_vmaf=target_vmaf,
        min_saving_percent=min_saving_percent,
        min_source_height=min_source_height,
        min_source_width=min_source_width,
        min_source_size_gb=min_source_size_gb,
        candidate_codecs=candidate_codecs,
        crop_sample_count=crop_sample_count,
        audio_languages=audio_languages if audio_languages is not None else default_audio_languages,
        subtitle_languages=subtitle_languages
        if subtitle_languages is not None
        else default_subtitle_languages,
    )
    extensions = extensions or [".mkv", ".mp4", ".avi", ".m4v", ".ts"]
    # the pause state lives beside the database (issue #250): no database,
    # no resident app to pause
    state_dir = db_file.resolve().parent if db_file is not None else None
    paused_reason: str | None = None
    window_reason: str | None = None
    known: set[str] = set()
    if db_file is not None:
        # the scanned library IS the memory (issue #231): every file row holds
        # the mtime+size its decision was made from, so a restart resumes
        # instead of re-probing the whole library - the cost that made every
        # container restart burn hours of CPU on files it had already decided
        known = _known_identities_from_db(db_file)
    lock_file = WATCH_LOCK_FILE
    sleep_interval = poll_interval

    # misconfiguration guard: when the output lands inside the watched
    # inbox, the next pass sees it as a new movie and re-encodes - for
    # inbox == output_dir that means HandBrake writes its own INPUT's path
    # (truncating the source mid-read, then A1 debris-removal on it).
    # Overlap in either direction poisons the candidate set => refuse.
    # the overlap guard applies ONLY to the two-directory form: in place there
    # is no second tree, and the staging/retained names are excluded from the
    # candidate scan instead
    if output_dir is not None:
        inbox_resolved = roots[0].resolve()
        output_resolved = output_dir.resolve()
        if (
            inbox_resolved == output_resolved
            or inbox_resolved in output_resolved.parents
            or (output_resolved in inbox_resolved.parents)
        ):
            raise ValueError(
                "inbox and output_dir must be separate directories (the output "
                f"must not live inside the inbox): inbox='{roots[0]}', output='{output_dir}'"
            )

    if not acquire_lock(lock_file):
        log_error("Another watcher seems to be running (lock file exists).")
        return

    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    # the watches need the roots to EXIST: _add_recursive walks each root, so
    # a root created here would otherwise be watched never (os.walk of a
    # missing directory yields nothing, silently - no events, _failed stays
    # False, the poll still works but the wake never fires)
    waiter = _FsWaiter(roots)
    waiter.start()

    # in place: the encode still needs a real path to write to, so it lands in
    # a working directory INSIDE the inbox - the same filesystem, so the swap's
    # two renames stay atomic and cheap. The output directory only says where
    # the bytes are written; the swap target is always the source.
    staging: contextlib.AbstractContextManager[str] = (
        contextlib.nullcontext(str(output_dir))
        if output_dir is not None
        else tempfile.TemporaryDirectory(prefix=STAGING_DIR_PREFIX, dir=roots[0])
    )
    disk_target = output_dir if output_dir is not None else roots[0]
    in_place_mode = output_dir is None

    try:
        with staging as work_dir:
            target_dir = Path(work_dir)
            while True:
                if stop_event is not None and stop_event.is_set():
                    log("Watch stopped (stop event set).")
                    return
                # every pass asks too, not only every file: an idle watcher
                # still notices a broken config edit within one poll interval
                # and reports it (issue #247)
                if settings is not None:
                    current = settings()
                # paused (by the operator, or for a manual encode, issue #250):
                # nothing enters `known`, so every file stays eligible, and the
                # base interval (not the idle backoff) notices a resume promptly
                reason = pause_reason(state_dir)
                if reason is not None:
                    if reason != paused_reason:
                        log(f"Watcher paused: {reason}. Files stay eligible.")
                    paused_reason = reason
                    if once:
                        return
                    _wait_or_stop(poll_interval, stop_event, waiter=waiter)
                    continue
                if paused_reason is not None:
                    log("Watcher resumed.")
                    paused_reason = None
                # the daily encode window (issue #434): outside it the pass
                # defers - nothing enters `known`, so every file stays
                # eligible, and the base interval notices the window opening
                window_open = encode_window_open(
                    current.encode_window_start, current.encode_window_end
                )
                if not window_open:
                    if window_reason is None:
                        log(
                            f"Watcher outside the encode window "
                            f"({current.encode_window_start}-"
                            f"{current.encode_window_end}): files stay eligible."
                        )
                        window_reason = "outside the encode window"
                    if once:
                        return
                    _wait_or_stop(poll_interval, stop_event, waiter=waiter)
                    continue
                if window_reason is not None:
                    log("Watcher inside the encode window: resuming.")
                    window_reason = None
                # pass-level disk guard: a full output drive would burn a full
                # CRF probe per candidate per pass (probe, watchdog cancel,
                # retry next pass, repeat). Skipping the pass BEFORE anything
                # enters `known` keeps the movies eligible for when space frees
                # up, and the adaptive poll backs off meanwhile.
                free = disk_free_bytes(disk_target)
                if free is not None and free < DISK_FREE_FLOOR_BYTES:
                    log_error(
                        f"Output drive nearly full: {free / (1024**3):.1f} GB free "
                        f"(floor 1.0 GB) => waiting. Movies stay eligible; the "
                        "watcher retries when space frees up."
                    )
                    if once:
                        return
                    _wait_or_stop(sleep_interval, stop_event, waiter=waiter)
                    continue
                candidates = [
                    (root, movie)
                    for root in roots
                    for movie in find_new_movies(root, extensions, known)
                ]
                for movie_root, movie in candidates:
                    if stop_event is not None and stop_event.is_set():
                        log("Watch stopped (stop event set).")
                        return
                    try:
                        stability = check_stability(movie)
                    except OSError:
                        stability = None
                    if stability is None or stability.young_for > 0:
                        # NOT marked known: the file is still copying and stays
                        # eligible (it must be reconsidered once stable - the
                        # old code added it to `known` first and never looked
                        # at it again, starving every normal copy arrival)
                        log(f"'{movie.name}' is not stable yet (still copying?) => later.")
                        continue
                    if stability.incomplete is not None:
                        # an old file that is still incomplete is a stalled
                        # download: another pass will not finish it. Marked known
                        # like a decided skip, so it is reported once; a resumed
                        # download writes, which gives it a new identity and
                        # brings it back (issue #479)
                        log(
                            f"Skipping '{movie.name}': {stability.incomplete} "
                            "=> re-decided when it changes."
                        )
                        with suppress(OSError):
                            known.add(_identity(movie))
                        continue
                    # a manual encode, a pause, or the encode window can begin
                    # mid-pass: hold this file and the rest back; the next pass
                    # reports it (issue #434)
                    if pause_reason(state_dir) is not None:
                        break
                    if not encode_window_open(
                        current.encode_window_start, current.encode_window_end
                    ):
                        break
                    try:
                        known.add(_identity(movie))
                    except OSError:
                        # the file vanished mid-pass (issue #427): skip it
                        continue

                    if settings is not None:
                        current = settings()
                    decision = decide(movie, current.target_vmaf, encoder)
                    if decision != "encode":
                        log(f"Skipping '{movie.name}': {decision}.")
                        continue
                    if submit is not None:
                        # the queue owns the work (issue #231): the watcher is a
                        # discovery loop, not a second engine. The file is
                        # SCANNED into the database here (the contract's first
                        # half) and the encode job decides from that fresh scan
                        # - with db rows, job history, retry and verification
                        # all coming from the queue's own path. submit is the
                        # enqueue seam the layer contract requires (engine
                        # cannot import server); False means the queue refused
                        # (e.g. the same file is already queued) and the file
                        # stays eligible for a later pass.
                        if _scan_into_db(movie, movie_root, db_file):
                            try:
                                queued = submit(movie)
                            except sqlite3.Error as e:
                                # the enqueue's database write failed (a read-only
                                # or full db, issue #427): a per-file failure -
                                # the file stays eligible, retried when the file changes
                                log_error(
                                    f"Queuing '{movie.name}' failed: {e}"
                                    " => retried when the file changes."
                                )
                                continue
                            if queued:
                                log(f"Queued '{movie.name}' for the backlog.")
                            continue
                        # the scan failed (unreadable file): log and retry when
                        # the file changes - it is already in `known`, so a
                        # failed read is not re-attempted until then
                        continue
                    # no queue in play (a stateless/test run): the direct path
                    # below is the engine's own, and it records through `record`
                    try:
                        # hold the encode slot on the MOVIE'S OWN DIRECTORY:
                        # that is where the staging copy and the swap's two
                        # renames happen, so it is the one key every writer can
                        # agree on. A library root is not an identity - a
                        # watcher on /media and a job on /media/Movies would
                        # take two different locks for one file (#379). One
                        # slot at a time, never nested: the pass is sequential.
                        lock_target = movie.parent if in_place_mode else target_dir
                        with guard_encodes(lock_target, holder="watch"):
                            result = encode_with_quality(
                                movie,
                                target_dir / movie.name,
                                # the measurement child opts out of the stall
                                # watchdog (issue #431): without this event a
                                # hung measurement is unbounded until the
                                # container stops - the shutdown event is the
                                # kill path (review on PR #468, finding 1)
                                cancel=stop_event,
                                target_vmaf=current.target_vmaf,
                                encoder=encoder,
                                probe_encoder_preset=probe_encoder_preset,
                                encoder_preset=encoder_preset,
                                encoder_threads=cap_threads(current.max_threads),
                                db_file=db_file,
                                # inbox automation must not silently depend on
                                # plan staleness: watch always measures fresh
                                force_reprobe=True,
                                min_source_height=current.min_source_height,
                                min_source_width=current.min_source_width,
                                min_source_size_gb=current.min_source_size_gb,
                                min_saving_percent=current.min_saving_percent,
                                candidate_codecs=current.candidate_codecs,
                                crop_sample_count=current.crop_sample_count,
                                # a struct without the filters (tests, older
                                # callers) falls back to the same defaults as
                                # the first pass: the provider's, or no filter
                                audio_languages=current.audio_languages
                                if current.audio_languages is not None
                                else default_audio_languages,
                                subtitle_languages=current.subtitle_languages
                                if current.subtitle_languages is not None
                                else default_subtitle_languages,
                                progress=progress,
                            )
                            # the ledger write and the swap stay INSIDE the
                            # slot (the review on this PR, finding 1): the
                            # in-place form's deterministic
                            # '<source>.shrinkray-staging' name and the swap's
                            # two renames are what two writers keyed on the
                            # same root race on - the job path covers them
                            # inside its guard, the watcher must too
                            if result.skipped_reason:
                                # NO line here (issue #471's no-duplicate rule):
                                # the decision line came from the engine site
                                # that decided it (plan.py's verdict or the
                                # pipeline's gate/skip lines); this branch only
                                # records
                                if record is not None:
                                    record(movie, result, None)
                            elif result.success:
                                encode_id = (
                                    record(movie, result, movie if in_place_mode else None)
                                    if record
                                    else None
                                )
                                if in_place_mode:
                                    if not _swap(
                                        db_file,
                                        movie,
                                        result.output,
                                        encode_id,
                                        error=result.error,
                                        input_size=result.input_size,
                                        input_mtime=getattr(result, "input_mtime", None),
                                    ):
                                        # declined (a verification finding, a grown
                                        # output or a refused rename): _swap said why,
                                        # and the green "Done" below would read as a
                                        # replacement
                                        continue
                                    log(
                                        f"Done: '{movie.name}' -> CRF {result.chosen_crf} "
                                        f"(VMAF {result.achieved_vmaf}), "
                                        f"{result.saved_percent:.1f}% saved (original retained)."
                                    )
                                else:
                                    log(
                                        f"Done: '{movie.name}' -> CRF {result.chosen_crf} "
                                        f"(VMAF {result.achieved_vmaf}), "
                                        f"{result.saved_percent:.1f}% saved (source left untouched)."
                                    )
                            else:
                                log_error(f"Encoding '{movie.name}' failed: {result.error}")
                    except EncodeBusyError as e:
                        log(f"Skipping '{movie.name}' for now: {e} => retry next pass.")
                        # the file vanished mid-pass (issue #427): there is
                        # nothing to retry - the next scan re-decides it
                        with suppress(OSError):
                            known.discard(_identity(movie))
                        continue
                    except Cancelled as e:
                        # disk watchdog (free space hit the floor) or user cancel:
                        # partial output already removed by the engine; retry
                        # when space freed up (or the user re-ran watch)
                        log_error(f"Encoding '{movie.name}' cancelled: {e} => retry next pass.")
                        # the file vanished mid-pass (issue #427)
                        with suppress(OSError):
                            known.discard(_identity(movie))
                        continue
                    except RuntimeError as e:
                        # a per-file failure (unreadable, unprobeable, encode
                        # error): logged with the file's name, and the WATCHER
                        # CONTINUES - one corrupt file must not end the loop
                        # that runs unattended for months (issue #260). The
                        # file is already in `known` (marked above), so it is
                        # not retried until it changes; whether it should be
                        # retried at all is #231's failure-memory decision.
                        log_error(f"Encoding '{movie.name}' failed: {e} => the watcher continues.")
                        continue
                    except (OSError, sqlite3.Error) as e:
                        # the DIRECT path's swap and encode filesystem work
                        # (issue #427): the stat/rename/unlink and db-write
                        # calls that are not SwapError-wrapped (stage/swap's
                        # own causes convert) - a PER-FILE failure under the same
                        # #260 rule as the RuntimeError branch above: logged
                        # with the file's name and the watcher continues. This
                        # branch covers the stateless/direct form; the QUEUE
                        # path's escapes are guarded in find_new_movies (the
                        # identity stat), _scan_into_db (the scan's reads) and
                        # the submit/enqueue call (its sqlite write).
                        log_error(f"Encoding '{movie.name}' failed: {e} => the watcher continues.")
                        continue

                sleep_interval = next_poll_interval(sleep_interval, poll_interval, bool(candidates))
                if once:
                    return
                _wait_or_stop(sleep_interval, stop_event, waiter=waiter)
    except KeyboardInterrupt:
        log("Watch stopped (current state is kept).")
    finally:
        waiter.stop()
        release_lock(lock_file)


def _swap(
    db_file: Path | None,
    source: Path,
    output: Path,
    encode_id: int | None = None,
    error: str | None = None,
    input_size: int | None = None,
    input_mtime: float | None = None,
) -> bool:
    """Swaps a verified encode into the source's path, retaining the original.

    The watcher's in-place form writes its encode into a staging directory and
    then moves it here, so no second copy survives in the watched tree. Size is a
    gate even though the engine already refused a grown encode: this is the last
    code before a file enters the library. A failed swap never loses the source -
    replace.swap renames the original back when the second rename fails.

    Args:
        db_file (Path | None): The database file, or None for a stateless run
            (the swap still happens, but no ledger row is written).
        source (Path): The library file the output replaces.
        output (Path): The verified encoded file.
        encode_id (int | None): The encode row the watcher wrote for this
            encode (issue #232). A DECLINED swap clears the row's output_file:
            the encode is discarded with its staging directory, so the row must
            not point at a path that is about to vanish.
        error (str | None): The encode's verification finding (issue #240: a
            lost track, an unverifiable probe). A finding declines the swap
            before any filesystem work: the source is never touched.
        input_size (int | None): The source's size in the run identity
            (issue #436), when the watcher carries one. A source whose size
            or mtime differs at swap time declines the swap (issue #585).
        input_mtime (float | None): The source's mtime in the run identity
            (issue #436). Compared strictly, like
            `engine.identity.ensure_unchanged`; the 1 s tolerance in `db.py`
            belongs to the plan cache's fingerprint, a different mechanism.

    Returns:
        bool: True when the source was replaced, False when the swap was
            declined (the reason is logged).
    """
    from shrinkray import db, replace

    def _discard_row(reason: str | None = None) -> None:
        if db_file is not None and encode_id is not None:
            connection = db.connect(db_file)
            try:
                # clearing the path IS the discard: one statement records the
                # outcome and its reason (issue #625)
                db.set_encode_output(connection, encode_id, None, reason)
            finally:
                connection.close()

    if error is not None:
        log_error(f"Not replacing '{source.name}': {error} => the original is kept.")
        _discard_row(error)
        return False
    if input_size is not None and input_mtime is not None:
        # the swap is the most consequential write (issue #585): a source that
        # changed since the encode was decided must not be replaced by an encode
        # of its old bytes (#436's identity, compared at the last code before
        # the library - strict, like `ensure_unchanged`, because the identity
        # was captured by a stat of this very file and an unchanged file
        # re-stats identical; a same-size, mtime-preserved change is #438's
        # boundary)
        try:
            current = source.stat()
        except OSError:
            current = None
        if current is None or current.st_size != input_size or current.st_mtime != input_mtime:
            log_error(
                f"Not replacing '{source.name}': the source changed during the "
                "encode => the original is kept, the encode is discarded."
            )
            _discard_row("the source changed during the encode")
            return False
    size_before = source.stat().st_size
    size_after = output.stat().st_size
    if size_after >= size_before:
        reason = f"the output is not smaller ({size_before} -> {size_after} bytes)"
        log_error(f"Not replacing '{source.name}': {reason}")
        _discard_row(reason)
        return False
    try:
        staged = replace.stage(output, source)
        retained = replace.swap(source, staged)
    except replace.SwapError as swap_error:
        log_error(f"Not replacing '{source.name}': {swap_error}")
        _discard_row(str(swap_error))
        return False
    if db_file is not None:
        connection = db.connect(db_file)
        try:
            db.add_retained(connection, source, retained, None, size_before, size_after)
        finally:
            connection.close()
    output.unlink(missing_ok=True)
    log(
        f"Replaced '{source.name}': the original is retained at "
        f"'{retained.name}' ({size_before / (1024**3):.2f} GB); delete it when "
        "you are satisfied (see the retained command)."
    )
    return True
