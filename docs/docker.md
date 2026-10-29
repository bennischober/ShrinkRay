# Docker deployment

Status: **built** (issue #34) **and published** (issue #165). `Dockerfile`
defines two targets - `runtime` for deployment and `dev` for the gate - and
`docker-compose.yml` is the deployment shape. Every `v*` tag builds the
runtime image, smoke-tests it (boot + token-authenticated healthcheck) and
pushes it to `ghcr.io/bennischober/shrinkray:<tag>` and `:latest` with OCI
labels carrying the version - so the common install is `docker pull`, and the
source build is the development path. The trade-offs recorded below are the
ones the build actually had to respect; where a decision was made, it is
stated with the reason.

Shutdown (`docker stop`, compose down): the app's exit hook arms the shutdown
inside the signal handler (non-blocking - a join there would defer the second
Ctrl+C, uvicorn's force-quit escape), setting the SSE flag and the running
job's cancel event; the teardown itself runs once `serve` returns - the
worker's stop joins (2 x 30s), the child's kill window (terminate_tree's
SIGTERM/SIGKILL plus the pipe drains), the watcher's join (10s). The compose
comment on `stop_grace_period: 120s` carries the full budget (issue #431); a
raw `docker stop -t` below that kills the container mid-unwind, and the job is
recovered as failed (`interrupted by app restart`) on the next boot (the
fallback, not the contract). The stop signals (SIGTERM, and SIGBREAK on
Windows) are ignored for the whole window - uvicorn's capture, the re-raise
after it, and the teardown (a second SIGTERM mid-join would otherwise kill
the process with the CANCELLED row unwritten); a hard kill (`-9`, OOM) leaves
the job to the boot-time recovery instead.

> **Direction (issue #244).** The deployment target is an **Unraid Community App**
> (PRODUCT.md "Deployment target"): the WebUI button opens the read-only dashboard,
> the Console button opens a shell that prints what to type (`shrinkray setup` to
> configure, `shrinkray encode /media/<path>` for one file). That changes three
> things below: `/data` becomes the appdata folder holding *all* operator state
> (config, database, pause flag, generated token), the image takes `shrinkray` as its
> entrypoint so the console can run any command (issue #242, **done** — see *One-shot
> runs*), and the user is chosen at runtime with `PUID`/`PGID` (issue #248, **done** —
> see *Running as your user*).

## What shipped

- **Published to GHCR on release tags** (issue #165): a `v*` tag runs the
  release job, then builds the runtime image, smoke-tests it (boot +
  token-authenticated `/api/status` healthcheck — the image must turn
  healthy before anything reaches the registry) and pushes
  `ghcr.io/bennischober/shrinkray:<tag>` + `:latest` with OCI labels
  (`org.opencontainers.image.version` carries the tag, so `docker inspect`
  answers "what am I running"). No new secret: the workflow's built-in
  `GITHUB_TOKEN` with `packages: write`.
- **`Dockerfile`, two targets.** `runtime` is the deployment image; `dev` is the
  same base plus the dev group and Node, so the seven gate commands run in the
  environment they ship in.
- **`.dockerignore`** excludes credentials (`.opencode/`, `*.key`, `.env`), user data
  (`*.db`), media (`test_media/`), and build state (`.venv/`, `node_modules/`).
  A test asserts the exclusions still hold, because a bare `COPY . .` would bake a
  credential and a database into a layer readable forever.
- **A pinned, checksummed toolchain.** Stock Debian/Ubuntu ffmpeg is built
  without libvmaf, so quality mode cannot run on it at all - apt is not an option.
  ffmpeg comes from a dated BtbN release and HandBrakeCLI is compiled from the
  pinned source (no Linux CLI binary is published). Both are verified by sha256, and
  the build fails if `ffmpeg -h filter=libvmaf` fails or either version string
  does not match the pin: an image measuring with a different libvmaf would write
  scores that are not comparable with anyone else's.
- **Every command runs as the operator's user** (issue #248): `PUID`/`PGID`/`UMASK`
  at runtime, defaulting to 1000/1000/022 — see *Running as your user*.
- **The baked config carries the common video containers** (issue #204): the
  image's `config.json` sets `file.extensions` to `.mkv`, `.mp4`, `.m4v`, `.avi`,
  `.mov`, `.ts`, `.m2ts`, `.webm` — without it the code default (`.mkv` alone)
  made a mounted `.mp4` library scan nothing, and the container logged a warning
  and exited 0. The README documents the narrow code default.
- **Volumes:** `/media` (library, writable — Mode B encodes in place), `/data` (every piece of operator state — see *What lives in `/data`* below), `/output` (encoded files for
  the non-replace flow), and `/tmp/shrinkray` (probe scratch, which is sized to the
  film rather than to the container layer).
- **One config path, on the volume** (issues #236, #247): the image sets
  `SHRINKRAY_CONFIG=/data/config.json` and the server's command passes NO `--config`,
  so `/data/config.json` is the config for the server and for every one-shot command.
  The first command that finds it missing creates it from the image's baked defaults
  (`SHRINKRAY_CONFIG_SEED=/app/config.json`: auto_backlog on, the common extensions),
  written atomically and **never over an existing file** — so the operator's edits
  survive restarts and image updates, and a new image's defaults never replace them.
  If `/data` is not writable, the seeding failure is logged and the command runs on the
  code defaults. Before #236 the entrypoint's `--config` overrode the env var, so a
  mounted config was silently dead.
- **A saved config applies at the next file** (issue #247): the resident app re-reads
  `/data/config.json` between files — the watcher before each candidate, a plan or
  encode job before each movie — so a running encode finishes with the settings it
  started with and no restart is needed. An edit that does not parse or validate is
  **not applied**: the previous settings stay in effect, the error is logged once, and
  the dashboard shows it until the file is fixed. `resources.idle_priority` is the one
  setting that needs a restart (lowering a process's priority cannot be undone).
- **Healthcheck** on `GET /api/status`, which is the app's own summary; no new
  endpoint was needed. It lives in `tools/healthcheck.py` and checks only a server:
  a one-shot CLI container reports healthy rather than turning `unhealthy` for the
  hours an encode takes (issue #242). Docker still shows a *stopped* container as
  `unhealthy`; that is Docker's bookkeeping, not a failed check.
- **CPU quota is honoured.** `os.cpu_count()` reports the machine, not the
  container's share, so `resources.cpu_count()` prefers `os.process_cpu_count()`
  (3.13+) and falls back for 3.10-3.12. Without it a `--cpus=4` container would
  plan 24 parallel encodes.

## Two things a reader must know

1. **Publishing a port requires a token, and the image makes one.** The app refuses
   to bind a non-loopback address without a token (issue #36). Set `SHRINKRAY_TOKEN`
   to choose it yourself; leave it unset and the server generates one on first start
   into `/data/token` (`SHRINKRAY_TOKEN_FILE`, issue #249): 32 random bytes, file mode
   0600, reused on every later start, never logged. Read it from the appdata folder
   (`cat <data folder>/token`) and paste it into the dashboard once. Delete the file
   to have a new one generated. The healthcheck reads the same source. There is still
   no TLS: terminate it at a reverse proxy, and do not read the token as making a
   plain-HTTP port safe.
2. **The library is mounted writable because Mode B encodes in place.** The resident
   app's watch mode rewrites files at their own paths: the encode lands in a staging
   folder inside the library, is verified, and is then swapped in with the original
   renamed aside as `<name>.shrinkray-original` **inside the library** (never deleted -
   the sacred-source rule holds; the original lives beside the source so the final
   rename is same-filesystem and therefore atomic). `shrinkray retained list` and the
   dashboard's Retained page tell the operator what they may delete. A read-only mount
   disables the product: the watch loop crashes with `[Errno 30] Read-only file system`
   on its staging folder (issue #193 owns the startup refusal). `--replace` is a Mode A
   CLI flag and has nothing to do with the container; the compose file mounts `/media`
   writable (issue #198). For test containers and special setups that must keep
   `/media` read-only, `SHRINKRAY_WATCH_OUTPUT=/output` (issue #193, Zef-X's decision:
   writable default, env var for the exceptions) points the watcher's encodes at a
   writable volume instead - the app validates the path at boot and refuses to start
   when it is not writable, so the failure is loud, never a silent dead watcher.

## The ask

Run ShrinkRay in a container on a NAS/mini-PC: bind-mount the media
library (`/media`), let the existing watcher (or cron) drive the
scan/encode cycle, dashboard reachable over the network.

## Verdict

A good fit that is not free. ShrinkRay is subprocess-driven (ffprobe,
ffmpeg, HandBrakeCLI), headless-capable, stateless except sqlite +
config, and already a polite guest (nice, thread caps, pid/encode
locks, WAL). All of that ports cleanly into a container. What does not
port for free: ownership/permissions and the "second artifact to keep
fresh" cost. Details below.

## Why the container fits

- **Subprocess model is container-native.** The binaries just get baked
  into the image; the Python code only orchestrates.
- **Deployment simplicity is real.** `docker run -v /media:/media` plus
  one env/config volume is a lower barrier than any bare-metal path for a
  friend's NAS.
- **Reproducibility.** The exact toolchain (ffmpeg + libvmaf version,
  HandBrake version) is pinned per image instead of varying per host -
  VMAF scores are only comparable within one toolchain (README), so a
  pinned image actually *improves* measurement consistency.
- **Nearly container-ready already.** Env-var-based secrets (config.json
  names the env var), headless web dashboard, watch loop with stability
  checks and adaptive backoff, single-writer encode locks.

## What genuinely changes (caveats)

1. **Ownership/permissions move into the container.** The process runs
   as some UID that must read/write the bind mount. On Synology/QNAP
   this is the classic PUID/PGID dance (linuxserver-style images solve
   it with s6 / entrypoint user-fixing). Needs a deliberate design.
2. **Trash/recycle semantics break.** No longer applicable: ShrinkRay does not
   delete source files at all, and the last `send2trash` user (the duplicates
   delete route) is gone with issue #37. Retained originals are ordinary files on the
   library volume, so they work in a container without a host trash mount. This
   also simplifies the swap-in-place design — the original is renamed aside, not
   trashed.
3. **Scheduling becomes deployment, not code.** Watcher (preferred:
   stability checks, adaptive backoff) or cron both work in-container;
   either way some logic moves into the friend's runtime. Needs docs.
4. **Second artifact to keep fresh.** The no-PyInstaller decision
   (2026-10-22) rejected frozen binaries partly to avoid maintaining a
   second distribution artifact. A container image is a different kind
   of artifact (environment, not binary) but the maintenance cost
   argument still applies: every toolchain update = image rebuild +
   registry push. Needs an explicit amendment to the Decisions list,
   not silent drift.
5. **Image weight.** ffmpeg (full build ~80 MB) + HandBrakeCLI + Python
   + uv lands around 1-2 GB. Fine on a NAS with big disks; still a
   real download per update.
6. **Persistence design.** sqlite db + config.json belong in a volume
   (`/config`), the library stays on the bind mount; never bake state
   into the image (db is user data, not source).
7. **One container per output directory.** The pid/encode locks work,
   but they are per-output-dir files on the shared mount - two
   containers on the same output dir are the same conflict the locks
   already handle (EncodeBusyError), not a corruption risk. Document
   "one watcher per library" anyway.

## Decisions taken

1. **Where retained originals live.** Alongside the source, as
   `<name>.shrinkray-original` (issue #35). Not a configured mirror and not a
   `./retained` volume: the final rename must be same-filesystem to be atomic,
   and a fixed suffix works on any mount with no configuration. The operator
   reads the list with `shrinkray retained list` or the Retained page and deletes
   them in their own file manager.
2. **PUID/PGID at runtime** (issue #248, superseding the fixed build-time user).
   The first decision was a fixed non-root user built with `--build-arg UID/GID`,
   because a runtime user switch is a step that can fail in ways a stranger has
   to debug. The Unraid target (#244) reversed it: a template passes the user to
   run as (`PUID`/`PGID`, conventionally 99/100, plus `UMASK`), and a pull-and-run
   image cannot be rebuilt per host. The original concern is answered by failing
   loudly: every way the switch can go wrong (a non-numeric id, `PUID=0`, a
   `/data` or `/media` the user cannot write) is a refusal at start that names the
   folder and the uid, never a dead watcher. The defaults stay the build user
   (1000/1000), so an existing `/data` keeps working after an update.
3. **Docker is THE deployment channel.** The image is the NAS path; the
   systemd unit (`deploy/shrinkray.service`) was removed with the container
   path established (2026-10-25).
4. **Registry.** ghcr.io (issue #165): the release workflow builds, smoke-tests
   and pushes on `v*` tags; no extra secret is needed (`GITHUB_TOKEN` +
   `packages: write`). Docker Hub can be added as a second push target later
   without rework if discovery ever matters.

## Running it

Pull the published image (no build, no clone):

```bash
docker pull ghcr.io/bennischober/shrinkray:latest

docker run -d --name shrinkray \
  -p 8420:8420 \
  -v /srv/media:/media \
  -v /srv/shrinkray-data:/data \
  ghcr.io/bennischober/shrinkray:latest

cat /srv/shrinkray-data/token     # the generated dashboard token
```

Or via compose (un-comment the `image:` line, comment the `build:` block):

```bash
MEDIA_DIR=/path/to/library docker compose up -d   # SHRINKRAY_TOKEN=... to choose the token
```

The healthcheck calls `GET /api/status` with the token. `--root /media` is in the
default command because the default allowlist on Linux is the working directory, so
without it every library path is a 403.

The image's entrypoint is `shrinkray` and its default command is `serve --db
/data/metadata.db --host 0.0.0.0 --root /media` (issue #242). Anything you pass after the
image name **replaces that whole command**: to change a server flag, spell out the full
`serve ...` command, e.g. `docker run ... <image> serve --db /data/metadata.db --host
0.0.0.0 --root /media --port 9000`.

## Running as your user

Every file ShrinkRay writes on your disks belongs to the user you choose:

| Variable | Default | Meaning |
|---|---|---|
| `PUID` | `1000` | the user id every command runs as (`0` is refused) |
| `PGID` | `1000` | its group id |
| `UMASK` | `022` | the permission mask for new files and folders (`002` keeps them group-writable) |

On Unraid that is `PUID=99`, `PGID=100` (`nobody:users`, the owner of the shares).

How it works: the container starts as root only long enough to give its user these
ids, then runs the server or your command as that user. `docker exec <container>
shrinkray ...` runs as that user too, and so does an interactive console shell
(Unraid's Console button): it drops from root on start. For maintenance as root,
open it with `docker exec -it -e SHRINKRAY_ROOT_SHELL=1 <container> sh`.

What it changes on your folders:

- **A mounted `/media` is never touched.** It must be writable by `PUID`/`PGID` for the server's
  in-place encodes; otherwise the container refuses to start and says so (or set
  `SHRINKRAY_WATCH_OUTPUT`, see above). One-shot commands only need what they write.
- **`/data`, `/output` and the scratch folder are taken over while they are empty** (a
  `chown` of the folder itself, never recursive). That is how a fresh appdata folder
  arrives: Docker creates a missing host folder owned by root. A folder that already
  holds files is never changed: if `PUID`/`PGID` cannot write it, the container refuses
  to start and names the folder, the uid and the fix (`chown -R <PUID>:<PGID> <host
  folder>`, or the matching `PUID`/`PGID`).
- **`docker run --user`** bypasses all of this and runs as the given user directly; the
  folders must then already be writable for it.

## What lives in `/data`

`/data` is the appdata folder: everything the operator chose or the app learned, and
nothing that comes with the image. One flat folder, one file per kind of state:

| File | What |
|---|---|
| `config.json` | the settings (created from the image's defaults on first start, issue #247) |
| `metadata.db` (+ `-wal`, `-shm`) | the catalog, plans, encodes and retained originals |
| `token` | the generated dashboard token, mode 0600, when `SHRINKRAY_TOKEN` is unset (issue #249) |
| `paused` | present while the operator paused the backlog and the watcher (issue #250) |
| `manual-encodes/` | one marker per running manual `shrinkray encode`; the watcher waits for them (issue #250) |
| `logs/` | failure bundles, written beside the database |

New state goes into the same folder under a name of its own, never into the image and
never into the library.

## The console

Opening a shell in the container (Unraid's Console button, or `docker exec -it
<container> sh`) prints what to type, computed from the container's state (issue #251):
whether the server runs, `shrinkray setup` to configure, pause and resume (issue #252), how
to encode one file and list retained originals, and the dashboard's token. It prints once per console, for interactive shells
only, so `docker exec <container> <command>` output stays clean; the shell itself runs as
`PUID`/`PGID` (see *Running as your user*).

## Pausing

The resident app starts no new file while it is paused; the file it is working on
always finishes. Two things pause it (issue #250):

- **You.** `touch /data/paused` (or create `paused` in the appdata folder) pauses the
  watcher and the backlog; removing the file resumes them. It survives restarts on
  purpose: a reboot never resumes a backlog you stopped. `shrinkray setup` has the
  switch for it (issue #252).
- **A manual encode.** `shrinkray encode <path>` from the container console pauses the
  watcher and the backlog until it ends, so the two never compete for the CPU. A crash,
  a closed console or a restart cannot leave it paused: a marker whose process is gone
  is ignored and removed.

The dashboard shows the pause and its reason; it cannot pause or resume anything.

## One-shot runs (no server)

The image runs either mode. With no arguments it starts the resident server; with
arguments it runs one CLI command and exits. Nothing is installed on the host: ffmpeg
(with libvmaf) and HandBrakeCLI come from the image.

```bash
IMAGE=ghcr.io/bennischober/shrinkray:latest

# what would change, without writing anything
docker run --rm -v /srv/media:/media "$IMAGE" encode /media/Movies --dry-run

# encode one file in place: the original is renamed to <name>.shrinkray-original and kept
docker run --rm \
  -v /srv/media:/media \
  -v /srv/shrinkray-scratch:/tmp/shrinkray \
  "$IMAGE" encode /media/Movies/film.mkv

# predict without encoding
docker run --rm -v /srv/media:/media "$IMAGE" plan /media/Movies
```

A one-shot run needs no token, no published port and no database: it is the same
stateless CLI as a native install. Things to know:

- **Paths are container paths.** Mount the library at `/media` and name files as
  `/media/...`.
- **Mount scratch for anything film-sized.** Probes and verification write to
  `/tmp/shrinkray`; without a volume that lands in the container's writable layer.
  A run killed hard (SIGKILL, power loss) leaves its workdir there; `shrinkray
  doctor` lists stale probe-scratch directories with their age and size
  (report-only - it never deletes anything), so the operator can decide about
  the debris (issue #440).
- **The library must be writable by `PUID`/`PGID`** (default 1000/1000) for an in-place
  encode; pass `-e PUID=... -e PGID=...` for the user that owns it (see *Running as
  your user*).
- **The config is `/data/config.json`**, as for the server: mount the same `/data`
  volume to share the server's settings. Without a `/data` volume the run creates a
  throwaway copy of the image's defaults inside the container (issue #247).
- **A stateless run records nothing**, so `shrinkray retained list` has no database
  to read: the original sits beside the encoded file as `<name>.shrinkray-original`.
  To list what the *resident* app retained, run `retained list --db /data/metadata.db`
  with its `/data` volume mounted.
- **Detach anything long.** An encode runs for as long as the container does; start it
  with `-d` instead of `--rm` in the foreground and follow it with `docker logs -f`, so
  a closed terminal cannot take it down mid-run. A cancelled encode leaves the source in
  place: the swap only happens once the encode has finished.

## Observing it (issue #168)

ShrinkRay is an observable source, not its own sink: it exposes the numbers, and
you point your own stack at them. Nothing is pushed anywhere and the container
opens no outbound connection.

`GET /metrics` serves the Prometheus text exposition format. Point a scraper at it:

```yaml
scrape_configs:
  - job_name: shrinkray
    static_configs:
      - targets: ['shrinkray:8420']
    authorization:
      credentials: 'a-long-random-secret'
```

The route sits behind the same `SHRINKRAY_TOKEN` gate as the rest of the API, so a
scrape without the bearer token gets a 401. It exposes job counts by state and
kind, queue depth, whether a job is running, encodes and bytes reclaimed, the
stall-watchdog count, retained-original totals, the catalog size and the free
space on the library filesystem. Every value is a query over state that already
exists - the route measures nothing new.

## Not problems

- Resource politeness: `nice` works in containers; thread caps are
  config; RSS self-monitoring reads /proc (works on Linux).
- `os.startfile` (open dashboard) is Windows-only and already guarded.
- Network: the dashboard is a plain web UI, ports are publishable - with the
  token above.
- Tests: the suite runs without ffmpeg (by-design skips), and the `dev` target
  carries the pinned toolchain so the smoke tests can run there too.

## Two-copy caveat

Two containers pointed at one output directory are the same conflict the
pid-stamped encode locks already handle (`EncodeBusyError`), not a corruption
risk. Still: one watcher per library.

## Related

- The changelog's decisions (CHANGELOG.md) - the no-PyInstaller decision
  this extends
- GitHub issue #35 (swap-in-place with a retained original) - decides where
  retained originals live, which sets whether the volume story needs a separate
  mount. Decide the two together.
