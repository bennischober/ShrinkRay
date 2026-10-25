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

> **Direction (issue #244).** The deployment target is an **Unraid Community App**
> (PRODUCT.md "Deployment target"): the WebUI button opens the read-only dashboard,
> the Console button opens a shell that prints what to type (`shrinkray setup` to
> configure, `shrinkray encode /media/<path>` for one file). That changes three
> things below: `/data` becomes the appdata folder holding *all* operator state
> (config, database, pause flag, generated token), the image takes `shrinkray` as its
> entrypoint so the console can run any command (issue #242, **done** — see *One-shot
> runs*), and the fixed-UID decision is reopened for `PUID`/`PGID`.

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
- **Non-root by default** (`--build-arg UID/GID`), so everything written on a bind
  mount is host-owned.
- **The baked config carries the common video containers** (issue #204): the
  image's `config.json` sets `file.extensions` to `.mkv`, `.mp4`, `.m4v`, `.avi`,
  `.mov`, `.ts`, `.m2ts`, `.webm` — without it the code default (`.mkv` alone)
  made a mounted `.mp4` library scan nothing, and the container logged a warning
  and exited 0. The README documents the narrow code default.
- **Volumes:** `/media` (library, writable — Mode B encodes in place), `/data` (the database AND
  `config.json` - the app writes config next to the db), `/output` (encoded files for
  the non-replace flow), and `/tmp/shrinkray` (probe scratch, which is sized to the
  film rather than to the container layer).
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

1. **Publishing a port requires a token.** The app refuses to bind a non-loopback
   address without `SHRINKRAY_TOKEN` (issue #36), so the compose file passes one and
   the healthcheck sends it. There is still no TLS: terminate it at a reverse proxy,
   and do not read the token as making a plain-HTTP port safe.
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
2. **PUID/PGID.** A fixed non-root user built with `--build-arg UID/GID`, not
   entrypoint-driven user fixing. It is one `useradd` in the Dockerfile instead
   of a runtime step that can fail in ways a stranger has to debug.
   **Reopened by issue #244:** an Unraid template passes the user to run as at
   runtime (`PUID`/`PGID`, conventionally 99/100, plus `UMASK`), and a pull-and-run
   image cannot be rebuilt per host. The trade-off above still stands — a runtime
   user switch is a step that can fail — so the follow-up must make it fail loudly
   at start (unwritable `/data` or `/media` is a refusal, never a dead watcher).
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
  -e SHRINKRAY_TOKEN='a-long-random-secret' \
  -v /srv/media:/media \
  -v /srv/shrinkray-data:/data \
  ghcr.io/bennischober/shrinkray:latest
```

Or via compose (un-comment the `image:` line, comment the `build:` block):

```bash
SHRINKRAY_TOKEN=... MEDIA_DIR=/path/to/library docker compose up -d
```

The healthcheck calls `GET /api/status` with the token. `--root /media` is in the
default command because the default allowlist on Linux is the working directory, so
without it every library path is a 403.

The image's entrypoint is `shrinkray` and its default command is `serve --db
/data/metadata.db --host 0.0.0.0 --root /media --config /app/config.json` (issue #242).
Anything you pass after the image name **replaces that whole command**: to change a
server flag, spell out the full `serve ...` command, e.g. `docker run ... <image> serve
--db /data/metadata.db --host 0.0.0.0 --root /media --config /app/config.json --port 9000`.

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
- **The library must be writable by uid 1000** (the image's user) for an in-place
  encode. Runtime `PUID`/`PGID` is issue #248; running the image under another
  `--user` is untested, and its built-in scratch folder belongs to uid 1000.
- **The config is the image's** (`/app/config.json`: the common video extensions,
  `auto_backlog` is irrelevant to a one-shot run). Pass `--config /data/config.json`
  with a `/data` volume to use your own.
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
