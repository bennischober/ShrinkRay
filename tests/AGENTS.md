# tests/ — the suite

Flat `test_*.py` modules plus `conftest.py` and an empty `__init__.py`; every test class is a `unittest.TestCase`.

Flat by design: `tests/fixtures/` is the only subdirectory, and no `tests/engine/`-style mirror exists yet. A file is named after its *concern*, not its module — the table below is the map.

## WHERE TO LOOK

| Test file | What it pins |
|---|---|
| `test_quality` | `engine.search` + `engine.samples` CRF bisect, probe cache, plan reuse, subprocess timeouts; also greps `src/shrinkray/{__main__,server/app,server/jobs}.py` for a restated `95.0` literal |
| `test_db` | `db` schema + migrations, `engine.plan`, `server.app`, `server.jobs` |
| `test_encode` | `engine.pipeline` + `engine.lock`, `__main__` encode callbacks (`_record_encodes`, `_quality_progress_callback`), `tools.vmaf`, stall-watchdog CR/LF |
| `test_watch` | `engine.watch` (`decide`, `is_stable`, `find_new_movies`, `_identity`), lock acquire/release, the `replace` staging/retained suffixes |
| `test_jobs` | `server.jobs` lifecycle, retention, thread cap, UI dist routes |
| `test_cli` | `__main__` commands through `typer.testing.CliRunner`, plus `TestReadmeInvocations` parsing real `README.md` argv |
| `test_crop_timeline` | `engine.crop` timeline pass; asserts a library scan never runs it |
| `test_probe_progress`, `test_watch_progress`, `test_watch_output`, `test_preflight`, `test_fs_waiter`, `test_bracket_paths`, `test_single_file_scope`, `test_plan_exit`, `test_plan_no_table` | one CLI/engine seam each — progress output, env-gated output, preflight problems, `_FsWaiter`/`_sleep_plain`, path resolution, plan exit codes |
| `test_media`, `test_hb_probe`, `test_missing_binary`, `test_resources`, `test_hdr_fixture` | `tools.*`: frame-rate/duration parsing, `supports_svt_av1`, binary-absent degradation, RSS/`_run`, HDR detection |
| `test_auth`, `test_metrics` | `server.app` token gate, Prometheus render |
| `test_replace`, `test_plans`, `test_misc`, `test_timestamps`, `test_telemetry_leak` | swap-in-place retention; plans table (`db` + `service`); scan-time conversions (`engine/scan.py`), `config` schema and encoder presets, subprocess timeouts; console log format; the `logs/` leak when `encode_with_quality(verify=True)` has no `db_file` |
| `test_setup_tui` | `tui.SetupApp` through Textual's headless pilot (`asyncio.run` per test): shown values, save keeps other keys and permissions, schema-refused and non-numeric values never written, the re-measure warning, discard, the pause switch, the command refusing a non-terminal (issue #252) |
| `test_pause` | `engine.pause` (operator flag, manual-encode markers, dead/reused pid, no-/proc age fallback), the watcher holding files back, a plan job waiting and reporting, `paused` on `/api/status`, the CLI encode's marker (issue #250) |
| `test_config_reload` | the config on `/data` (issue #247): `config.seed_config_file`/`seed_config_from_env` and the CLI callback, `refresh_config` (reload, invalid edit kept out, error once), the watcher's `WatchSettings` provider, per-movie refresh in `server.jobs`, `config_error` on `/api/status` |
| `test_container`, `test_container_config`, `test_release_notes` | repo artifacts, not the package: cgroup CPU plan, `container-config.json` + `Dockerfile` + `.dockerignore`, `tools/healthcheck.py`, `tools/console_banner.py` (every named command exists), `tools/container-shrinkray.sh` + `tools/container-console.sh` (syntax and rules; behaviour is the release smoke test's), `tools/release_notes.py` (via `sys.path.insert`) |
| `test_workflows` | `.github/workflows/codeql-analysis.yml` + `dependabot.yml`: all three `github/codeql-action` steps pin one commit, and Dependabot groups `github/codeql-action/*` so the next bump stays uniform (#448, #449) |

## CONVENTIONS

- **Coverage runs on every `pytest` invocation.** `addopts = "-ra --cov=shrinkray --cov-report=term-missing"`, `[tool.coverage.run] branch = true`, `[tool.coverage.report] fail_under = 70`. `tools/gate.sh --no-cov` exists only for the release job.
- **The gate does not type-check this directory.** `[tool.mypy] files = ["src"]`, so `uv run mypy --platform …` never sees `tests/`; the `[[tool.mypy.overrides]] module = "tests.*"` (`disallow_untyped_defs = false`) only applies when tests are passed explicitly. Annotate anyway — ruff still lints them (`src = ["src", "tests"]`, line-length 100, bandit `S` on).
- **`conftest.py` is one session-scoped autouse guard, `_no_working_tree_pollution`:** it diffs `git ls-files --others --directory --exclude-standard` before and after the session and fails on any new entry (empty directories included — `git status` was blind to issue #38's literal `Z:` directory). It returns `None`, i.e. does nothing, outside a git checkout. In this shared checkout, a file another agent creates mid-run fails the suite too; the message names it.
- **Fixture style: `tempfile.TemporaryDirectory()` in `setUp`/`tearDown`** (some call `.resolve()` before a scope assertion). No test uses `tmp_path`.
- **Pure `unittest`, no plugin idioms:** no `parametrize`, no `pytest-asyncio` in the dev deps, and no `async def test_` — the one coroutine route is driven by `asyncio.run` from a sync method in `test_auth`. Routes are exercised with `fastapi.testclient.TestClient` inside sync tests.
- **Optional-binary tests degrade per test, not per file:** `test_hdr_fixture` always runs its dict-level cases against the committed `fixtures/hdr10_probe.json` and skips only the live-ffprobe case; `test_jobs` `skipTest`s when `ui/dist` is not built.

## HOTSPOTS & SEAMS

- Most of the suite's lines sit in six files: `test_quality`, `test_db`, `test_encode`, `test_watch`, `test_jobs`, `test_cli`. Some files still open with a "Split from `tests/test_shrinkray.py` (2026-10-23)" header — that file no longer exists.
- **Patch seams are load-bearing.** `find_handbrake` is faked at three shapes: `shrinkray.engine.pipeline.find_handbrake`, `shrinkray.tools.handbrake.find_handbrake` and `patch.object(…, "find_handbrake")` — the lookup home differs per caller. `plan_quality` and `encode_with_quality` are the two seams the encode surface is faked at.
- Private names the suite imports (renaming them breaks tests, not just internals): `__main__._record_encodes`, `._swap_into_place`, `._quality_progress_callback`; `pipeline._check_cancel`, `._probe_movie`, `._encode_with_command`; `watch._identity`, `._FsWaiter`, `._sleep_plain`; `lock._lock_path`; `tools.process._run`; `samples._run`; `server.app._sse_stream`, `._serve_forever` (the ASGI-server seam, #373).
- `test_container` reads `.dockerignore` and asserts the patterns that keep secrets, `*.db` and `test_media/` out of the image layer; it is the only test that names `test_media/`.
- Never lower `fail_under` to make a run pass; the comment in `pyproject.toml` says raise it deliberately.
