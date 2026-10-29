"""Watch mode: automatically encode new movies arriving in a folder.

Polls an inbox directory for new movie files, waits until they are stable
(no longer growing => the download/copy finished), applies the same skip
logic as the encode/plan commands and encodes them (source stays untouched
=> the human stays in the loop for replacing library files).
"""

import contextlib
import ctypes
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

from shrinkray.config import DEFAULT_MIN_SOURCE_HEIGHT, DEFAULT_MIN_SOURCE_WIDTH
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

# a file is stable when its size is unchanged between two checks ...
STABLE_CHECK_INTERVAL = 30.0
# ... and it is at least this old (grace period after the last modification)
STABLE_MIN_AGE = 60.0
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
    the queue runs (`submit`). A failed scan returns False and the caller
    leaves the file eligible (it is already in `known`, so the retry happens
    when the file changes).

    Args:
        movie (Path): The file to scan.
        root (Path): The watch root (the scan's category root).
        db_file (Path | None): The database file; None records nothing.

    Returns:
        bool: True when the file was scanned and recorded (or nothing needed
            recording), False when the scan failed.
    """
    if db_file is None:
        return True
    from shrinkray.config import load_config

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


def is_stable(path: Path) -> bool:
    """Checks whether a file has finished growing (download/copy complete).

    A file is stable when its size does not change between two checks and
    it was last modified at least STABLE_MIN_AGE seconds ago.

    Args:
        path (Path): The file to check.

    Returns:
        bool: True if the file can be considered complete.
    """
    try:
        size_1 = path.stat().st_size
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age < STABLE_MIN_AGE:
        return False
    time.sleep(STABLE_CHECK_INTERVAL)
    try:
        size_2 = path.stat().st_size
    except OSError:
        return False
    return size_1 == size_2


def wait_for_stable(
    path: Path,
    limit: float = STABLE_WAIT_LIMIT,
    cancel: threading.Event | None = None,
) -> bool:
    """Waits until the file settles, bounded and cancel-aware; the one seam
    for both entries (issue #437).

    The witness is the watcher's `is_stable` (size unchanged across a
    STABLE_CHECK_INTERVAL sleep, last change at least STABLE_MIN_AGE ago);
    the seam paces its calls so a settling file costs ONE interval TOTAL,
    not one per stage stacked: while the file is too young, the seam sleeps
    the REMAINING age once and then does the size check DIRECTLY (one
    interval, here - calling is_stable would sleep the interval a second
    time, the measured 90 s); while it is old enough, the witness's verdict
    IS the answer (its size-check sleep runs inside it). The watcher's loop
    needs no bound (it reconsiders on its next pass) and keeps its single
    is_stable check - an extraction, not a policy change; the one-shot CLI
    has no next pass, so it waits here and gives up loudly at the limit. A
    set cancel event raises Cancelled (the #326 contract: closing the
    terminal cancels the wait too, it never absorbs a signal for minutes).

    Args:
        path (Path): The file to wait for.
        limit (float): Total seconds to wait before giving up.
        cancel (threading.Event | None): Cooperative cancellation; when set,
            the wait raises instead of returning.

    Returns:
        bool: True when the file settled, False when the limit expired
            (the caller reports and skips; the file stays eligible).

    Raises:
        Cancelled: When the cancel event fires during the wait.
    """
    deadline = time.monotonic() + limit
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled while waiting for the file to settle")
        if time.monotonic() >= deadline:
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            # unreadable/vanished: same answer the witness would give, and
            # the bounded wait reports it as never-settled at the limit
            if time.monotonic() >= deadline:
                return False
            time.sleep(STABLE_CHECK_INTERVAL)
            continue
        if age >= STABLE_MIN_AGE:
            # old enough for the age gate: the witness's size check (one
            # interval, inside it) is the only remaining question
            if is_stable(path):
                return True
            if time.monotonic() >= deadline:
                return False
            # the witness slept one interval for the size check and returned
            # False (the size changed): sleep nothing extra, the next turn's
            # witness call re-checks at the new size
            continue
        # too young: sleep the REMAINING age, then the size check DIRECTLY
        # (one interval, here) - calling is_stable would sleep the interval a
        # SECOND time (the measured 90 s: 60 age + 30 interval on a file at
        # age ~0, identical to the old chunking). One age sleep + one size
        # interval is the whole cost of a complete fresh file.
        remaining = STABLE_MIN_AGE - age
        if time.monotonic() + remaining + STABLE_CHECK_INTERVAL >= deadline:
            return False
        time.sleep(remaining)
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled while waiting for the file to settle")
        try:
            size_1 = path.stat().st_size
        except OSError:
            # vanished during the age sleep: the next turn re-reads
            continue
        time.sleep(STABLE_CHECK_INTERVAL)
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled while waiting for the file to settle")
        try:
            size_2 = path.stat().st_size
        except OSError:
            continue
        if size_1 == size_2:
            return True
        # the size changed during the interval: still growing, the next
        # turn re-derives the age from the NEW mtime
        continue


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
    audio_languages: list[str] | None = None
    subtitle_languages: list[str] | None = None


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

    This function blocks (unless once=True); stop it with Ctrl+C - the
    running encode finishes first, then the state is written and the
    process exits cleanly.

    Args:
        roots (list[Path]): The folders to watch, each walked recursively
            so a new or changed file anywhere under one is picked up.
        output_dir (Path | None): Directory for the encoded files, or None to
            encode IN PLACE: each verified output takes the source's own path
            and the original is renamed aside and kept.
        target_vmaf (float): The VMAF quality floor.
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
            app (checked between passes; the running encode finishes first).
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
                for _root, movie in candidates:
                    if stop_event is not None and stop_event.is_set():
                        log("Watch stopped (stop event set).")
                        return
                    if not is_stable(movie):
                        # NOT marked known: the file is still copying and stays
                        # eligible (it must be reconsidered once stable - the
                        # old code added it to `known` first and never looked
                        # at it again, starving every normal copy arrival)
                        log(f"'{movie.name}' is not stable yet (still copying?) => later.")
                        continue
                    # a manual encode or a pause can begin mid-pass: hold this
                    # file and the rest back; the next pass reports the pause
                    if pause_reason(state_dir) is not None:
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
                        if _scan_into_db(movie, roots[0], db_file):
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
                                target_vmaf=current.target_vmaf,
                                encoder=encoder,
                                probe_encoder_preset=probe_encoder_preset,
                                encoder_preset=encoder_preset,
                                encoder_threads=cap_threads(current.max_threads),
                                db_file=db_file,
                                # inbox automation must not silently depend on
                                # plan staleness: watch always measures fresh
                                force_reprobe=True,
                                # the resident mode verifies every encode
                                # (issue #235): nobody watches the log in an
                                # unattended container, and the full-file VMAF
                                # is the only evidence the encode met the
                                # target that the operator will ever get
                                verify=True,
                                min_source_height=current.min_source_height,
                                min_source_width=current.min_source_width,
                                min_source_size_gb=current.min_source_size_gb,
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
                                log(f"Skipping '{movie.name}': {result.skipped_reason}.")
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
                                        db_file, movie, result.output, encode_id, error=result.error
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

    Returns:
        bool: True when the source was replaced, False when the swap was
            declined (the reason is logged).
    """
    from shrinkray import db, replace

    def _discard_row() -> None:
        if db_file is not None and encode_id is not None:
            connection = db.connect(db_file)
            try:
                db.set_encode_output(connection, encode_id, None)
            finally:
                connection.close()

    if error is not None:
        log_error(f"Not replacing '{source.name}': {error} => the original is kept.")
        _discard_row()
        return False
    size_before = source.stat().st_size
    size_after = output.stat().st_size
    if size_after >= size_before:
        log_error(
            f"Not replacing '{source.name}': the output is not smaller "
            f"({size_before} -> {size_after} bytes)"
        )
        _discard_row()
        return False
    try:
        staged = replace.stage(output, source)
        retained = replace.swap(source, staged)
    except replace.SwapError as swap_error:
        log_error(f"Not replacing '{source.name}': {swap_error}")
        _discard_row()
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
