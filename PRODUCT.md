# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

A Svelte 5 + Vite dashboard served by the resident Python app (FastAPI), plus a
first-class CLI with a terminal UI for configuration and control. The backend is an
orchestrator: all heavy work is delegated to external binaries (`ffprobe`, `ffmpeg`,
`HandBrakeCLI`) via `subprocess`. No build-step-heavy SPA framework; the dashboard is
an **observability surface for the resident mode**, not the product and not a control
panel. Control lives in the terminal (see *Operating modes* and *Deployment target*).

## Users

One operator with a large, **already-organized** video library on a home server or NAS
(tens to thousands of files, on local disks or a network share). The collection is
curated upstream: files are already named, already matched, already free of duplicates.
ShrinkRay is pointed at it and does one job.

That last part is the defining constraint. Everything that falls under *managing a
library* — naming, renaming, moving, organizing, matching, scraping, deduplicating,
acquiring, playing, serving — has already been done by the time ShrinkRay sees a file,
and ShrinkRay never does any of it. The operator is comfortable with terminals and
containers, wants unattended operation, and will not accept a tool that can lose a file.

## Ecosystem position (issue #166)

ShrinkRay has one specific thing to do: **make movies smaller at measured quality.**
It is a companion to the media-stack tools, not a competitor to any of them:

- **Radarr / Sonarr** decide *what* the library contains and keep it organized —
  naming, matching, deduplication, acquisition. ShrinkRay never does any of that.
- **Plex / Jellyfin** *play* the library. ShrinkRay never serves media.
- ShrinkRay sits beside them on the **same mounted library**: the *arrs write into
  it, the player reads it, ShrinkRay rewrites files in place at measured quality and
  retains every original (`<name>.shrinkray-original`) so coexisting with other
  automation on the tree stays safe.
- The **web dashboard is reachable from any browser** on the network — no desktop
  app, no SSH tunnel. It is an observability surface (see *Operating modes*), not a
  control panel. Configuration and control happen in the terminal, through the
  container's console (see *Deployment target*).

This section names the ecosystem in prose only: no code path may import, probe,
parse for, or depend on any of these tools (see the scope boundary in AGENTS.md).

## Auth roadmap (issue #166)

