# AGENTS.md — working rules for AI agents in this repository

Instructions for AI coding agents (OpenCode, Codex, Claude Code, ...) working in this repository. These rules are the distilled, non-negotiable core; the README documents the product, PRODUCT.md the product direction, and `docs/encode-decision.md` the encode contract.

**Generated:** 2026-10-25 · **Commit:** 31c5833 · **Branch:** main

## What this project is

A Python CLI + resident server app that answers two questions per file: **is this file bigger than its quality requires?** and **does it carry baked-in black bars** (a frame whose aspect ratio differs from its content's)? It catalogs a collection into a sqlite database, predicts savings (`plan`), re-encodes at measured quality (`encode`, a VMAF-driven CRF search) and swaps the verified result into the source's path. The Python code is an orchestrator: all heavy work is delegated to external binaries (`ffprobe`, `ffmpeg`, `HandBrakeCLI`).

The CLI is five commands and no more — `encode`, `plan`, `serve`, `retained`, `doctor`. Scanning is the resident backlog's first job and an engine call, not a command; there is no `scan`, `report`, `compare` or separate watch command.

It is built to be **given to the community**: it will be installed by strangers on their own servers, pointed at libraries they cannot re-acquire, and run unattended. That is why the engineering standard below is non-negotiable — a defect here can destroy someone else's data, not just annoy its author. The target shape is a container on a server, pointed at a media folder, running unattended with a watchdog and a LAN dashboard. The CLI is a first-class product, not a debugging surface: every capability works without the resident process.

The product rules a change must not contradict (PRODUCT.md has the detail; check the code before assuming one is implemented):

- **Two operating modes, one engine.** A stateless one-shot CLI over a file, a folder or a glob; and a resident container that scans a mounted tree, detects bars, runs the CRF search for the whole collection, encodes the backlog and watches for changes. Neither mode may require the other, and the CLI must never require the resident process.
- **The dashboard is Mode B's status window, not a control surface.** It reports what is running, planned, remaining and reclaimed; it does not start work. A control panel implies a watching operator, which is what the resident mode exists to remove — and a read-only surface is a smaller attack surface. Do not add a route or a button that starts, cancels or reconfigures work without a product decision first.
- **The one-shot mode does not need the database.** A run against a file, folder or glob goes straight to the CRF search and the encode. The database is an optimization for repeated runs, never a prerequisite.
- **In place, retaining the original.** A verified output takes the source's path; the original is renamed aside and kept. No code path deletes a source file.
- **Codec age is the only codec gate, and 1080p the only resolution floor.** H.264 and newer are eligible, MPEG-2/VC-1 and older never are, and files below 1080p are never re-encoded. AV1 is the output format, and whether an AV1 source is probed at all is an open question recorded in `docs/encode-decision.md`; codec is never a reason to encode.
- The watchdog scans every new or changed file into the database and re-decides it from the fresh scan.
- **The CRF search always runs on an eligible file; a re-encode happens only when the file would get smaller.** The search finds a CRF, not an encode — it is allowed, and expected, to end in "do not encode". Size is the only permission. Never add a path that re-encodes to remove bars, and never treat "has bars" as a precondition for probing.
- **Bar removal is part of every re-encode, not an option.** Any file that is encoded has its bars detected and removed, so a re-encoded file is always clean. Do not add a flag to keep them, do not skip a file for lacking them, and do not add a "crop-only" path. The changing-aspect-ratio case is handled by cropping only the bars every sustained segment agrees on. `docs/encode-decision.md` is the authoritative walk-through of every gate.
- There is exactly one encode mode: a VMAF-driven CRF search against a required VMAF (96 by default), then one full encode. Never add a fixed-preset or otherwise unmeasured encode path. The output is AV1, encoded on the CPU, from 8-bit or 10-bit sources — no other output codec, no hardware encoder. Only the video stream is re-encoded; audio and subtitle tracks are copied 1:1.
- **The collection is already organized.** The target library is a large set of already-named files with no duplicates. Never name, vendor, or assume any particular acquisition, naming or media-server product in code, comments or commit messages (PRODUCT.md's "Ecosystem position" section is the single prose exception, issue #166) — ShrinkRay is written for "a large existing video collection" and nothing more specific. Never add a feature whose usefulness depends on knowing what a file is called or which file it is.

## Knowledge base — where the maps live

Orientation only; the rules below are normative, and each area's map is in its own file. Read the one for the code you touch, not all six.

| Area | Map |
|------|-----|
| the package: layers, root modules, package-wide conventions | `src/shrinkray/AGENTS.md` |
| CRF search, plan gates, encode/verify, bars, watchdog, lock | `src/shrinkray/engine/AGENTS.md` |
| routes, token auth, path containment, jobs, SSE | `src/shrinkray/server/AGENTS.md` |
| the subprocess boundary, one module per binary | `src/shrinkray/tools/AGENTS.md` |
| the flat `unittest` suite, fixtures, patch seams | `tests/AGENTS.md` |
| the Svelte dashboard and its own toolchain | `ui/AGENTS.md` |

Facts that decide how you work before you open one:

- **Doc precedence.** `docs/encode-decision.md` (the encode contract: "where the code disagrees with this document, the code is the defect") outranks this file, which outranks `docs/decisions/0001-module-layout.md` (enforced mechanically by `lint-imports`), then PRODUCT.md and RULES.md. `docs/plan-reuse.md` governs the plan cache, `docs/black-bar-spike.md` the crop measurements (its #131 two-measurement decision is superseded by the one timeline detector of #181), `docs/docker.md` the deployment, and `docs/tui.md` is an idea with nothing built.
- **The gate is one script.** `tools/gate.sh` runs six `uv run` steps. Count the script, never the prose: this file, README, CONTRIBUTING and the script's own header have all been wrong about the number. `.github/workflows/ci.yml` runs `uv lock --check` before it and `uv audit --locked` after; `.github/workflows/release.yml` runs it with `--no-cov`.
- **`ui/` is a second toolchain with its own gate.** The CI job `ui` runs `npm ci`, `npm run check`, `npm run test`, `npm run build` in `ui/`; a change under `ui/` is unverified until those four pass. The API contract is duplicated by hand — a new FastAPI field that is not added to `ui/src/lib/api.ts` is invisible to the dashboard, so add both in one change.
- **Tests are flat.** 32 `test_*.py` modules directly in `tests/` (named per concern, not per source file), with `tests/fixtures/` as the only tracked subdirectory; there is no `tests/engine/` mirror.
- **Moving `validate_path` silently disables CodeQL's sanitiser.** `.github/codeql/custom/ValidatePathSanitizer.qll` recognises it only when the call site's path string is `src/shrinkray/server/app.py`.
- **The container is the deployment path** (`deploy/` was removed). `/media` is mounted writable because Mode B encodes in place; a read-only mount needs `$SHRINKRAY_WATCH_OUTPUT` pointing at a writable volume, or the watch loop dies on its staging folder. A non-loopback bind without `$SHRINKRAY_TOKEN` is a startup failure. `.devcontainer/` uses apt `ffmpeg`, which has no libvmaf: lint and test only.
- **`ui/dist` is build output** served by `server/app.py` (realpath containment); never edit it by hand.

## Engineering standard (non-negotiable)

This is not a hobby project. It is built to be **given to the community**, which raises the bar from "works on my machine" to "a stranger can trust it with their library". Read every rule below as a requirement, not an aspiration.

1. **Secure by construction.** Every path from an untrusted input to a file, a subprocess or a socket is validated at the boundary. Argument lists, never shell strings. Paths contained to an allowlist after `realpath`. No secret in a tracked file. A destructive action is opt-in, explicit and recoverable. "It's only on my LAN" is not a security boundary — the target deployment is a NAS reachable by every device on that network.
2. **Correct under interruption.** This runs unattended for hours against a library that cannot be re-acquired. Every write is atomic or resumable; every crash leaves a recoverable state, never a missing or half-written file. A zero exit code is not success — verify the artifact.
3. **Honest about failure.** No silent fallback, no swallowed exception, no "it probably worked". If a decision cannot be measured, it is reported as unmeasured and the file is left alone. An error a user cannot see is a bug.
4. **Tested where it matters.** A behaviour change ships with a test that would fail without it. Tests never pollute the working tree, never depend on the wall clock or timing luck, and never encode a platform assumption that silently inverts on another OS.
5. **Readable by a stranger.** A module has one coherent responsibility, names state intent, and the control flow is direct. The test is whether someone who has never seen the file can change it safely. Cleverness is a cost, not a credential.
6. **No unsupported claim in a doc.** Every `file:line`, every performance number, every "supported" in the README is checked against the code or the measurement it describes. A stale doc is a defect with a longer half-life than a stale test.

"Proper" here means exactly what RULES.md means: only what is needed, never less than required. It does not mean maximal — it means nothing missing and nothing superfluous.

## Scope boundary (read before adding a feature)

ShrinkRay changes a file's **bytes**, never its **identity**. Path, filename and the set of files stay as they are.

- **In scope:** re-encoding at measured quality, black-bar cropping, deciding *whether* a file is worth touching, and recording that decision.
- **Out of scope:** naming, renaming, moving, organizing, matching, scraping, deduplicating, acquiring, playing or serving media. That work belongs to the tooling that already assembled the collection.

Do not add a feature whose usefulness depends on knowing what a file is *called* or *which* file it is. "Is this file the size its quality needs?" and "Does its frame carry black bars its content does not need?" are the only questions this tool answers. Never name, vendor, or assume any particular acquisition or naming automation in code, comments, or commit messages, and never couple any code path to one: no import, no path assumption, no filename parsing. The one exception is PRODUCT.md's "Ecosystem position" section (issue #166), which may name the companion ecosystem in prose to describe the product's place among those tools.

## Non-negotiable rules

1. **Never delete a source movie file.** The library is sacred (see PRODUCT.md). ShrinkRay has no code path that deletes a source; a replaced original is renamed aside as `<name>.shrinkray-original` and kept until the operator removes it manually. `replace.py` performs the staged swap, `encode --replace` asks for it (implied when no output directory is given), the resident watcher always does it, and `shrinkray retained` lists and recovers. Bar removal is part of every encode, never a flag; do not add one. A destructive action in a feature request is a signal to stop and ask, not to implement.
2. **Never commit secrets.** Local keys and tool config live under `.opencode/`, which is gitignored. Do not copy keys into code, tests, logs or commit messages. Do not use PII in commit messages.
3. **Run the full gate before every commit.** `tools/gate.sh` must pass, and it is ONE script — the same contract `.github/workflows/ci.yml`, `.github/workflows/release.yml` and the dev image's `CMD` run, so there is exactly one place where the commands live:

   ```sh
   tools/gate.sh
   # uv run: ruff check src tests | ruff format --check src tests
   #   | mypy --platform linux | mypy --platform win32 | pytest -q | lint-imports
   ```

   Both mypy runs are explicit so the gate is identical on every host: `--platform` defaults to `sys.platform`, so a bare `uv run mypy` only ever checks the host's own view and is blind to the other one. A Windows-only API (`os.startfile`) passes on a Windows machine and breaks every Linux/NAS user; a POSIX-only import (`pwd`, `fcntl`) passes on the Linux runner and breaks a Windows contributor. Checking both directions catches both, whichever machine you are on. `lint-imports` enforces the layered layout of `docs/decisions/0001-module-layout.md`; a module in the wrong layer is a red gate, not a review comment. CI runs exactly this script (plus the `ui` job). If you cannot make the gate pass, do not commit; report the blocker instead.

## Project conventions

- **Package manager is uv.** Use `uv add` / `uv remove`, not pip. Never edit `uv.lock` by hand. `requirements.txt` does not exist anymore — do not create one.
- **Python 3.15** (`.python-version`), minimum supported 3.10 (`pyproject.toml`). **src layout**: code in `src/shrinkray/`, tests in `tests/`; entry point `shrinkray` (typer app) or `python -m shrinkray`.
- **Formatting is ruff's job, not yours.** After writing or restructuring code, run `uv run ruff format` on the touched files before committing; the CI format check is strict.
- **Type hints are checked by mypy** with `check_untyped_defs`, `no_implicit_optional`, `disallow_untyped_defs`, `disallow_incomplete_defs`, `warn_return_any`, `no_implicit_reexport` and `strict_equality` — every module under `src/shrinkray/` is fully annotated, so the strict flags apply everywhere, not module by module (issue #40). `[tool.mypy] files = ["src"]`, so the `tests.*` override never runs in the gate: tests are linted by ruff only. New public functions get full type hints and a Google-style docstring.
- **Tests live in `tests/` as plain `unittest` classes run by pytest** — 32 `test_*.py` modules named per concern, so one module may be pinned by several files. Opt-in smoke tests against the real binaries live in `tests/test_smoke.py`, skipped unless `SHRINKRAY_SMOKE=1` is set AND ffmpeg/HandBrakeCLI are on PATH; they never run in CI or the default gate, so run them explicitly after changes to the probe/encode/watchdog paths. New features need tests; bug fixes need a test that would have caught the bug. Tests must not require ffmpeg/HandBrake binaries or network access — mock the subprocess seam or use a temporary directory. Tests that touch the filesystem or path scopes use **real `tempfile` directories, never fictional paths**: `D:/Movies` is absolute on Windows but relative on Linux/CI, and scope containment and `resolve()` behave differently, which the local gate cannot see.
- **`test_media/` is for real media, and it is never committed.** The folder is tracked via `test_media/README.md` and every other file in it is ignored. Drop real video files there for manual runs and end-to-end validation and expect tens of GB in the working tree — that is the point, and why the ignore rule exists. Never `git add -f` a file from this folder, never remove the ignore rule, and never point a test or a script at it without a skip guard. **No automated test reads `test_media/` today**: `tests/test_smoke.py` generates its own synthetic clip in a `tempfile` directory. Real media is for a **manual** end-to-end run — `plan`, then `encode`, then `shrinkray retained` to read the result back — which is the only way to catch a defect that only real content shows (grain, letterboxing, HDR, an aspect-ratio change). Never commit the media itself, and never reference these filenames in code, tests or docs: the set of files is the operator's business.
- **Module layout (decision record `docs/decisions/0001-module-layout.md`, issue #63; landed via #97).** The import stack is `__main__ > server > service > engine > tools > db > config > logger`, enforced by the import-linter layers contract — `uv run lint-imports` is red in CI on any upward edge. Standing rules: **no new `helpers`/`utils`/`common` module** (a function lives with its concern or its only caller — `helpers.py` is dissolving into its callers, do not grow it); **one implementation per external fact** (one module owns each binary's invocation and parsing, `engine/encoders.py` alone decides the encoder for HDR/SDR, `engine/crop.py` alone detects bars) — the short-read exceptions are listed under External binaries. The per-module map is in `src/shrinkray/AGENTS.md`.
- **Commit messages follow Conventional Commits** (`feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`, `ci`, `build`, `style`) with a scope where it adds context, e.g. `fix(encode): ...`. Body explains the *why* when the *what* is not obvious. One logical change per commit.
- **Markdown prose is written as single long lines.** In issue bodies, PR bodies, and `CHANGELOG.md`, paragraphs and list items are ONE line each — no hand-wrapped 80-column breaks. Markdown reflows; hard breaks render as chopped lines on the issue/release pages. Structure (headings, blank lines, lists, tables, code fences, link-reference footers) stays as it is. This applies to every text an agent writes for humans to render, including `gh pr create`/`gh issue create`.
- **Every PR that changes code or docs adds a `CHANGELOG.md` entry in the same PR.** One bullet under `## [Unreleased]` in the matching subsection (`Added` / `Changed` / `Fixed` / `Removed`), phrased as the user-visible change and referencing the issue: `- **Short statement** (issue #N).` A PR without a changelog entry is incomplete the same way a behaviour change without a test is. Internal refactors with no user-visible effect get an entry only when a reader of the release would care (e.g. the module layout). The release workflow turns the `[Unreleased]` section into the GitHub release notes, so the entry IS the release note.
- **The sqlite database (`metadata.db`) is user data, not source.** Schema changes go through a migration in `db.py` (bump `SCHEMA_VERSION`, currently 16 at `db.py:10`; see the v3 migration for the pattern). Never commit a `.db` file.

## External binaries

`ffprobe`, `ffmpeg` and `HandBrakeCLI` are subprocess dependencies. When a feature needs one of them: always pass argument **lists** to `subprocess` (never shell strings); set a `timeout`; treat a zero exit code as *not sufficient* success and verify the expected artifact exists (see the encode truncation fix for why); and degrade gracefully when the binary is missing (see `find_handbrake()`).

`tools/` is that boundary **by contract, not yet by fact**: `engine/crop.py` (four sites), `engine/samples.py:109` and `engine/pipeline.py:1032` still call `subprocess.run` directly for short ffprobe reads, `engine/pipeline.py:398` runs the unwatched encode when the stall watchdog is disabled (issue #62), and `server/app.py` shells out to `open`/`xdg-open`. New short reads belong in the matching `tools/` module, not in a fourth copy.

## Issue workflow (the default for every change)

Working on an issue always means the same five steps, in this order. There is no "small enough to skip the branch" exception — a one-line fix goes through the same path, because the point is that `main` only ever receives reviewed work. The only way around it is a deliberate direct push, which has its own rules below.

1. **Claim it.** `gh issue edit <N> --add-assignee @me` (see Multi-agent etiquette). An assigned issue is work in progress; an unassigned one is free to take.
2. **Branch from an up-to-date `main`.** Work on a branch, not on `main`: `git switch main && git pull --ff-only`, then `git switch -c <type>/<issue>-<slug>` (e.g. `fix/38-test-pollutes-tree`). The issue number belongs in the branch name, so `git branch --list` answers "what is in flight" without consulting GitHub.
3. **Do the work, then pass the gate.** `tools/gate.sh` must pass locally before anything is pushed. Commit in reviewable units, not one dump — see the commit rules above.
4. **Push the branch and open a pull request**, referencing the issue so it links and closes on merge: `git push -u origin <branch>` then `gh pr create --fill --body "Closes #<N>"`. The PR body says what changed and what was verified, not just what was intended. A PR that cannot state how it was verified is not ready.
5. **Merge only when CI is green.** Merge to `main`, delete the branch, and let the `Closes #<N>` reference close the issue. If the work is handed off instead, see Multi-agent etiquette — do not open a half-finished PR as a way to park it.

### When a PR does not go through

A red or conflicting PR is not finished work, and it is yours until it merges. Do not close it and open a second one for the same issue, do not ask for a review while CI is red, and do not force-push a branch a reviewer may already have read. Bring `main` into the branch, fix what broke, and push the same PR again:

```sh
gh pr checks <N>                              # what is actually red
gh pr view <N> --json mergeable,mergeStateStatus
git switch <branch> && git fetch origin && git merge origin/main   # resolve HERE
# fix the failure, run tools/gate.sh, commit, git push  -> same PR, in place
```

Repeat until every check is green and the PR reports no conflicts, then merge. This is the normal case, not an exception: when several agents work at once, `main` moves while your PR is open, so expect to merge `origin/main` into your branch at least once on any PR that lives longer than a few hours. **Merge, do not rebase**: `git merge origin/main` records what was resolved and keeps the history a reviewer already read, while a rebase rewrites those commits and needs a force-push. If a conflict is genuinely yours to resolve (two agents edited the same hunk), resolve it in the branch and say in the PR body what the resolution was. This is about *updating your branch*, not about how the PR lands — squash-and-merge at the end is fine and is what most merges here use.

### Pushing directly to `main`

Direct pushes to `main` are blocked by branch protection (GitHub returns 403), and that is by design: a PR runs CI before anything lands, while a direct push would make `main` red first and debug later. `main` is what a stranger clones; it must be green at every commit. GitHub does not let an author approve their own pull request, and every PR here is opened by the same account that would merge it — so "approved" in practice means "green checks and no conflicts", which is what step 5 gates on.

## Multi-agent etiquette

Multiple agents (and humans) work in this repository concurrently.

- **Claim an issue before you start working on it.** Assign it to yourself as the first action, before the first edit (`gh issue edit <number> --add-assignee @me`). An assigned issue is work in progress; an unassigned one is free to take. This is the only coordination channel that works across separate checkouts, separate agent sessions and separate humans — a local branch name is invisible to everyone else. Pick an unassigned issue, or one already assigned to you; if it is assigned to somebody else, do not start.
- **One issue, one agent.** Do not assign yourself a second issue while one is still assigned to you and unfinished — the point of the claim is that it means something.
- **When you stop, say where it stands.** A merged PR closes its issue through the `Closes #<N>` reference — verify it closed, and close it yourself if it did not. If you hand off mid-way, leave the assignment in place and comment on the issue with what is done and what is left, so the next agent resumes instead of restarting. Unassign only if you are abandoning it.
- **Do not touch files you did not "own" in the current task** unless you have read them first. Uncommitted changes belong to whoever is working — check `git status` before any `git add -A` or `git stash`, and only stage files from your own task.
- Prefer short-lived branches over long-lived uncommitted WIP in the shared checkout. `main` should always be green — see the issue workflow for the exact branch/PR sequence. If you must switch branches in the shared checkout, check for uncommitted changes first and stash them with a descriptive message. After pulling, run the gate again before you continue — someone may have merged something that interacts with your change.

## Autonomy

- Work through the issue tracker without stopping to ask what comes next: open issues are the task list, and needs-decision issues stay open until the maintainer decides. Quick doc/format fixes may land directly on `main` when the maintainer says so; everything else goes through the issue workflow above.
- Prefer sensible defaults over questions; document the choice you made. Ask only for irreversible or destructive actions, missing credentials, or genuinely ambiguous product direction.
- When you stop, state what is done, what is verified, and what remains — not as a substitute for doing it.
- Long-running background work (encodes, watch loops) must be started with a clear note on how to stop it, and must never block the session.
