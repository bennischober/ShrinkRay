# Black-bar detector spike: measured results (issue #51)

**Status: spike complete.** This file records what was measured, with every number
traceable to a run. It lands no production code: the output is the data plus a
proposed follow-up issue.

The harness is `tools/spike_crop_timeline.py` (a throwaway measurement tool, not
imported by `src/`). All runs used the image pinned by #34, so the ffmpeg is
`N-127957-g935f29eaae-20261023` with libvmaf v3.

## Corpus

The local `test_media/` folder holds three real files. They do **not** cover the
whole test set the issue asks for, and the gaps are listed at the end rather than
papered over.

| file | codec | resolution | transfer | duration |
|---|---|---|---|---|
| Media01 | HEVC | 3840x2160 (10-bit) | smpte2084 (HDR) | 5762.7 s |
| Media02 | H.264 | 1920x1080 | SDR | 7893.8 s |
| Media03 | H.264 | 1920x1080 | SDR | 7976.3 s |

## Q1: Cost of a keyframe-only pass

`cropdetect` with `-skip_frame nokey` and `reset=1`, measured over the first
120 s of each file, then scaled to an hour of runtime:

| file | 120 s pass | per hour of runtime |
|---|---|---|
| Media01 (4K HDR HEVC) | 6.9 s | **206 s** |
| Media02 (1080p H.264) | 3.1 s | **94 s** |
| Media03 (1080p H.264) | 3.2 s | **95 s** |

A 2-hour film therefore costs roughly **7 minutes** (4K HDR) or **3 minutes**
(1080p H.264) of wall time. That is cheap enough to run during `scan` for a
library of a few hundred films, but it is not free: on a 1000-film library at 4K it
is ~2 hours of pure detection. **Recommendation: run it lazily** (on `plan`, and
cached per file version), with `scan` optionally queueing it as a background job.
The decode is the cost, and it scales with resolution, not with runtime alone.

## Q2: Keyframe spacing

Median interval between readings, from the same runs:

| file | min | median | max | readings in 120 s |
|---|---|---|---|---|
| Media01 (4K HEVC) | 0.04 s | 1.00 s | 1.00 s | 137 |
| Media02 (1080p H.264) | 0.46 s | 1.00 s | 2.00 s | 124 |
| Media03 (1080p H.264) | 0.46 s | 1.00 s | 3.00 s | 130 |

A 1 s median means the run-length filter does **not** degenerate on this corpus:
there are ~120 readings per 2 minutes, so a 30 s segment is ~30 readings. The
worst case observed is a 3 s gap, not the 5-10 s the issue feared.

**But the filter must count seconds, not readings.** With a 1 s median and a 3 s
worst case, counting readings would make the threshold depend on the keyframe
interval, which varies by encoder settings. Counting seconds is invariant.
Recommendation: **a run-length filter in seconds**, with a floor of at least 3
readings so a file with sparse keyframes cannot satisfy it by accident.

## Q4: Run-length structure, and the cost of the naive combine

This is the measurement that decides the design. Readings over ~600 s, grouped into
runs of consecutive identical picture heights:

| file | runs | longest run |
|---|---|---|
| Media01 (4K HDR) | 1 | 599 s (100%) |
| Media02 (1080p) | 85 | 137 s (23%) |
| Media03 (1080p) | 153 | 84 s (14%) |

Then the two candidate combine rules, over the whole reading set:

| file | safe crop (union of picture areas) | naive "largest bars" crop |
|---|---|---|
| Media01 | 3840x2160, keeps 100% | 3840x2160, keeps 100% |
| Media02 | 1920x804, keeps **85%** | 2x28, keeps **0%** |
| Media03 | 1920x800, keeps **85%** | 14x8, keeps **0%** |

The naive rule destroys the whole picture on both SDR films, because a single
reading with a dark or logo-heavy frame reports enormous bars. The safe rule crops
only the bars common to every reading: 15% of the frame, which is exactly the
letterbox, and nothing more.

**This is the decisive result of the spike.** The safe rule is the only one that
can be applied to a file the operator cannot re-acquire, and it is the rule the
issue hypothesised. The naive combine must never be implemented.

## Q5: HDR / 10-bit black level

Media01 is HDR (PQ / smpte2084, 10-bit) and reads as **full frame at every
limit** tried:

| cropdetect limit | most common reading |
|---|---|
| 8 | 3840x2160 (100%) |
| 16 | 3840x2160 (100%) |
| 24 (the current constant) | 3840x2160 (100%) |
| 32 | 3840x2160 (100%) |
| 48 | 3840x2160 (100%) |

So this particular HDR file has no bars to find at any limit, and the run of 599 s
at full frame is consistent with that. It does **not** prove that the current limit
of 24 is correct for PQ sources generally: a file that genuinely has bars would be
needed to test that, and this corpus has none. **The issue's Q5 is therefore
unanswered, not answered** — one HDR film with no bars cannot calibrate a limit.

## Q6: Dark segments

This is the blind spot, and the corpus quantifies it. Media02 and Media03 keep a
constant bar height (804px and 800px of a 1080p frame) but report a *different
picture width* on 18% and 26% of readings, sometimes as narrow as 376px and 698px.
Those narrow readings are dark scenes being read as side bars, not real aspect-ratio
changes.

Under the safe rule the cost is bounded and correct: the union keeps the full width,
so the dark scenes cost the *crop opportunity*, not picture. Under the naive rule they
cost the picture — see the table above, where one such reading destroys the frame.

