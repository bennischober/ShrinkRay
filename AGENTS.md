# AGENTS.md — working rules for AI agents in this repository

Instructions for AI coding agents (OpenCode, Codex, Claude Code, ...) working in
this repository. These rules are the distilled, non-negotiable core; the
README documents the product, PRODUCT.md the product direction.

## What this project is

A Python CLI + resident server app that answers two questions per file: **is
this file bigger than its quality requires?** and **does it carry baked-in black
bars** (a frame whose aspect ratio differs from its content's)? It catalogs a video collection
(`scan`), predicts savings (`plan`), re-encodes with measured quality
(`encode`, VMAF-driven CRF search) and renders an HTML report. The Python
code is an orchestrator: all heavy work is delegated to external binaries
(`ffprobe`, `ffmpeg`, `HandBrakeCLI`) via `subprocess`.

It is built to be **given to the community**: it will be installed by strangers on
their own servers, pointed at libraries they cannot re-acquire, and run unattended.
That is why the engineering standard below is non-negotiable — a defect here can
destroy someone else's data, not just annoy its author.

The target shape is a container on a server, pointed at a media folder, running
unattended with a watchdog and a LAN dashboard. The CLI is a first-class
product, not a debugging surface: every capability works without the resident
process. See PRODUCT.md for the product contract.

The product rules a change must not contradict (PRODUCT.md has the detail; most
are target behaviour, not yet implemented — check the code before assuming one):

- **Two operating modes, one engine.** A stateless one-shot CLI over a file, a folder
  or a glob; and a resident container that scans a mounted tree, detects bars, runs the
  CRF search for the whole collection, encodes the backlog and watches for changes.
  Neither mode may require the other, and the CLI must never require the resident
  process.
- **The dashboard is Mode B's status window, not a control surface.** It reports what is
  running, planned, remaining and reclaimed; it does not start work. A control panel
  implies a watching operator, which is what the resident mode exists to remove — and a
  read-only surface is a smaller attack surface. Do not add a route or a button that
  starts, cancels or reconfigures work without a product decision first.
- **The one-shot mode does not need the database.** A run against a file, folder or glob
  goes straight to the CRF search and the encode. The database is an optimization for
  repeated runs, never a prerequisite.
- **In place, retaining the original.** A verified output takes the source's path; the
  original is renamed aside and kept. No code path deletes a source file.
- Files below 1080p or in a codec older than H.264 are never re-encoded.
- **The CRF search always runs on an eligible file; a re-encode happens only when the
  file would get smaller.** The search finds a CRF, not an encode — it is allowed, and
  expected, to end in "do not encode". Size is the only permission. Never add a path that
  re-encodes to remove bars, and never treat "has bars" as a precondition for probing.
- **Bar removal is part of every re-encode, not an option.** Any file that is encoded has
  its bars detected and removed, so a re-encoded file is always clean. Do not add a flag to
  keep them, do not skip a file for lacking them, and do not add a "crop-only" path. The
  changing-aspect-ratio case is handled by cropping only the bars common to every sampled
  frame. `docs/encode-decision.md` is the authoritative walk-through of every gate.
- **Codec age is the only codec gate.** H.264 and newer are eligible; MPEG-2/VC-1 and
  older are never re-encoded. AV1 is the output format — whether an AV1 source is probed is
  an open question recorded in `docs/encode-decision.md`.
- There is exactly one encode mode: a VMAF-driven CRF search against a required
  VMAF (96 by default), then one full encode. Never add a fixed-preset or otherwise
  unmeasured encode path.
- The output is AV1, encoded on the CPU, from 8-bit or 10-bit sources. No other
  output codec, no hardware encoder.
- Only the video stream is re-encoded; audio and subtitle tracks are copied 1:1.
- The watchdog scans every new or changed file into the database and re-decides it
  from the fresh scan.
- **The collection is already organized.** The target library is a large set of
  already-named files with no duplicates. Never name, vendor, or assume any particular
  acquisition, naming or media-server product in code, comments, docs or commit
  messages — ShrinkRay is written for "a large existing video collection" and nothing
  more specific. Never add a feature whose usefulness depends on knowing what a file is
  called or which file it is.

## PROJECT KNOWLEDGE BASE

**Generated:** 2026-10-24 · **Commit:** a1a5c33 · **Branch:** main

Orientation only: the rules below this block are normative, and the two module maps
live in `src/shrinkray/AGENTS.md` and `ui/AGENTS.md`. Conventions, the gate and the
anti-patterns are the sections further down — this block does not restate them.

### STRUCTURE

```
./
├── src/shrinkray/    # 18 flat modules, ~11.4k LOC — the whole Python product
├── ui/               # Svelte 5 dashboard, second toolchain with its own gate
├── tests/            # one file per source module, plain unittest, run by pytest
├── testdata/         # gitignored local fixtures (mkv clips, inbox/movies trees)
├── test_media/       # gitignored real media, manual end-to-end runs only
├── docs/             # decisions/0001-module-layout.md, docker.md, tui.md, plan-reuse.md
├── deploy/           # shrinkray.service (systemd)
├── scripts/          # verify_fullmovie.py — manual full-movie check
└── .github/workflows/ci.yml   # job `test` (the six commands) + job `ui`
```

### WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Change a CLI command or flag | `src/shrinkray/__main__.py` (1067 L) | typer app; no business logic |
| Change the VMAF/CRF decision | `src/shrinkray/quality.py` (2821 L) | measure, crop detect, sampling, search, plan cache |
| Change an HTTP route or SSE | `src/shrinkray/app.py` (1311 L) | FastAPI, 16 routes incl. SSE `/api/events` |
| Change the schema | `src/shrinkray/db.py` (1082 L) | migration + `SCHEMA_VERSION` bump |
| Change what gets encoded | `src/shrinkray/encode.py` (1029 L) | HandBrake invocation, artifact verification |
| Change candidate selection | `src/shrinkray/quality.py` | `CANDIDATE_CODECS` / `MODERN_CODECS` gate |
| Change the watchdog | `src/shrinkray/watch.py` | poll interval, stability, `decide()` |
| Change the dashboard | `ui/src/lib/pages/` | contract also in `ui/src/lib/api.ts` |
| Understand a past decision | `docs/decisions/`, issue #63 | layout target, not current state |

### CODE MAP

| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `DEFAULT_TARGET_VMAF` | const 96.0 | `engine/search.py:48` | 8 | the decided default; every CLI option, the API model and the job payload derive from it |
| `CANDIDATE_CODECS` | const | `quality.py:65` | 5 | `("h264",)` — the only size-alone candidates |
| `MODERN_CODECS` | const | `quality.py:66` | 5 | `("hevc","av1")` — bars-only candidates |
| `guard_encodes` | context mgr | `encode_lock.py:142` | 14 | per-output-dir lock, 6 h stale window |
| `SCHEMA_VERSION` | const 14 | `db.py:8` | 9 | bump with every migration |
| `find_handbrake` | function | `encode.py:175` | 40 | binary discovery, degrades to `None` |
| `encode_file` | function | `encode.py:742` | — | the one full encode |

### NOTES (gotchas that cost a reader time)

- **VMAF default: 96, and the code now ships 96.** `DEFAULT_TARGET_VMAF`
  (`engine/search.py:48`) is `96.0`, and every `--target-vmaf` default, the API request
  model and the job payload derive from it rather than repeating a literal — the literals
  were the defect (issue #103). The target participates in the stored plan fingerprint, so
  the 95.0 -> 96.0 change deliberately invalidated every cached plan: a library planned
  under the old default re-probes once. The HDR default is `DEFAULT_TARGET_VMAF -
  HDR_VMAF_OFFSET` (95.5), expressed as a difference so the two cannot drift apart.
- **The package is flat today.** The `tools/`/`engine/`/`server/` tree in Project
  conventions is the #63 target, not the current layout. Do not describe it as
  current, and do not restructure as a drive-by on an unrelated issue.
- **One writer per output directory.** `encode_lock.guard_encodes` is the only guard;
  any new code path that writes into an output dir must take it.
- **Tests patch module-qualified names** (`shrinkray.encode.find_handbrake`), so moving
  a function between modules silently breaks patch targets — a #63 move must update
  the patch strings in the same commit.
- **The gate is not the whole gate.** The six commands cover Python only; CI also runs
  a separate `ui` job (`npm ci`, `npm run check`, `npm run test`, `npm run build` in
  `ui/` — vitest covers the api contract helpers, the SSE progress store and the jobs
  page render). A change under `ui/` is unverified until those four pass too.
- **The API contract is duplicated by hand.** A new FastAPI field that is not added to
  `ui/src/lib/api.ts` is invisible to the dashboard; add both in one change.
- **`ui/dist` is served by `app.py`** (`/` no-cache, `/assets/*` immutable, realpath
  containment) — it is build output, never edit it by hand.
- **Two stale docs, one accurate.** `DESIGN.md` describes a grid/table toggle and a
  poster card the app no longer has (`LibraryPage` is table-only, `PosterCard.svelte`
  is orphaned); `ui/README.md` is unedited Vite template text. The root README's
  "Web UI (ui/)" section is the accurate one.
- **`helpers.py` still exists** (257 L). The "no helpers module" rule governs new code
  and the #63 moves; do not grow it.

## Engineering standard (non-negotiable)

This is not a hobby project. It is built to be **given to the community**, which
raises the bar from "works on my machine" to "a stranger can trust it with their
library". Read every rule below as a requirement, not an aspiration.

1. **Secure by construction.** Every path from an untrusted input to a file, a
   subprocess or a socket is validated at the boundary. Argument lists, never shell
   strings. Paths contained to an allowlist after `realpath`. No secret in a tracked
   file. A destructive action is opt-in, explicit and recoverable. "It's only on my
   LAN" is not a security boundary — the target deployment is a NAS reachable by
   every device on that network.
2. **Correct under interruption.** This runs unattended for hours against a library
   that cannot be re-acquired. Every write is atomic or resumable; every crash leaves
   a recoverable state, never a missing or half-written file. A zero exit code is not
   success — verify the artifact.
3. **Honest about failure.** No silent fallback, no swallowed exception, no "it
   probably worked". If a decision cannot be measured, it is reported as unmeasured
   and the file is left alone. An error a user cannot see is a bug.
4. **Tested where it matters.** A behaviour change ships with a test that would fail
   without it. Tests never pollute the working tree, never depend on the wall clock or
   timing luck, and never encode a platform assumption that silently inverts on
   another OS.
5. **Readable by a stranger.** A module has one coherent responsibility, names state
   intent, and the control flow is direct. The test is whether someone who has never
   seen the file can change it safely. Cleverness is a cost, not a credential.
6. **No unsupported claim in a doc.** Every `file:line`, every performance number,
   every "supported" in the README is checked against the code or the measurement it
   describes. A stale doc is a defect with a longer half-life than a stale test.

"Proper" here means exactly what RULES.md means: only what is needed, never less than
required. It does not mean maximal — it means nothing missing and nothing superfluous.

## Scope boundary (read before adding a feature)

ShrinkRay changes a file's **bytes**, never its **identity**. Path, filename and
the set of files stay as they are.

- **In scope:** re-encoding at measured quality, black-bar cropping, deciding
  *whether* a file is worth touching, and recording that decision.
- **Out of scope:** naming, renaming, moving, organizing, matching, scraping,
  deduplicating, acquiring, playing or serving media. That work belongs to the
  tooling that already assembled the collection.

Do not add a feature whose usefulness depends on knowing what a file is *called*
or *which* file it is. "Is this file the size its quality needs?" and "Does its
frame carry black bars its content does not need?" are the only questions this tool
answers.

Never name, vendor, or assume any particular acquisition or naming automation in
code, comments, docs or commit messages. ShrinkRay is written for "a large
existing video collection" and nothing more specific.

## Non-negotiable rules

1. **Never delete a source movie file.** The library is sacred (see
   PRODUCT.md). ShrinkRay has no code path that deletes a source; replaced
   originals are renamed aside and kept until the operator removes them
   manually. A destructive action in a feature request is a signal to stop and
   ask, not to implement.

   **Swap-in-place is the intended shape** (encode to a temp file, verify it,
   then move it into the original's path with the original renamed aside), but
   it is **not implemented yet** — today `encode` writes to a separate output
   directory. Bar removal is part of every encode, never a flag; do not add one.
2. **Never commit secrets.** The litellm API key lives in
   `.opencode/orthos-key` (gitignored). Do not copy keys into code, tests,
   logs or commit messages. Do not use PII in commit messages.
3. **Run the full gate before every commit.** All six must pass:

   ```sh
   uv run ruff check src tests
   uv run ruff format --check src tests
   uv run mypy --platform linux
   uv run mypy --platform win32
   uv run pytest -q
   ```

   Both mypy runs are explicit so the gate is identical on every host. Running
   them is the whole point: `--platform` defaults to `sys.platform`, so a bare
   `uv run mypy` only ever checks the host's own view and is blind to the other
   one. A Windows-only API (`os.startfile`) passes on a Windows machine and
   breaks every Linux/NAS user; a POSIX-only import (`pwd`, `fcntl`) passes on the
   Linux runner and breaks a Windows contributor. Checking both directions catches
   both, whichever machine you are on.

   CI runs exactly these six. If you cannot make the gate pass, do not commit;
   report the blocker instead.

## Project conventions

- **Package manager is uv.** Use `uv add` / `uv remove`, not pip. Never
  edit `uv.lock` by hand. `requirements.txt` does not exist anymore — do
  not create one.
- **Python 3.15** (`.python-version`), minimum supported 3.10 (`pyproject.toml`).
- **src layout**: code lives in `src/shrinkray/`, tests in `tests/`.
  Entry point: `shrinkray` (typer app) or `python -m shrinkray`.
- **Formatting is ruff's job, not yours.** After writing or restructuring
  code, run `uv run ruff format` on the touched files before committing.
  The CI format check is strict.
- **Type hints are checked by mypy** with `check_untyped_defs`,
  `no_implicit_optional`, `disallow_untyped_defs`, `disallow_incomplete_defs`,
  `warn_return_any`, `no_implicit_reexport` and `strict_equality` — every
  module under `src/shrinkray/` is fully annotated, so the strict flags apply
  everywhere, not module by module (issue #40). New public functions get full
  type hints and a docstring in Google style (see existing modules for the
  pattern).
- **Tests live in `tests/` as plain `unittest`** classes run by pytest,
  one file per source module (`test_encode.py`, `test_quality.py`,
  `test_watch.py`, `test_db.py`, `test_report.py`, `test_cli.py`,
  `test_jobs.py`, `test_resources.py`,
  `test_media.py`, `test_misc.py`). Opt-in smoke tests against the real
  binaries live in `tests/test_smoke.py` — skipped unless
  `SHRINKRAY_SMOKE=1` is set AND ffmpeg/HandBrakeCLI are on PATH (they
  never run in CI or the default gate; run them explicitly after changes
  to the probe/encode/watchdog paths). New features need tests; bug fixes
  need a test that would have caught the bug. Tests must not require
  ffmpeg/HandBrake binaries or network access — mock or use `tmp_path`
  fixtures. Tests that touch the filesystem or path scopes use **real
  `tempfile`
  directories, never fictional paths** (`D:/Movies` is absolute on
  Windows but a relative path on Linux/CI — scope containment and
  `resolve()` behave differently and the local gate cannot see it).
- **`test_media/` is for real media, and it is never committed.** The
  folder is part of the repository (tracked via `test_media/README.md`); every
  file in it is ignored by `/test_media/*`. Drop real video files there for
  manual runs and for end-to-end validation, and expect tens of GB to sit in the
  working tree — that is the point, and it is why the ignore rule exists. Never
  `git add -f` a file from this folder, never remove the ignore rule, and
  never point a test or a script at it without a skip guard.
  **No automated test reads `test_media/` today.** `tests/test_smoke.py`
  generates its own synthetic 100s clip in a `tempfile` directory, so the
  default gate and the smoke tests both pass with the folder empty. Real media
  is for a **manual** end-to-end run: `scan`, then `plan`, then `encode`
  against the files in this folder, and `report` to read the result back. That
  is the only way to catch a defect that only real content shows (grain,
  letterboxing, HDR, an aspect-ratio change) — a stub-based test cannot.
  Never commit the media itself, and never reference these filenames in code,
  tests or docs: the set of files in the folder is the operator's business.
- **Module layout (decision record, issue #63).** Three shallow subpackages
  will hold the product, split by reason-to-change — this is the agreed target
  every refactor PR moves toward, not a daily obligation:

    ```
    src/shrinkray/
      __main__.py      CLI: parse arguments, call service/engine, render. No business logic.
      config.py        pure data; loading it has no side effects
      logger.py  db.py  telemetry.py  report.py  service.py
      tools/           the subprocess boundary: the only code that runs an external binary
        process.py       one runner: argument lists, timeout, stall watchdog, priority, termination
        ffprobe.py  vmaf.py  handbrake.py
      engine/          the product: "is this file bigger than its quality requires?"
        scan.py  crop.py  samples.py  search.py  encoders.py  plan.py  pipeline.py  lock.py  watch.py
      server/          the network surface: nothing else listens on a socket
        app.py  routes_*.py  security.py  jobs.py
    ```

  **Imports only go downward** (entry points → service → engine → tools →
  foundation), to be enforced by an `import-linter` layers contract as a
  seventh gate command (`uv run lint-imports`) once the tree satisfies it.
  Until then the rule is discipline: no new code may add an upward import
  (`tools` importing `engine`, `engine` importing `server`, foundation
  importing anything above itself) or a function-level `shrinkray` import
  to dodge a cycle. Other standing rules from the record: **no
  `helpers`/`utils`/`common` module** (a function lives with its concern
  or its only caller — `helpers.py` dissolves into its callers);
  **one implementation per external fact** (only `tools/ffprobe.py` runs and
  parses ffprobe, only `engine/encoders.py` decides the encoder for HDR/SDR,
  only `engine/crop.py` detects bars); **tests mirror the layout**
  (`tests/engine/test_search.py`, ...). Full record with the phase order and
  the move table: issue #63.
- **Commit messages follow Conventional Commits** (`feat`, `fix`, `docs`,
  `refactor`, `perf`, `test`, `chore`, `ci`, `build`, `style`) with a scope
  where it adds context, e.g. `fix(encode): ...`. Body explains the *why*
  when the *what* is not obvious. One logical change per commit — see the
  issue workflow above for how a change reaches `main`.
- **The sqlite database (`metadata.db`) is user data, not source.** Schema
  changes go through a migration in `db.py` (bump `SCHEMA_VERSION`, see the
  v3 migration for the pattern). Never commit a `.db` file.

## External binaries

`ffprobe`, `ffmpeg` and `HandBrakeCLI` are subprocess dependencies. When a
feature needs one of them:

- always pass argument **lists** to `subprocess` (never shell strings),
- set a `timeout`,
- treat a zero exit code as *not sufficient* success — verify the expected
  artifact exists (see the encode truncation fix for why),
- degrade gracefully when the binary is missing (see `find_handbrake()`).

## Issue workflow (the default for every change)

Working on an issue always means the same five steps, in this order. There is no
"small enough to skip the branch" exception — a one-line fix goes through the same
path, because the point is that `main` only ever receives reviewed work. The only way
around it is a deliberate direct push, which has its own rules below.

1. **Claim it.** `gh issue edit <N> --add-assignee @me` (see Multi-agent etiquette
   below). An assigned issue is work in progress; an unassigned one is free to take.
2. **Branch from an up-to-date `main`.** Work on a branch, not on `main`:

   ```sh
   git switch main && git pull --ff-only
   git switch -c <type>/<issue>-<slug>      # e.g. fix/38-test-pollutes-tree
   ```

   The issue number belongs in the branch name, so `git branch --list` answers "what
   is in flight" without consulting GitHub.
3. **Do the work, then pass the gate.** The five commands in Non-negotiable rule 3
   must pass locally before anything is pushed. Commit in reviewable units, not one
   dump — see the commit rules below.
4. **Push the branch and open a pull request.** Reference the issue so it links and
   closes on merge:

   ```sh
   git push -u origin <branch>
   gh pr create --fill --body "Closes #<N>"
   ```

   The PR body says what changed and what was verified, not just what was intended.
   A PR that cannot state how it was verified is not ready.
5. **Merge only when CI is green.** Merge to `main`, delete the branch, and let
   the `Closes #<N>` reference close the issue. If the work is handed off instead,
   see Multi-agent etiquette — do not open a half-finished PR as a way to park it.

### When a PR does not go through

A red or conflicting PR is not finished work, and it is yours until it merges. Do not
close it and open a second one for the same issue, do not ask for a review while CI is
red, and do not force-push a branch a reviewer may already have read. Bring `main` into
the branch, fix what broke, and push the same PR again:

```sh
gh pr checks <N>                              # what is actually red
gh pr view <N> --json mergeable,mergeStateStatus

git switch <branch>
git fetch origin && git merge origin/main       # resolve conflicts HERE, in your branch
# fix the failure, run the five gate commands, commit
git push                                      # the same PR updates in place
```

Repeat until every check is green and the PR reports no conflicts, then merge. This is
the normal case, not an exception: when several agents work at once, `main` moves while
your PR is open, so expect to merge `origin/main` into your branch at least once on any
PR that lives longer than a few hours.

**Merge, do not rebase.** `git merge origin/main` records what was resolved and keeps
the history a reviewer already read; a rebase rewrites those commits and needs a
force-push. If a conflict is genuinely yours to resolve (two agents edited the same
hunk), resolve it in the branch and say in the PR body what the resolution was.

This is about *updating your branch*, not about how the PR lands — squash-and-merge at the
end is fine and is what most merges here use. Squashing the PR is safe; rebasing the open
branch under a reviewer is not.

### Pushing directly to `main`

Direct pushes are technically possible — branch protection is not enabled (verified
2026-10-23: the protection API returns 404) and the `push` permission is held — and
they are permitted when you have a concrete reason: an urgent fix, unblocking a broken
`main`, or a change that cannot be reviewed meaningfully as a diff. The default is still
branch → PR → merge, for one reason: a direct push runs the gate *after* `main` is
already red, while a PR runs CI before anything lands. `main` is what a stranger
clones; it must be green at every commit.

When you do push directly, **Multi-agent etiquette still applies in full**: claim the
issue, keep one issue in flight, check `git status` before staging, and pull before you
push. A direct push is a shortcut around the review step, never around the coordination.

GitHub does not let an author approve their own pull request, and every PR here is opened
by the same account that would merge it — so "approved" in practice means "green checks and
no conflicts", which is what step 5 gates on.

## Multi-agent etiquette

Multiple agents (and humans) work in this repository concurrently.

- **Claim an issue before you start working on it.** Assign it to yourself as the
  first action, before the first edit:

  ```sh
  gh issue edit <number> --add-assignee @me
  ```

  An assigned issue is work in progress; an unassigned one is free to take. This is
  the only coordination channel that works across separate checkouts, separate agent
  sessions and separate humans — a local branch name is invisible to everyone else.
  Pick an unassigned issue, or one already assigned to you. If it is assigned to
  somebody else, do not start: leave it, and pick a different one.
- **When you stop, say where it stands.** A merged PR closes its issue through
  the `Closes #<N>` reference — verify it closed, and close it yourself if it did
  not. If you are handing off mid-way, leave the assignment in place and write
  a comment on the issue saying what is done and what is left, so the next agent
  resumes instead of restarting. Unassign only if you are abandoning it.
- **One issue, one agent.** Do not assign yourself a second issue while one is still
  assigned to you and unfinished — the point of the claim is that it means something.
- **Do not touch files you did not "own" in the current task** unless you
  have read them first. Uncommitted changes in the working tree belong to
  whoever is working — check `git status` before any `git add -A` or
  `git stash`, and only stage files from your own task.
- Prefer short-lived branches over long-lived
  uncommitted WIP in the shared checkout. `main` should always be green — see the
  issue workflow above for the exact branch/PR sequence.
- If you must switch branches in the shared checkout, check for uncommitted
  changes first and stash them with a descriptive message.
- After pulling, run the gate again before you continue — someone may have
  merged something that interacts with your change.

## Autonomy

- Work through the task list without stopping to ask what comes next;
  `TODO.md` records agreed next steps and known limitations. Work that is
  already filed as a GitHub issue lives there, not in `TODO.md`.
- Prefer sensible defaults over questions; document the choice you made.
  Ask only for: irreversible or destructive actions, missing credentials,
  or genuinely ambiguous product direction.
- When you stop, state what is done, what is verified, and what remains —
  not as a substitute for doing it.
- Long-running background work (encodes, watch loops) must be started with
  a clear note on how to stop it, and must never block the session.
