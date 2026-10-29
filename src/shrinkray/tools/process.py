"""Process politeness and control (the subprocess runner's foundations).

ShrinkRay is a guest on machines that do other things (a desktop today,
a small media server tomorrow). These helpers keep it a polite guest:

- long-running children (encodes, probes, VMAF measurements) run below
  normal priority so they yield instantly to interactive work
- the resident app watches its own memory so "we stay lean" stays a
  measured fact instead of an assumption
- children are spawned in their own session and terminated tree-wide

No third-party dependencies: priority is a creation flag (Windows) or
process niceness (POSIX), memory is read from psapi (Windows) or
/proc (Linux).

Moved verbatim from resources.py (issue #97 phase 2): the tools/ package
is the only code that talks to external binaries.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING

from shrinkray.logger import log_error

if TYPE_CHECKING:
    from shrinkray.config import Conf

# module state: politeness is on by default; apply() can disable it
_IDLE_PRIORITY = True

# windows: priority classes are per-child creation flags; POSIX children
# inherit the process niceness set by nice_self() instead
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", None)


def session_kwargs() -> dict:
    """Keyword arguments for spawning cancellable children in their own
    session.

    POSIX: `start_new_session=True` puts the child (and everything it
    spawns) into a new process group, so `terminate_tree` can kill the
    whole subtree with one killpg — the documented upgrade path for
    tree-children. Production children (HandBrakeCLI, ffmpeg) are leaf
    processes today, but the test stubs (`sh` that runs `sleep`) are not:
    a group-kill is the only reliable way to end them AND keep the pipes
    EOF-clean (an orphaned grandchild holds the write-end, the drainer
    never sees EOF, the caller hangs - observed in CI on Linux).
    Windows: taskkill /T /F already walks the tree; nothing needed here.

    Returns:
        dict: kwargs to splat into subprocess.Popen (empty on Windows).
    """
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


def terminate_tree(popen: subprocess.Popen) -> None:
    """Terminates a child process INCLUDING all of its descendants.

    Why the tree matters: killing only the direct child leaves grandchildren
    alive, and they hold the inherited pipe write-handles - no EOF, the
    drain threads block forever, the caller hangs even though the kill
    "worked" (observed live: cmd.exe died at 1.0s, the orphaned ping kept
    the pipe open and the function returned 58s later when ping happened to
    finish). Windows: taskkill /T /F walks the descendant tree (terminate()
    is TerminateProcess there anyway - force-kill is the only option, and
    the terminate->kill escalation in the classic path is POSIX semantics).
    POSIX: the child runs in its own session (session_kwargs at spawn, the
    process group IS the tree) => killpg SIGTERM -> SIGKILL escalation on
    the whole group; every member dies and the pipes reach EOF. The group
    is only killed when the child actually sits in its own group (verified
    via getpgid against ours) - a child spawned without a session shares
    OUR group, and killpg there would terminate the caller itself. Such
    children (and unreadable ones) take the classic direct-child
    escalation below.

    Args:
        popen (subprocess.Popen): The child to stop (all of it).
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(popen.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass  # taskkill missing/blocked: classic escalation below
    # POSIX: killpg the child's group, but only when it differs from ours
    # (start_new_session makes the group the tree; a shared group must take
    # the classic path - killpg on it would hit the caller too). Platform
    # guard, not getattr-dancing: mypy narrows sys.platform, so the Windows
    # pass never type-checks this block (its stubs lack getpgid/killpg).
    if sys.platform != "win32":
        child_pgid: int | None = None
        own_pgid: int | None = None
        try:
            child_pgid = os.getpgid(popen.pid)
            own_pgid = os.getpgid(0)
        except OSError:
            pass  # child already gone / unreadable: classic path
        if child_pgid is not None and own_pgid is not None and child_pgid != own_pgid:
            try:
                os.killpg(child_pgid, signal.SIGTERM)
            except OSError:
                pass  # group already gone between getpgid and killpg
            else:
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child_pgid, signal.SIGKILL)
                    popen.wait(timeout=5)
                return
    # classic escalation fallback (Windows non-taskkill path, or a POSIX
    # child in our own group / already gone)
    popen.terminate()
    try:
        popen.wait(timeout=5)
    except subprocess.TimeoutExpired:
        popen.kill()
        popen.wait(timeout=5)