**Quantified:** on Media02 the safe crop keeps 85% of the frame and the 18% of
readings that report side bars contribute nothing to the crop. A wrong crop is
avoided; the bar-removal saving on those segments is forfeited. That is the correct
trade on a file that cannot be re-acquired.

## Q7: Burned-in subtitles in the bar area

**Not measured.** The corpus has no film with burned-in subtitles in the bars, so this
question cannot be answered from these three files. What can be said from the code is
that `cropdetect` treats a non-black pixel as picture, so a subtitle in the bar blocks
the crop — the safe direction. That is an argument from the filter's definition, not a
measurement, and is labelled as such.

## Q3: Credits

**Not measured**, for the same reason: none of the three films has a credits sequence
whose aspect ratio differs from the body. The tail-exclusion question needs a film where
it does.

## Decision: the scan flag and the encode rectangle (issue #131)

There are two bar measurements today, and they disagree:

| | Where | What it produces | Sampling |
|---|---|---|---|
| scan flag | `engine/crop.py:193` (`has_black_bars`), stored in `files.crop` | 1/0 | one `cropdetect` run over the file |
| encode rectangle | `engine/crop.py:110` (`detect_crop_rect`) | a `(top, bottom, left, right)` rect | `crop_sample_count` windows, intersected |

**Decision: they stay two measurements, and the flag is a cheap pre-filter — not a second
opinion.** The reason is the cost measurement above: a keyframe-only pass costs ~3 min/hour of
runtime, which is affordable per film at plan/encode time but *not* across a whole library
during `scan`. The scan exists to answer "what is in this collection" cheaply; making it run the
full timeline pass for every file would turn a scan of a few thousand titles into hours of
`cropdetect` work before anything is planned.

So the two are not redundant measurements of the same thing, and they are not supposed to agree:

- The **flag** answers "is this file worth looking at?" It is allowed to be coarse, and a false
  positive costs nothing: the encode measures the rectangle anyway.
- The **rectangle** answers "what do I crop?" It is the only value that ever reaches an encode,
  and it is always measured fresh from sampled windows under the safe union rule.

A file flagged "has bars" that then produces no crop is therefore **not a contradiction** — it is
the flag doing its cheap job and the rectangle doing its careful one. What was missing was not
agreement, it was that the operator could not see this; that is the report work below.

**What this decision rejects, and why.** The alternative — one detector for both, cached per file
version — is the follow-up issue the spike proposes above, and it is the right end state. It is not
this change because it needs a `crops` table, a migration, and a keyframe-timeline pass in `scan`;
that is a feature, and #131 asked for a decision first. Unifying later does not invalidate this
decision: when the one detector lands, the flag becomes its cached summary rather than a separate
run.

**Report requirement (the second acceptance criterion).** Until the unification lands, the report
must show both values and explain a disagreement, so "flagged but no crop" is visible rather than
mysterious. That is tracked as the reconcile-the-report work in the follow-up issue.

## What this spike establishes, and what it does not

**Established by measurement:**

1. A keyframe-only pass costs ~3 min/hour (1080p H.264) to ~3.5 min/hour (4K HDR
   HEVC). Cheap enough to run lazily per film, expensive enough that a library-wide
   `scan` should not do it synchronously.
2. Keyframe intervals are ~1 s median, 3 s worst case on this corpus, so a
   run-length filter in **seconds** is well-founded and must not count readings.
3. **The safe "bars common to all frames" rule keeps 85% of the frame where the
   naive "largest bars" rule keeps 0%.** The naive rule must never ship.
4. Dark segments are the dominant noise source, at 18-26% of readings on the two SDR
   films, and the safe rule handles them correctly by giving up the crop opportunity
   instead of the picture.
5. One HDR film with no bars reads as full frame at every limit from 8 to 48.

**Not established (needs more media):**

- The HDR black level (Q5): one film with no bars cannot calibrate a limit.
- Burned-in subtitles in the bars (Q7): no such film here.
- Credits (Q3): no film here whose credits differ in aspect ratio.
- A genuinely variable-aspect-ratio film: **none of the three changes aspect ratio.**
  The runs on Media02/03 are dark-scene noise, not real segments. So the
  "IMAX alternating with scope" case the issue is really about is untested.
- 4K HEVC with long keyframe intervals: Media01's intervals are 1 s.

## Proposed follow-up issue

A single static crop per encode, computed as the **union of picture areas over
all readings** (the bars common to every frame), from a keyframe-only
`cropdetect` pass with `reset=1`:

- Run the pass lazily on `plan`, cached in the database keyed by size + mtime
  (a new `crops` table, schema migration), so `scan` stays cheap.
- Store the summary: the safe crop, the distinct sustained ratios with their share
  of runtime, and the reading count — the report shows "variable aspect ratio:
  2.20:1 for 51%, 1.78:1 for 49% — cropped only common bars".
- Replace both `detect_crop_rect` and the scan's `check_black_bars` with this one
  detector, so the two rules cannot disagree.
- Filter noise by a **run-length in seconds** with a floor of 3 readings; classify
  by share of runtime for the report only, never to decide the crop.
- Acceptance tests on synthetic timelines (a real segment, a dark segment, an isolated
  logo, a short 4:3 insert); real files only in the smoke tests.

The naive combine stays out of the design entirely: it is measured above to destroy
the picture.
