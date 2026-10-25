# Changelog

All notable changes to ShrinkRay are documented here. Releases are tagged in git; the GitHub release for each tag carries the same notes.

## [Unreleased]

### Removed

- **The bare-metal systemd unit is removed; the container is the deployment path** : `deploy/shrinkray.service` (and the `deploy/` folder with it) assumed a uv-from-source install on a user's home directory — a path nothing documents as supported anymore now that the ghcr image is the NAS shape. The README's headless section points at the container run, and docs/docker.md records the decision. Nothing consumed the unit file (no CI, test or build reference).

- **The CLI is one product: `encode`, `plan`, `serve`, `retained`, `doctor`** (issue #202): the nine-command surface was sediment from three earlier products, and three of the commands contradicted the product contract — `encode` without `--quality` ran an unmeasured fixed-preset encode that could take a library file's path, `plan --fixed` advertised a mode that did not exist, and `watch-mode` was a second watcher beside the resident app's that could not share its database. Removed: the fixed-preset encode path (`encode` is always VMAF-measured now; `--quality` is accepted and ignored, `--fixed` too), `scan` (the catalog wrote `movies.txt`/`output.csv` artifacts no product surface reads; scanning stays the resident backlog's first job), `report` (the CLI command AND the dashboard's `/report` route — the fossil per the issue's comment; the provenance surface is the dashboard's per-title chain), `compare` (its provenance chain is the dashboard's), and `watch-mode` (the resident `serve` owns watching). `app` is renamed to `serve` with `app` kept as a hidden alias, so the container entrypoint keeps working. `doctor` stays as the maintainer's health check (per the issue's comment, it is debugging and stays undocumented in PRODUCT.md).

### Added

- **The encode settings are documented against a reference HandBrake preset** (issue #240): `docs/handbrake-preset-reference.json` is a verbatim export of a working HandBrake preset that handles the non-video half of an encode the way ShrinkRay should (Matroska container, every audio track copied, every subtitle track kept, chapters and metadata preserved), and `docs/encode-settings.md` is the key-by-key contract derived from it — which key we already match, which we deliberately differ on and why (the measured CRF, the probe/final preset split, the HDR encoder, the crop rect, no resolution cap), which we must never adopt (burning subtitles, the deinterlacer, a stereo mixdown), and which are gaps still open (#240: the container is autodetected from the output extension instead of pinned to MKV, `mp2`/`opus` are transcoded instead of copied, the fallback is lossy AAC instead of lossless FLAC, and nothing verifies that audio/subtitle tracks survived). The JSON is a reference artifact, not a runtime dependency: nothing in `src/` reads it, because importing a preset would make its language lists filter tracks invisibly and would let the installed HandBrake version silently rewrite the settings.

- **The resident watcher's output directory is configurable via `$SHRINKRAY_WATCH_OUTPUT`** (issue #193): the resident app hardcoded in-place encoding, so a read-only `/media` (test containers, special setups) crashed the watch thread on its staging folder with no way to point the encodes elsewhere — the operator could mount writable (the default, per #198) but nothing more. The env var points the watcher at a writable volume; the app validates the path at boot (creates it, checks writability) and refuses to start on a bad path, so a typo fails loudly instead of killing the watcher hours later. Zef-X's decision on the issue: writable `/media` by default, the env var for the exceptions.

- **One timeline-based crop detector replaces the two bar measurements** (issue #181): `plan`/`encode` now measure bars with a single keyframe-only cropdetect pass over the whole runtime (`-skip_frame nokey`, the #131 spike's measured ~3 min/hour of runtime), filtered to segments sustained for at least 30 s (counted in seconds, never readings, with a 3-reading floor) and cropped only where every sustained segment agrees — the safe union, so a dark-scene flash or an isolated logo can never shrink the picture. The result is cached per file version in a new `crops` database table (schema v16, keyed by size + mtime), `scan`'s 1/0 flag becomes the cached summary's projection when one exists (the cheap probe stays for never-planned files and scan never runs the expensive pass), and the report's bar note shows the sustained picture-size breakdown (e.g. "1920x800 (60% of the time)") instead of the generic pre-filter wording. Stateless runs (no `--db`) keep the previous sampled-window detector as fallback.

- **`watch` wakes on filesystem events instead of only polling** (issue #176): between passes the loop sleeps on an inotify waiter (Linux) that fires on file creation, close and moves anywhere under a watched root, so a movie that lands in the inbox starts encoding within seconds instead of after the backoff window. The adaptive poll stays the fallback and the reconciler — a wake never bypasses the stability check — and platforms without a supported event API (and every setup error) degrade to exactly today's polling behavior.

### Fixed

- **The release workflow parses again** : the gate step's name introduced with the gate-script change carried an unquoted `: ` inside the YAML mapping value, which made the whole workflow file unparseable — GitHub registered that as a startup failure with zero jobs on every push to any branch (8 consecutive red runs), and the next `v*` tag release would have failed the same way. The name now uses a dash; every workflow file and the compose file parse clean.

- **The published container sees the common video containers, not just `.mkv`** (issue #204): the image's baked `config.json` now sets `file.extensions` to `.mkv`, `.mp4`, `.m4v`, `.avi`, `.mov`, `.ts`, `.m2ts`, `.webm` — before, the code default (`.mkv` alone) made a mounted `.mp4` library scan nothing while `scan`/`encode` still exited 0. The README states the narrow code default explicitly.

- **`plan` exits 1 when nothing could be planned** (issue #203): `encode` already failed loudly on an unusable toolchain; `plan` rendered the identical failure as an error row and exited 0. Now an all-error plan is a failed command, a plan with results alongside error rows still succeeds (a partial answer is an answer), and an all-skipped plan stays at 0 (a skip is a decision, not a failure).

- **`plan --no-table` prints the plan without the rich table** (issue #201): `scan` already had the flag for scripted runs; `plan` — the other read-only, scripted command — now accepts the same `--no-table` (no box-drawing characters or ANSI codes in a pipe), while the summary line and the `--db` plan cache keep working unchanged.

- **`scan` fails honestly when every file failed to read** (issue #196): the run guarded its output on the FOUND list, so an all-error scan exited 0 and wrote a fabricated report (`Total;None;0` in the CSV). Now the outputs (table, `movies.txt`, CSV) are written only when at least one file was read, an all-error scan exits 1 with a summary naming the count, a partial scan keeps exit 0 with its per-file errors, and the empty-folder case keeps its warning and exit 0.

- **A missing ffprobe/ffmpeg is reported as missing, not as "Permission denied"** (issue #195): on WSL a failed execve of an absent binary surfaces as `PermissionError [Errno 13]`, so the raw-OSError interpolation sent the operator to chmod a file that does not exist. `run_ffprobe` and `detect_vmaf_style` now catch `FileNotFoundError` (names the binary and the install action) and `PermissionError` (names the executable-bit cause) separately; every other failure keeps its old message.

- **The container docs no longer promise that a read-only library mount is safe** (issue #198): the paragraph attributed the container's behavior to `--replace`, a Mode A CLI flag the `shrinkray app` entrypoint does not have, while Mode B encodes in place and needs the mount writable — a read-only mount crashes the watch loop on its staging folder. The compose file and both doc examples now mount `/media` writable, and the docs state the real contract (in-place rewrite, original renamed aside as `<name>.shrinkray-original`, never deleted).

- **A filename containing `[` no longer breaks `scan`** (issue #200): any path with a bracket was treated as a glob, so an existing file named `with [BRACKETS] name.mkv` — the target library's common naming — was reported as matching no movie file. The resolver now treats `[` as glob syntax only when the path does not exist AND a `[`-pattern expansion finds files; a real file or folder with brackets scans exactly as named, and a nonexistent bracketed path still errors with "does not exist".

- **A one-file command touches exactly that file** (issue #197): all three commands (`scan`, `plan`, `encode`) resolved a named file to its PARENT folder and rescanned it, so on a flat library `encode a.mkv` batch-encoded everything beside it. `resolve_input_paths`' explicit file list now flows through `scan_movies`' new `files` parameter instead of being discarded; a folder still scans the folder, a glob still scans its matches, and the server's `resolve_movies` behavior (which already did this right) is now the CLI's too.

- **The commands preflight their toolchain** (issue #194): `toolchain_problems()` never checked ffprobe, and only `doctor`/`run_app` called it — a host without ffprobe passed the doctor clean and then failed every file in `scan`/`plan`/`encode`, one error line at a time (scan exited 0). The check now includes ffprobe, the entries are grouped by prerequisite so each command gates on what IT needs (`scan`: ffprobe only; `plan`/`encode`: everything), and the refusal happens before any work, mirroring doctor's wording.

### Changed

- **The product direction is an Unraid Community App with a terminal control surface** (issue #244): PRODUCT.md gains a "Deployment target" section and a "terminal is where the operator configures and controls" section, and AGENTS.md, `docs/tui.md`, `docs/docker.md`, the README and the per-directory knowledge bases follow it. The web dashboard stays read-only; a planned `shrinkray setup` TUI, reached through the container console, configures everything (required VMAF, thread caps, eligibility gates — the 1080p floor may be raised, never lowered) and pauses or resumes the backlog through state in `/data`, never through a network route. A manual `shrinkray encode <path>` is the single-file path and pauses the resident watcher while it runs. `/data` holds every piece of operator state, the token is generated when absent, and the fixed-UID image decision is reopened for `PUID`/`PGID`. Documentation only: none of it is built yet, and each part is its own follow-up issue.

- **The agent knowledge base is split into per-directory `AGENTS.md` files** (issue #223): the root file keeps the product rules, the engineering standard, the gate and the workflow, and points at one file per directory that earns one — `src/shrinkray/`, its `engine/`, `server/` and `tools/` subpackages, `tests/` and `ui/`. A directory map inside the root file could not stay true as the code moved; a file next to the code it describes is updated when that code changes. This pass also corrected the root file where it had drifted from the tree.
- **The README documents the gate chain as a table** : "which files get encoded?" was answered only inside docs/encode-decision.md's walk-through; the README now carries the eight-gate table (duration, resolution, size floor, codec age, bar detection, CRF search, size gate, encode) with each gate's config key and default, plus the two clarifications that keep coming up — the "not worth it" gates (rows 2-4) fire before any ffmpeg probe runs, and "already efficient" is a measured outcome (row 7), not a threshold. Cross-referenced with the open old-codec-with-bars decision (issue #224).

- **CI tests the supported floor and the shipped interpreter, not three points between** : the test matrix drops Python 3.12 and keeps 3.10 and 3.15 — 3.10 enforces the `requires-python = ">=3.10"` promise (PR #217 proved its worth when 3.15-local missed an annotation NameError that 3.10 caught), 3.15 is the interpreter the container actually ships, and 3.12 sat between two tested versions catching almost nothing at ~90 s of CI time per push.

- **The gate, the container config and the release notes each live in one place** : the five gate commands are now `tools/gate.sh`, called unchanged by ci.yml, release.yml, the dev image's CMD and documented in AGENTS.md (four copies of one contract become one); the container's config.json is a tracked `container-config.json` COPYed into the image instead of JSON smuggled through shell escapes in a `printf` (the old form had to be parsed back out of the Dockerfile to be tested); docker-compose.yml drops its byte-identical healthcheck block (compose inherits the image's token-authenticated HEALTHCHECK); and the release job extracts the release notes with tested `tools/release_notes.py` instead of inline awk/sed pattern matching on CHANGELOG.md (same section extraction, same fallback for a mis-tagged release, now unit-tested).

- **The helpers grab bag is dissolved** : `helpers.py` (190 lines, seven functions) is gone — the #63 decision record's dissolution is complete. Two functions were already dead since the CLI rework (the `movies.txt`/`output.csv` formatters), and the remaining five moved verbatim to their single consumer modules (`engine/scan.py` owns the size/time/progress helpers, `__main__.py` owns `calc_time`); the dead `print_table`/`print_summary` went with them. One module fewer, no behavior change.

## [0.4.0] - 2026-10-25

### Added

- **Retained originals are surfaced on the overview and in the report** (issue #182): the overview gains a retained-originals panel — count and total bytes held aside, linking to the Retained page — whenever present rows exist, and the report's summary cards gain the same totals. Purely additive reporting: no delete button, no auto-cleanup, the sacred-source rule is untouched.

- **`shrinkray watch-mode` renders a live status panel** (issue #178): watched roots, output mode (in place vs output dir), disk free against the floor, and the current activity with a percent bar while encoding — fed by the engine's own progress events. `--no-live` restores the plain log stream, and a non-TTY session degrades automatically. No new dependency (rich was already core).

- **The pull-first install path is documented** (issue #175): the README's Container section and docs/docker.md's Running-it section lead with `docker pull ghcr.io/bennischober/shrinkray:latest` + a single `docker run` (mounts + token), build-from-source is the development fallback, and the compose file's published-image line is ready to un-comment. docs/docker.md records the registry decision and the tag scheme.

- **`scripts/verify_fullmovie.py` runs from anywhere uv exists** (issue #179): the script carries PEP 723 inline metadata, so the manual full-movie VMAF check works against an installed ShrinkRay without the dev checkout — which is where real-media verification actually happens. The pass also fixed a latent break: the script still imported `measure_vmaf` from `shrinkray.quality` (deleted in the #97 module move) and has been unrunnable since; it now imports from `shrinkray.tools.vmaf`.

- **A container image for development and headless runs** (issue #34): one `Dockerfile` with a `runtime` target (the deployment shape) and a `dev` target that runs the gate inside the image, plus a `docker-compose.yml`. The toolchain is pinned by version **and** sha256 - stock Debian/Ubuntu `ffmpeg` is built without libvmaf, so quality mode cannot run on it at all, and an unpinned build makes stored VMAF scores incomparable across machines. The build fails unless `ffmpeg -h filter=libvmaf` succeeds and both version strings match the pins.

- **`resources.cpu_count()` respects a container's CPU quota.** `--cpus=2` on a 24-core host must plan 2 workers, not 24. Measured: inside a 2-CPU container `os.cpu_count()` *and* `os.process_cpu_count()` both still report 24, so the quota is read from the cgroup (`cpu.max` on v2, `cpu.cfs_quota_us`/`cpu.cfs_period_us` on v1) with the machine count as the fallback. Without it a quota-limited container plans a 24-way parallel encode inside its quota and the encodes thrash.

- **Swap in place with a retained original** (issue #35): `encode --replace` moves a verified output into its source's path and renames the original aside as `<name>.shrinkray-original` - **kept, never deleted**. The library is sacred: ShrinkRay has no code path that removes a source, and the only `unlink` in `replace.py` removes a staging file it created itself.

- **A retention ledger** (schema v14, `retained` table) records what each swap replaced: source path, retained path, the encode that superseded it and both sizes. `shrinkray retained list` and the dashboard's Retained page read it, so the operator can reclaim that space manually, in their own file manager. There is deliberately no delete button.

- **Crash recovery for an interrupted swap**: the swap is two same-directory renames, so after a crash at any instant the source path holds either the original or the new file, never neither. `shrinkray retained recover --dir <folder>` resolves the leftovers, and the app does it for every allowlisted root at startup.

- **Optional bearer-token authentication for the dashboard** (issue #36). `SHRINKRAY_TOKEN` turns auth on; every route then requires `Authorization: Bearer <token>`, the SSE stream also accepts `?token=` (an `EventSource` cannot send a header), and the UI asks for the token once and keeps it in the browser. The guard is a middleware, not a per-route dependency, so a route added later cannot be left unauthenticated by forgetting a decorator.

- **A non-loopback bind without a token is now a startup failure**, not a silently open dashboard. Binding `0.0.0.0` with no `SHRINKRAY_TOKEN` raises with the variable named in the message. The loopback development flow is unchanged: no token, no prompt.

- **The container image is published to GHCR on release tags** (issue #165): a `v*` tag runs the full gate, publishes the Python release, then builds the runtime image, **smoke-tests it** (boot + token-authenticated `/api/status` healthcheck) and only then pushes `ghcr.io/bennischober/shrinkray:<tag>` and `:latest` with OCI labels. The smoke test caught three real defects before anything shipped: two missing shared libraries (`libmp3lame0`, `libvorbisenc2`) that made HandBrakeCLI unloadable and the image refuse to boot; a healthcheck that did not send the token (401 forever); and `supports_svt_av1()` reading the encoder probe's preset list from stdout when HandBrakeCLI writes it to stderr, which made a healthy toolchain report as unusable.

- **Toolchain preflight** (issue #132): the resident app and `shrinkray doctor` refuse to start on a toolchain that cannot do the product's one job — HandBrakeCLI missing or without `svt_av1`, or VMAF unmeasurable — naming the missing capability and the version that provides it, instead of failing confusingly mid-run.

- **A fresh container starts the backlog by itself** (issue #126): `auto_backlog` runs scan → plan → encode over the mounted library on boot, so the deployment shape is genuinely fire-and-forget. Off by default outside the image.

- **Watch mode is a real resident loop**: it walks the whole library tree instead of one inbox folder (issue #133), encodes in place like the CLI (issue #127), and every new or stable-changed file is re-scanned and re-decided.

- **The CLI accepts a file, a folder or a glob** (issue #123) and is stateless unless `--db` is passed (issue #125): a one-shot run needs no database, no config, no resident process. `encode` writes in place when no output directory is given (issue #124), retaining the original per the sacred-source rule.

- **VMAF 96 is the default target, derived everywhere** (issue #103): every CLI option, the API model and the job payload derive from `DEFAULT_TARGET_VMAF` instead of repeating a literal; the HDR target is expressed as a difference (`HDR_VMAF_OFFSET`) so the two cannot drift.

- **The dashboard is a status view, not a control surface** (issue #134): no route or button starts, cancels or reconfigures work.

- **`shrinkray doctor`** — a library health check (db integrity, missing toolchain, config validity, allowlisted roots) before work starts.

- **Post-encode verification (`--verify`)**: a full-file VMAF measurement of the output against the source as ground truth, recorded with the encode; a failed verification is non-fatal and reported.

- **Encode guards**: a disk-space preflight refuses what the drive cannot finish (A2); a disk watchdog cancels rather than filling a shared drive; a stall watchdog with silence-proof cancellation and tree-safe termination; interrupted-encode debris is re-encoded, not skipped forever (A1).

- **Live progress everywhere**: SSE stream replaces DB-as-bus (progress is flow, not state), the CLI renders a progress bar for quality-mode encodes (A4), plan jobs stream their steps, and the jobs ETag covers RUNNING progress so bars do not freeze mid-encode.

- **Measured-data charts on the dashboard**: bitrate drift (a descending staircase with real pixels and labeled VMAF lane), the reclaimable figure, and a memory sparkline; the detail page shows the stored plan — predictions where the decision lives.

- **Config validated with a pydantic schema** (#61): a bad `config.json` fails at load with the offending field named, and one config location is shared by dashboard and engine (issue #58); writes are validated first and atomic (issue #60).

- **`config.json` written atomically** (issue #60): validate first, write to a temp file, rename — a crash mid-write leaves the old config, never a half-written one.

- **Plan cache across runs** (schema v9-#14): a valid stored plan skips probing entirely (`crf_source: plan`); the fingerprint keys on encoder, presets, target, CRF range, sample count, crop-sample count and the statistic, so any settings change re-probes. The detail page shows the prediction where the decision lives.

- **Bracketing interpolation in the CRF search** (issue #23): the binary search narrows with interpolated midpoints between measured brackets, fewer probe encodes for the same decision quality.

- **Probe encodes honour the configured thread cap**, and the cap reaches SVT-AV1 as `--encopts pin=N` (the x265-style `threads=N` is silently ignored by SVT's parser).

- **Crop detection samples ten windows** (issue #17, configurable via `--crop-samples`) and crops only bars every sample agrees on — the safe direction for an irreversible crop.

- **The scanner gates on resolution, size and codec age** (issues #18, #19, #20): below 1080p, below a size floor, or older than H.264 is skipped with a reason before any probe cost; an unreadable value is probed rather than silently dropped.


- **Probe density is duration-based (`sample_every`)**: `quality.sample_every` (default 600s) and `--sample-every`/`--sample-count` derive the probe window count from the runtime, so coverage is a fixed fraction of the file (`sample_seconds / sample_every`) instead of shrinking as the movie gets longer (issue #14). A count-based knob made coverage inverse to runtime: the longer the movie, the more scenes it has and the less of it got probed. `--sample-count` overrides the density when set explicitly, so a latency-sensitive caller (watch mode) can still pin a fixed probe budget. The derived count is resolved before the plan lookup, so it is recorded as the plan's `sample_count` and a changed density misses the cache.

- **Ten cropdetect windows per movie, configurable**: `ffmpeg.crop_sample_count` (default 10) and `--crop-samples N` raise the crop detector's coverage from three samples to ten (issue #17). With the bars common to every sample, the crop is monotone non-increasing in the sample count, so more samples only ever shrink it - the safe direction for an irreversible crop - and an aspect-ratio switch is far less likely to be missed. The count is part of the plan fingerprint (schema v11), so a plan made with three crop samples is not reused by a run that samples ten. Cost is 10 x 2s of cropdetect per movie when `--remove-bars` is set.

- **The source-codec gate (PRODUCT.md's candidate rule)**: an H.264 source is a candidate on size alone, while an HEVC or AV1 source is already efficient and becomes a candidate only when it has black bars to remove (issue #20). Anything older than H.264 is never re-encoded. An HEVC/AV1 source without `--remove-bars`, or with no bars actually detected, is skipped as `already efficient`; the bars that justify its encode are detected once by the gate and then applied, rather than sampled a second time. `eligibility.candidate_codecs` (default `["h264"]`) names the codecs that are candidates on size alone. An unreadable codec is probed rather than excluded: an unknown value must never silently drop a file the operator expected to be considered.

- **A minimum-size gate**: `eligibility.min_source_size_gb` (default 0, off) skips a source below the configured size with the reason `below min size`, before sample extraction or any probe encode (issue #19). Probe cost is roughly constant per file while the payoff scales with the file's size, so the smallest files have the worst cost/benefit ratio; the gate reads one stat and decides from it. It is off by default because the right floor is a property of the operator's library, not a universal truth: a 3 GB movie can still save 1.5 GB, and a 20 GB movie that is already efficient saves nothing.

- **A minimum-resolution gate**: `eligibility.min_source_height` (default 1080, matching the floor PRODUCT.md states) skips a source whose height is below it with the reason `resolution too low`, before any sample extraction or probe encode (issue #18). A lower-resolution source has too few pixels to save meaningfully, so the probe cost buys nothing, and the gate decides from cached metadata instead of a new probe. The gate is off when set to 0, and an unreadable height is probed rather than excluded: an unknown resolution must never silently drop a file the operator expected to be considered.

### Changed

- **The module layout is layered and machine-checked** (issue #97): the flat package is now `tools/` → `engine/` → `server/` + CLI, enforced by import-linter as the seventh gate command (`uv run lint-imports`) in the local gate and CI; all transitional shim modules are deleted. The module map lives in `src/shrinkray/AGENTS.md`.

- **Probe every eligible codec; bar removal is part of every encode** (issue #128): the codec gate no longer lets a modern codec skip the search, the `--remove-bars` flag is gone from code, CLI, API and the plan fingerprint, and the size check runs at the CRF the search chose (issue #129) — an encode that would not shrink the file is refused (issue #130).

- **One config.json for dashboard and engine** (issue #58): both read the same file (the dashboard wrote a second config the engine ignored).

- **Config writes are validated and atomic** (issue #60): a bad value fails at the API boundary with the field named, never half-written.

- **Probe samples are stream-copied, not re-encoded** (issue #16): the lossy x264 pass biased every VMAF measurement; the copy keeps bit depth, colour tags and HDR side data, with a logged lossless ffv1 fallback for containers that cannot hold the source codec.

- **Every short-lived subprocess call has a timeout** (issue #62).

- **The retry replays the stored payload** (issue #100): payload paths union `path` and `paths`, so a retried job re-runs what the original ran.

- **The HDR size estimate is labelled with its measured bias** (issue #98), and the effective HDR target/encoder is recorded on the result and the plan, not the caller's assumption.

- **`record_plan` refuses a file changed between probing and recording** (mtime/size check), so a plan cannot be stored under a fingerprint the file no longer matches.

- **Console timestamps carry the date and seconds** (issue #167): `YYYY-MM-DD HH:MM:SS` local time in the console log (the resident mode runs for days; a time-only stamp loses the day the moment the log leaves the terminal); the report's generated line gains seconds (UTC).

- **The strict typing bar is enforced on every module** (issue #40): mypy runs `disallow_untyped_defs`, `warn_return_any`, `strict_equality` and friends over the whole package; `py.typed` ships in the wheel.

- **POSIX tree-kill via process groups** unblocks Linux CI; the tree-safe termination applies to every encode path.

- **The report creates its output directory and shows the verification column.**

- **AV1 is the only output codec**: SDR encodes with `svt_av1`, HDR with `svt_av1_10bit` (issue #21). x265 is gone from every path - not demoted to a fallback, not kept as an option. `encoder_for(hdr)` is the single source of the choice; the six HDR ternaries that used to be written per call site are replaced by it, and `handbrake.encoder` is a two-value assertion that rejects a config still naming x265 at load rather than silently producing H.265.

- **The thread cap reaches SVT-AV1 as `--encopts pin=N`**: `threads=N` is an x264/x265 option, and SVT-AV1's parameter parser silently ignores an unknown key. `pin` restricts execution to the first N cores, which is what the cap means here.

- **The decision statistic is the mean, not the minimum**: `probe_crf` aggregates the per-sample VMAF means with the mean instead of the minimum (issue #15). A minimum over samples is monotone non-increasing in the sample count: every window added can only lower it or leave it unchanged, which made it structurally hostile to the denser sampling #14 introduced - the better the coverage, the more likely one pathological window sank the whole score, pushed the search to the floor CRF and reported "target unreachable" for a film that was perfectly encodable. `achieved_vmaf` is now documented as the mean across probe windows, which is a better predictor of overall viewing quality; a user comparing runs across the change should know it is not the same statistic. What the mean gives up is stated plainly: "every probed window meets the target" is no longer literally true. Denser sampling makes a genuinely bad scene more likely to be observed and to move the average, but does not eliminate it.

- **A skip now carries its evidence**: `SkipEncode` carries the best CRF, the VMAF measured there and the probe count, and `save_skip` stores them on the encode row. Previously an `encode` skip dropped that evidence, so the next run could not tell "already probed at the floor and failed" from "never probed" and re-probed the same window every time.

- **A plan recorded under `min` is not reused as a `mean` one**: the aggregation is part of the plan fingerprint (schema v13), because a plan made under the minimum was a different decision. Existing rows default to `min`, which is what they actually measured.


- **`plan` extracts each probe window once instead of three times**: the sample windows are extracted up front and the same list is handed to the CRF search and to the reported size factor (issue #30). `find_optimal_crf_and_skip` used to extract its own copy for the search while `plan_quality` extracted a second one for the size estimate, and the size estimate used only the first sample of that second list, so every window was decoded and written more than once per plan. With the duration-based sample counts that multiplier applies to a larger base cost. A fresh `encode` extracted twice for the same reason.

- **The typing, lint and coverage bar is enforced, not aspirational**: every module under `src/shrinkray/` is now fully annotated and mypy runs with `disallow_untyped_defs`, `disallow_incomplete_defs`, `warn_return_any`, `no_implicit_reexport` and `strict_equality` (issue #40). `py.typed` ships in the wheel, ruff runs the `S` (bandit) rules with each exception carrying its reason, and coverage has a floor so a regression fails instead of sliding. The per-module mypy rollout list is gone: one config applies to all of it.

### Fixed

- **A stall watchdog that killed healthy encodes**: piped HandBrake never sends newlines, so the newline-based liveness check starved and killed running encodes; the watchdog now reads progress correctly and stall telemetry reaches every encode path.

- **The encode result survives an absent ffmpeg** (CI run 160): version metadata probing is guarded, so verification metadata never fails an encode.

- **Watch mode's recursive/liveness traps** fixed, with prompt app shutdown on stop.

- **UI fixes**: readable primary hover; the two audit trails link to each other; a job trail shows the movie's own outcome inside a batch job; signed colored deltas, one unit spelling, one VMAF precision; drift chart geometry was lying (real pixels, labeled lane, visible marks); drift summary crashes; derived out-bars dressed as measured; `driftW` declared before use.

- **Test-suite fixes**: the test_db job import follows the server/ move; the `logs/` telemetry-leak fix (a verify run without a db file no longer writes failure bundles into the working directory, `tests/test_telemetry_leak.py` pins it); the CodeQL sanitizer follows the server/ move (issue #55).


- **Probe samples were re-encoded with lossy libx264**: sample extraction now stream-copies the source (`-c:v copy`, keyframe-aligned, timestamps normalised with `-avoid_negative_ts make_zero`) instead of running a CRF 12 x264 pass (issue #16). The samples are both the VMAF reference and the probe encode's input, so the re-encode biased every measurement: it smoothed grain and fine detail, which made the probe source easier than the real one and pushed the chosen CRF too high. The copy also keeps the source's bit depth, colour tags and HDR side data, which the x264 pass dropped. If a container cannot hold the source codec the extraction falls back to a lossless `ffv1` re-extract and logs that it did, rather than silently encoding lossily.

- **A batch encode's live progress was silently dropped**: `encode_movies` rebound its `progress` parameter with its own rich progress bar, so a resident app job never streamed a percent for a fixed-preset batch. The caller's callback now receives every event alongside the bar.

### Removed

- **The orphaned PosterCard component and its design tokens** (issue #180): `PosterCard.svelte` was created in the round-2 redesign, demoted (DESIGN.md's own note: 354 near-identical typographic tiles differentiated state but not identity), restored by accident, and never wired into a page. Gone together with the unused `--poster-ground` / `--poster-line` tokens; DESIGN.md's section is marked historical. Recoverable from git history if the poster grid ever returns.

- **Duplicate detection and the only destructive route**: `dedupe.py`, the `/api/duplicates` read route, `/api/duplicates/delete`, the Duplicates page, the `dedupe` job kind, and the `send2trash`/`rapidfuzz` dependencies are gone (issue #37). Deduplication is library management, which PRODUCT.md puts outside this tool's scope, and the delete route was the one code path in ShrinkRay that could remove a file from the library. A `metadata.db` that still holds `dedupe` rows keeps them as history: they read back as a generic `legacy` job, and a `queued` one is retired as failed at startup with the reason recorded instead of being executed.

- **Jellyfin integration**: the opt-in library-refresh notification after encode batches is gone (issue #43). ShrinkRay changes a file's bytes; telling another application about it is that application's job — and the integration was the only outbound network call in the codebase and an unauthenticated path to the process environment (any dashboard visitor could point `integrations.jellyfin.url` at their own host and exfiltrate any environment variable's value as a request header). A `config.json` that still carries the `integrations` key loads normally and logs one warning naming the removed key.

## [0.3.0] - 2026-10-22

The MetaDataScrapper era ends with this release: the tool is renamed to **ShrinkRay** and its purpose has grown from reading video metadata to a complete, quality-measured library shrinking pipeline.

### Added

- **Quality-measured re-encoding (the core feature)**: per-title VMAF-driven CRF search — short samples are probed at different CRF values, each scored with VMAF against its source, and a binary search finds the largest CRF that still meets the quality floor (default 95, roughly "transparent"); the movie is then encoded once with exactly that setting. Fixed-preset mode remains available.
- **HDR10 support**: tone-mapped (hable) VMAF measurement on both inputs, automatic 10-bit encoder switch (`x265_10bit`), calibrated HDR target and CRF range. Verified end-to-end on real content (The Mandalorian S01E02: 4.26 GB → 3.45 GB at full-movie VMAF 95.72).
- **Duplicate detection**: byte-identical (content hash), same-title and fuzzy-title grouping with duration gating; group summaries with wasted bytes; keep-rationale explains honestly why the best file is kept (byte-identical / highest resolution / highest bitrate / "no quality signal separates them"). DELETE moves files to the OS recycle bin (recoverable) and retires the row immediately; restored files recover on the next scan.
- **Resident app + web dashboard**: job queue (scan, plan, encode, dedupe) with live progress and per-movie fractions, adaptive polling snapshot API (one round-trip, ETag-cached library), provenance chain per title, config editing from the UI.
- **Generation tracking (schema v7)**: every encode records input/output bitrate, source/output paths and its generation number; re-encoding an encode chains automatically. `shrinkray compare` renders a title's provenance chain (the generation-drift ledger).
- **Resource politeness**: encodes, probes and VMAF measurements run below normal priority (config: `resources.idle_priority`); per-encode thread cap (`handbrake.max_threads`, CLI `--threads`); the resident app logs its own memory every 5 minutes and exposes `rss_mib` in the status API (measured: ~16-64 MiB resident).
- **Concurrent encode guard**: a pid-stamped lock file per output directory makes encoder collisions (job worker vs watch loop vs CLI) impossible instead of unlikely; stale locks of dead processes are stolen.
- **Opt-in Jellyfin integration**: one fire-and-forget `POST /Library/Refresh` after encode batches (token via environment variable, never in config.json).
- **Soft-missing reconciliation (schema v6)**: deleted files are flagged and hard-deleted only after 3 consecutive missing scans; reappearing files recover automatically; mass-flagging emits an accident warning.
- **Failure evidence bundles**: failed VMAF measurements and encodes bundle command, stderr (empty recorded explicitly), traceback and artifacts into `logs/` for diagnosis.
- **Windows installer**: `install.ps1` one-liner (uv, GPL ffmpeg with libvmaf, HandBrakeCLI, repo, dashboard build; idempotent).
- **Linux/NAS deployment**: verified end-to-end on Ubuntu (WSL rehearsal): full gate, VMAF measurement, headless recoverable trash, graceful HandBrake absence, POSIX niceness and `/proc` RSS; systemd unit in `deploy/`, README Linux/NAS section.

### Changed

- Rename MetaDataScrapper → ShrinkRay (package `src/shrinkray/`, CLI `shrinkray`, GitHub repo). All tool configs, ignores and docs swept.
- Scans are deterministic on every filesystem (sorted output; os.walk order is hash-ordered on ext4).
- Encode results are recorded benchmark-grade (schema v5): source snapshot, CRF, VMAF, probe statistics, tool versions.
- Duplicates API and UI exclude retired/missing rows.

### Fixed

- Same-thread db locking crash ("cannot release un-acquired lock").
- Live progress deadlock: subprocess pipes are drained concurrently while streaming stdout.
- Encode truncation: outputs are duration-verified, not trusted by exit code alone (HandBrake can exit 0 on failure).
- Path allowlist hardened (realpath + containment; traversal rejected).
- `os.startfile` guarded so mypy's linux view passes CI.
- Test suite uses real temp directories (fictional `D:/...` paths broke scope containment on Linux); scan order determinism; rename leftovers in tool configs (coverage target, first-party lists, gitignore model path).

### Security

- Supply-chain hardening: uv malware-check gate, 7-day new-package cooldown with security-fix exemption, CodeQL-recognized path sanitization.
- No secrets in tracked files: integration tokens live in environment variables by construction.

[0.4.0]: https://github.com/bennischober/ShrinkRay/releases/tag/v0.4.0
[0.3.0]: https://github.com/bennischober/ShrinkRay/releases/tag/v0.3.0