def apply(config: Conf) -> None:
    """Applies the resource policy from a config (Conf) object.

    Called once from load_config() so every entry point (CLI commands,
    resident app, watch loop) inherits the same policy without each call
    site having to thread it through.

    Args:
        config: The config object (reads idle_priority).
    """
    global _IDLE_PRIORITY
    _IDLE_PRIORITY = bool(getattr(config, "idle_priority", True))
    if _IDLE_PRIORITY:
        nice_self()


def spawn_kwargs() -> dict:
    """Keyword arguments for spawning long-running children politely.

    On Windows this lowers the child's scheduling priority below normal so
    it yields to everything else on the machine. On POSIX children inherit
    the process niceness (set once via apply()/nice_self), so nothing is
    needed here. Use for the LONG children only (encodes, probes,
    measurements) — short ffprobes stay normal priority. Honors the
    idle_priority config (off => empty kwargs).

    Returns:
        dict: kwargs to splat into subprocess.run / subprocess.Popen
            (empty when politeness is disabled or on POSIX).
    """
    if not _IDLE_PRIORITY:
        return {}
    if sys.platform == "win32" and _BELOW_NORMAL is not None:
        return {"creationflags": _BELOW_NORMAL}
    return {}


def nice_self(increment: int = 10) -> None:
    """Lowers this process's scheduling priority (POSIX niceness).

    Children spawned later inherit the niceness, so every child of the
    process runs politely. On Windows this is a no-op (per-child priority
    comes from spawn_kwargs instead). Failures are ignored: politeness
    must never break the actual work.

    Args:
        increment (int): The niceness increment (10 = low background work).
    """
    if sys.platform == "win32":
        return
    nice = getattr(os, "nice", None)
    if nice is None:
        return
    with contextlib.suppress(OSError):
        nice(increment)  # raising niceness is always allowed; guard anyway


