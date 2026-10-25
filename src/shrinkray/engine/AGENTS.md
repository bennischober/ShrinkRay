# src/shrinkray/engine — the product

Why this file (score 12, distinct domain): this subtree IS the question the product
answers — is this file bigger than its quality requires, and does it carry bars? It holds
10 modules / 5333 L, and `shrinkray.engine` is named 260 times outside the subtree,
so a change here reaches every entry point. Root AGENTS.md has the product rules;
`src/shrinkray/AGENTS.md` has the layering and the package conventions.

## MODULE MAP

| Question | Module | Notes |
|---|---|---|
| The CRF search | `search.py` (820 L) | `bisect_crf` (`:197`), `probe_crf` (`:262`), `find_optimal_crf_and_skip` (`:592`), `SkipEncode` (`:796`), `DEFAULT_TARGET_VMAF` (re-exported from `config.py`, #252), `DEFAULT_CRF_RANGE = (14, 45)` (`:56`) |
| The plan: gates, HDR override, reuse | `plan.py` (736 L) | `plan_quality` (`:401`), `_lookup_plan` (`:91`), `SOURCE_CODECS` whitelist (`:55`), `is_1080p_class` (`:244`), `MIN_ENCODE_SECONDS = 90.0` (`:59`) |
| Encode + verify + batch loop | `pipeline.py` (1656 L) | `encode_with_quality` (`:1210`, 447 L), `_encode_with_command` (`:338`), `encode_movies` (`:758`), `check_disk_space` (`:207`) |
| Black bars, one detector | `crop.py` (656 L) | keyframe timeline pass + `sustained_runs` (`:128`) + `intersect_crop_rects` (`:182`); DB-cached; `detect_crop_rect` (`:515`) is the no-database fallback |
| Sample windows | `samples.py` (201 L) | `sample_offsets`, `extract_samples`, `DEFAULT_SAMPLE_EVERY = 600.0` (`:28`) |
| HDR/SDR encoder choice | `encoders.py` (51 L) | `encoder_for` (`:36`), `DEFAULT_ENCODER`/`HDR_ENCODER` (`:14-15`) |
| Scan -> DB rows | `scan.py` (619 L) | `scan_movies` (`:405`), `process_movie` (`:324`), `resolve_input_paths` (`:118`); pool `min(8, cpu)` (`:506`) |
| Inbox watchdog | `watch.py` (661 L) | `watch` (`:404`), `_FsWaiter` (`:208`), `decide` (`:150`), `next_poll_interval` (`:45`) |
| One encoder per output dir | `lock.py` (134 L) | `guard_encodes` (`:120`), 6 h stale window (`:27`); busy raises, never waits |
| Pause: the operator's, and for a manual encode | `pause.py` | `pause_reason`, `manual_encode` (CLI marker), `wait_while_paused` (jobs), `set_operator_paused` (the TUI's switch); markers carry the process start time, so a reused pid never pauses (issue #250) |

## HOW THE DECISION FLOWS

`plan_quality` and `encode_with_quality` run the same gates in the same order —
duration (`plan.py:499`), resolution class (`:511`), size (`:551`), codec whitelist
(`:578`) — then bar detection, then sample extraction, then the CRF search. The only
difference is the tail: `plan` returns a prediction, `encode` runs the full encode
and optionally verifies. Sizing permission is a skip, never an error:
`skipped_reason` is one of `too short`, `resolution too low`, `below min size`,
`source codec not a candidate`, `no size gain`, `target unreachable`,
`output not smaller` (`pipeline.py:1575`).

HDR is an override, not a branch: `encoder_for(bit_depth, hdr)` switches to `svt_av1_10bit`
when the source is 10-bit or HDR and `plan.py:490-497` replaces the target and CRF range
with the HDR pair before any measurement, so every stored fingerprint field carries the
OVERRIDDEN values.

## CONVENTIONS

- Probe preset and final preset are different facts (`config.py:31-32`: 5 and 3). The
  plan fingerprint keys on the PROBE preset only (`plan.py:333`, `db.py:296`): changing
  the final preset must reuse a stored plan, changing the probe preset must not.
- Plan reuse is the default for an explicit encode; `force_reprobe` (`--replan`,
  `pipeline.py:1226`) bypasses the plan AND the probe cache. The watchdog always
  passes `True` so it stays independent of plan staleness.
- One bar measurement per file version: `detect_crop_summary_cached` caches in the
  `crops` table; `scan` only projects the cached flag and never runs the timeline
  pass (`scan.py:340`); the sampled fallback keeps runs without a DB working.
- A hung child is a FAILURE, not a cancel (`pipeline.py:361-363`): a stall is
  self-terminated and must not be retried by the watch loop.

## ANTI-PATTERNS

- Never ship the "largest bars" combine (`crop.py:3-11`): it measured at keeping 0%
  of the frame. The shipped rule is the intersection of sustained readings.
- Never skip bar detection on a re-encode (`plan.py:478`): no bars is a normal
  outcome, not a skip. There is no crop-only path and no keep-bars flag.
- Never let a broken database or a failed verification stop an encode
  (`crop.py:495`, `pipeline.py:1596`): compute uncached, and report the verification
  as unmeasured rather than failing the artifact.
- Never write into an output dir without `guard_encodes` — and take the slot BEFORE
  the pending/debris filter (`pipeline.py:820`), or a live encode's output is
  classified as debris and deleted.
- Never add a fixed-preset encode path back (#202): the search is the only mode.
