# When ShrinkRay probes, and when it re-encodes

**Status: the contract.** Written 2026-10-24, corrected twice against the operator's own
prior art. Where the code disagrees with this document, the code is the defect.

Two corrections produced this file. The first: "HEVC and AV1 sources are candidates only
when they have black bars to remove" reads as *never re-encode a modern codec without bars*,
and that is wrong. The second: `--remove-bars` is not an opt-in feature that may be switched
off — bar removal is simply **part of every re-encode**.

## The rule

> **Every eligible file is probed. A re-encode happens only if the file would get smaller —
> and whenever a re-encode happens, bars are removed.**

Two sentences, two decisions:

1. **Whether to encode** is decided by size alone. Nothing else. Not codec, not bars.
2. **What the encode looks like** always includes bar removal. There is no encode without it.

There is no third case. No "crop-only" encode, no codec-gated probing, no way to ask
ShrinkRay to keep bars in a re-encoded file.

## Why bar removal is not optional

A re-encode is the only moment the picture can be corrected for free: the encoder is already
decoding and re-encoding every frame, so removing bars costs nothing extra. Leaving them in would
bake the same wasted area into a *smaller* file, which is strictly worse than the source — you pay
the generation loss and keep the padding.

The operator's framing: *"whenever a re-encode actually happens, we also wanna remove any bars if
existant so we have a clean file."* A `--remove-bars` that could be turned off would only ever
produce a worse output, so it is not a choice worth offering.

Bar detection is therefore always on for any file that will be encoded. It is a measurement inside
the pipeline, not a feature flag.

## Why there is no "off" case

A detector that cannot be trusted would need an escape hatch. The operator's judgement: it can be
trusted, and the one hard case is solved by the safe rule.

- **Simple films** — one aspect ratio throughout — are easy: the bars are constant and the safe
  crop is the same rectangle every sample reports.
- **Changing aspect ratio** is the one real case, and it has a decided answer: crop only the bars
  **common to every frame** (the union of picture areas). A film alternating 1.78:1 and 2.40:1 gets
  *no* crop, because the 1.78:1 sequences reach the frame edge. That is the safe direction: a
  missed crop costs a little space, a wrong crop costs picture you cannot get back.

So the hard case is already handled by the safe rule, and no film needs a manual override. The
`docs/black-bar-spike.md` measurements back this up: the naive "largest bars" rule kept **0%** of
the frame on two real films where the safe rule kept 85%.

## The pipeline, in order

Every gate is a cheap question asked before the expensive next step.

| # | Gate | Question | If it fails |
|---|---|---|---|
| 1 | Duration | At least 90 s? (`MIN_ENCODE_SECONDS`, `encode.py:167`) | skip — not a movie |
| 2 | Resolution | At least 1080p tall? (`min_source_height`, `quality.py:568`) | skip — too few pixels to save |
| 3 | Size | At least the configured size floor? (`min_source_size_gb`, `quality.py:590`) | skip — payoff below the probe cost |
| 4 | Codec age | H.264 or newer? (`MIN_SOURCE_CODEC`, `quality.py:176`, checked at `:615`) | skip — an older codec is not re-encoded |
| 5 | **Bar detection** | Which bars are common to every sampled frame? (`detect_crop_rect`, `engine/crop.py:109`, called at `quality.py:646`) | no crop; the encode proceeds uncropped |
| 6 | **CRF search** | Largest CRF that meets the VMAF target? (`find_optimal_crf_and_skip`, `engine/search.py:592`) | skip — target unreachable |
| 7 | **Size gate** | Would the result be smaller? (`engine/search.py:675`, and again at the chosen CRF `:721`) | skip — "would not shrink" |
| 8 | Encode | One full encode at the chosen CRF, **with the crop** | — |

Note gate 5 cannot fail in the sense of aborting: "no bars found" is a normal outcome and the
encode continues. That is the whole point of removing the codec branch — every codec takes the same
path through this table.

## Every scenario