def rss_mib() -> float | None:
    """Returns this process's resident memory in MiB (None if unknown).

    A monitoring number, not a promise: the resident set is what the OS
    keeps in RAM right now. Read from psapi on Windows and /proc on Linux
    (getrusage peak as the POSIX fallback, e.g. macOS).

    Returns:
        float | None: Resident set size in MiB, None when unreadable.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                """The psapi structure for GetProcessMemoryInfo."""

                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.windll.psapi
            # declare types: without argtypes the pseudo-handle (-1) is not
            # marshalled as a real HANDLE on 64-bit => error 6, no data
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = wintypes.HANDLE(-1)  # GetCurrentProcess() pseudo-handle
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return float(counters.WorkingSetSize) / (1024 * 1024)
            return None
        # POSIX: VmRSS from /proc is the honest current number (Linux)
        try:
            with open("/proc/self/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024
        except OSError:
            pass
        # fallback (e.g. macOS): getrusage peak (KiB on Linux, bytes on macOS)
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return peak / (1024 * 1024)
        return peak / 1024
    except (OSError, ValueError):
        return None


def apply_config(config: Conf) -> None:
    """Applies the process-level policy of a loaded config (one hook).

    The one call an ENTRY POINT makes after loading its config: process
    priority (apply) and the stall watchdog (configure_stall). Lives here
    so config.py stays side-effect free (issue #97 phase 2: loading config
    must not import engine modules or mutate process state as a
    side effect).

    Args:
        config: The loaded config (reads idle_priority,
            handbrake_stall_minutes).
    """
    apply(config)
    configure_stall(config.handbrake_stall_minutes)


def apply_reloaded_config(config: Conf) -> None:
    """Applies the part of the process policy that may change while running.

    The resident app re-reads its config between files (issue #247). The
    stall watchdog follows that reload. Process priority does not:
    nice_self() can only lower it, so re-applying it on every reload would
    keep lowering it, and `idle_priority` takes effect at the next start.

    Args:
        config: The config now in effect (reads handbrake_stall_minutes).
    """
    configure_stall(config.handbrake_stall_minutes)


def cpu_count() -> int:
    """Returns the CPUs this process may actually use (at least 1).

    os.cpu_count() reports the machine's cores, not the process's share. In a
    container with --cpus=4 on a 24-core host it returns 24, so a plan would
    start 24 parallel encodes inside a 4-CPU quota: they thrash and the
    watchdog's timing assumptions stop holding.

    Measured, not assumed: inside a container limited to 2 CPUs,
    os.cpu_count() AND os.process_cpu_count() both still report the host's 24,
    so the quota has to be read from the cgroup directly. cpu.max carries it on
    v2 ("<quota> <period>", "max" = unlimited) and cpu.cfs_quota_us /
    cpu.cfs_period_us on v1. Where neither is readable (bare metal, macOS,
    Windows) the machine's core count is the right answer.

    Returns:
        int: The usable CPU count, never less than 1.
    """
    quota = _cgroup_cpu_quota()
    if quota is not None:
        return max(1, quota)
    return max(1, os.cpu_count() or 1)


def _cgroup_cpu_quota() -> int | None:
    """Returns how many whole CPUs a cgroup quota permits.

    Returns:
        int | None: The permitted whole CPUs, or None when unlimited or
        unreadable.
    """
    try:
        parts = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except OSError:
        parts = []
    if len(parts) == 2 and parts[0] != "max":
        try:
            return int(parts[0]) // int(parts[1])
        except (ValueError, ZeroDivisionError):
            return None
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
    except (OSError, ValueError):
        return None
    if quota <= 0:
        return None
    return quota // period


# The stall watchdog (moved from encode.py, issue #97 phase 2).

# default stall timeout: a healthy HandBrake reports progress at least once
# per second; minutes of silence mean a hung child. Configurable via
# handbrake.stall_minutes (0 = disabled). Generous by design: final muxing
# of a large file can be silent for a while.
DEFAULT_STALL_MINUTES = 10.0

_stall_minutes: float | None = DEFAULT_STALL_MINUTES


def configure_stall(minutes: float) -> None:
    """Sets the stall timeout for encode/probe subprocesses.

    Args:
        minutes (float): Minutes without child output before a subprocess is
            terminated as hung; 0 disables the watchdog.
    """
    global _stall_minutes
    _stall_minutes = minutes if minutes and minutes > 0 else None


def stall_timeout_seconds() -> float | None:
    """Returns the stall timeout in seconds (None = disabled).

    Returns:
        float | None: Seconds of tolerated child silence, None when off.
    """
    return _stall_minutes * 60.0 if _stall_minutes is not None else None


# The streaming subprocess runner and the cancellation exception
# (moved verbatim from quality.py, issue #97 phase 2: the tools/
# package owns every subprocess run).


def _run(
    command: list[str],
    cwd: Path | None = None,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
    watchdog: bool = True,
) -> tuple[int, str]:
    """Runs a subprocess and returns (returncode, stderr as text).

    With a cancel event, the process is polled and terminated when the
    event fires; the returncode -1 signals the cancellation to the caller.
    Both pipes are drained by daemon threads while the process runs: a
    child writing more than the ~64K pipe buffer (HandBrake progress on
    stdout, libvmaf/ffmpeg warnings on stderr) would otherwise block
    forever while this loop only polls for exit — a silent deadlock.

    Args:
        command (list[str]): The command to run.
        cwd (Path | None): Working directory.
        cancel (threading.Event | None): Cooperative cancellation.
        timeout (float | None): Wall-clock ceiling in seconds for BOUNDED
            reads (issue #384): the crop pass needs its duration-derived
            ceiling even when the caller passes no cancel event. A timeout
            expiry terminates the tree and returns -2 (the stall code —
            same "the child did not finish" semantics); None keeps the
            documented no-timeout behaviour of the unprotected path.
        watchdog (bool): Whether the stall watchdog applies to this child
            (issue #431). libvmaf is silent by design until the whole file
            is scored, so the measurement path passes False and keeps the
            cancel event as its kill path; a child whose output cadence is
            the progress itself (HandBrake) keeps the default True.

    Returns:
        tuple[int, str]: The returncode (-1 when cancelled, -2 when timed
            out or stalled) and stderr text.
    """
    if cancel is None and timeout is None and stall_timeout_seconds() is None:
        # Neither cancellation nor stall watching is possible: the simple
        # blocking run (used when both protections are explicitly off).
        # Fallback decision (issue #62): no timeout here on purpose — the
        # probes this runs are minutes-long encodes, a fixed timeout would
        # kill healthy work, and the operator has explicitly disabled stall
        # protection. "Watchdog off" means no protection; the streaming
        # Popen path below is the protected shape.
        proc = subprocess.run(command, capture_output=True, cwd=cwd, **spawn_kwargs())
        return proc.returncode, proc.stderr.decode(errors="replace").strip()
    popen = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        **session_kwargs(),
        **spawn_kwargs(),
    )
    assert popen.stdout is not None and popen.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    last_output_cell = [time.monotonic()]  # drain threads report progress

    def _drain(pipe: IO[bytes], chunks: list[bytes]) -> None:
        """Reads a pipe to its EOF into chunks (runs in a daemon thread).

        Every chunk read also refreshes the shared last-output timestamp:
        the stall watchdog in the poll loop cannot see the pipes itself,
        and a healthy child must never be mistaken for a hung one.
        """
        try:
            for chunk in iter(lambda: pipe.read(4096), b""):
                chunks.append(chunk)
                last_output_cell[0] = time.monotonic()
        except (ValueError, OSError):
            pass  # pipe closed during shutdown (cancellation race)

    drainers = [
        threading.Thread(target=_drain, args=(popen.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=_drain, args=(popen.stderr, stderr_chunks), daemon=True),
    ]
    for drainer in drainers:
        drainer.start()
    cancelled = False
    stalled = False
    stall_seconds = stall_timeout_seconds()
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while popen.poll() is None:
            if cancel is not None and cancel.is_set():
                terminate_tree(popen)
                cancelled = True
                break
            # stall watchdog: probes/measurements must make progress too
            # (ffmpeg can hang on odd streams); same semantics as the final
            # encode - terminate the hung child, report it as a failure.
            # Opted out per child (watchdog=False, issue #431): a silent-by-
            # design child (libvmaf) would be killed mid-run as hung.
            if (
                watchdog
                and stall_seconds is not None
                and time.monotonic() - last_output_cell[0] > stall_seconds
            ):
                log_error(
                    f"Stall watchdog: no output from the probe/measure child for "
                    f"{stall_seconds / 60:.0f} min => terminating it (hung child)."
                )
                terminate_tree(popen)
                cancelled = False
                stalled = True
                break
            # the caller's bounded-read ceiling (issue #384): the crop pass's
            # duration-derived timeout, honoured even without a cancel event
            if deadline is not None and time.monotonic() > deadline:
                terminate_tree(popen)
                return -2, f"timed out after {timeout:.0f}s (terminated)"
            time.sleep(0.2)
        for drainer in drainers:
            drainer.join(timeout=5)
        if cancelled:
            return -1, "cancelled by user"
        if stalled:
            assert stall_seconds is not None  # stalled=True implies it was set
            return -2, (f"stalled: no output for {stall_seconds / 60:.0f} min (terminated)")
        stderr = b"".join(stderr_chunks).decode(errors="replace").strip()
        return popen.returncode, stderr
    finally:
        # close OUR read ends first: a drainer blocked in read() (orphan
        # still holding the write end) must not deadlock close() itself -
        # closing breaks the read, the bounded join above cleans up
        if popen.stdout:
            popen.stdout.close()
        if popen.stderr:
            popen.stderr.close()


class Cancelled(Exception):
    """Raised inside the engine when the job's cancel event fires.

    The worker turns this into a proper CANCELLED job state; partial
    artifacts (probe files, half-written output) are the caller's to
    clean up (the sample workdir is removed by its own finally).
    """


def process_exists(pid: int) -> bool:
    """Checks whether a process id is alive right now.

    POSIX: kill(pid, 0) (no signal delivered, existence only).
    Windows: a tasklist probe (OpenProcess semantics via subprocess).

    Args:
        pid (int): The process id to probe.

    Returns:
        bool: True if a process with this pid exists.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # a Windows process exists iff tasklist finds its pid; decode
            # lossily (the console codepage is not utf-8)
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                timeout=10,
            )
            return str(pid) in listing.stdout.decode(errors="replace")
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def process_start_ticks(pid: int) -> int | None:
    """Returns when a process started, to tell it apart from a reused pid.

    Linux only: field 22 of /proc/<pid>/stat, in clock ticks since boot. Two
    processes that ever share a pid have different start times, which matters
    in a container, where pids restart from 1 on every start.

    Args:
        pid (int): The process id.

    Returns:
        int | None: The start time, or None when the process does not exist
            or the platform has no /proc (the caller falls back to the pid).
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    # the command name (field 2) is parenthesised and may hold spaces or
    # parentheses itself: count the fields after its LAST closing parenthesis
    fields = stat[stat.rfind(")") + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None
