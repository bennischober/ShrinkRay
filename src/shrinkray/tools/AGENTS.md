# src/shrinkray/tools — the subprocess boundary

Why this file (score 11, distinct domain): this is where the subprocess contract
lives — the one runner, the spawn policy, and one module per external binary.
5 modules / 1417 L, and `shrinkray.tools` is named 115 times outside the subtree.
Nothing here imports upward (the layer contract forbids it); `engine/`, `service/`
and `server/` all reach the binaries through these modules.

**Known deviation (measured at 31c5833):** three engine modules still call
`subprocess.run` directly for SHORT reads — `engine/crop.py` (4 sites, incl. its own
`ffprobe -show_streams`), `engine/samples.py` (`:109`), `engine/pipeline.py` (`:1032`).
The long children (encodes, VMAF) all go through `_run`/`spawn_kwargs`. New short
reads belong in the matching `tools/` module, not in a fourth copy.

## MODULE MAP

| Binary | Module | Notes |
|---|---|---|
| the runner, priority, cgroups | `process.py` (502 L) | `_run` (`:360`), `spawn_kwargs` (`:151`), `session_kwargs` (`:45`), `terminate_tree` (`:67`), `cpu_count` (`:274`), `rss_mib` (`:192`), `Cancelled` (`:466`), `apply_config` (`:257`), `DEFAULT_STALL_MINUTES` (`:330`) |
| `ffprobe` | `ffprobe.py` (392 L) | `FFPROBE_TIMEOUT = 60` (`:19`), `run_ffprobe` (`:65`), `probe_duration_seconds` (`:22`), `get_data` (`:330`), pure parse helpers |
| `HandBrakeCLI` | `handbrake.py` (214 L) | `find_handbrake` (`:20`), `build_encode_command` (`:89`), `supports_svt_av1` (`:186`), `MIN_HANDBRAKE_VERSION = "1.6"` (`:162`) |
| `ffmpeg` + libvmaf | `vmaf.py` (309 L) | `measure_vmaf` (`:196`), `build_vmaf_command` (`:88`), `detect_vmaf_style` (`:36`), `MAX_VMAF_THREADS = 8` (`:85`), model path (`:26`) |

## THE RUNNER CONTRACT

`_run(command, cwd, cancel)` returns `(returncode, stderr)` and signals
cancellation with `-1`. Both pipes are drained by daemon threads (`process.py:369-380`)
— a child writing more than the ~64 K pipe buffer would otherwise deadlock a poll-only
loop. With neither a cancel event nor a stall watchdog, it falls back to a plain
blocking `subprocess.run` and deliberately sets NO timeout (`process.py:385-390`):
"watchdog off" means no protection, and a fixed timeout would kill healthy
minutes-long probes.

## CONVENTIONS

- Argument lists only, never a shell string; `S603`/`S607` are reviewed ignores in
  `pyproject.toml:109-114` because the binaries are user-installed and resolved from PATH.
- Every long child is spawned through `spawn_kwargs()` (below-normal priority / nice)
  and in its own session via `session_kwargs()` so a cancellation can terminate the
  tree; short ffprobes stay at normal priority.
- A zero exit code is not success: callers verify the artifact (output exists,
  non-empty, duration matches) instead of trusting the code.
- Politeness is module state, set once by `apply_config(conf)` (`process.py:257`):
  `apply()` sets priority, `configure_stall()` arms the watchdog. Both default ON,
  and a test that flips them must reset them.
- Tool metadata is documentation, not function: `vmaf.py:305` omits the libvmaf token
  rather than failing an encode when ffmpeg is absent.
- Degrade, never crash, on a missing binary: `find_handbrake` returns `None` and the
  caller decides (`handbrake.py:20`).

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
