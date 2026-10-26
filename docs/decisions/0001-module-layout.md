**Type: decision record + multi-phase refactor.** The decision below is made; the phases are
independent pieces of work, each claimable on its own (open one issue per phase, or claim this one
for a single phase and leave a comment). Audited against main@08765f4.

## Decision

Three shallow subpackages plus a small flat foundation, with a one-directional import rule enforced in
the gate. Not one large file per area, and not many tiny files: every boundary exists because a rule in
AGENTS.md or PRODUCT.md needs a single home, and for no other reason.

```
src/shrinkray/
  __main__.py      CLI: parse arguments, call service/engine, render. No business logic.
  config.py        pure data; loading it has no side effects
  logger.py  db.py  telemetry.py  report.py  service.py

  tools/           the subprocess boundary: the only code that runs an external binary
    process.py       one runner: argument lists, timeout, stall watchdog, priority, termination
    ffprobe.py  vmaf.py  handbrake.py

  engine/          the product: "is this file bigger than its quality requires?"
    scan.py  crop.py  samples.py  search.py  encoders.py  plan.py  pipeline.py  lock.py  watch.py

  server/          the network surface: nothing else listens on a socket
    app.py  routes_*.py  security.py  jobs.py
```

**Import direction (downward only):**

```
__main__ | server          entry points; nothing imports them
service  | report          shared use-cases and rendering
engine                     product logic
tools                      external binaries
db | config | logger | telemetry   foundation
```

## Why this shape (each package maps to a rule)

