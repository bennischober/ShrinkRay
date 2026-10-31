# Plan-result reuse ("quicksave")

An encode run skips probing when a valid plan exists — and equally skips
movies the plan already rejected (skip decisions are as valuable as the
CRF).

## Why this is sound

`plan_quality()` and `encode_with_quality()` (src/shrinkray/engine/plan.py and src/shrinkray/engine/pipeline.py)
run the EXACT same probing pipeline: `_probe_movie()` ->
`find_optimal_crf_and_skip()` — same samples, same bisect, same VMAF
measurement. The plan's `chosen_crf` is precisely what the encode would
recompute. Reuse = caching a deterministic computation, not an
approximation.

## Contract

1. **Write path** — every successful `plan_quality()` run (job worker AND
   CLI `plan` command) stores the plan via `service.record_plan()`. Skip
   outcomes are stored too (`skipped_reason` + chosen_crf/achieved): they
   short-circuit encodes later.
2. **Read path** — `encode_with_quality()` looks up a valid plan BEFORE
   probing. Valid hit:
   - skip decision => return the skip `QualityResult` immediately (no
     probing, no encode);
   - encode decision => proceed straight to the final encode with the
     plan's `chosen_crf` and `crop`; the plan's `achieved_vmaf` carries
     over as the sample-based measurement.
3. **Fingerprint validation** — reuse ONLY when ALL match; ANY mismatch
   => normal probing path (fail-safe to re-probe, never guess):
   input mtime+size, encoder, encoder_preset, target_vmaf, crf_range,
   sample_count, sample_seconds, crop_sample_count, vmaf_statistic,
   vmaf_recipe. HDR: `encode_with_quality()` overrides target_vmaf/crf_range
   for HDR before the lookup — the lookup derives from the OVERRIDDEN
   values, so HDR consistency falls out of the same rule.
   Deliberately EXCLUDED: `encoder_threads` (does not change output
   bytes). `vmaf_recipe` is the measurement's crop-independent half
   (`VmafRecipe.fingerprint`, issue #308): the recipe version, the analysis
   pixel format and the tone-map. The plan lookup runs BEFORE bar detection,
   so it cannot know the post-crop dimensions the model and scale follow —
   those are already pinned by the file's identity plus `crop_sample_count`
   (a different file or a different crop window count re-measures the crop).
   A recipe bump therefore re-probes every file; the probe cache carries the
   full recipe (`VmafRecipe.identifier`), so its rows miss too.
4. **Provenance** — a plan-driven encode records `crf_source='plan'` +
   `plan_created_at` (in the QualityResult AND the encodes row). A
   decision's origin stays visible: "probed now" vs "trusted plan from
   <date>". The tool's credibility rests on measured decisions.
5. **Escape hatch** — `force_reprobe: bool = False` on
   `encode_with_quality()` (CLI: `--replan`) bypasses lookup entirely.
6. **Scope guard** — watch mode stays as-is (no plan lookup): inbox
   automation must not silently depend on plan staleness. Only explicit
   encode runs (CLI quality mode + job worker) consult plans.

## Schema (v9)

- table `plans`: one row per file (upsert replaces the previous plan;
  plan history stays in the job payloads). Columns: file_id (FK files),
  chosen_crf, achieved_vmaf, crop (nullable), probes (JSON),
  skipped_reason, estimated_size (the size prediction; NULL for skips),
  input_mtime, input_size, encoder, encoder_preset,
  target_vmaf, crf_low, crf_high, sample_count, sample_seconds,
  crop_sample_count, vmaf_statistic, created_at.
- `encodes` gains `crf_source` (nullable TEXT: 'plan' | 'probe') and
  `plan_created_at` (nullable TEXT).

## Pitfalls (from the review)

- The crop rect is always applied now: bar removal is part of every
  re-encode (issue #128), so there is no setting under which a stored plan's
  `crop` would be wrong. Migration 15 dropped every plan stored while the
  `remove_bars` flag existed, because those were probed without a crop when
  the flag was off - reusing one would have kept the bars.
- Plans for soft-missing files are ignored (mtime+size guard covers a
  rescanned file; no lookup when the file no longer exists).
- A database the lookup cannot open or read (damaged, foreign, out of file
  handles) is logged and treated as no plan: the encode probes (issue #507).
- `achieved_vmaf` from a plan is SAMPLE-based; `--verify` (full-file) is
  unaffected and still runs when requested.
- Size estimates stay the plan's job; the encode path never recomputes
  them.

## Honest payoff

Probing is usually the minor share of a quality run — this saves tens of
minutes per movie (the plan's ~6 min on Lilyhammer), not hours. The
second encode of an unchanged file with unchanged settings performs zero
probe encodes.

The stored plan is also the detail page's prediction display:
GET /api/movie carries the plan (with a file-level staleness flag), so
the page shows what a future encode would do — predictions labeled as
predictions, measurements as measurements.
