# src/shrinkray — package internals

Why this file: the package is layered (`tools/` -> `engine/` -> `server/` -> the
CLI in `__main__.py`), enforced by import-linter (see the root AGENTS.md gate).
Module choice is the whole design decision; `models/` holds one data file and
earns no file of its own.
Root AGENTS.md covers the product rules and the gate; this file is the module map.

## MODULE MAP

| Question | Module | Notes |
|---|---|---|
| CLI command, output tables | `__main__.py` (1304 L) | typer app; `scan/encode/plan/report/compare/doctor/watch/app`; no business logic |
| HTTP + dashboard | `server/app.py` (1334 L) | FastAPI, routes incl. `/api/events` SSE, `/api/reveal`, static UI; job boot + auto-backlog |
| Job queue, worker thread, live progress | `server/jobs.py` (1004 L) | `JobQueue` (persisted), `ProgressBus` (in-memory, bounded) |
| Config schema and defaults | `config.py` (442 L) | pydantic `ConfigModel`, `Conf` flat facade, `resolve_config_path` |
| sqlite schema, migrations, queries | `db.py` (1193 L) | `SCHEMA_VERSION = 15` (`db.py:8`), `_migrate` |
| Encode machinery, post-encode verify, batch loop | `engine/pipeline.py` (1647 L) | `encode_with_quality`, `encode_movies`, `_encode_with_command`, `EncodeResult`, `check_disk_space` |
| VMAF-driven CRF search | `engine/search.py` (820 L) | `DEFAULT_TARGET_VMAF = 96.0` (`search.py:48`), `bisect_crf`, `sample_count_for` |
| Plan cache + decision inputs | `engine/plan.py` (596 L) | `plan_quality`, `_lookup_plan`, `QualityResult`, crop planning |
| Sampling (extract, probe windows) | `engine/samples.py` (201 L) | `extract_samples`, `DEFAULT_SAMPLE_EVERY` |
| Black-bar detection | `engine/crop.py` (249 L) | `detect_crop_rect`, `has_black_bars` — one module owns both bar detectors |
| HDR decision + encoder constants | `engine/encoders.py` (51 L) | `encoder_for`, `HDR_VMAF_OFFSET` |
| Inbox watchdog | `engine/watch.py` (474 L) | `watch()`, `decide`, stability + lock |
| One-encoder-per-output-dir lock | `engine/lock.py` (134 L) | `guard_encodes` context manager; 6 h stale window |
| Scan -> DB rows | `engine/scan.py` (486 L) | `scan_movies`, thread pool `min(8, cpu)` |
| HandBrake command builder | `tools/handbrake.py` (210 L) | `build_encode_command`, `find_handbrake` |
| ffprobe parse | `tools/ffprobe.py` (379 L) | `run_ffprobe`, `get_data`, `probe_duration_seconds` |
| VMAF measure | `tools/vmaf.py` (296 L) | `measure_vmaf`, `build_vmaf_command`; model at `tools/vmaf.py:26` |
| Priority / nice / RSS / cpu quota | `tools/process.py` (502 L) | `apply`, `spawn_kwargs`, `cpu_count` (cgroup-aware) |
| Batch orchestration + result persistence | `service.py` (558 L) | `record_plan`, `save_encode`, `save_skip`, `doctor` |
| HTML report | `report.py` (289 L) | self-contained, reads sqlite directly |
| Failure bundles | `telemetry.py` (102 L) | `save_failure` -> `logs/` next to the caller's db file (None => no bundle), best-effort by design |
| Logging | `logger.py` (23 L) | loguru; `log`/`log_warning`/`log_error` |
| Grab bag (dissolving, #63) | `helpers.py` (190 L) | imported only by `__main__.py` and `engine/scan.py` |

`models/vmaf_v0.6.1.json` is shipped data, not source; `py.typed` makes the
package's annotations visible to consumers.

## LAYER RULES (enforced by `uv run lint-imports`, 7th gate command)

- `tools/` imports nothing above it.
- `engine/` may import `tools/`; never `server/` or the CLI.
- `server/` and `__main__.py` may import `engine/` and `tools/`; the
  foundation-forbidden contract keeps direct `server -> tools` edges out (they
  go through `engine`).
- New module in the wrong layer = red `lint-imports`, not a review comment.

## CONVENTIONS LOCAL TO THE PACKAGE

- The engine decides: anything that measures or predicts lives under `engine/`,
  never in `__main__.py` or `server/`. The CLI and the server deliberately
  duplicate the per-movie loop (progress rendering differs); keep the decision
  in the engine.
- `Conf` exposes flat attributes (`config.handbrake_encoder_preset`); there is no
  `config.handbrake.encoder` access path outside the pydantic model.
- Every long child goes through `tools/process.spawn_kwargs()`; short ffprobes
  stay at normal priority.
- Encode slot: any new entry point that writes into an output dir must wrap its
  work in `engine/lock.guard_encodes(...)` — the CLI, the pipeline and the
  watchdog all do.
- Tests patch the LOOKUP home, not the definition home
  (`mock.patch("shrinkray.engine.pipeline.find_handbrake")` — the pipeline's
  own binding, not `tools.handbrake`'s). Moving a function between modules
  changes the patch target, not the logic.

## GOTCHAS

- `DEFAULT_TARGET_VMAF = 96.0` (`engine/search.py:48`); HDR lowers it by
  `HDR_VMAF_OFFSET` (0.5). Quote the constant, not the prose.
- `CANDIDATE_CODECS` and `MODERN_CODECS` live in `engine/plan.py`: HEVC/AV1 are
  only candidates when bars must be removed.
- HDR switches the engine to the 10-bit encoder and a lower VMAF floor; the
  probe preset differs from the final preset by design (#22) and the plan
  fingerprint keys on it.
- `engine/pipeline.py` (1647 L) and `server/app.py` (1334 L) are the two files
  where a change deserves an issue first — a small edit near the CRF search or
  a route's containment check can silently change what every other path
  measures.