- **`tools/` ← AGENTS.md "External binaries"** (argument lists, a timeout, verify the artifact, degrade
  gracefully). Today those rules are re-implemented per call site and already broken: four probe calls
  have no timeout (#62). `ffprobe` is invoked directly in six places across three modules
  (`media.py:25`, `encode.py:200`, `:698`, `quality.py:216`, `:488`, `:880`) although `media.run_ffprobe`
  exists. With one runner, a call without a timeout cannot be written.
- **`engine/` ← PRODUCT.md "the CLI is a first-class product: every capability is available without the
  resident process".** With the import rule this becomes structural: engine cannot import server, so no
  capability can exist only behind HTTP. Today `__main__.py` (901 lines) and `jobs.py` each carry their own
  orchestration (`_record_encodes` vs `execute_encode`), which is why fixes like #25 had to be applied twice.
- **`server/` ← AGENTS.md rule 1 "secure by construction"; "it's only on my LAN is not a security
  boundary".** One package owns the network surface, and one `security.py` owns path containment
  (`allow_roots`, `validate_path`, `_default_roots`, today in `app.py:78-120`, `:1238`) and later auth (#36).
  A new route cannot bypass what it has to import.
- **Why not deeper:** ~10k lines do not justify more nesting; three packages map to three rules.
  **Why not flat:** a flat layout cannot make the layering visible or enforceable for a stranger
  (AGENTS.md rule 5).

## Evidence of the current problems

- `quality.py` is 1,978 lines with at least six concerns that change for different reasons (VMAF
  measurement, crop detection, sampling, CRF search, plan cache, orchestration); about a dozen open issues
  edit it, which is a constant merge-conflict source with several agents working in parallel.
- Duplicated facts: `_source_snapshot` (`encode.py:681`, `quality.py:859`); HDR detection (`media.is_hdr`,
  `quality.is_hdr_file`); crop detection (`helpers.check_black_bars`, `quality.detect_crop_rect`, see #51);
  the "HDR → `x265_10bit`" rule written out at six sites (#21, #26).
- Inverted dependencies: `config.py` imports `encode` and `resources` (`:107`, `:113`), so loading config
  configures the stall watchdog and process priority. 40 function-level `from shrinkray...` imports exist to
  dodge import cycles (15 in `jobs.py`).
- `helpers.py` is a grab-bag (unit conversion, string generation, time parsing, black-bar detection,
  progress bars). `app.py` (1,305 lines) mixes routes, path security, SSE, static UI serving and config
  serialization.

## Rules (go into AGENTS.md "Project conventions")

1. **Imports only go downward** per the diagram, enforced by `import-linter` (a layers contract in
   `pyproject.toml`) as the **sixth gate command** (`uv run lint-imports`), added to CI alongside the
   existing five.
2. **No `helpers` / `utils` / `common` module.** A function lives with its concern or its only caller.
3. **One implementation per external fact:** only `tools/ffprobe.py` runs and parses ffprobe; only
   `engine/encoders.py` decides the encoder for HDR/SDR (#21's table); only `engine/crop.py` detects bars (#51).
4. **Split by reason-to-change, never by line count.** A long module with one concern stays one module.
5. **No function-level `shrinkray` imports** once the layering holds; any remaining one carries a comment
   saying why (e.g. an optional heavy dependency), never "to avoid a cycle".
6. **Tests mirror the layout** (`tests/engine/test_search.py`, `tests/tools/test_vmaf.py`), which keeps
   AGENTS.md's "one test file per source module" true; update its list of test files.

## Where today's code goes (guidance; confirm while moving)

| today | target |
|---|---|
| `quality._run`, `encode._terminate`, `configure_stall`, `stall_timeout_seconds`, `resources.py` (priority, spawn/session kwargs, `terminate_tree`) | `tools/process.py` |
| `media.py`, `encode.probe_duration_seconds`, both `_source_snapshot`, `quality.is_hdr_file`, `_output_bitrate` | `tools/ffprobe.py` (one snapshot, one HDR check) |
| `detect_vmaf_style`, `build_vmaf_command`, `parse_vmaf_log`, `measure_vmaf` | `tools/vmaf.py` |
| `find_handbrake`, `quality.build_encode_command`, HandBrake progress parsing | `tools/handbrake.py` |
| `_tool_versions` | composed from version functions in the `tools/` wrappers |
| `scanner.py` | `engine/scan.py` |
| `parse_crop_line`, `intersect_crop_rects`, `detect_crop_rect`, `helpers.check_black_bars` | `engine/crop.py` (one detector; #51 replaces the algorithm) |
| `sample_offsets`, `extract_samples` | `engine/samples.py` |
| `SampleProbe`, `bisect_crf`, `probe_crf`, `estimate_size_factor`, `find_optimal_crf`, `find_optimal_crf_and_skip` | `engine/search.py` |
| the six HDR-encoder ternaries, `service.py:117` | `engine/encoders.py` → `encoder_for(hdr)` (#21, #26) |
| `plan_quality`, `_plan_crop`, `_plan_probes`, `_lookup_plan` | `engine/plan.py` |
| `QualityResult`, `SkipEncode`, `Cancelled`, `_probe_movie`, `encode_with_quality`, `encode_with_quality`'s batch loop (`_record_encodes` lives in `__main__`), `EncodeResult`, `check_disk_space`, `durations_match` | `engine/pipeline.py` (split once more only if a second reason-to-change shows up) |
| `encode_lock.py` | `engine/lock.py` |
| `watch.py` | `engine/watch.py` (used by the CLI and the server) |
| `_record_encodes` and other logic in `__main__.py` | `service.py`, shared with `server/jobs.py` |
| `app.py` | `server/app.py` (wiring) + `routes_*.py` (FastAPI `APIRouter`s) + `security.py` |
| `jobs.py` | `server/jobs.py` |
| `helpers.py` | dissolved: progress bars → `__main__.py`; unit/time formatting → its callers or a named module |
| `dedupe.py`, `integrations.py` | deleted (#37, #43), not moved |

## Phases (in this order)

0. **Agree and document.** Update AGENTS.md (layout, rules 1–6, the sixth gate command, the test-file
   list) and the module list in PRODUCT.md ("Engine modules"). No code moves.
1. **Delete before moving:** #43 (Jellyfin) and #37 (dedupe) land first; relocating code that is about to be
   deleted is wasted work.
2. **`tools/`** — the only phase that fixes defects, so it ships as a behaviour change with tests: one runner
   in `tools/process.py`, every short call with a timeout (#62, if not already fixed), ffprobe in one place.
   Make `config.py` side-effect free in the same phase (entry points call `apply_resources` /
   `configure_stall` explicitly after loading).
3. **Split `quality.py` (and `encode.py`) into `engine/`, early**, as **pure moves**, one seam per PR:
   VMAF → crop → samples → search → plan → pipeline. Every week of delay adds conflicts with the dozen
   open quality issues; claim each PR via an issue so other agents see it.
4. **`server/`** — `app.py` into routers + `security.py`, ideally before #36 so auth lands in its final home.
5. **Merge the duplicates** (`_source_snapshot`, HDR check, crop detector, encoder rule) as separate small
   PRs: each one is a behaviour change and needs its own test.
6. **Turn on the `import-linter` contract** once the tree satisfies it; remove any transitional re-exports.

## Pitfalls for whoever implements this

- **`mock.patch` targets move with the code.** Tests patch by import path
  (`mock.patch("shrinkray.integrations.notify_after_encodes")`, `shrinkray.quality.measure_vmaf`, ...).
  A re-export shim in the old module does **not** redirect a patch: patch where the name is *looked up*.
  A pure-move PR updates the patch targets and nothing else in the tests.
- **Pure-move PRs must be reviewable as moves:** no renames of functions, no logic edits, no reformatting
  beyond `ruff format`; `git diff -M` / `--color-moved` should show moved blocks. Any fix found while moving
  becomes its own PR.
- **Transitional shims** (the old module re-exporting the moved names so in-flight branches still import)
  are allowed only until phase 6 and must say so in a comment.
- **`file:line` references in issues, README, AGENTS.md and docs go stale on every move.** Issue bodies
  revised on 2026-10-23 also name the symbol next to each line ref; the README/docs refs must be updated in the
  same PR that moves the code (AGENTS.md rule 6).
- Packaging needs no change (`packages = ["src/shrinkray"]` includes subpackages; mypy runs on `src`), but
  confirm the wheel still contains every module and that `python -m shrinkray` still works.

## Acceptance criteria (for the whole decision)

- The tree matches the layout above (or a recorded deviation with its reason).
- `uv run lint-imports` passes and is part of the gate in AGENTS.md and CI; a test or CI run demonstrates it
  fails on a deliberately wrong-direction import.
- No `subprocess` call outside `tools/`; no `helpers`/`utils`/`common` module; no uncommented
  function-level `shrinkray` import.
- Each external fact has exactly one implementation (snapshot, HDR check, crop detector, encoder choice).
- AGENTS.md, PRODUCT.md and README reflect the new layout; the full gate passes at every phase.

## Related

#62 (timeouts, first piece of `tools/`), #43 and #37 (deletions before moves), #51 (single crop detector),
#21 and #26 (encoder table / single encoder rule), #36 (auth lands in `server/security.py`), #30 and #45
(shared probe pipeline), #40 (typing/lint bar; annotate while moving is fine, but not inside a pure-move PR).

