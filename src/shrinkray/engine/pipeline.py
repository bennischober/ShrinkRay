"""The encode pipeline: from movie list to verified output.

Moved verbatim from encode.py (issue #97 phase 3): the batch orchestration
(encode_file/encode_movies/_encode_pending), the command runner
(_encode_with_command), the disk guard (check_disk_space/disk_free_bytes)
and the result type. The stall watchdog and its configuration stay in
tools/process.py; the command builders stay in tools/handbrake.py.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from rich.progress import Progress

from shrinkray import resources
from shrinkray.engine.plan import (
    MIN_ENCODE_SECONDS as MIN_ENCODE_SECONDS,
)
from shrinkray.logger import log, log_error
from shrinkray.tools.ffprobe import (
    FFPROBE_TIMEOUT as FFPROBE_TIMEOUT,
)
from shrinkray.tools.ffprobe import (
    probe_duration_seconds as probe_duration_seconds,
)
from shrinkray.tools.handbrake import (
    HANDBRAKE_BINARY as HANDBRAKE_BINARY,
)
from shrinkray.tools.handbrake import (
    build_command as build_command,
)
from shrinkray.tools.handbrake import (
    build_encode_command as build_encode_command,
)
from shrinkray.tools.handbrake import (
    find_handbrake as find_handbrake,
)
from shrinkray.tools.process import (
    DEFAULT_STALL_MINUTES as DEFAULT_STALL_MINUTES,
)
from shrinkray.tools.process import (
    Cancelled as Cancelled,
)
from shrinkray.tools.process import (
    configure_stall as configure_stall,
)
from shrinkray.tools.process import (
    spawn_kwargs as spawn_kwargs,
)
from shrinkray.tools.process import (
    stall_timeout_seconds as stall_timeout_seconds,
)
from shrinkray.tools.process import (
    terminate_tree as terminate_tree,
)

ProgressCallback = Callable[[dict], None]


def cap_threads(max_threads: int | None) -> int | None:
    """Normalizes a configured thread cap into an encoder thread count.

    Args:
        max_threads (int | None): The configured cap (0 or None = no cap;
            negative values are also treated as no cap).

    Returns:
        int | None: The thread count for HandBrake's --encopts threads=N,
            None when uncapped.
    """
    if max_threads is None or max_threads <= 0:
        return None
    return max_threads


def check_disk_space(target_dir: Path, required_bytes: int) -> tuple[bool, str]:
    """Checks whether the target's drive can hold the required bytes.

    The honest worst-case bound for a re-encode run is the SUM of the
    pending inputs (the estimate can be wrong; the inputs exist and can
    not shrink below themselves). Returns a problem description instead
    of raising so callers can choose error vs warning.

    Args:
        target_dir (Path): The directory the encodes write into (its drive
            is checked; created if missing).
        required_bytes (int): The worst-case bytes needed.

    Returns:
        tuple[bool, str]: (ok, message). ok=False => the encode cannot
            finish; ok=True with a non-empty message => tight but possible
            (the caller warns); ok=True with an empty message => plenty.
    """
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target_dir).free
    except OSError as e:
        # unreadable drive (network share without statvfs etc.): degrade
        # gracefully - never block an encode on a broken probe
        return True, f"disk space could not be checked ({e})"
    if free < required_bytes:
        return False, (
            f"not enough disk space on '{target_dir}': "
            f"{free / (1024**3):.1f} GB free, need up to {required_bytes / (1024**3):.1f} GB "
            f"(worst case = the pending inputs' total size)"
        )
    if free < required_bytes * 1.5:
        return True, (
            f"disk space is tight on '{target_dir}': {free / (1024**3):.1f} GB free, "
            f"up to {required_bytes / (1024**3):.1f} GB needed"
        )
    return True, ""


DISK_FREE_FLOOR_BYTES = 1 * 1024**3


def _terminate(popen: subprocess.Popen) -> None:
    """Terminates a child INCLUDING its descendants (tree-safe).

    Args:
        popen (subprocess.Popen): The child to stop.
    """
    resources.terminate_tree(popen)


def disk_free_bytes(target_dir: Path) -> int | None:
    """Reads the free bytes on the drive of target_dir (None if unreadable).

    Args:
        target_dir (Path): Any directory on the drive to check.

    Returns:
        int | None: Free bytes, or None when the drive cannot be probed
            (the watchdog must never crash an encode on a broken statvfs).
    """
    try:
        return shutil.disk_usage(target_dir).free
    except OSError:
        return None


@dataclass
class EncodeResult:
    """Result of a HandBrake encode.

    Attributes:
        input (Path): The input file.
        output (Path): The output file.
        success (bool): True if the encode succeeded.
        input_size (int): Size of the input file in bytes.
        output_size (int): Size of the output file in bytes.
        error (str | None): The error message if the encode failed.
        cancelled (bool): True when the encode was aborted by the job's
            cancel event (the output is partial and was removed).
    """

    input: Path
    output: Path
    success: bool
    input_size: int = 0
    output_size: int = 0
    error: str | None = None
    cancelled: bool = False
    input_width: int | None = None
    input_height: int | None = None
    input_codec: str | None = None
    input_duration: float | None = None

    @property
    def saved_percent(self) -> float:
        """Percentage of disk space saved by the encode."""
        if self.input_size <= 0 or self.output_size <= 0:
            return 0.0
        return round((1 - self.output_size / self.input_size) * 100, 2)


def durations_match(input_file: Path, output_file: Path) -> bool:
    """Checks that the encoded output has (almost) the input duration.

    HandBrake may exit with 0 even for an interrupted encode that only wrote
    part of the file. A truncated output is shorter or has no readable
    duration at all.

    Args:
        input_file (Path): The source file.
        output_file (Path): The encoded file.

    Returns:
        bool: True if the durations match within a small tolerance.
    """
    source = probe_duration_seconds(input_file)
    encoded = probe_duration_seconds(output_file)
    if source is None and encoded is None:
        # cannot verify at all (e.g. ffprobe missing) => do not report failure
        return True
    if encoded is None:
        # source readable but output unreadable => genuinely broken
        return False
    if source is None:
        return True
    # tolerate frame rounding; 1% or 2s, whichever is larger
    tolerance = max(2.0, source * 0.01)
    return abs(source - encoded) <= tolerance


def _encode_with_command(
    input_file: Path,
    output_file: Path,
    command: list[str],
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    db_file: Path | None = None,
) -> EncodeResult:
    """Runs one HandBrake command and verifies its output.

    With a progress callback, HandBrake's stdout is streamed and its
    percentage lines ('Encoding: task 1 of 1, 42.31 %') are reported live
    as {'stage': 'encode', 'percent': 42.31, 'movie': name}. With a cancel
    event, the streamed loop aborts and the process is terminated.

    The streamed loop is also the disk watchdog: on every percent line the
    free space on the output drive is checked, and when it drops to the
    floor (DISK_FREE_FLOOR_BYTES) the encode is cancelled before the drive
    fills - the preflight's prediction can be wrong (size estimate,
    fixed-mode growth, a co-tenant consuming space concurrently).

    On any cancellation (cancel event or disk watchdog) the partial output
    file is removed: it is truncated debris by definition (A1 semantics),
    and it is OUR product from THIS run - never a pre-existing file. A stall
    (no child output for stall_minutes) is self-terminated too, but recorded
    as a FAILURE: a hung encode must not be retried by the watch loop.

    Args:
        input_file (Path): The source file.
        output_file (Path): The target file.
        command (list[str]): The full HandBrakeCLI command.
        progress (callable | None): Callback progress(dict) for live updates.
            MUST be a callable taking one dict (a rich Progress object is
            NOT callable and would crash on the first percent line); pass
            None only when neither progress nor the disk watchdog is wanted
            (non-streamed blocking run).
        db_file (Path | None): The database file, used to locate logs/ for
            the stall failure bundle (None: caller without a DB skips
            telemetry — CLI one-shots already print the reason).

    Returns:
        EncodeResult: The result of the encode.
    """
    stderr = ""
    returncode: int
    stderr_bytes: bytes | None = None
    cancelled = False
    stall_failed = False
    cancel_reason = "cancelled by user"
    stderr_chunks: list[bytes] = []
    try:
        if progress is None:
            # Fallback decision (issue #62): when the operator disables the
            # stall watchdog (handbrake.stall_minutes = 0) AND no progress /
            # cancellation is requested, this blocking run intentionally has
            # no timeout — encodes legitimately take hours, so a fixed
            # timeout would kill healthy work, and the operator has
            # explicitly opted out of stall protection. "Watchdog off"
            # means no protection; the streaming path below is the protected
            # shape and is what every product entry point uses.
            proc = subprocess.run(command, capture_output=True, **resources.spawn_kwargs())
            returncode = proc.returncode
            stderr_bytes = proc.stderr
        else:
            # bufsize=0: UNBUFFERED pipes. The default (~8KB buffered reader)
            # would make stream.read(4096) block until the buffer fills —
            # HandBrake's ~30-byte \r-updates never do, and the stall
            # watchdog would kill a healthy encode again (the round-6.11
            # false positive had two layers: readline() waiting for \n AND
            # the buffered read waiting for a full buffer; raw os.read on
            # the real binary showed updates every ~0.2s).
            popen = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                **resources.session_kwargs(),
                **resources.spawn_kwargs(),
            )
            assert popen.stdout is not None and popen.stderr is not None

            # stderr must be drained WHILE stdout is being read: HandBrake
            # writes work warnings to stderr and would block on a full pipe
            # buffer even though we keep reading stdout (the classic
            # communicate-vs-stream deadlock)
            def _drain_stderr() -> None:
                """Reads the stderr pipe to EOF into stderr_chunks."""
                stream = popen.stderr
                if stream is None:
                    return
                try:
                    for chunk in iter(lambda: stream.read(4096), b""):
                        stderr_chunks.append(chunk)
                except (ValueError, OSError):
                    pass  # pipe closed during shutdown (cancellation race)

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            # stdout is read by a dedicated thread into a queue: the main
            # loop polls with a timeout, so cancellation and the stall
            # watchdog are checked EVERY 0.2s - even while HandBrake is
            # silent. The old `for raw in iter(readline, b"")` blocked
            # inside readline() when the child hung: the cancel event was
            # never consulted (it is only checked between lines), so even
            # an explicit user cancel could not interrupt a stuck encode.
            stdout_queue: queue.Queue[bytes | None] = queue.Queue()

            def _pump_stdout() -> None:
                """Reads raw stdout chunks into the queue until EOF (None
                sentinel).

                Chunk reads (read1), NOT readline(): HandBrake's piped
                progress is \r-separated and never contains a \n (measured
                live: ~14k chunks/120s, every one CR-only, largest gap
                0.27s). readline() blocked forever between such updates, so
                a HEALTHY encode looked silent and the stall watchdog
                killed it after 10 minutes (the round-6.11 false positive:
                a real encode died mid-run with cores fully busy).
                """
                stream = popen.stdout
                if stream is None:
                    return
                try:
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        stdout_queue.put(chunk)
                except (ValueError, OSError):
                    pass  # pipe closed during shutdown (cancellation race)
                finally:
                    stdout_queue.put(None)

            stdout_thread = threading.Thread(target=_pump_stdout, daemon=True)
            stdout_thread.start()

            last_output = time.monotonic()
            stall_seconds = stall_timeout_seconds()
            pending = b""  # raw tail: a percent value may span chunks

            def _stdout_lines(chunk: bytes) -> list[str]:
                """Splits one raw stdout chunk into complete lines.

                Both \r and \n end a line (HandBrake overwrites its
                progress line with \r; its log lines end \r\n). The
                incomplete tail is kept for the next chunk — a percent
                value can be split mid-number across writes.

                Args:
                    chunk (bytes): The raw bytes just read from stdout.

                Returns:
                    list[str]: The complete lines, decoded and stripped.
                """
                nonlocal pending
                pending += chunk
                *complete, pending = re.split(rb"[\r\n]", pending)
                return [p.decode(errors="replace").strip() for p in complete]

            def _on_percent(match: re.Match) -> bool:
                """Handles one percent match: disk watchdog, live event.

                Args:
                    match (re.Match): The percent regex match.

                Returns:
                    bool: True when the encode must abort (disk floor hit);
                    the main loop breaks then.
                """
                nonlocal cancelled, returncode, cancel_reason, stderr_bytes
                # disk watchdog: the preflight's prediction can be wrong
                # (size estimate, fixed-mode growth, another process
                # consuming the drive). When free space drops to the
                # floor, cancel the encode rather than fill a shared
                # disk. One statvfs per percent line is effectively free.
                free = disk_free_bytes(output_file.parent)
                if free is not None and free < DISK_FREE_FLOOR_BYTES:
                    log(
                        f"Disk watchdog: only {free / (1024**3):.1f} GB free on "
                        f"'{output_file.parent}' => cancelling the encode of "
                        f"'{input_file.name}' before the drive fills."
                    )
                    resources.terminate_tree(popen)
                    cancelled = True
                    returncode = -1
                    cancel_reason = "cancelled: disk space exhausted"
                    stderr_bytes = b"cancelled: disk space exhausted"
                    return True
                progress(
                    {
                        "stage": "encode",
                        "percent": float(match.group(1)),
                        "movie": input_file.name,
                    }
                )
                return False

            def _consume(raw: bytes) -> bool:
                """Scans one chunk for percent updates.

                Args:
                    raw (bytes): The raw chunk from the pump thread.

                Returns:
                    bool: True when the encode must abort (disk floor hit).
                """
                for line in _stdout_lines(raw):
                    match = re.search(r"(\d{1,3}\.\d{2})\s*%", line)
                    if match and _on_percent(match):
                        return True
                return False

            while True:
                # cooperative cancel: checked every tick, silent child or not
                if cancel is not None and cancel.is_set():
                    _terminate(popen)
                    cancelled = True
                    returncode = -1
                    cancel_reason = "cancelled by user"
                    stderr_bytes = b"cancelled by user"
                    break  # stall watchdog: a healthy HandBrake reports at least once
                # per second; long silences mean a hung child (deadlocked
                # muxer, stuck network source). Self-termination after
                # stall_seconds - a cancel the process sets ITSELF. Recorded
                # as a FAILURE (cancelled=False): watch mode must not retry
                # a stalled encode (it would stall again).
                if stall_seconds is not None and time.monotonic() - last_output > stall_seconds:
                    log_error(
                        f"Stall watchdog: no HandBrake output for "
                        f"{stall_seconds / 60:.0f} min => terminating the encode of "
                        f"'{input_file.name}' (hung child)."
                    )
                    _terminate(popen)
                    returncode = -1
                    # keep the child's own collected stderr as evidence —
                    # the one-line stall message goes in FRONT of it (the
                    # pre-2026-10-23 code OVERWROTE the collected stderr
                    # here, discarding the evidence of what the child was
                    # doing when it looked hung)
                    collected = b"".join(stderr_chunks)
                    stderr_bytes = (
                        b"stalled: no HandBrake output for "
                        + f"{stall_seconds / 60:.0f} min".encode()
                        + b"\n--- child stderr (last output before termination) ---\n"
                        + collected
                    )
                    stall_failed = True
                    break
                try:
                    raw = stdout_queue.get(timeout=0.2)
                except queue.Empty:
                    continue  # silent child: re-check cancel/stall on the next tick
                if raw is None:  # EOF sentinel: the child exited
                    break
                last_output = (
                    time.monotonic()
                )  # ANY bytes count: a working HandBrake streams \r-updates
                if _consume(raw):
                    break
            # EOF flush: the last progress line may lack its terminator.
            # Only the event fires here — the disk watchdog ran on every
            # prior percent line, and terminating an already-exited child
            # would turn a finished encode into a failure.
            if pending and not cancelled and not stall_failed:
                match = re.search(r"(\d{1,3}\.\d{2})\s*%", pending.decode(errors="replace"))
                if match:
                    progress(
                        {
                            "stage": "encode",
                            "percent": float(match.group(1)),
                            "movie": input_file.name,
                        }
                    )
            if not cancelled and not stall_failed:
                # close FIRST, join second: closing our read-end breaks a
                # reader blocked in read() (and EOF cannot depend on an
                # orphaned grandchild still holding the write-end - the
                # join() with a timeout below is bounded regardless)
                popen.stdout.close()
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)
                stderr_bytes = b"".join(stderr_chunks)
                # Popen.returncode is None until the process is reaped:
                # pipes at EOF mean HandBrake has exited, but without an
                # explicit wait() the attribute never gets set and a
                # successful encode read as `None != 0` => failed with the
                # whole log as its error (first full streamed encode, job
                # #20, exposed this)
                try:
                    returncode = popen.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    popen.kill()
                    returncode = popen.wait(timeout=10)
            else:
                # terminated mid-stream: close our pipe ends so the reader
                # threads unblock even if an orphan still holds the write
                # end (bounded joins, daemon threads: tidy, never hanging)
                popen.stdout.close()
                popen.stderr.close()
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return EncodeResult(input=input_file, output=output_file, success=False, error=str(e))

    if cancelled:
        # a killed HandBrake leaves a truncated partial: OUR product from
        # THIS run, debris by definition => remove it immediately (never a
        # pre-existing file)
        output_file.unlink(missing_ok=True)
        return EncodeResult(
            input=input_file,
            output=output_file,
            success=False,
            cancelled=True,
            error=cancel_reason,
        )
    if stall_failed:
        # the stall watchdog fired: same partial-output semantics as any
        # termination, but NOT a cancel - the caller must not retry it.
        # Telemetry: a stall kill is a diagnosis problem (was the child
        # really hung?) - leave the same evidence bundle as any other
        # failure (pre-2026-10-23 the path returned silently, logs/ never
        # even existed after one)
        output_file.unlink(missing_ok=True)
        if db_file is not None:
            from shrinkray.telemetry import save_failure

            save_failure(
                db_file=db_file,
                kind="encode-stall",
                command=command,
                stderr=(stderr_bytes or b"").decode(errors="replace").strip(),
                context={
                    "movie": input_file.name,
                    "output": str(output_file),
                    "stall_minutes": stall_seconds / 60.0 if stall_seconds else None,
                },
            )
        return EncodeResult(
            input=input_file,
            output=output_file,
            success=False,
            cancelled=False,
            error=(stderr_bytes or b"").decode(errors="replace").strip() or "stalled",
        )

    stderr = (stderr_bytes or b"").decode(errors="replace").strip()
    if returncode is None or returncode != 0:
        return EncodeResult(input=input_file, output=output_file, success=False, error=stderr)

    # HandBrake may exit with 0 even if the encode failed => the output file
    # must exist, be non-empty and have the full duration
    if not output_file.is_file() or output_file.stat().st_size == 0:
        return EncodeResult(
            input=input_file,
            output=output_file,
            success=False,
            error=stderr or "no output file was written",
        )
    if not durations_match(input_file, output_file):
        return EncodeResult(
            input=input_file,
            output=output_file,
            success=False,
            error="output is truncated (duration mismatch)",
        )

    # encode succeeded => collect sizes + the source snapshot
    from shrinkray.quality import _source_snapshot

    snapshot = _source_snapshot(input_file)
    return EncodeResult(
        input=input_file,
        output=output_file,
        success=True,
        input_size=input_file.stat().st_size,
        output_size=output_file.stat().st_size,
        input_width=snapshot["width"],
        input_height=snapshot["height"],
        input_codec=snapshot["codec"],
        input_duration=snapshot["duration"],
    )


def encode_file(
    input_file: Path,
    output_file: Path,
    preset: str = "Fast 1080p30",
    encoder: str = "svt_av1",
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> EncodeResult:
    """Re-encodes a movie file with HandBrakeCLI to reduce its file size.

    The HandBrakeCLI binary must be installed and findable (see find_handbrake).

    Args:
        input_file (Path): The file to re-encode.
        output_file (Path): The target file (must not exist, or overwrite=True).
        preset (str): The HandBrake preset, e.g. 'Fast 1080p30'.
        encoder (e.g. str): The video encoder, e.g. 'svt_av1'.
        overwrite (bool): Whether an existing output file may be replaced.
        progress (callable | None): Callback progress(dict) for live updates.

    Returns:
        EncodeResult: The result of the encode.
    """
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file '{input_file}' does not exist!")

    handbrake = find_handbrake()
    if handbrake is None:
        raise RuntimeError(
            f"'{HANDBRAKE_BINARY}' not found! Install HandBrake CLI "
            "(https://handbrake.fr/docs/en/latest/cli/cli-options.html)"
        )

    command = build_command(handbrake, input_file, output_file, preset, encoder)
    return _encode_with_command(input_file, output_file, command, progress=progress)


def encode_movies(
    movies: list[Path],
    output_dir: Path,
    preset: str = "Fast 1080p30",
    encoder: str = "svt_av1",
    dry_run: bool = False,
    overwrite: bool = False,
    workers: int | None = None,
    max_threads: int | None = None,
    progress: ProgressCallback | None = None,
    db_file: Path | None = None,
) -> list[EncodeResult]:
    """Re-encodes multiple movie files into an output directory.

    With workers > 1, several HandBrake processes run concurrently, each
    capped to a share of the CPU threads so the encodes do not oversubscribe
    the machine. Results are collected by index => deterministic order.

    Args:
        movies (list[Path]): The movie files to re-encode.
        output_dir (Path): Directory for the encoded files (created if missing).
        preset (str): The HandBrake preset, e.g. 'Fast 1080p30'.
        encoder (str): The video encoder, e.g. 'svt_av1'.
        dry_run (bool): If True, only show what would be encoded.
        overwrite (bool): Whether existing output files may be replaced.
        workers (int | None): Number of concurrent encodes (1 = sequential,
            None = automatic: 1, since a single SVT-AV1 encode already scales
            across all cores).
        max_threads (int | None): Per-encode thread cap (None/0 = all cores).
            With workers > 1 the cap only applies when it is tighter than
            the per-encode CPU share.
        progress (ProgressCallback | None): The caller's live-progress
            callback (the resident app's SSE channel); the rich bar is drawn
            alongside it.
        db_file (Path | None): The sqlite database file - enables the stall
            watchdog's failure-bundle telemetry (None: no telemetry).

    Returns:
        list[EncodeResult]: The results of the encodes.
    """
    results: list[EncodeResult] = []

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        for movie in movies:
            output_file = output_dir / movie.name
            log(f"[dry-run] Would encode '{movie}' -> '{output_file}'")
        return results

    if workers is None:
        # a single SVT-AV1 encode already uses all cores efficiently; parallel
        # encodes only help when the per-encode thread count is capped lower
        workers = 1
    workers = max(1, workers)

    # one encoder per output directory: the watch loop, the job worker and
    # CLI runs must never write the same output dir concurrently (clobbered
    # outputs); short-lived, pid-stamped lock file, stale locks are stolen.
    # EncodeBusyError propagates: the CLI exits with the message, the job
    # worker fails the job with it, the watch loop logs and retries later.
    # The slot is held BEFORE the pending-filter runs: a concurrent encoder
    # is writing a truncated in-flight output right now - without the slot,
    # the filter below would classify that file as debris and delete a live
    # encode's output.
    from shrinkray.engine.lock import guard_encodes

    with guard_encodes(output_dir, holder="encode"):
        # filter inside the slot (deterministic, before any encoding); the
        # debris check sees only our own outputs now
        pending: list[tuple[Path, Path]] = []
        for movie in movies:
            duration = probe_duration_seconds(movie)
            if duration is not None and duration < MIN_ENCODE_SECONDS:
                log(
                    f"Skipping '{movie.name}': duration {duration:.0f}s is below "
                    f"the {MIN_ENCODE_SECONDS:.0f}s minimum (not a movie?)."
                )
                continue
            output_file = output_dir / movie.name
            if output_file.exists() and not overwrite:
                if output_file.stat().st_size == 0:
                    log(f"Re-encoding '{movie.name}': output file is empty (0 bytes).")
                elif not durations_match(movie, output_file):
                    log(
                        f"Re-encoding '{movie.name}': existing output is truncated "
                        f"(debris from an interrupted encode) => removing it."
                    )
                    output_file.unlink()
                else:
                    log(f"Skipping '{movie.name}': output file already exists and verifies.")
                    continue
            pending.append((movie, output_file))
        if not pending:
            return results
        # worst-case bound: the pending inputs themselves (the size estimate
        # can be optimistic; the inputs exist and cannot shrink)
        ok, message = check_disk_space(output_dir, sum(m.stat().st_size for m, _ in pending))
        if not ok:
            raise RuntimeError(message)
        if message:
            log(f"Warning: {message}")
        return _encode_pending(
            pending, preset, encoder, workers, max_threads, progress, results, db_file=db_file
        )


def _encode_pending(
    pending: list[tuple[Path, Path]],
    preset: str,
    encoder: str,
    workers: int,
    max_threads: int | None,
    progress: ProgressCallback | None,
    results: list[EncodeResult],
    db_file: Path | None = None,
) -> list[EncodeResult]:
    """Runs the actual encode loop (the caller holds the encode slot).

    Args:
        pending (list[tuple[Path, Path]]): The (movie, output_file) pairs.
        preset (str): The HandBrake preset.
        encoder (str): The video encoder.
        workers (int): Number of concurrent encodes (1 = sequential).
        max_threads (int | None): Per-encode thread cap.
        progress: The rich progress bar.
        results (list[EncodeResult]): The list to append results to.
        db_file (Path | None): The sqlite database file - enables the stall
            watchdog's failure-bundle telemetry (None: no telemetry).

    Returns:
        list[EncodeResult]: The results (same list as the results arg).
    """
    # longest job first: big movies start early so the tail of the run is
    # short jobs, not an idle half machine next to one capped long encode
    pending.sort(key=lambda pair: -(probe_duration_seconds(pair[0]) or 0))

    cpu = resources.cpu_count()

    def run_one(movie: Path, output_file: Path, threads: int | None) -> EncodeResult:
        handbrake = find_handbrake()
        if handbrake is None:
            raise RuntimeError(
                f"'{HANDBRAKE_BINARY}' not found! Install HandBrake CLI "
                "(https://handbrake.fr/docs/en/latest/cli/cli-options.html)"
            )
        # the configured cap wins over the share when it is tighter; the
        # share only matters for parallel encodes sharing the machine
        cap = cap_threads(max_threads)
        if threads is not None and (cap is None or cap > threads):
            cap = threads
        command = build_command(
            handbrake,
            movie,
            output_file,
            preset,
            encoder,
            encoder_threads=cap,
        )
        # always stream: the streamed loop is the disk watchdog too (a
        # blocking subprocess.run would encode unwatched)
        return _encode_with_command(
            movie, output_file, command, progress=percent_callback, db_file=db_file
        )

    with Progress() as bar:
        task = bar.add_task("Encoding movies", total=len(pending))

        def percent_callback(data: dict) -> None:
            """Feeds engine percent events into the rich bar (a rich Progress
            is not callable itself - passing it as the callback crashed on
            the first HandBrake percent line).

            The caller's own callback (the resident app's live progress) gets
            the same events: a job that streams a percent must not fall silent
            because the CLI also draws a bar.
            """
            if progress is not None:
                progress(data)
            if "percent" in data:
                bar.update(task, completed=float(data["percent"]))

        if workers == 1:
            for movie, output_file in pending:
                result = run_one(movie, output_file, None)
                _report_encode(bar, result)
                results.append(result)
                bar.advance(task)
        else:
            # work-queue scheduling: every started encode holds a slot with a
            # fair share of the CPU; when a slot frees, the next movie starts
            # with the share of ALL free slots (a lone long movie reclaims the
            # full machine as the short ones finish)
            from concurrent.futures import ThreadPoolExecutor
            from threading import Lock

            lock = Lock()
            running: list[int] = []  # thread budgets of running encodes

            def slot_budget() -> int:
                with lock:
                    free = max(1, workers - len(running))
                    # shares are equal among the encodes that would run
                    return max(1, cpu // max(1, len(running) + free) * free)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                index = 0
                while index < len(pending) or running:
                    with lock:
                        free = workers - len(running)
                    if free > 0 and index < len(pending):
                        movie, output_file = pending[index]
                        index += 1
                        budget = slot_budget()
                        with lock:
                            running.append(budget)
                        future = executor.submit(run_one, movie, output_file, budget)

                        def done(fut: Future[EncodeResult], budget: int = budget) -> None:
                            with lock:
                                running.remove(budget)
                            result = fut.result()
                            _report_encode(bar, result)
                            results.append(result)
                            bar.advance(task)

                        future.add_done_callback(done)
                        futures.append(future)
                    else:
                        import time

                        time.sleep(0.5)
                for future in futures:
                    future.result()

    return results


def _report_encode(progress: Progress, result: EncodeResult) -> None:
    """Logs one finished encode to the progress console."""
    if result.success:
        saved = result.saved_percent
        change = f"{saved}% saved" if saved >= 0 else f"{abs(saved)}% larger"
        progress.console.print(
            f"Encoded '{result.input.name}': {result.input_size / (1024**3):.2f} GB -> "
            f"{result.output_size / (1024**3):.2f} GB ({change})"
        )
    else:
        log_error(f"Encoding '{result.input.name}' failed: {result.error}")