| # | Source | Size | Bars | Outcome |
|---|---|---|---|---|
| A | H.264 | above floor | none | probed → encoded if smaller |
| B | H.264 | above floor | yes | probed **with crop** → encoded if smaller |
| C | H.264 | below floor | any | skipped at gate 3 |
| D | HEVC | above floor | yes | probed **with crop** → encoded if smaller |
| E | HEVC | above floor | none | probed → encoded if smaller |
| F | AV1 | above floor | any | probed → encoded if smaller *(see the open question)* |
| G | MPEG-2 / VC-1 | any | any | skipped at gate 4 |
| H | any | < 1080p | any | skipped at gate 2 |
| I | any | < 90 s | any | skipped at gate 1 |
| J | any | above floor | bars, would grow | skipped at gate 7 — **with the crop applied to the estimate** |
| K | any | above floor | bars, target unreachable | skipped at gate 6 |
| L | any | above floor | bars, shrinks | encoded **with the crop**, original retained |

Scenario E is the case the old rules forbade: a large HEVC file with no bars is probed and encoded.
Scenario J is why gate 5 runs before gate 7 — the estimate must reflect the crop, or a file that
would only shrink *after* cropping gets a false "won't shrink".

## What the search never does

- **Never re-encodes to remove bars.** A crop is a setting on the encode, never a reason for one.
  Bar removal cannot make a file worth encoding that size alone did not.
- **Never re-encodes a file that would grow.** Size is the only permission, checked at the **chosen**
  CRF, not only at the top of the range.
- **Never re-encodes an older codec.** H.264 is the floor; MPEG-2/VC-1/VC-1 are left alone.
- **Never skips an encode because quality could not be measured.** An unmeasured file is reported as
  unmeasured, never silently encoded and never silently dropped.
- **Never leaves bars in a re-encoded file.** There is no flag to keep them.

## The prior art this matches

`Find_CRF_for_folder_V4.ps1` walks the directory, filters by size (`-MinFileSizeGB`) and height
(`-MinHeight`), and for a modern codec optionally runs `cropdetect` — skipping the file only when the
bars are *not significant* (1% tolerance). Then it runs `ab-av1 crf-search` on **every remaining file
regardless of codec** and writes the found CRF to a status file. It never encodes anything.

The one place this document goes **beyond** the PS1 is bar removal: the PS1 only measured the crop
(`-DetectCrop`, a measurement aid), while ShrinkRay now applies it to every encode. That is a deliberate
difference, not a drift — the PS1 was a CRF finder, and ShrinkRay also does the encode.

## Where the code disagreed, and what closed it

Every row below was a real disagreement between this document and the code. All are now closed;
the table is kept as the record of what the change had to fix.

| # | Where | What it did | Closed by |
|---|---|---|---|
| 1 | `quality.py` codec gate | a modern codec with no bars returned "already efficient" before the search | **#128** — the branch is gone; every eligible file is probed |
| 2 | `quality.py` crop detection | bars were checked only when `--remove-bars` was on | **#128** — detection is unconditional |
| 3 | `__main__.py`, `app.py`, `quality.py`, `watch.py` | `remove_bars` defaulted to `False` in eight places | **#128** — the flag is gone from code, CLI, API and the plan fingerprint |
| 4 | `find_optimal_crf_and_skip` | the size gate probed only the top CRF, never the chosen one | **#129** — the chosen CRF is re-checked |
| 5 | `encode_with_quality` | `success=True` regardless of the actual size | **#130** — a grown encode is a failure |
| 6 | `__main__.py` encode report | printed `"N% larger"` as a success line | **#130** — the branch is removed |
| 7 | `PRODUCT.md`, `AGENTS.md`, `README.md` | "size alone never triggers a re-encode" for a modern codec | corrected in the same change as this file |
| 8 | `config.py` | `min_source_size_gb` defaulted to `0.0` | **#19** — the floor exists by default |

Item 3 also needed a schema migration: a plan stored while the flag existed was probed
*without* a crop when the flag was off, so reusing it would encode the film with its bars kept.
Migration 15 drops every stored plan rather than reuse one that describes an uncropped output.

## The one open question

**AV1 sources.** Re-encoding AV1 to AV1 buys no codec generation gain, so the only saving would come
from the crop and from a better encode. That may still be worth it, or it may not. Two options:

- **Treat AV1 like HEVC**: probed, encoded if smaller. Consistent, and the size gate protects you.
- **Exclude AV1**: an AV1 source is already the output format, so the only reason to touch it is bars,
  which the rule forbids as a reason. That makes AV1 a skip at gate 4 alongside MPEG-2.

The second reading is closer to "AV1 is the output format" — re-encoding the same codec is generation
loss for whatever small gain the crop gives. But the first is defensible and simpler. **Not decided;
record the choice here before implementing.**
