# Manual VMAF parity run on real content

Status: **the manual leg of issue #306**, the counterpart of the opt-in test
`tests/test_vmaf_parity.py`. That test proves the measurement recipe on synthetic
clips generated at test time; this procedure runs the same comparison on real
content from `test_media/`, which is the only place grain, real letterboxing, a real
HDR master and a real open-GOP source show up. Issue #312's acceptance run uses it.

## Why both exist

The synthetic pairs are exact: `testsrc2` re-encoded with libx264, so a delta of
0.0000 proves the two measurements agree *on that clip*. They cannot prove the
recipe survives real content, and they structurally cannot catch a defect in the
HandBrake encode path, because every synthetic SDR pair is encoded with
ffmpeg/libx264 (which preserves colour tags and frame counts) while production
encodes with HandBrakeCLI (which does neither - see #258 and #408).

That is not hypothetical. The first real run of this procedure measured a pair at VMAF
17.71 while the synthetic pairs all read 0.0000 delta, because the delta is the
*agreement* between two tools, not the correctness of either. That pair was a harness
artifact (a `-ss` copy-cut reference against a `--start-at` HandBrake segment, which cover
different content windows) and its frame counts differed - 729 against 719 (issue #408). The
script now counts frames before scoring and refuses a pair whose counts do not match; a
production-shaped pair is aligned.

The comparison is always the **measurement**, not the search: for the same
(reference, distorted) pair, `measure_vmaf(distorted, reference)` against
`ab-av1 vmaf --reference <ref> --distorted <dis>`. Comparing chosen CRFs would not
be meaningful, because ab-av1 encodes with ffmpeg's libsvtav1 and ShrinkRay with
HandBrakeCLI.

## Tolerance

`|ShrinkRay - ab-av1| <= 0.1` VMAF per pair (issue #306's proposal). One exception,
documented rather than waived: an HDR pair measured through ShrinkRay's tone-map is
**reported, not held** (the deviation in `docs/encode-decision.md`, measured at
~2.9 VMAF on a real HDR10 pair by #340). The same pair with tone-mapping off is
held, and that is what proves the rest of the pipeline matches.

## What you need

| Tool | Version | Why |
|---|---|---|
| ffmpeg | with `libvmaf` and `zscale` | the measurement itself; `ffmpeg -hide_banner -h filter=libvmaf` must list the filter |
| ab-av1 | **v0.11.7** | the reference implementation; the version is part of the claim, because `--verify` arrived in v0.11.7 |
| HandBrakeCLI | 1.6 or newer | only for the probe-sample pair (the encode side) |

ab-av1 is a **development tool only**: nothing under `src/` invokes it and it is not
a dependency. Install it from its releases page
(`https://github.com/alexheretic/ab-av1/releases/tag/v0.11.7`); the Linux asset is a
`.tar.zst`, so `zstd -d` is needed to unpack it.

The repository's own smoke image has the right ffmpeg and HandBrake:

```sh
docker run --rm --entrypoint sh shrinkray:test -c \
  'ffmpeg -version | head -1; HandBrakeCLI --version'
```

`ab-av1` is not in that image; mount it in, as the examples below do.

## The pairs to cover

`test_media/` holds whatever the operator put there. Cover what your library has,
and say in the report which of these you actually ran:

| Pair | What it exercises | How to make the distorted side |
|---|---|---|
| SDR 1080p, no bars | the baseline case | any HandBrake encode of a source clip |
| SDR with black bars | the reference crop, and the cropped dimensions driving the model | `shrinkray encode` a letterboxed file, measure its output against the source with `--crop` |
| SDR 10-bit | the 10-bit analysis pixel format | `--pix_fmt yuv420p10le` on the distorter |
| 2160p | the 4K model and ab-av1's auto scale | `shrinkray encode` a 4K file (or a 4K `--vf` crop of one) |
| Probe sample | the stream copy + HandBrake probe path | the probe-sample recipe below |
| Full-file verification | the frame alignment between a source with audio and a HandBrake output | `shrinkray encode` a real file, then measure its output against the source (step 7) |
| HDR10 | the tone-map deviation, and the PQ-direct parity underneath | `shrinkray encode` an HDR10 file |

No filename from `test_media/` may appear in code, docs or a test (AGENTS.md); refer
to files by role ("the 4K HDR episode"), never by name.

## Procedure

Run from the repository root. The script appends every result to
`test_media/scratch/vmaf_parity.md`, so a long run keeps what finished and a
Ctrl+C loses nothing already measured.

### 1. A plain SDR pair

Pick a source and an encoded version of it (any encode whose output is smaller -
`shrinkray encode` output, or an existing file you know is derived from the source):

```sh
uv run scripts/vmaf_parity.py --pair "/path/to/source.mkv" "/path/to/encoded.mkv" \
  --label "SDR 1080p"
```

### 2. A pair with bars

Pass the same rectangle the encoder removed, as `top,bottom,left,right`. ShrinkRay
crops the reference; ab-av1 gets the identical `--reference-vfilter crop=...`:

```sh
uv run scripts/vmaf_parity.py --pair "/path/to/source.mkv" "/path/to/encoded.mkv" \
  --label "SDR with bars" --crop 140,140,0,0
```

Read the rectangle off the encode's own log line
(`Removing stable black bars of '<name>': top N, bottom N, left N, right N`) or from
`shrinkray plan`'s output. If the numbers do not match what the encoder actually
removed, the pair measures a geometry mismatch, not a quality difference.

### 3. A 10-bit pair

The same command as step 1 on a 10-bit source. The script derives the analysis pixel
format from the distorted file's own bit depth, so a 10-bit source is compared at
10-bit on both sides.

### 4. A 4K pair

The same command on a 2160p source. Above 2560x1440 both sides switch to
`vmaf_4k_v0.6.1`, and neither scales (both are at or above their 4K auto-scale
threshold). A 4K scope film cropped below 1440 lines stays on the 1080p model at
native size, exactly as ab-av1 scores it - pass `--crop` so both sides agree.

**Check the recipe line, not just the delta.** The script derives the model and the
analysis pixel format from the pair itself, and it prints them:

    recipe: vmaf=2;model=vmaf_4k_v0.6.1.json;scale=none;fmt=yuv420p10le;hdr=no

Two rules decide that line, and getting either wrong produces a plausible number that
is not a parity result:

- **Model and scale follow the DISTORTED file's dimensions** (post-crop), so a 2160p pair
  uses `vmaf_4k_v0.6.1`. This is ab-av1's rule (#309) and what
  `engine/pipeline.py` passes. Until #421 the harness called `vmaf_recipe()` with no
  dimensions, so every pair - including 4K ones - was scored with the 1080p model, and
  nothing noticed because every previously measured pair was 1080p.
- **The analysis pixel format follows the SOURCE's bit depth**, because that is what
  `engine/pipeline.py` passes as `source_bit_depth`. A 10-bit source whose encode came
  out 8-bit is scored at 10-bit on both sides, as ab-av1 does. Scoring it at 8-bit
  measured **97.9706 vs ab-av1's 98.1441 (delta 0.1736)** - outside the 0.1 tolerance -
  on the 2160p pair.

If the printed recipe does not match ab-av1's own `format=` and `model=` on the same
pair, the run is measuring something else. Compare the two command lines (the script
prints ab-av1's), do not read the delta.

**A stream-copy window of this open-GOP remux cannot produce a measurable window
pair.** Measured on the library's 2160p HEVC remux (issue #432): the 10-minute window
cut with an input seek to 600 s - the seek point lands just before the keyframe at
600.125 s, so the copy starts on that keyframe (first key at 0.125 s internally) - and
its HandBrake encode dropped frames at the head: the first run's log says `sync: video
time went backwards 83 ms, dropped 2 frames`, and the frame counts measure 14,411 /
14,408. The first run's two numbers are different measures (the log names the frames
sync discarded; the count delta also carries what the muxer absorbed); the third
frame is unexplained, and the log's own drop count varies between the runs (2 then 3)
- also unexplained. A re-measure re-cut the window "onto its first keyframe" (`-ss 0.125`
before `-i`) and produced the SAME window - identical duration (601.351 s), identical
head (keyframe 0.125, then 0.167/0.209/0.251), identical mid keyframe (299.466) and
tail (601.309), equal size - because ffmpeg's copy mode snaps packets to the keyframe
span either way; its encode again dropped frames (14,411 / 14,408; that run's log
named 3). So the misalignment reproduced on an identical window, and the proposed
keyframe recipe was NOT actually tested as a different cut shape - copy mode made
both commands the same window. The working hypothesis, unmeasured: the damage lives
in the GOP structure after that first keyframe - on an open-GOP source the B-frames
after it reference frames before it, those references dangle at a window's head, and
HandBrake's sync drops the dangling frames while the copy keeps them. Both runs'
pairs were refused by the parity script's frame-count gate (#408/#419), which is that
gate working, not a parity defect; this source is declared unmeasurable for the window
pair on that evidence, with the counts recorded.
The encode path did not fail on this window: it encoded, chose CRF 33 by probe against
this run's target 95.5 (the HDR override: 96.0 default minus the 0.5 calibration, issue
#103), saved 94.13%, and replaced normally. (The recorded probe VMAF 95.63 is
reported as measured, not as an aligned-pair score: the probe search measures probe
encodes of this window's samples, the same shape whose alignment this section shows is
not guaranteed on this source, and whether the probe path's alignment is affected here
is unmeasured.) An operator who wants a whole-file VMAF number on this class runs the
full file (not a window) - believed, not verified here: the pipeline's `--verify`
measurement has no window head to dangle on a whole file, but whether it tolerates
this source's structure is untested; treat it as the candidate, not a documented
working route.

### 5. An HDR10 pair

```sh
uv run scripts/vmaf_parity.py --pair "/path/to/hdr_source.mkv" "/path/to/hdr_encoded.mkv" \
  --label "HDR10" --hdr true
```

This measures the pair twice:

- **tone-mapped** (ShrinkRay's shipped path) - REPORTED against ab-av1's PQ-direct
  score. The gap is the documented deviation, expected around 3 VMAF on real content
  (#340 measured ~2.9); a much larger gap means something else broke.
- **PQ-direct** (tone-mapping off) - HELD to the tolerance. This is the like-for-like
  comparison and it is what #312's HDR criterion asks for.

### 6. The probe-sample pair

This is the one pair that exercises the real probe pipeline: a stream-copied window,
encoded by HandBrake at the probe preset, exactly as `engine/samples.extract_samples`
and `probe_crf` produce it.

```sh
# 1. a stream-copied window, video-only (this is what extract_samples does)
ffmpeg -ss 600 -i "/path/to/source.mkv" -t 10 -map 0:v:0 -c:v copy -an \
  -avoid_negative_ts make_zero /tmp/sample.mkv

# 2. HandBrake at the probe preset - the shape probe_crf builds
HandBrakeCLI -i /tmp/sample.mkv -o /tmp/sample_enc.mkv \
  -e svt_av1 --encoder-preset 5 --quality 28 -f av_mkv \
  --crop 0:0:0:0 --all-audio --aencoder copy

# 3. measure both ways
uv run scripts/vmaf_parity.py --pair /tmp/sample.mkv /tmp/sample_enc.mkv \
  --label "probe sample"
```

### 7. The full-file pair

The verification path compares a HandBrake output against the source **with audio**, so
this is the pair that exercises it. **Always print the frame counts** before trusting a
number - the script now does this for every pair and refuses to score one whose counts
differ:

```sh
for f in ref.mkv dis.mkv; do
  printf '%s frames: ' "$f"
  ffprobe -v error -select_streams v:0 -count_frames \
    -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$f"
  printf '%s start:  ' "$f"
  ffprobe -v error -select_streams v:0 -show_entries stream=start_time \
    -of default=nw=1:nk=1 "$f"
done
```

A frame-count mismatch means the two sides are not comparing the same frames, and the
score is meaningless whatever the delta says. `scripts/vmaf_parity.py` reports such a pair
as **unmeasured** and fails the run instead of printing a number for it.

**Build this pair from whole files.** Two kinds of pair reproducibly produce a mismatch.
One is a reference cut with `-ss` (whose keyframe-accurate copy cut starts on an earlier
keyframe, so it holds more content than the window asked for) measured against a
HandBrake segment made with `--start-at`/`--stop-at` (which trims to the exact frame).
The other is a stream-copy window of the open-GOP remux pair 4 measures, against its own
whole-window HandBrake encode: the window's head lost frames in both measured runs (the
same shape step 6's probe pair builds in production, whose alignment on this source is
unmeasured). Neither shape is the shape the verification path compares -
`build_encode_command` has no trim options, and the verification compares a whole output
against a whole source. On the first shape a mismatch is a harness artifact; on the
second it is the probe measurement disagreeing with its own reference - and production
catches none of it: `measure_vmaf` counts no frames, so the search keeps the misaligned
probe score (the 95.63 above) and the CRF choice rests on it. Either way the score must
never be reported as a parity result (issues #408 and #432).

## Reporting the run

Post a table on the issue (or the PR) with one row per pair, both scores, the delta,
and the verdict. Include the versions you ran:

```sh
ffmpeg -version | head -1
ab-av1 --version
HandBrakeCLI --version | head -1
```

The script writes exactly that table to `test_media/scratch/vmaf_parity.md`, which is
gitignored - copy the rows into the issue, do not commit the file.

A pair outside tolerance is a finding, not a number to wave through: either it gets a
fix, or it becomes a documented deviation in `docs/encode-decision.md` with its reason.

### Evidence from the first real run (2026-10-26)

Run on the untagged 1080p h264 WEBDL in `test_media/`, in the pinned image with ab-av1
v0.11.7:

| Pair | ShrinkRay | ab-av1 | delta | frames (ref / dis) |
|---|---|---|---|---|
| zero-based window, video-only | 92.6662 | 92.6662 | 0.0000 | 729 / 729 |
| window with audio, HandBrake segment | **17.7090** | 17.7090 | 0.0000 | **729 / 719** |

The second row is a perfect 0.0000 delta on a meaningless score: 729 frames measured
against 719 compare different content, so neither number describes the encode. It is kept
here as the reason the frame-count gate exists, **not** as a parity result - the pair was
built with a `-ss` copy-cut reference and a `--start-at` HandBrake segment, a shape
production never produces (issue #408). The script now reports such a pair as unmeasured
and fails the run.

A production-shaped full-file pair, measured the same way on the same source, is aligned
and scores normally: 2882 / 2882 frames, VMAF 96.10.

## What this run cannot tell you

- **Whether the two sides compare the same frames.** This is the caveat that matters
  most, and the script now checks it before scoring: a pair whose frame counts differ is
  reported as unmeasured and fails the run. On real content a HandBrake output with 719
  frames against a 729-frame reference scored **17.71**, and both tools reported the
  same wrong number. Parity between the tools says they measure the same thing, never
  that they measure the right thing (issue #408).
- **Whether the encode itself is faithful.** It measures a file you already have
  against its source; parity says the two tools measure the same thing, not that the
  artifact is right. Colour is settled, not a live question: #258's acceptance run
  found no conversion on the way out (the re-tagging of unset components to BT.709 on 1080p-class 8-bit SDR sources
  is signalling only - `docs/encode-settings.md`); a low score on untagged
  content is therefore not evidence of colour damage, but the real-title low
  readings that opened #258 are tracked as issue #443 and remain unexplained.
- **Whether the tone-map is the best available chain.** #340 owns that, and this
  procedure only reports its footprint.
- **Whether the CRF search picks the right CRF.** That is the search, tracked outside
  the parity milestone (#313-#317).
