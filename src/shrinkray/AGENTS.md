# src/shrinkray — package internals

Why this file: the layering is machine-enforced, so which module a change lands in
IS the design decision. Root AGENTS.md owns the product rules and the gate; this
file is the module map plus the rules that bind the whole package. `engine/`,
`server/` and `tools/` each carry their own file — read it before editing them.
`models/` holds one JSON data file and no Python module, so it earns none.

## MODULE MAP (root level)

| Question | Module | Notes |
|---|---|---|
| CLI commands, tables, progress | `__main__.py` (936 L) | typer: `encode`, `plan`, `serve` (+ hidden `app`), `setup`, `retained`, `doctor`; no business logic |
| The terminal UI (`shrinkray setup`) | `tui.py` | Textual: edits config.json through `Conf` (invalid values are never written), warns on fingerprint settings, switches the pause; a front end beside `server`, never importing it (#252) |
| Config schema, defaults, flat facade | `config.py` (442 L) | pydantic sections + `Conf`; loading has no side effects |
| sqlite schema, migrations, queries | `db.py` (1318 L) | `SCHEMA_VERSION = 16` (`db.py:10`), `MIGRATIONS`, `prune_missing` |
| Swap-in-place, retained originals | `replace.py` (263 L) | `stage`/`swap`/`recover`; `RETAINED_SUFFIX` (`replace.py:37`) |
| Batch resume + result persistence | `service.py` (580 L) | `record_plan`, `save_encode`, `save_skip`, `doctor`, `all_problems` |
| Failure bundles | `telemetry.py` (102 L) | `save_failure` -> `logs/` beside the db file; best-effort by design |
| Logging | `logger.py` (29 L) | loguru; `log`, `log_warning`, `log_error` |
| Scan-time conversions | `engine/scan.py` | size/time/progress helpers moved here when `helpers.py` dissolved (#227); `__main__.py` keeps `calc_time` |

`__main__.py:64` defaults the database to `metadata.db` in the cwd. `py.typed`
(PEP 561, forced into the wheel) and `models/vmaf_v0.6.1.json` ship as package
data — the model is read from `tools/vmaf.py:26` and copied into a temp workdir per
measurement, never edited.

## LAYER RULES (`pyproject.toml:150`, enforced by `uv run lint-imports`)

Layers, highest to lowest: `__main__ > (server | tui) > service > engine > tools >
db > config > logger`; `server` and `tui` are independent siblings (#252). A second, forbidden-imports contract keeps `db`/`config`/`logger`/
`telemetry` from importing anything above them. Exactly one exemption exists:
`shrinkray.db -> shrinkray.engine.crop`, a `TYPE_CHECKING`-only annotation for the
crop summary (`db.py:12`) — annotation-only, never a runtime import. A module in the
wrong layer is a red gate, not a review comment.

## CONVENTIONS

- The engine decides. Anything that measures, predicts or skips lives under
  `engine/`; `__main__.py` and `server/jobs.py` both drive `plan_quality` /
  `encode_with_quality` and differ only in how they render progress.
- Constants are single-home and derived: the VMAF default lives in `config.py` (so the
  `quality.target_vmaf` key can default to it; `engine/search.py` re-exports it, #252) and
  the CRF range in `engine/search.py`; the HDR floor is computed from them in
  `engine/encoders.py:33`.
  Never restate a literal in a CLI option default or an API model.
- `Conf` is flat (`config.handbrake_encoder_preset`); there is no nested access
  path outside the pydantic model. `tools/process.apply_config(conf)` is the one
  entry-point hook — the CLI, `run_app` and each job executor call it.
- Tests are flat `tests/test_*.py` (there is no `tests/engine/` mirror), and they
  patch the LOOKUP home — `shrinkray.engine.pipeline.find_handbrake`,
  `shrinkray.engine.watch.find_handbrake` — never `tools.handbrake`.

## GOTCHAS

- A new eligibility gate must land in BOTH `engine/plan.py` and `engine/pipeline.py`:
  `plan` predicts, `encode` decides, and the two must agree (see `engine/AGENTS.md`).
- An unknown codec or height is probed, never excluded (`engine/plan.py:154-179`):
  an unreadable value must not silently drop a file.
- `--quality/--fixed` is a hidden no-op pair kept for one release (#202); there is
  no fixed-preset path left to restore.
- `engine/pipeline.py` (1656 L) and `server/app.py` (1355 L) are the two files
  where a small edit changes what every path measures — open an issue first.
