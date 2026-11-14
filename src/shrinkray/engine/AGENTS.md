# src/shrinkray/engine — the product

Why this file (distinct domain): this subtree IS the question the product
answers — is this file bigger than its quality requires, and does it carry bars?
`shrinkray.engine` is imported by every entry point, so a change here reaches all of them. Root AGENTS.md has the product rules;
`src/shrinkray/AGENTS.md` has the layering and the package conventions.

## MODULE MAP

| Question | Module | Notes |
|---|---|---|
| The CRF search | `search.py` | `bisect_crf`, `probe_crf`, `find_optimal_crf_and_skip`, `SkipEncode`, `DEFAULT_TARGET_VMAF` (re-exported from `config.py`, #252), `DEFAULT_CRF_RANGE = (14, 70), calibrated in #315` |
| The plan: gates, reuse | `plan.py` | `plan_quality`, `_lookup_plan`, `SOURCE_CODECS` whitelist, `is_1080p_class`, `MIN_ENCODE_SECONDS = 90.0` |
| Encode + batch loop | `pipeline.py` | `encode_with_quality`, `_encode_with_command`, `check_disk_space` |
| Black bars, one detector | `crop.py` | keyframe timeline pass + `sustained_runs` + `intersect_crop_rects`; DB-cached; `detect_crop_rect` is the fallback for a pass that learned nothing (issue #525) |
| Sample windows | `samples.py` | `sample_offsets`, `extract_samples`; the sampling defaults live in `config.py` (`DEFAULT_SAMPLE_EVERY = 240.0`, `DEFAULT_SAMPLE_SECONDS = 4.0`) and `samples.py` imports them |
| Encoder by bit depth | `encoders.py` | `encoder_for` (>8 bit: `HDR_ENCODER`), `DEFAULT_ENCODER` |
| Scan -> DB rows | `scan.py` | `scan_movies`, `process_movie`, `resolve_input_paths`; pool `min(8, cpu)` |
| Inbox watchdog | `watch.py` | `watch`, `_FsWaiter`, `decide`, `next_poll_interval`; `check_stability` (the is-it-complete witness: age + not sparse, one stat, no sleep, #479) and `wait_until_settled` (the CLI's bounded wait on it) |
| One encoder per contended dir | `lock.py` | `guard_encodes`, keyed on the movie's own directory in place; 60 s heartbeat, 5 min silence window; busy raises, never waits |
| Pause: the operator's, and for a manual encode | `pause.py` | `pause_reason`, `manual_encode` (CLI marker), `wait_while_paused` (jobs), `set_operator_paused` (the TUI's switch); markers carry the process start time, so a reused pid never pauses (issue #250) |

## HOW THE DECISION FLOWS

`plan_quality` and `encode_with_quality` run the same gates in the same order —
duration (`plan.py`), resolution class, size, codec whitelist —
then bar detection, then sample extraction, then the CRF search. The only
difference is the tail: `plan` returns a prediction, `encode` runs the full encode
and checks the artifact. Sizing permission is a skip, never an error:
`skipped_reason` is one of `too short`, `resolution too low`, `below min size`,
`source codec not a candidate`, `hdr not supported` (issue #518: the maintainer's
#340 decision — HDR is not supported until libvmaf's HDR models, #519; the gate keys
on the TRANSFER characteristic, so a bt2020nc SDR master and 10-bit SDR stay eligible),
`no size gain`, `target unreachable`, `output not smaller` (`pipeline.py`).

HDR is not an override but a skip (issue #518, the maintainer's #340 decision): a
source with an HDR transfer is refused at the gates (`is_hdr_file`, the code-fixed
transfer set in `tools/ffprobe.py`) before any sample is measured. `encoder_for(bit_depth)`
chooses by depth alone; the recipe's `hdr` field stays in the fingerprint (always False)
so stored SDR plans keep matching while historical `hdr=hable` plans never do.

## CONVENTIONS

- Probe preset and final preset are different facts (`config.py`: 5 and 3). The
  plan fingerprint keys on the PROBE preset only (`plan.py`, `db.py`): changing
  the final preset must reuse a stored plan, changing the probe preset must not.
- Plan reuse is the default for an explicit encode; `force_reprobe` (`--replan`,
  `pipeline.py`) bypasses the plan AND the probe cache. The watchdog always
  passes `True` so it stays independent of plan staleness.
- One bar measurement per file version: `detect_crop_summary_cached` caches in the
  `crops` table; `scan` only projects the cached flag and never runs the timeline
  pass (`scan.py`); the sampled fallback runs only for a pass that learned nothing (issue #525), stateless or not.
- A hung child is a FAILURE, not a cancel (`pipeline.py`): a stall is
  self-terminated and must not be retried by the watch loop.

## ANTI-PATTERNS

- Never hand cropdetect an unscaled limit (`crop.py`): it reads an absolute limit in
  the stream's native range, so the 8-bit 24 finds no black in 10-bit video (#637).
  Every pass takes the source's depth and uses `cropdetect_limit`.
- Never ship the "largest bars" combine (`crop.py`): it measured at keeping 0%
  of the frame. The shipped rule is the intersection of sustained readings.
- Never skip bar detection on a re-encode (`plan.py`): no bars is a normal
  outcome, not a skip. There is no crop-only path and no keep-bars flag.
- Never let a broken database or a failed artifact check stop an encode
  (`crop.py`, `pipeline.py`): compute uncached, and report the failed check
  rather than failing the artifact.
- Never write into an output dir without `guard_encodes` — and take the slot BEFORE
  the pending/debris filter (`pipeline.py`), or a live encode's output is
  classified as debris and deleted.
- Never add a fixed-preset encode path back (#202): the search is the only mode.
