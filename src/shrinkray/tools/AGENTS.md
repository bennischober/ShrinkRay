# src/shrinkray/tools — the subprocess boundary

Why this file (distinct domain): this is where the subprocess contract
lives — the one runner, the spawn policy, and one module per external binary.
Nothing here imports upward (the layer contract forbids it); `engine/`, `service/`
and `server/` all reach the binaries through these modules.

**Known deviation:** three engine modules still call
`subprocess.run` directly for SHORT reads — `engine/crop.py` (2 sites, incl. its own
`ffprobe -show_streams`), `engine/samples.py`, `engine/pipeline.py`.
The long children (encodes, VMAF) all go through `_run`/`spawn_kwargs`. New short
reads belong in the matching `tools/` module, not in a fourth copy.

## MODULE MAP

| Binary | Module | Notes |
|---|---|---|
| the runner, priority, cgroups | `process.py` | `_run`, `spawn_kwargs`, `session_kwargs`, `terminate_tree`, `cpu_count`, `rss_mib`, `Cancelled`, `apply_config`, `DEFAULT_STALL_MINUTES` |
| `ffprobe` | `ffprobe.py` | `FFPROBE_TIMEOUT = 60`, `run_ffprobe`, `probe_duration_seconds`, `get_data`, pure parse helpers |
| `HandBrakeCLI` | `handbrake.py` | `find_handbrake`, `build_encode_command`, `supports_svt_av1`, `MIN_HANDBRAKE_VERSION = "1.6"` |
| `ffmpeg` + libvmaf | `vmaf.py` | `measure_vmaf`, `build_vmaf_command`, `detect_vmaf_style`, `MAX_VMAF_THREADS = 8`, model path |

## THE RUNNER CONTRACT

`_run(command, cwd, cancel, timeout, watchdog=True)` returns `(returncode, stderr)`, signals
cancellation with `-1`, and a child it terminated (stall watchdog, or the
caller's `timeout` ceiling) with `-2` and a message saying which. `watchdog=False`
opts one child out of the stall watchdog (issue #431): libvmaf is silent by design
until the whole file is scored, so the measurement path (`vmaf.measure_vmaf`) passes
False and keeps the cancel event as its kill path — a silent-by-design child must
not be killed as hung. The kill path is the caller's cancel event, and every
production caller wires one (the worker's stop sets the running job's event; the
watcher's own direct path passes its stop event — reachable only outside the
resident app, where `watch()` runs without a queue; the resident app always goes
through the queue's submit seam and the worker owns the event; the scripts translate
signals). Both pipes are
drained by daemon threads (`process.py`) — a child writing more than the ~64 K pipe
buffer would otherwise deadlock a poll-only loop. `timeout` is for bounded reads
only (the crop timeline pass's duration-derived ceiling, #384). With neither a
cancel event, a timeout nor a stall watchdog, it falls back to a plain blocking
`subprocess.run` and deliberately sets NO timeout (`process.py`): "watchdog off"
means no protection, and a fixed timeout would kill healthy minutes-long probes. A
spawn failure (the binary missing from PATH, EMFILE/ENOMEM) raises `OSError` through
to the caller: the return codes mean the child RAN, so the runner adds no sentinel —
the call site converts the failure into its step's documented shape (issue #401).

## CONVENTIONS

- Argument lists only, never a shell string; `S603`/`S607` are reviewed ignores in
  `pyproject.toml` because the binaries are user-installed and resolved from PATH.
- Every long child is spawned through `spawn_kwargs()` (below-normal priority / nice)
  and in its own session via `session_kwargs()` so a cancellation can terminate the
  tree; short ffprobes stay at normal priority.
- A zero exit code is not success: callers verify the artifact (output exists,
  non-empty, duration matches) instead of trusting the code.
- Politeness is module state, set once by `apply_config(conf)` (`process.py`):
  `apply()` sets priority, `configure_stall()` arms the watchdog. Both default ON,
  and a test that flips them must reset them.
- Tool metadata is documentation, not function: `vmaf.py` omits the libvmaf token
  rather than failing an encode when ffmpeg is absent.
- Degrade, never crash, on a missing binary: `find_handbrake` returns `None` and the
  caller decides (`handbrake.py`).

## ANTI-PATTERNS

- Do not bypass `spawn_kwargs()`/`session_kwargs()` for a long child, and do not add a
  fixed timeout to the unprotected blocking path.
- Do not import anything above this layer — not `engine`, `service`, `server` or
  `__main__` — and do not add a new module here for a parse helper that belongs with
  its concern.
- Do not grow `process.py` into a general utils module: it is politeness and process
  control, and `engine/pipeline.py` imports it as `resources`.
- Do not add a second implementation of an external fact (a second ffprobe parser, a
  second command builder) — one module per binary, per the #97 decision record.