- **Now:** a shared-secret token (`SHRINKRAY_TOKEN`, issue #36) gates any
  non-loopback bind; on a trusted LAN the practical deployment is token-or-nothing
  at the operator's discretion. **Planned (issue #244):** the container generates a
  token on first start when none is set, stores it in `/data` and shows it in the
  console banner, so a non-loopback bind is never tokenless and never needs the
  operator to invent a secret. Reverse-proxy termination is documented
  (docs/docker.md); there is no TLS in the app.
- **Later:** proper per-user authentication — the shape (session login,
  proxy-forwarded identity, OIDC) is a future decision, deliberately not made here.
  The deferral is safe because the dashboard is **read-only in intent** (issue
  #134): nothing in the UI can start, cancel or reconfigure work, so an open
  dashboard leaks information, never control. Control is reachable only from a
  shell on the host or in the container (the terminal surface, issue #244), which
  the network never exposes.

## Operating modes

There are exactly **two ways to run ShrinkRay**, and they share one engine. They differ
in who holds the state and who drives the loop — not in what a re-encode is.

### Mode A — one-shot CLI (the direct mode)

The operator points the CLI at work and it does that work, then exits:

```sh
shrinkray encode 'media/movies/Some Film (2020).mkv'   # one file
shrinkray encode 'media/movies/'                        # a whole folder
shrinkray encode 'media/movies/*.mkv'                   # a glob
```

The input is **a file, a folder, or a glob**. By default this mode **does not use the
database at all**: no scan to persist, no plan cache, no verdict history. It goes
straight to the VMAF-driven CRF search for each file it was given and then re-encodes
that file **in place**. The original is never deleted — it is renamed aside and kept, so
the operator can delete it themselves once satisfied.

The database is an optimization for repeated runs, not a prerequisite. Mode A must work
with no `metadata.db` present and must not create one unless asked.

Mode A is also the answer to **"I have one video that needs to be smaller"**: on a desktop,
or from the console of a running container, `shrinkray encode <path>` re-encodes that one
file with no library scan behind it. The run lives in the terminal that started it, and the
CLI says so when it starts: closing that terminal cancels the encode (the source is
untouched until the verified result is swapped in). When a resident watcher runs beside it,
the watcher **pauses** for the duration of the manual encode and resumes afterwards; the
CLI signals this through the shared state on disk, never by calling the resident process
(issue #244).

### Mode B — fire and forget (the resident mode)

The deployment shape: a container on a server, pointed at the mounted library, left
alone. It owns its own sqlite database and runs the whole cycle without a babysitter:

1. **Scan** the mounted folder into the database — codec, resolution, duration, HDR,
   and the rest of each file's properties.
2. **Black-bar detection for every file**, recorded in the database, so the crop decision
   is a stored fact rather than a per-run measurement.
3. **CRF search for every file that passes the gates**, with the result stored so the
   work is never repeated on an unchanged file.
4. **Re-encode the backlog automatically**, in place, retaining each original.
5. **Watch** the folder and scan every stable new or changed file as it appears, then
   decide it from the fresh scan.

The loop is the product. The operator starts the container once and does not touch it
again; the library gets smaller on its own and every decision is recorded and
explainable.

Both modes are first-class. Mode A is what a person runs to try the tool, to handle a
single stubborn file, or to script their own batch; Mode B is what runs on the server.
Every capability available in one is available in the other, and the CLI never requires
the resident process.

### The dashboard is Mode B's window, not its steering wheel

The web UI exists to **observe Mode B**: what is happening right now, what is planned
next, what still needs doing. It is a status surface. The operator starts the container
once and the machine decides; the dashboard answers "where does it stand?" without
becoming another way to drive the library.

That means the dashboard's job is to show, not to do: the collection and its state, the
running job with live progress, the plan (what is queued, at which CRF, for how much
predicted saving), what has been reclaimed, what failed and why, and the retained
originals waiting for manual deletion. Read-only by intent.

This is a deliberate boundary, and it is the honest one for a fire-and-forget tool: a
control surface implies an operator watching, which is the thing Mode B exists to
remove. It is also the smaller attack surface — a status page needs no authorization
model beyond the token that guards the network, and no route that can start work by
accident.

> **Implementation status.** The dashboard is **observe-only** (issue #134). The
enqueue/cancel/retry buttons, the watchdog start/stop toggle and the `config.json` editor
are gone, and so are the routes behind them: `POST /api/jobs`, `POST /api/jobs/{id}/cancel`,
`POST /api/watch/{action}` and `PUT /api/config` no longer exist. What remains is reads —
the collection, the running job with live progress, the plan, what was reclaimed, what
failed and why, and the retained originals. The app starts the backlog and the watcher
itself, so a container needs no browser to make progress.

### The terminal is where the operator configures and controls

Everything the dashboard deliberately cannot do happens in the terminal. `shrinkray setup`
is a terminal UI (TUI) that configures **everything** an operator may change: the
required VMAF, thread caps, the eligibility gates (resolution floor, minimum file size),
and the rest of `config.json`; and, where a resident process runs, **pause and resume**
of the backlog. The watched folder is not a setting: it is whatever is mounted at
`/media`.

The split is by role, not by convenience. A browser on the LAN can observe; changing what
the machine does requires a shell on the host or in the container, which the network never
exposes. So the TUI talks to the resident process through the state it already shares on
disk (the config file and a pause flag in `/data`), never through a network route — the
dashboard stays read-only by construction, not by discipline.

Rules the TUI must keep:

- **It edits settings, it does not loosen product rules.** The 1080p resolution floor
  may be **raised** (for example to encode 4K only), never lowered: below 1080p a
  re-encode saves too little to be worth the work. The same holds for every rule in
  *Product Purpose* — the TUI exposes the settings those rules leave open, nothing more.
- **It says what a change costs.** A setting that is part of the stored plan fingerprint
  (the required VMAF, the probe preset) invalidates every cached plan; the TUI names that
  consequence before it saves.
- **A saved change applies at the next file.** A running encode finishes with the settings
  it started with.
- **The same TUI works on a native install**, editing that install's config. Pause and
  resume appear only where a resident process is running.

> **Implementation status.** Both modes are implemented. Mode A accepts a file, a folder or
a glob, creates no database unless `--db` is passed, and encodes in place when no output
directory is given (the original is renamed aside and kept). Mode B scans, plans and encodes
the backlog on start and watches the tree for new and changed files. **The terminal surface
is not built** (issue #244 records the direction; the build is split into follow-up issues):
there is no `shrinkray setup`, no console banner, no pause, and a manual encode does not
pause the watcher yet. Today the configuration is `config.json`, edited by hand, followed by
a restart.

## Deployment target: an Unraid Community App (issue #244)

The resident mode's intended home is a home server, and the concrete target is an
**Unraid Community App**: one container, installed from the Community Applications catalog,
pointed at a share, left running. The shape follows the established "open the console and
follow the steps" pattern of Unraid apps:

- **WebUI button → the dashboard.** Read-only: what is running, planned, remaining,
  reclaimed and retained.
- **Console button → a shell in the container that prints what to type.** The banner is
  the operator's manual:

  ```text
  ShrinkRay — the resident encoder is running.
    Configure, pause or resume:   shrinkray setup
    Encode one file now:          shrinkray encode /media/<path>
    List or recover originals:    shrinkray retained
    Dashboard:                    http://<server>:8420   (token: ...)
  ```

- **`/data` is the appdata folder** (e.g. `/mnt/user/appdata/shrinkray`): config, database,
  pause state and the generated token live there, so everything survives a restart and an
  image update. Nothing the operator chose lives in the image.
- **`/media` is the library share**, mounted writable because the resident mode encodes in
  place; the watched folder is exactly this mount.
- **Files are written as the operator's user.** Unraid templates pass the user and group to
  run as (`PUID`/`PGID`, with `UMASK`); a file written to the share as any other owner is a
  support ticket. This reopens the fixed-UID decision in `docs/docker.md` for this target.
- **The token is generated when absent** (see *Auth roadmap*); a template field may set it
  instead.

The same image runs the one-shot CLI with no server at all (`docker run --rm ... encode
/media/<file>`, issue #242), so a desktop user with one video and an Unraid operator with a
library use one artifact.

> **Implementation status.** Not built. The image runs the resident mode only (its
entrypoint pins `serve`, issue #242), as a fixed UID, with its config baked into the image
(issue #236). The template, the banner, `PUID`/`PGID`, the generated token and the Community
Applications listing are follow-up issues of #244.

## Product Purpose

ShrinkRay answers two questions per file:

1. **Is this file bigger than its quality requires?** Asked only of H.264 sources.
   When the answer is yes, it re-encodes the file smaller at the same measured
   perceptual quality (VMAF-driven CRF search), and when the answer is no it leaves
   the file alone and records the verdict so the work is never repeated.
2. **Does this file carry baked-in black bars?** That is, does the frame's aspect
   ratio differ from the aspect ratio of its actual content? Bars are detected for every file
   that is going to be re-encoded and cropped away during that encode — a re-encode always
   produces a clean file, never one with the padding still baked in.

Before either question is asked, a file must be worth touching at all:

- **Minimum resolution: 1080p.** Below that, a re-encode saves too little to be
  worth the work.
- **Minimum codec: H.264.** Sources in an older codec are not re-encoded.

Among eligible files, **the CRF search always runs** — and a re-encode happens only when
that search shows the file would actually get smaller. That one rule replaces the older
"codec decides what triggers an encode" framing, which was wrong twice: it read as *never
re-encode a modern codec without bars*, and it implied a re-encode might happen *in order
to* remove bars. Neither is true.

Bars are not a gate at all. Every file that is re-encoded has its bars detected and removed
as part of that encode, because the encoder is already re-encoding every frame — leaving the bars
in would bake the same wasted area into a smaller file, which is worse than the source. There is
no flag to keep them, and no file is skipped for lacking them.

The one hard case, a film whose aspect ratio changes, is solved by cropping only the bars
**common to every sampled frame**: a 1.78:1 sequence reaching the frame edge vetoes the crop for
the 2.40:1 sequences too. A missed crop costs a little space; a wrong crop costs picture.

There is exactly **one reason to re-encode**: the file gets smaller. Never to remove bars,
never because a codec is old. [docs/encode-decision.md](docs/encode-decision.md) is the
authoritative walk-through of every gate, in order.

There is exactly **one way to re-encode**: a VMAF-driven CRF search against a required
quality floor (**VMAF 96 by default**), then one full encode at the CRF it found. The
output is always **AV1, encoded on the CPU** — no other output codec and no hardware
encoder. Both 8-bit and 10-bit sources are supported. Only the video stream is
re-encoded: every audio and subtitle track is carried over 1:1 from the source,
untouched. A bar
removal goes through the same search. There is no fixed-preset or "just crop it"
encode — an encode whose quality is not measured would miss the point of the tool.

The CRF search always runs against the required quality floor — **VMAF 96**, which is
`DEFAULT_TARGET_VMAF` in `engine/search.py`. Every `--target-vmaf` option, the API request
model and the job payload derive from that one constant, so there is no second place to drift.
The target participates in the stored plan fingerprint: raising it from the 95.0 the code shipped
before deliberately invalidated every cached plan, so a library planned under the old default re-probes
once.

In practice it fuses two steps an operator would otherwise run by hand per file — an
`ab-av1`-style CRF search, then the encode at the CRF it found — into one, and applies
that to an entire library on its own. It is meant to be **fire and forget**: point it
at the library once, and it works through the backlog, picks up new and changed files
through its watchdog, and keeps a record of every decision so nothing is done twice.

The watchdog drives the whole loop. It watches the library folder; when a file
appears or changes, it scans that file (codec, resolution, black bars and the rest
of its properties), writes the result to ShrinkRay's own database, and decides again
from the fresh scan whether a re-encode is needed. A changed file is a new question:
its earlier verdict no longer applies.

Success = the library is smaller, every re-encoded file is provably no worse than its
source, every untouched file has a recorded reason, and nothing was ever lost.

**This is not a hobby project.** It is meant to be given to the community: installed
by strangers on their own servers, pointed at libraries they cannot re-acquire, and run
unattended for hours. That is a different obligation from a personal script — a defect
here can destroy someone else's data. The engineering standard this implies (secure at
every boundary, correct under interruption, honest about failure, tested where it
matters, readable by a stranger, no unsupported claim in a doc) is written down as
non-negotiable in [AGENTS.md](AGENTS.md).

## Positioning

The storage-efficiency layer of a self-hosted media stack: per-file, *measured*
compression that no neighbouring tool provides. Media servers play; acquisition tools
acquire and name; this tool shrinks — complementary by design, and deliberately
nothing else.

**It is not a library manager.** It does not name, rename, move, organize, match,
scrape, deduplicate, acquire, play, or serve media. Those are somebody else's job,
already done, upstream. ShrinkRay's only opinions about a file are its **size relative
to its quality** and whether its **frame matches its content**.

## Operating Context

- Headless Linux server or NAS, run as a container (the target install, an Unraid
  Community App — see *Deployment target*) or from a source checkout with `uv`; the CLI
  also runs on a Windows workstation for development and for one-off runs.
- The library arrives as one bind-mounted directory tree of **already-organized**
  files. ShrinkRay reads it, decides per file, and writes only inside it.
- **Mode A needs no state.** A one-shot CLI run against a file, folder or glob works
  from a bare checkout with no database, no config, and no resident process.
- **Mode B owns its state.** The container keeps its sqlite database, its
  `config.json`, its pause state and its token on the `/data` volume (Unraid's appdata
  folder) beside the library, never inside it and never in the image.
- HandBrakeCLI + ffmpeg (with `libvmaf`) are external binaries baked into the
  image; a pinned image is also what makes VMAF scores comparable across runs.
- Encodes run for hours. The service must be a polite guest: idle priority, thread
  caps, and a stall watchdog, so playback on the same box keeps its cores.
- The dashboard is reached over the LAN and is **read-only in intent**: it reports
  Mode B's state (running, planned, remaining, reclaimed, retained) and does not start
  work. The terminal is the control surface: `shrinkray setup` configures and pauses,
  `shrinkray encode` runs a single file. The CLI is a first-class product: every
  capability is available without the resident process, and automation may drive it
  alone.
- Files appear and change continuously. A watchdog notices every stable new or
  changed file, re-scans it, and re-decides it without being asked.

## Capabilities and Constraints

- **Two operating modes** (see *Operating modes*): a stateless one-shot CLI over a
  file / folder / glob, and a resident container that scans a mounted tree, detects
  bars and CRF for the whole collection, encodes the backlog, and watches for changes.
- **The dashboard observes, it does not steer**: it shows the collection's state, the
  running job, the plan, what remains and what was reclaimed. It is Mode B's status
  window; it is not a second control path into the library.
- **The terminal configures and controls** (planned, issue #244): `shrinkray setup` edits
  every setting and pauses or resumes the backlog; a manual `shrinkray encode` pauses the
  resident watcher while it runs. No network route reaches either.
- **One encode mode**: VMAF-driven CRF search per file against the required VMAF
  (96 by default; sampled probes, measured quality), then one full encode at the
  winning setting. Every re-encode, bar removal included, goes through it. The
  decision statistic and sampling density are one coupled decision, not two.
- **Output format**: AV1 only, CPU encoder only. 8-bit and 10-bit sources are both
  supported.
- **Video only**: audio and subtitle tracks are copied 1:1 from the source, never
  re-encoded, converted or dropped.
- **Skip guardrails**: already-efficient sources, unreachable targets, too-short
  files, and encodes that would not shrink are skipped and remembered.
- **Eligibility limits**: files below 1080p or in a codec older than H.264 are
  never re-encoded. The resolution floor may be raised in configuration, never
  lowered below 1080p (issue #244; enforced when the config loads, issue #246).
- **Source-codec gate**: the CRF search runs on every eligible file. H.264 is the
  floor; MPEG-2/VC-1 and older are never re-encoded. Size alone decides whether a
  re-encode happens, for every codec.
- **Black-bar removal is part of every re-encode**, not an option. Bars are detected for
  any file that will be encoded and removed during that encode, so a re-encoded file is
  always clean. There is no flag to keep them, and no file is skipped for lacking them.
  The one hard case, a changing aspect ratio, is handled by cropping only the bars common
  to every sampled frame. The crop is irreversible, which is why that rule is the safe one.
- **Swap-in-place with a retained original**: a verified output takes the original's
  path; the original is *renamed aside*, never deleted. Replaced originals are listed
  in the UI for the operator to delete manually. Nothing in ShrinkRay ever deletes a
  source file.
- **Watchdog**: stable new and changed files are scanned into the database and
  re-decided automatically; interrupted work is recovered; finished work on an
  unchanged file is never redone.
- **Audit**: sqlite records every scan, verdict, encode, and replacement, so any
  decision can be explained and any run repeated.
- **Authentication: none today.** The dashboard has no auth and must not be exposed
  beyond a trusted network. This is a deliberate development-stage trade-off and a
  known gap, not a design position — see the roadmap item for auth.
- HDR10 is supported; Dolby Vision dual-layer is not yet.
- VMAF scores are only comparable within one libvmaf version.

## Brand Commitments

Name: ShrinkRay (historical, kept). Voice: precise, honest about limitations, no
marketing fluff; documentation tone like the current README (professional, no emoji
decoration per user preference).

## Evidence on Hand

- Measured results on real content (x265 output; AV1 output is not measured yet):
  Django Unchained 5.55→1.98 GB (64.3%) at VMAF 95.25; Bloodshot 0.42→0.07 GB
  (82.7%); a parallel run of 3 movies in 34.3 min.
- An already-efficient HEVC source is correctly *skipped* rather than re-encoded.
- Working dashboard (Svelte 5, dark/light), an HTML report, and a per-module test
  suite as implementation evidence.
- Engine modules: `src/shrinkray/{quality,encode,watch,jobs,db,app}.py`
  (agreed target layout: `tools/`, `engine/`, `server/` subpackages — issue #63).

## Product Principles

1. **Measured, not guessed.** Every quality decision carries its VMAF number;
   nothing ships on hope, and a size estimate is labelled as an estimate.
2. **The library is sacred.** No source file is ever deleted by ShrinkRay. A
   replaced original is renamed and retained until the operator removes it.
3. **Two questions, answered honestly.** "Is this file bigger than its quality
   requires?" and "Does it carry black bars its content does not need?" — including
   the honest answer "no, leave it alone", recorded so it is never asked twice.
4. **Nothing outside its lane.** No naming, moving, organizing, matching,
   deduplicating, acquiring, or serving. If a feature needs to know what a file is
   *called* or *which* file it is, it belongs to another tool.
5. **Unattended by default, inspectable always.** It runs without a babysitter
   and explains every decision it made, on the dashboard and in the CLI.
6. **Built to be trusted by strangers.** Secure at every boundary, correct under
   interruption, and honest about failure — the standard in AGENTS.md, not a
   preference. Community software that mishandles someone's library is worse than no
   software.
7. **Honest boundaries.** Documented limitations (HDR, cross-version VMAF,
   missing auth) instead of silent misbehaviour.

## Accessibility & Inclusion

No specific requirements recorded yet; the dashboard is a single-operator LAN
surface. Baseline: keyboard-operable controls, readable contrast, and the dark/light
theme pair already implemented.
