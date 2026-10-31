<div align="center">

<img src="docs/shrinkray-icons/svg/shrinkray-mark.svg" alt="ShrinkRay logomark" width="96" />

# ShrinkRay

**Scan your collection — then shrink it without losing quality.**

Answers two questions per file: *is this file bigger than its quality requires?*
and *does it carry baked-in black bars?* Where a re-encode is warranted, it
re-encodes with Netflix-style, quality-measured encoding — the smallest file that
is still perceptually transparent. Where it is not, it leaves the file alone and
remembers why.

Built to run unattended on a server against a whole collection — fire and forget —
with a watchdog for new arrivals and a web dashboard for status. The CLI is a first-class
product: every capability works without the resident process, and it can be pointed at a
single file, a folder, or a glob. The target home is an **Unraid Community App**: the
dashboard to watch it, the container's console to configure it (see [Direction](#direction)).

**It shrinks files. It does not manage a library** — no renaming, moving,
organizing, matching or deduplicating. That belongs to the tooling that already
put the collection together.

[![CI](https://github.com/bennischober/ShrinkRay/actions/workflows/ci.yml/badge.svg)](https://github.com/bennischober/ShrinkRay/actions/workflows/ci.yml)
[![CodeQL](https://github.com/bennischober/ShrinkRay/actions/workflows/codeql-analysis.yml/badge.svg)](https://github.com/bennischober/ShrinkRay/actions/workflows/codeql-analysis.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://docs.astral.sh/uv/)
[![GitHub release](https://img.shields.io/github/v/release/bennischober/ShrinkRay)](https://github.com/bennischober/ShrinkRay/releases)

</div>

---

## Two ways to run it

**One-shot, from the CLI.** Point it at work and it does that work, then exits:

```bash
shrinkray encode 'media/movies/Some Film (2020).mkv'   # one file
shrinkray encode 'media/movies/'                        # a whole folder
shrinkray encode 'media/movies/*.mkv'                   # a glob
```

No database, no config, no resident process — straight to the CRF search, then the file
is re-encoded in place. The original is renamed aside and kept; nothing is deleted.

**Fire and forget, in a container.** Point it at the mounted library once and leave it.
It scans the collection into its own sqlite database, detects black bars for every file,
runs the CRF search for the ones that pass the gates, works through the backlog on its
own, and keeps a watchdog on the folder so new and changed files are picked up within
seconds. The dashboard is its status window — what is running, what is planned, what is
left, what was reclaimed — not a control panel. Configuration and control belong to the
terminal: `shrinkray setup` (see [`setup`](#setup--configure-pause-and-resume)).

> [!IMPORTANT]
> **Both shapes run today.** `encode` takes a file, a folder or a glob, and omitting the
> output directory re-encodes **in place**: the verified output takes the source's own path
> and the original is renamed aside and kept. No database is created unless you pass `--db`.
> Naming an output directory instead writes a second copy there.

## What does it do?

Every run starts by **scanning** the files it was given: duration, codecs, resolution,
FPS, audio languages, HDR detection and a coarse black-bar check. With a database
(`--db`, or the resident app) the scan is cached, so an unchanged file is not probed again.
There is no separate scan command: `encode`, `plan` and the resident app scan as they go.

**`encode`** re-encodes your library with HandBrake to save disk space. Every
encode is quality-measured — there is no fixed-preset mode:

> **Fix quality, not bitrate.** For every movie, short samples are probed at
> different CRF values, each probe is scored with [VMAF](https://github.com/Netflix/vmaf)
> (Netflix's Emmy-winning perceptual quality metric), and a search finds
> the *largest* CRF (or one step below it) that still meets the required VMAF — then the movie is encoded
> once with exactly that setting.

Nothing is guessed: every decision is measured, and movies that cannot profit
(already efficient sources) are skipped — not wasted. With a database (`--db`, or
the resident app) every decision is also recorded, so it is not measured twice.

> [!NOTE]
> **HDR10 sources are processed, not guaranteed** (issue
> [#358](https://github.com/bennischober/ShrinkRay/issues/358)). They are
> neither blocked nor validated: by default (`codec.hdr`) a file whose colour
> space is bt2020nc or whose transfer is PQ (smpte2084) is treated as HDR, is
> re-encoded only to 10-bit, and — because VMAF has no HDR model — is measured
> on a tone-mapped SDR copy of both files against a fixed HDR target of 95.5,
> which `--target-vmaf` and `quality.target_vmaf` do not change ([#361](https://github.com/bennischober/ShrinkRay/issues/361)). That
> score is an approximation — on the one real HDR10 pair compared so far it
> read ~2.9 below the reference tool's, which errs toward larger files, not
> worse ones ([#340](https://github.com/bennischober/ShrinkRay/issues/340)).
> Carrying the HDR10 metadata into the output is left to HandBrake's defaults;
> nothing verifies it survives an encode. Dolby Vision is not handled
> ([#104](https://github.com/bennischober/ShrinkRay/issues/104)). The original
> is always kept aside, so check an HDR result before removing it.

## Quickstart

**Windows (PowerShell) — the one-liner install:**

```powershell
irm https://raw.githubusercontent.com/bennischober/ShrinkRay/main/install.ps1 | iex
```

Installs uv, the GPL ffmpeg build, HandBrake and the repository to
`~\.shrinkray` (idempotent — safe to re-run). Then:

```powershell
cd ~\.shrinkray
uv run shrinkray encode 'D:\Movies' --dry-run        # preview what would change
uv run shrinkray encode 'D:\Movies'
uv run shrinkray serve        # the dashboard on http://127.0.0.1:8420
```

> A file, a folder or a glob all work as the input, and no database is created unless you
> pass `--db`: a one-shot run is stateless. Omit the output directory and the encode lands in
> the source's own path, with the original renamed aside and kept — nothing is deleted.

**Already have uv?** Run straight from GitHub without cloning (the `--from git+…` form matters: the bare `uvx shrinkray` name on PyPI belongs to an unrelated project — issue #321):

```bash
uvx --from git+https://github.com/bennischober/ShrinkRay shrinkray encode 'D:\Movies' --dry-run

# shrink it (quality-measured, black bars removed)
uvx --from git+https://github.com/bennischober/ShrinkRay shrinkray encode 'D:\Movies' 'D:\Encoded'
```

> [!TIP]
> Preview anything without writing files by adding `--dry-run`.

## Installation

<details>
<summary><strong>Option A — the install script (recommended for Windows)</strong></summary>

```powershell
irm https://raw.githubusercontent.com/bennischober/ShrinkRay/main/install.ps1 | iex
```

Checks and installs everything (uv, ffmpeg GPL build with cropdetect/libvmaf,
HandBrakeCLI), clones the repo to `~\.shrinkray`, syncs the environment and
builds the dashboard (when Node is present). Re-running is safe.

</details>

<details>
<summary><strong>Option B — run directly from GitHub (no clone, uv users)</strong></summary>

```bash
uvx --from git+https://github.com/bennischober/ShrinkRay shrinkray encode 'D:\Movies' --dry-run
```

</details>

<details>
<summary><strong>Option C — clone the repository</strong></summary>

```bash
git clone https://github.com/bennischober/ShrinkRay.git
cd ShrinkRay
uv sync
uv run shrinkray encode 'D:\Movies' --dry-run
```

</details>

### Prerequisites

Two hard requirements decide whether the tool can do anything at all — check them
with `shrinkray doctor` after installing (it runs the toolchain check without a
database, so it works on a fresh install):

| Tool | Needed for | Hard requirement | Install |
|---|---|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Python & dependencies (manages everything else) | — | `winget install astral-sh.uv` |
| [FFmpeg](https://www.ffmpeg.org/download.html) | scanning, bar detection, VMAF scoring | **must include `libvmaf`** — Debian/Ubuntu's `apt` build does not, and without it the CRF search cannot measure a single file | Windows: `winget install Gyan.FFmpeg`; Linux: a static GPL build (see below); or the container |
| [HandBrake CLI](https://handbrake.fr/downloads.php) | every encode and every probe | **>= 1.6** (the `svt_av1` encoder id; Ubuntu 22.04 ships 1.5.x, which cannot) | Windows: `winget install HandBrake.HandBrake.CLI` (the command-line package; `HandBrake.HandBrake` is the GUI); Linux: compiled from source or the container |

The two routes below bring a correct toolchain themselves: the Windows installer
(`install.ps1`) and the Docker image. Native Linux is the route that needs care.

### Linux / NAS

The container is the recommended route on a NAS or home server: the image
pins ffmpeg-with-libvmaf and HandBrakeCLI 1.11 and verifies both by checksum
([docs/docker.md](docs/docker.md)). On bare metal, the distro packages are
the trap: `apt install ffmpeg` ships without libvmaf, `apt install
handbrake-cli` ships 1.5.x on Ubuntu 22.04 — both pass `apt` and both make
every encode fail.

```bash
# 1. uv + the repo (no sudo needed anywhere)
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/bennischober/ShrinkRay.git ~/.shrinkray
cd ~/.shrinkray
uv sync

# 2. ffmpeg WITH libvmaf: a static GPL build (the distro package cannot
#    measure VMAF). Verified working:
curl -L -o /tmp/ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
sudo tar -xf /tmp/ffmpeg.tar.xz -C /opt --strip-components=1
sudo ln -sf /opt/ffmpeg /usr/local/bin/ffmpeg
sudo ln -sf /opt/ffprobe /usr/local/bin/ffprobe

# 3. HandBrake CLI >= 1.6: no Linux binary is published, so build it from source
#    following HandBrake's own instructions (dependencies differ per distro):
#    https://handbrake.fr/docs/en/latest/developer/build-linux.html
#    (or use the container, which carries it pinned)

# 4. verify the toolchain — the last install step
cd ~/.shrinkray && uv run shrinkray doctor

# 5. try it
uv run shrinkray encode /srv/media/movies --dry-run
uv run shrinkray serve            # dashboard on http://localhost:8420
```

For a headless box that should run the app at boot, the supported path is
the container (see [docs/docker.md](docs/docker.md)): `docker run -d --restart unless-stopped`
with the library mounted and the dashboard port published. The app is a polite
guest on such a machine either way: encodes and probes run below normal
priority and each encode can be thread-capped (`handbrake.max_threads`) so
other services keep their cores.

## How it works

1. **Discovery** — the library is walked recursively for configured extensions
   (`config.json`), with the output directory always excluded. Hidden
   (dot-)directories are never library content: media inside them is not
   discovered, catalogued or re-encoded (#439) — a file you DO name explicitly
   (a path or glob) is still scanned, wherever it lives.
2. **Metadata** — `ffprobe` extracts stream details; duration, aspect ratio and
   size are computed and cached (unchanged files skip probing entirely).
3. **Black-bar detection** — `cropdetect` runs over the whole runtime, reading
   only keyframes, and only bars that every sustained stretch of the film agrees
   on are cropped. With a database the result is cached per file version. When
   that pass yields no bars, a sampled detector (`--crop-samples` windows, bars
   common to every window) decides instead — including, today, when the pass
   *measured* a full-frame stretch, which can crop a film whose aspect ratio
   changes ([#525](https://github.com/bennischober/ShrinkRay/issues/525)).
4. **Quality search** — 10-second samples are encoded at a few CRF values and
   scored with VMAF against their source: 3 samples for `encode` and `plan`, one
   per 10 minutes of runtime in the resident app (the modes differ today —
   [#526](https://github.com/bennischober/ShrinkRay/issues/526)). The search
   interpolates between a passing and a failing probe and settles on the largest
   CRF that meets the required VMAF, or one step below it when an early stop
   saves the last probe.
5. **Encode & verify** — the full movie is encoded once with the winning CRF;
   the output duration is verified against the source to catch interrupted
   encodes.

## Commands

### `encode` — shrink your library

```bash
# quality-measured, in place (the default: original renamed aside, kept)
uv run shrinkray encode 'D:\Movies'

# write a second copy into an output directory instead
uv run shrinkray encode 'D:\Movies' 'D:\Encoded'

# with options
uv run shrinkray encode 'D:\Movies' \
    --target-vmaf 96
```

Every encode is **quality-measured**: a VMAF-driven CRF search per movie picks
the largest CRF that meets the required VMAF (or one step below it, when an early stop
saves the last probe), bars are detected and removed,
and a grown output is refused. There is no fixed-preset mode (issue #202
removed it: an unmeasured encode can take a library file's path).

| Option | Effect |
|---|---|
| `--target-vmaf N` | quality floor (default: `96`) |
| `--probe-encoder-preset N` | the PROBES' encoder speed preset (default: the `handbrake.probe_encoder_preset` config value, shipped as `5`; SVT-AV1 speed -1 to 10, higher = faster (a lower preset is slower and compresses better); a measurement setting — changing it invalidates stored plans) |
| `--encoder-preset N` | the FINAL encode's speed preset (default: the `handbrake.encoder_preset` config value, shipped as `3`; an output setting — the probes run `--probe-encoder-preset`) |
| `--replace` | with an output directory: also swap the verified output into the source's path, renaming the original aside (never deleted). Implied when no output directory is given |
| `--crop-samples N` | cropdetect windows for the sampled fallback detector (more samples can only shrink the crop) |
| `--sample-count N` | the number of probe windows (default: `quality.sample_count`, 3) |
| `--sample-every S` | seconds of runtime per probe window — today shadowed by the always-set sample count, so it has no effect on `encode` ([#526](https://github.com/bennischober/ShrinkRay/issues/526)) |
| `--verify` | after each encode, measure VMAF on the FULL output against the source (slow; ground truth beyond the samples) |
| `--replan` | ignore stored plans and re-probe every movie |
| `--threads N` | per-encode thread cap; 0 or omitted uses `handbrake.max_threads`, whose 0 means all cores |
| `--db PATH` | keep a database (scan cache, plans, history); omitted, the run is stateless |
| `--config PATH` | the config.json to use |
| `--dry-run` | show what would be encoded, without encoding |
| `--overwrite` | re-encode even if the output already exists |

**Batch-safe, with a database:** with `--db` (or in the resident app) every
finished encode is recorded, so an interrupted run skips its completed work on the
next start, and movies whose quality target is unreachable (e.g. already-efficient
HEVC) or that would not shrink are remembered — future runs do not re-probe them.
A stateless run (no `--db`) records nothing and decides every file afresh.

### `plan` — look before you leap

```bash
uv run shrinkray plan 'D:\Movies'
```

Runs the same probing pipeline as a quality encode (samples, CRF search, bar
detection) and prints the predicted outcome per movie — encode or skip, with
CRF, VMAF and an estimated size — plus the estimated total savings. Use it to
decide whether (and with which options) a real encode run is worth it. The
size estimate comes from the short probe samples, so treat it as a ballpark;
the probing predicts the decision, the encode measures it — the chosen CRF
can differ by ±1 between runs.

**HDR estimates are biased optimistic.** The plan's size estimate encodes one
early window (`estimate_size_factor`, `engine/search.py`), and HDR samples skew
toward easy content (dark, static shots), so the predicted savings land far
above the real ones — measured on an HDR 4K title:
19.1% real vs 78% predicted. Every surface that shows an HDR estimate (the
`plan` command, the dashboard's plan display) carries this note; plan an HDR
title expecting the real number to be much smaller than the prediction.

### `setup` — configure, pause and resume

```bash
uv run shrinkray setup            # in the container: docker exec -it <container> shrinkray setup
```

A terminal UI for everything you may change (issue #252): the required VMAF, the encode
presets, thread cap and stall watchdog, the eligibility gates, the file extensions and the
backlog-on-start switch; and, where the resident app keeps its data (the container's
`/data`, or `--db`), a pause/resume switch. Values go through the same schema as a config
file loaded from disk, so a value the app would refuse is shown and never written (the 1080p
floor, for example, can be raised but not lowered). A change to a setting that is part of
every stored plan (the VMAF, the probe preset, the probe density) says that it re-measures
the library before you save it. The resident app applies a save at its next file. The
dashboard cannot do any of this: it only observes.

### `serve` — the resident application

```bash
uv run shrinkray serve            # http://127.0.0.1:8420
```

Starts the resident application: a local server owning the job queue and the
watch loop, plus the **web dashboard** (open the printed URL). Terminal and
browser are just two consumers of the same engine and database:

- the dashboard shows live job status (running/queued with progress and
  elapsed time), the scanned library with decision chips, plan/encode results
  and job history
- the dashboard is read-only: it starts, cancels and reconfigures nothing
  (issue #134). The resident app plans and encodes its backlog and watches
  for changes on its own; settings and pause/resume live in the terminal
  (`shrinkray setup`)
- jobs survive restarts; jobs interrupted by a restart are marked as failed

The server binds to localhost by default. **Authentication is opt-in**, and binding to a
non-loopback address without it is refused at startup: an open dashboard on a LAN is
reachable by every device on that LAN, including guests' phones and any compromised
IoT device. To expose it deliberately, set a token:

```sh
SHRINKRAY_TOKEN='a-long-random-secret' uv run shrinkray serve --host 0.0.0.0
```

The token lives in the environment, never in `config.json` (which is a tracked file).
With it set, every route requires `Authorization: Bearer <token>`; the dashboard asks
for it once and keeps it in the browser. The SSE stream (`/api/events`) also accepts
`?token=` because an `EventSource` cannot send a header. With no token set the
loopback development flow is unchanged — no token, no prompt. TLS is deliberately
left to a reverse proxy. Stop with Ctrl+C; the running job's state is recovered on
the next start.

### `retained` — the originals kept aside

```bash
uv run shrinkray retained                          # list every retained original (reads metadata.db, or --db)
uv run shrinkray retained recover --dir 'D:\Movies\Some Film'   # resolve an interrupted swap
```

ShrinkRay never deletes a source: a replaced original is renamed to
`<name>.shrinkray-original` next to the new file. `list` shows the swaps recorded in the
database (the resident app's, or a run with `--db`); a stateless one-shot run records
nothing, so its originals are found by that suffix on disk. `recover` inspects one
folder (not recursively) and resolves a swap a crash interrupted. Deleting an original is
your own action, once you are satisfied with the result.

### `doctor` — check the toolchain and the library

```bash
uv run shrinkray doctor               # toolchain + library health
uv run shrinkray doctor --probe-all   # also ffprobe every catalogued file for truncation (slow)
```

Checks the toolchain first (ffprobe, HandBrakeCLI, ffmpeg with libvmaf) and stale
scratch folders — both work without a database, so `doctor` is the last step of an
install. With a database (`metadata.db`, or `--db`) it also reports files the database
knows but the disk lost and encode rows whose output vanished; `--probe-all` ffprobes
every file row in that database, so it needs one. It is read-only: it reports, it never
repairs.

> [!NOTE]
> VMAF scores are only comparable within the same libvmaf version: the same
> encode scored 95.76 on ffmpeg 5 (libvmaf v2) and 94.40 on ffmpeg 9 (libvmaf v3).
> Decisions stay consistent because probing measures and decides with the local
> toolchain.

## Which files get encoded: the gate chain

Every file walks the same chain of cheap questions before any expensive step
runs — the same order for `encode`, `plan`, the boot backlog and the
watchdog, because there is exactly one decision path (issue #202 removed the
last fork). A gate that fails skips the file cheaply; no later gate runs.

| # | Gate | Question | Default | If it fails |
|---|---|---|---|---|
| 1 | Duration | At least 90 s? | fixed | skip — not a movie |
| 2 | Resolution | At least the 1080p class — width 1920 OR height 1080? (`is_1080p_class`; `eligibility.min_source_width`/`min_source_height`) | 1920x1080 (each floor may be raised, e.g. `2160` for 4K only; a lower value is rejected at load) | skip — a lower-resolution source saves too little |
| 3 | Size | At least the configured floor? (`eligibility.min_source_size_gb`) | off (`0`) | skip — payoff below the probe cost |
| 4 | Codec age | In the candidate codec list? (`SOURCE_CODECS` + `eligibility.candidate_codecs`) | h264, hevc, vp9, av1 (extendable, never shrinkable) | skip — a source codec outside the list is never re-encoded |
| 5 | Bar detection | Which bars does every sustained segment share? (sampled fallback: every sampled window, [#525](https://github.com/bennischober/ShrinkRay/issues/525)) | — | never skips — "no bars" is a normal outcome |
| 6 | CRF search | Largest CRF (or one step below it) that meets the VMAF target? | 96 | skip — target unreachable |
| 7 | Size gate | Would the result actually be smaller? | measured | skip — "would not shrink" |
| 8 | Encode | One full encode at the chosen CRF, with the crop | — | — |

The two rows people ask about:

- **Rows 2-4 are the "not worth it" gates**: a 480p DVD rip
  fails row 2 today, an MPEG-2/Xvid/VC-1 rip fails row 4, and a small file
  fails row 3 the moment you set a floor. All three are config keys under
  `eligibility`, and all three are checked before any ffmpeg probe runs — a
  skipped file costs one `ffprobe`, not an encode.
- **Row 7 is the real answer to "is this file already efficient?"**: the
  search measures a size probe at the most aggressive CRF, then decides at
  the chosen CRF from the encoded byte counts the VMAF probes already
  recorded for every window (a pre-#314 cache falls back to one encoded
  window), and skips when the encode would grow the file. "Already
  small" is a measurement per file, not a threshold — a well-encoded 4 GB
  H.264 movie is skipped by evidence even with the size floor at 0.

Bars are deliberately NOT a gate: a file is never encoded *in order to*
remove bars (issue #128), but any file that gets encoded for size reasons
leaves with its bars removed — the encoder is already re-encoding every
frame, so the crop is free. Whether older codecs with bars should become
candidates is an open product decision (issue #224). The full walk-through
with every scenario and the history of each gate is
[docs/encode-decision.md](docs/encode-decision.md).

## Configuration

Optional `config.json` (defaults are used when missing). `file.extensions`
defaults to `.mkv` alone (issue #204): a library of `.mp4` files scans nothing
until the list is extended — the shipped container config carries the common
video containers for exactly that reason. **One location, one
order** (issue #58) — the dashboard and the engine read the same file:

1. `--config PATH` (every command; an explicit file wins over everything),
2. `$SHRINKRAY_CONFIG` (the container/NAS mount point; the image sets it to
   `/data/config.json` and creates that file from its baked defaults,
   `$SHRINKRAY_CONFIG_SEED`, when it is missing — never over an existing one),
3. next to the database file (`--db`'s directory — the directory the
   operator already chose for user data; the app writes there too),
4. `./config.json` (the working directory; with the default `--db` that is
   the same as 3).

The resolved path is logged at startup (`Using config ...`). There is no
package-relative lookup: in an installed wheel that directory is not a
user-writable location. The resident app (`serve`) re-reads the file between
files, so a saved change applies at the next file without a restart; an edit
that fails validation is not applied, is logged, and is shown on the dashboard
while the previous settings stay in effect (issue #247).

**Every value is validated** against a schema (issue #61): an unknown key, a
string where a boolean belongs, an out-of-range number — each is an **error
that names the key** at load time, never a silently-applied default (a typo'd
section used to load with all defaults and tell you nothing). An empty
`file.extensions` list is rejected too: it would silently scan nothing. The
old `file.output.seperator` spelling still loads with one deprecation warning;
write `separator`.

Example [config.json](config.json):

```jsonc
{
    "file": {
        "extensions": [".mkv"],
        "output": { "separator": ";" }
    },
    "ffmpeg": {
        "black_bars": { "MIN_WIDTH": 100, "MIN_HEIGHT": 100 }
    },
    "handbrake": {
        "preset": "Fast 1080p30",        // legacy: unread by the measured pipeline
        "encoder": "svt_av1",
        "probe_encoder_preset": "5", // the PROBES' speed preset
        "encoder_preset": "3",         // the FINAL encode's preset
        "max_threads": 0                  // per-encode thread cap (0 = all cores)
    },
    "quality": {
        "target_vmaf": 96             // the required VMAF (--target-vmaf overrides it)
    },
    "resources": {
        "idle_priority": true       // encodes run below normal priority
    },
    "codec": {
        "hdr": { "color_space": ["bt2020nc"], "color_transfer": ["smpte2084"] }  // the defaults; an empty list means "nothing is HDR"
    },
    "app": {
        "auto_backlog": false      // start scan -> plan -> encode on boot
    },
    "language": "en"
}
```

> **`app.auto_backlog` is off by default.** Set it to `true` (the container sets it) and a
> started app enqueues the backlog for every allowlisted root with no client: a scan, then a
> plan, then an encode of everything the plan marked worth encoding. The queue is FIFO and the
> worker single-threaded, so the order holds by construction; a restart does not re-encode
> finished files, because the database already records them as done.

Three keys named "preset" with different meanings — the distinction:
`handbrake.preset` is a legacy key the measured pipeline no longer reads
(it named the removed fixed mode's HandBrake preset *name*). The other two
are the encoder *speed* presets — the SVT-AV1 ladder. **`probe_encoder_preset` is a measurement
setting**: the CRF search's throwaway probe encodes run at it (default
`5` — cheap, and representative of what the final encode will look
like). **`encoder_preset` is an output setting**: the final encode — the
artifact that gets kept — runs at it (default `3`, slower and better).

**The measured-at-5 / encoded-at-3 trade-off is deliberate** (issue
#22): the probe predicts the final encode, and preset shifts the CRF→VMAF
curve, so a probe at a different preset measures a slightly different curve
than the final encode runs — the chosen CRF can miss the target a little in
either direction. That is accepted so the search stays cheap; if a drift
investigation ever shows it matters on real titles, the fallback is to probe
at the final preset and pay the time. The plan's size estimate comes from a
probe-preset encode, so it describes a preset-`5` file, not the
preset-`3` file that gets written. Both presets are recorded on every
encode row (`encoder_preset`, `probe_encoder_preset`) so the provenance
page shows which number produced which measurement. A change to the PROBE
preset invalidates stored plans (they are re-probed); a change to the FINAL
preset does not (a plan is a probe result). Values outside the encoder's
vocabulary are rejected at load, never passed to HandBrake.

**Resource politeness** (why the defaults are what they are): encodes and
quality probes run **below normal priority** — they yield instantly to
whatever else the machine is doing (a movie night on the same box, the
media server you will eventually run next to this). Set
`resources.idle_priority: false` to give encodes full priority again.
`handbrake.max_threads` caps each encode's threads (e.g. `8` on a 16-core
mini PC leaves half the machine for a media server). The resident app also
logs its own memory every 5 minutes (`memory: resident 42.0 MiB`) — staying
lean is a measured fact here, not an aspiration.

ShrinkRay does not talk to media servers: telling another application that
a file changed is that application's job (it can watch the folder itself).
The former opt-in refresh integration was removed (a config.json that still
carries its `integrations` key loads fine and logs one warning).

## Development

A [devcontainer](.devcontainer/devcontainer.json) is included - open in VS Code
and reopen in the container. Note that its apt `ffmpeg` is built **without
libvmaf**, so no encode or probe can run there; use the `dev` target of the
[Dockerfile](Dockerfile) for a full toolchain (see
[docs/docker.md](docs/docker.md)).

### Container

The published image is on GHCR — a `v*` tag builds, smoke-tests and pushes it
(issue #165), so most deployments never build anything:

```bash
docker pull ghcr.io/bennischober/shrinkray:latest

docker run -d --name shrinkray-app \
  --publish 8420:8420 \
  --volume /srv/media:/media \
  --volume /srv/shrinkray-data:/data \
  ghcr.io/bennischober/shrinkray:latest

cat /srv/shrinkray-data/token   # the dashboard token, generated on first start
```

The same image runs the one-shot CLI with no server, no token and nothing installed
on the host: anything after the image name is a `shrinkray` command.

```bash
docker run --rm -v /srv/media:/media ghcr.io/bennischober/shrinkray:latest \
  encode /media/Movies/film.mkv
```

See [docs/docker.md](docs/docker.md#one-shot-runs-no-server) for scratch space,
permissions and what a stateless run records.

Pin a release with the version tag (`ghcr.io/bennischober/shrinkray:v0.6.0`);
`docker inspect` on the image shows the version label without starting it.
The image runs every command as the user you pass in `PUID`/`PGID` (default
1000/1000, never root; Unraid: 99/100 — see
[docs/docker.md](docs/docker.md#running-as-your-user)) and expects the volume layout
below.

Build from source instead (for development or an untagged checkout): two
targets, one file:

```bash
docker build --target runtime -t shrinkray:local .
docker build --target dev     -t shrinkray:dev   .
```

`dev` runs the seven gate commands inside the image. `runtime` is the deployment
shape; see [docs/docker.md](docs/docker.md) for the volumes, the `--root`
requirement on Linux, and the fact that publishing a port **requires** a token:
the image generates one into `/data/token` when `SHRINKRAY_TOKEN` is unset (the
app refuses to bind `0.0.0.0` without one, and there is no TLS - terminate it at
a reverse proxy).

### Web UI (ui/)
The dashboard is a Svelte 5 + Vite + TypeScript app in `ui/`. Node.js is only
needed for *developing or rebuilding* the UI - using the tool does not require
it:
```bash
cd ui
npm ci             # once (the locked dependencies)
npm run dev        # dev server with hot reload (API proxied to :8420)
npm run check      # svelte-check (type checking)
npm run test       # the UI tests
npm run build      # production build to ui/dist (served by the app)
```

The Python checks are one script, the same one CI and the release job run:

```bash
tools/gate.sh          # lock check, ruff, format, mypy (linux AND win32), pytest, import layering
uv run ruff format <files>   # format the files you touched
```

A bare `uv run mypy` checks only your own platform's view; the gate checks both.

Releases are automated: push a tag, the workflow checks, builds and publishes.
The release cut that precedes the tag bumps the version in `pyproject.toml`, and
the image pin above must name the same version - the test suite fails until it does.

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

## Direction

ShrinkRay is heading for one shape: a container on a server, pointed at a media
folder, answering *"could this file be smaller at the same quality, and does it carry
black bars?"* and acting on the answer unattended. In effect it fuses an
`ab-av1`-style CRF search and the encode at the CRF it found into one step, applied
to a whole library. See [PRODUCT.md](PRODUCT.md) for the contract and the
[issue tracker](https://github.com/bennischober/ShrinkRay/issues) for the work that
gets it there.

The concrete target is an **Unraid Community App** (issue #244), built on two surfaces
with separate jobs:

- **The dashboard observes.** The WebUI button opens it: what is running, planned,
  remaining, reclaimed and retained. It cannot start, stop or reconfigure anything.
- **The terminal controls.** The Console button opens a shell in the container that
  prints what to type: `shrinkray setup`, a terminal UI that configures everything
  (required VMAF, thread caps, eligibility gates) and pauses or resumes the backlog;
  `shrinkray encode /media/<path>` to shrink one file right now, during which the
  watcher pauses; `shrinkray retained` to recover originals.
- **Everything the operator chose lives in the appdata folder** (`/data`): config,
  database, pause state and a token generated on first start, so restarts and image
  updates keep it.

All three are built: the pause (`touch /data/paused`, or the switch in `shrinkray setup`),
the console banner (see [docs/docker.md](docs/docker.md#the-console)) and `shrinkray setup`
itself. [docs/tui.md](docs/tui.md) records the decision and the open design work.

The behaviour, per file:

- **Watch, scan, record, decide.** A watchdog watches the library folder. When a file
  appears or changes, it is scanned (codec, resolution, black bars, ...), the result
  is written to ShrinkRay's database, and whether a re-encode is needed is decided
  again from that fresh scan.
- **Eligibility.** A source whose dimensions are both below the 1080p class, or whose
  codec is outside the candidate list, is never re-encoded.
- **Codec routing.** The CRF search runs on every eligible file; a re-encode happens
  only when the file would actually get smaller. The candidate list is h264, hevc, vp9 and
  av1 (extendable in config, never shrinkable); MPEG-2/VC-1 and older never are.
- **Bar removal is part of every re-encode**, not an option: any file that is encoded has
  its bars detected and removed, so a re-encoded file is always clean. There is no flag to
  keep them, and no file is skipped for lacking them. A film whose aspect ratio changes is
  handled by cropping only the bars every sustained segment agrees on (a fallback
  that can break this is tracked in [#525](https://github.com/bennischober/ShrinkRay/issues/525)).
- **One encode mode.** Every re-encode, bar removal included, is a VMAF-driven CRF
  search against a required quality floor (VMAF 96 by default) followed by one full
  encode. There is no fixed-preset encode.
- **Output.** AV1 only, encoded on the CPU. 8-bit and 10-bit sources are both
  supported. Only the video stream is re-encoded; audio and subtitle tracks are carried
  over from the source — all of them by default, or only the languages set in
  `handbrake.audio_languages` / `handbrake.subtitle_languages`. An audio codec HandBrake
  cannot pass through is converted losslessly (FLAC) rather than copied.

The boundary that shape implies: **ShrinkRay may change a file's bytes, never its
identity.** Path, filename and the set of files stay as they are. Re-encoding
replaces a file in place — the original is renamed aside and kept, and the
dashboard lists it for manual deletion. ShrinkRay never deletes a source file.
Everything else — naming, moving, organizing, matching, deduplicating, acquiring —
belongs to the tooling that already built the collection, and is out of scope here.

## FAQ

<details>
<summary><code>python</code>/<code>uv</code> is not found in the command prompt</summary>

Your PATH is missing the install location — [see this answer](https://stackoverflow.com/a/13596981).
</details>

<details>
<summary><code>No such filter: 'cropdetect'</code></summary>

On Windows you need the **GPL build** of
[ffmpeg](https://github.com/BtbN/FFmpeg-Builds/releases) (the LGPL build lacks
the filter). On Linux/UNIX [this might help](http://ffmpeg.org/pipermail/ffmpeg-user/2015-October/028753.html).
</details>

<details>
<summary><code>'HandBrakeCLI' not found!</code></summary>

Install [HandBrake CLI](https://handbrake.fr/downloads.php) and make sure it is
on your `PATH` (the tool also checks the default install locations).
</details>

## Resources

- [Changelog](CHANGELOG.md) — what shipped in each release
- **Project docs**: [PRODUCT.md](PRODUCT.md) (the contract) ·
  [DESIGN.md](DESIGN.md) (the dashboard's design system) ·
  [docs/docker.md](docs/docker.md) (deployment) ·
  [docs/encode-decision.md](docs/encode-decision.md) (every decision gate,
  walked through) · [docs/plan-reuse.md](docs/plan-reuse.md) (the plan
  cache) · [docs/black-bar-spike.md](docs/black-bar-spike.md) (the bar
  detector's measured data) · [docs/tui.md](docs/tui.md) (the terminal
  surface)
- [FFmpeg documentation](https://ffmpeg.org/ffmpeg.html) ·
  [cropdetect filter](https://ffmpeg.org/ffmpeg-filters.html#cropdetect) ·
  [libvmaf](https://github.com/Netflix/vmaf)
- [HandBrake CLI options](https://handbrake.fr/docs/en/latest/cli/cli-options.html)
- [typer](https://typer.tiangolo.com/) ·
  [rich](https://rich.readthedocs.io/) ·
  [loguru](https://loguru.readthedocs.io/) ·
  [uv](https://docs.astral.sh/uv/)
