<div align="center">

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
terminal (planned: `shrinkray setup`, see [Direction](#direction)).

> [!IMPORTANT]
> **The one-shot shape is what runs today.** `encode` takes a file, a folder or a glob, and
> omitting the output directory re-encodes **in place**: the verified output takes the source's
> own path and the original is renamed aside and kept. No database is created unless you pass
> `--db`. Naming an output directory instead keeps the older behaviour of writing a second copy
> there.

## What does it do?

**`scan`** walks your movie folders and catalogs every file: duration, codecs,
resolution, FPS, audio languages, HDR detection, black-bar detection — cached
in a sqlite database, so repeated scans are near-instant.

**`encode`** re-encodes your library with HandBrake to save disk space. The
default mode uses a fixed preset. The **quality mode** is where it gets
interesting:

> **Fix quality, not bitrate.** For every movie, short samples are probed at
> different CRF values, each probe is scored with [VMAF](https://github.com/Netflix/vmaf)
> (Netflix's Emmy-winning perceptual quality metric), and a binary search finds
> the *largest* CRF that stays visually transparent — then the movie is encoded
> once with exactly that setting.

Measured on real content (target VMAF 95, x265 — those runs predate the AV1 output and the
96 default, so the numbers below describe what they measured, not today's defaults):

| Movie | Content | Result | Saved |
|---|---|---|---|
| Django Unchained (PAL DVD) | grainy live action | CRF 19, VMAF 95.25, letterbox bars removed | **64 %** |
| Bloodshot (alt. ending) | 1080p live action | CRF 20, VMAF 95.76 | **73 %** |
| Rick & Morty S01E01 | already-efficient HEVC | skipped — target unreachable | *(correctly)* |

Nothing is guessed: every decision is measured, recorded in the database, and
movies that cannot profit (already efficient sources) are skipped — not
wasted.

> [!NOTE]
> **HDR10 content is supported** by quality mode (10-bit encoder + tone-mapped
> VMAF measurement). Dolby Vision dual-layer files are not yet (tracked in
> [issue #104](https://github.com/bennischober/ShrinkRay/issues/104)).

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

**Already have uv?** Run straight from GitHub without cloning:

```bash
uvx shrinkray encode 'D:\Movies' --dry-run

# shrink it (quality-measured, black bars removed)
uvx shrinkray encode 'D:\Movies' 'D:\Encoded' --quality
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

| Tool | Needed for | Install |
|---|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Python & dependencies (manages everything else) | `winget install astral-sh.uv` |
| [FFmpeg](https://www.ffmpeg.org/download.html) | scanning, bar detection, VMAF scoring (GPL build on Windows) | `winget install Gyan.FFmpeg` |
| [HandBrake CLI](https://handbrake.fr/downloads.php) | re-encoding (optional) | `winget install HandBrake.HandBrake` |

### Linux / NAS

Verified on Ubuntu 20.04 (WSL rehearsal for a mini-PC/NAS target): the full
gate passes and VMAF measurement works headless. This is the intended
production shape — a server with the library on a mounted volume, driven by
the resident app or the CLI.

```bash
# 1. uv + the repo (no sudo needed anywhere)
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/bennischober/ShrinkRay.git ~/.shrinkray
cd ~/.shrinkray
uv sync

# 2. ffmpeg: distro package, or a static build into ~/.local/bin when the
#    distro is old (a static build also ships libvmaf - verified working)
sudo apt install ffmpeg        # or: see johnvansickle.com static builds

# 3. HandBrake CLI >= 1.6 (required for AV1 output: it needs the svt_av1
#    encoder id, which older builds do not know)
sudo apt install handbrake-cli

# 4. try it
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
   (`config.json`), with the output directory always excluded.
2. **Metadata** — `ffprobe` extracts stream details; duration, aspect ratio and
   size are computed and cached (unchanged files skip probing entirely).
3. **Black-bar detection** — `cropdetect` samples several segments per movie;
   a threshold keeps noise pixels from being mistaken for bars.
4. **Quality search** *(quality mode)* — three 10-second samples are encoded at
   different CRF values and scored with VMAF against their source; a binary
   search converges on the largest CRF that still meets the quality floor.
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
the largest CRF that keeps perceptual quality, bars are detected and removed,
and a grown output is refused. There is no fixed-preset mode (issue #202
removed it: an unmeasured encode can take a library file's path).

| Option | Effect |
|---|---|
| `--target-vmaf N` | quality floor (default: `96`) |
| `--probe-encoder-preset N` | the PROBES' encoder speed preset (`5` default; SVT-AV1 speed 0-10, higher = slower; a measurement setting — changing it invalidates stored plans) |
| `--encoder-preset N` | the FINAL encode's speed preset (`3` default; an output setting — the probes run `--probe-encoder-preset`) |
| `--replace` | with an output directory: also swap the verified output into the source's path, renaming the original aside (never deleted). Implied when no output directory is given |
| `--crop-samples N` | cropdetect windows per movie (more samples can only shrink the crop) |
| `--dry-run` | show what would be encoded, without encoding |
| `--overwrite` | re-encode even if the output already exists |

**Batch-safe:** every finished encode is recorded, so an interrupted run skips
its completed work on the next start. Movies whose quality target is provably
unreachable (e.g. already-efficient HEVC) or that would not shrink are skipped
and remembered — future runs do not re-probe them.

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

**HDR estimates are biased optimistic.** The size probe encodes one early
window, and HDR samples skew toward easy content (dark, static shots), so the
predicted savings land far above the real ones — measured on an HDR 4K title:
19.1% real vs 78% predicted. Every surface that shows an HDR estimate (the
`plan` command, the dashboard's plan display) carries this note; plan an HDR
title expecting the real number to be much smaller than the prediction.

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
- jobs are enqueued from the UI (multi-directory: add several movie folders)
  or via the API (`POST /api/jobs`)
- the watch loop can be started/stopped from the UI or
  `POST /api/watch/start|stop`
- the configuration (`config.json`) is editable in the UI's settings dialog
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
| 5 | Bar detection | Which bars does EVERY sampled frame share? | — | never skips — "no bars" is a normal outcome |
| 6 | CRF search | Largest CRF that meets the VMAF target? | 96 | skip — target unreachable |
| 7 | Size gate | Would the result actually be smaller? | measured | skip — "would not shrink" |
| 8 | Encode | One full encode at the chosen CRF, with the crop | — | — |

The two rows people ask about:

- **Rows 2-4 are the "not worth it" gates** you asked for: a 480p DVD rip
  fails row 2 today, an MPEG-2/Xvid/VC-1 rip fails row 4, and a small file
  fails row 3 the moment you set a floor. All three are config keys under
  `eligibility`, and all three are checked before any ffmpeg probe runs — a
  skipped file costs one `ffprobe`, not an encode.
- **Row 7 is the real answer to "is this file already efficient?"**: the
  search measures a size probe at the most aggressive CRF and again at the
  chosen CRF, and skips when the encode would grow the file. "Already
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
        "preset": "Fast 1080p30",        // fixed-mode HandBrake preset NAME
        "encoder": "svt_av1",
        "probe_encoder_preset": "5", // quality mode: the PROBES' speed preset
        "encoder_preset": "3",         // quality mode: the FINAL encode's preset
        "max_threads": 0                  // per-encode thread cap (0 = all cores)
    },
    "resources": {
        "idle_priority": true       // encodes run below normal priority
    },
    "codec": { "hdr": { "color_space": [], "color_transfer": [] } },
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
`handbrake.preset` is the **fixed-mode** HandBrake preset *name* (a full
quality/speed/content bundle, e.g. `Fast 1080p30`). The other two are
**quality mode's** encoder *speed* presets — the x264/x265 ladder
(`ultrafast`…`placebo`). **`probe_encoder_preset` is a measurement
setting**: the CRF search's throwaway probe encodes run at it (default
`medium` — cheap, and representative of what the final encode will look
like). **`encoder_preset` is an output setting**: the final encode — the
artifact that gets kept — runs at it (default `slow`).

**The measured-at-medium / encoded-at-slow trade-off is deliberate** (issue
#22): the probe predicts the final encode, and preset shifts the CRF→VMAF
curve, so a probe at a different preset measures a slightly different curve
than the final encode runs — the chosen CRF can miss the target a little in
either direction. That is accepted so the search stays cheap; if a drift
investigation ever shows it matters on real titles, the fallback is to probe
at the final preset and pay the time. The plan's size estimate comes from a
probe-preset encode, so it describes a `medium`-preset file, not the
`slow`-preset file that gets written. Both presets are recorded on every
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
The opt-in Jellyfin refresh integration was removed (a config.json that
still carries its `integrations` key loads fine and logs one warning).

## Development

A [devcontainer](.devcontainer/devcontainer.json) is included - open in VS Code
and reopen in the container. Note that its apt `ffmpeg` is built **without
libvmaf**, so quality mode cannot run there; use the `dev` target of the
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

Pin a release with the version tag (`ghcr.io/bennischober/shrinkray:v0.4.0`);
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
npm install        # once
npm run dev        # dev server with hot reload (API proxied to :8420)
npm run build      # production build to ui/dist (served by the app)
npm run check      # svelte-check (type checking)
```

```bash
uv run pytest          # tests (with coverage report)
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # static type checking
```

Releases are automated: push a tag, the workflow checks, builds and publishes.

```bash
git tag v0.3.0
git push origin v0.3.0
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

The pause and the console banner are built (`touch /data/paused`, see
[docs/docker.md](docs/docker.md#the-console)); `shrinkray setup` is not yet. [docs/tui.md](docs/tui.md) records the
decision and the open design work.

The target behaviour, per file:

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
  handled by cropping only the bars common to every sampled frame.
- **One encode mode.** Every re-encode, bar removal included, is a VMAF-driven CRF
  search against a required quality floor (VMAF 96 by default) followed by one full
  encode. There is no fixed-preset encode.
- **Output.** AV1 only, encoded on the CPU. 8-bit and 10-bit sources are both
  supported. Only the video stream is re-encoded; every audio and subtitle track is
  carried over 1:1 from the source.

The boundary that shape implies: **ShrinkRay may change a file's bytes, never its
identity.** Path, filename and the set of files stay as they are. Re-encoding
replaces a file in place — the original is renamed aside and kept, and the
dashboard lists it for manual deletion. ShrinkRay never deletes a source file.
Everything else — naming, moving, organizing, matching, deduplicating, acquiring —
belongs to the tooling that already built the collection, and is out of scope here.

> [!NOTE]
> **Target shape vs. what ships today.** These two operating modes are the contract:
>
> - **One-shot CLI** — point it at a file, a folder, or a glob; it skips the database,
>   runs the CRF search, and re-encodes in place, retaining the original.
> - **Fire and forget** — a container that scans the mounted library into its own
>   database, detects black bars for every file, runs the CRF search for the whole
>   backlog, encodes it, and watches for changes.
>
> **Both operating modes are implemented.** The one-shot CLI takes a file, a
> folder, or a glob; it is stateless unless `--db` is passed; and it encodes
> in place when no output directory is given (the original is renamed aside
> and kept). Fire-and-forget is the container shape above. Follow the
> [issue tracker](https://github.com/bennischober/ShrinkRay/issues) for what
> is in flight.

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
  surface: decided, not built)
- [FFmpeg documentation](https://ffmpeg.org/ffmpeg.html) ·
  [cropdetect filter](https://ffmpeg.org/ffmpeg-filters.html#cropdetect) ·
  [libvmaf](https://github.com/Netflix/vmaf)
- [HandBrake CLI options](https://handbrake.fr/docs/en/latest/cli/cli-options.html)
- [typer](https://typer.tiangolo.com/) ·
  [rich](https://rich.readthedocs.io/) ·
  [loguru](https://loguru.readthedocs.io/) ·
  [uv](https://docs.astral.sh/uv/)
