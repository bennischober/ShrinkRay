# AGENTS.md — how to work in this repository

For AI coding agents (OpenCode, Codex, Claude Code, ...) and the people who review their work. It has four parts: **how to work** (the judgment expected of you), **the hard invariants** (never broken), **the process** (how a change reaches `main`), and **conventions and maps**. Detail lives in the documents this file points to; "Where to read more" says which one wins when two disagree.

## Why the bar is high

ShrinkRay is given to the community. Strangers install it on their own servers, point it at video libraries they cannot re-acquire, and leave it running unattended. A defect here can destroy someone else's data, and nobody is watching when it happens. So the standard is not "it works on my machine" but "a stranger can trust it with their library" — and the person who trusts your change will never meet you. Work so that you would be proud to have your name on it in a year.

**What it is.** A Python CLI and resident server that answer two questions per file: *is this file bigger than its quality requires?* and *does its frame carry black bars its content does not need?* It catalogs a collection into sqlite, predicts savings (`plan`), re-encodes at measured quality (`encode`, a VMAF-driven CRF search) and swaps the verified result into the source's path. The Python code orchestrates; the heavy work is done by `ffprobe`, `ffmpeg` and `HandBrakeCLI`. The CLI is six commands — `encode`, `plan`, `serve`, `setup` (the terminal UI that configures and controls, #244/#252), `retained`, `doctor` — and no other command is added without a product decision. The target deployment is a container on a server (an Unraid Community App), pointed at a media folder, with a watchdog and a LAN dashboard.

## How to work

### 1. Understand before you act

- **Read the whole issue**: the body, every comment, and the linked issues and PRs. Comments often carry the maintainer's decision that changed the original ask.
- **Read the code you will change and its surroundings**: the area map (see "Where to read more") and, for anything on the encode path, `docs/encode-decision.md`. Learn how things are done here — conventions, error handling, test style — and follow them even where you would prefer something else. A convention you believe is defective is raised (see "Report honestly"), not changed on the side.
- **Restate the problem.** When you claim the issue, comment with the problem in your own words and the approach you intend. If you cannot restate it, you do not understand it yet.
- **Ask or decide.** Ask — comment, add the `needs-decision` label, move on to other work — when the answer changes product behaviour, touches user data or anything destructive or irreversible, or when two reasonable readings of the issue lead to different designs. Implementation details you decide yourself with a sensible default, and say in the PR what you chose and why. An issue labelled `needs-decision` is not implemented until the maintainer has decided on it.
- **Check the code before trusting a doc, this one included.** Some product rules describe the target, and the code may not be there yet.

### 2. Find the root cause, not the symptom

- **Reproduce a bug before fixing it**, as a failing test wherever possible. Run it and watch it fail for the reason the issue describes; the PR says that you did.
- **Ask why it happened**, not only where. A change that makes the error go away without explaining it is a guess.
- **Look for the same flaw elsewhere** — search for the pattern. Fix the siblings the issue covers; open an issue for each one it does not.
- **Never make a failure disappear.** No broad `except`, no new `# type: ignore`, `noqa`, `skip`/`xfail`, lowered coverage threshold or loosened assertion unless the PR justifies that specific line. Never change a test to agree with broken behaviour.

### 3. Keep scope disciplined

Do what the issue asks, completely, and nothing more. Problems you notice along the way become new issues, not part of this diff. Do not refactor, rename or delete code outside the task. A small, focused PR is easy to review, easy to revert and easy to trust.

### 4. Write code for the next person

- **Every element earns its place.** A function, parameter, config option, comment or abstraction exists because correctness, readability, testability, security or operations require it. Only what is needed, never less than required: no speculative feature, unused abstraction or "just in case" parameter — and no missing error handling, validation or edge case the problem actually has. "Proper" means nothing missing and nothing superfluous; it does not mean fewest lines, and it does not mean maximal.
- **One coherent responsibility** per module, class and function; direct control flow; neither monoliths nor a confetti of tiny functions.
- **Names state intent; comments explain why** — a constraint, a trade-off, an incident — never what the next line does.
- **Prefer the boring, proven solution.** The standard library before a dependency; a new dependency needs a stated reason in the PR. When two approaches are both valid, choose the one that better meets these rules, not the shorter one.
- **Public interfaces are hard to misuse.** A CLI flag, config key or API field is a promise to users; changing one is a compatibility decision, not a cleanup.

### 5. "Done" means done

Nothing is done until all of this holds:

- **The checks pass**: `tools/gate.sh`; if `ui/` changed, `npm ci && npm run check && npm run test && npm run build` in `ui/`; if dependencies changed, `uv audit --locked`; if the scan, probe, encode, watch or replace path changed, the smoke tests (`SHRINKRAY_SMOKE=1`, needs ffmpeg with libvmaf and HandBrakeCLI) — or the PR says they were not run and why.
- **A test fails without the change** and passes with it.
- **The edge cases are thought through.** For this project at least: an empty folder or no eligible file; unicode, spaces and very long paths; Windows and POSIX paths; a crash or kill mid-write (what is left on disk?); a full disk; a missing or too-old binary; a multi-hour or multi-gigabyte file; two encoders, or the watcher and a manual encode, at once; HDR and 10-bit sources; a file that changes while it is read.
- **Docs and changelog** that describe the behaviour are updated in the same PR.
- **Compatibility is handled**: a schema change is a migration, an old config either loads or fails loudly, an API field is added to `ui/src/lib/api.ts` too.

### 6. Review your own diff

Before opening the PR, read `git diff origin/main...HEAD` top to bottom as if a stranger wrote it. Ask what it could break, what happens if it is interrupted halfway, and what you assumed without checking. Then it goes to the independent review (see Process) — your own read is necessary, not sufficient.

### 7. Report honestly

- **"Verified" means you ran it and saw the result.** Otherwise write "believed, not verified" and say why. Name what you could not do (no libvmaf, a platform you do not have).
- **Lay out trade-offs** instead of silently picking one.
- **Disagree openly.** If a better solution conflicts with the issue as written, neither ignore it nor silently override the issue: state the option, why it is better, and ask. This is for meaningful trade-offs, not style.
- **Own mistakes.** If you got something wrong, say so plainly in the PR or issue and fix it.
- **When you stop**, state what is done, what is verified, and what remains.

### 8. Write issues someone else can pick up

`gh issue create` bypasses the forms in `.github/ISSUE_TEMPLATE/`, so every issue you open carries them itself: a title that states the defect or need (not the solution); why it matters to a user; exact reproduction steps or the code path; expected versus actual behaviour; the environment where it matters; acceptance criteria that define "fixed"; and your hypothesis about the cause, labelled as a hypothesis. Anything exploitable goes through `SECURITY.md`, never a public issue.

## Hard invariants

This is the only non-negotiable list. Breaking one is a defect even when every test passes. A request that conflicts with one is a signal to stop and ask, not to implement. PRODUCT.md and `docs/encode-decision.md` hold the detail.

1. **Never delete a source file.** A verified output takes the source's path; the original is renamed aside as `<name>.shrinkray-original` (`replace.py`) and kept until the operator removes it; `shrinkray retained` lists and recovers. No code path deletes a source.
2. **Bytes, never identity.** ShrinkRay changes a file's bytes; its path, its name and the set of files stay as they are. The collection is assumed already organized and free of duplicates: naming, moving, organizing, matching, deduplicating, acquiring, playing or serving media are out of scope, and so is any feature whose usefulness depends on what a file is called or which file it is. Never name, vendor or couple to an acquisition, naming or media-server product in code, comments or commit messages — PRODUCT.md's "Ecosystem position" section is the single prose exception (#166).
3. **Secure by construction.** Every untrusted input reaching a file, a subprocess or a socket is validated at the boundary: argument lists, never shell strings; paths contained to an allowlist after `realpath`. A destructive action — on any file, the database or the data folder — is opt-in, explicit and recoverable. "Only on my LAN" is not a security boundary — the target is a NAS every device on the network can reach. A non-loopback bind without a token is a startup failure.
4. **Correct under interruption.** Every write is atomic or resumable; a crash leaves a recoverable state, never a missing or half-written file. A zero exit code is not success — verify the artifact.
5. **Honest about failure.** No silent fallback, no swallowed exception. A decision that cannot be measured is reported as unmeasured and the file is left alone. An error the user cannot see is a bug.
6. **One encode mode.** A VMAF-driven CRF search against a required VMAF (96 by default), then one full encode, to AV1 on the CPU, from 8- or 10-bit sources. No fixed-preset or otherwise unmeasured path, no other output codec, no hardware encoder. Only the video stream is re-encoded; audio and subtitles are copied.
7. **Codec age is the only codec gate, the 1080p class the only resolution floor.** The source-codec whitelist (`SOURCE_CODECS`: h264, hevc, vp9, av1) may be extended by config, never shrunk; MPEG-2, VC-1 and older are outside it. A source below the 1080p class (neither width 1920 nor height 1080 reached) is never re-encoded; the floor may be raised, never lowered. Whether an AV1 source is probed at all is still an open question in `docs/encode-decision.md`, which also lists the remaining gates (duration, size floor). Codec is never a reason to encode.
8. **Size is the only permission.** The CRF search always runs on an eligible file and may end in "do not encode"; a re-encode happens only when the file gets smaller. Bars are removed on every re-encode — never a flag to keep them, never a reason to skip or to encode, never a crop-only path; with a changing aspect ratio, only the bars every sustained segment agrees on are cropped.
9. **Two modes, one engine.** The one-shot CLI (a file, folder or glob) never requires the resident process or the database. The CLI reaches the resident process only through state they share in the data folder, never through a network route. The resident watcher scans every new or changed file and re-decides it from the fresh scan.
10. **The dashboard reports; it does not control.** No route or button that starts, cancels or reconfigures work without a product decision. The control surface is the terminal (`shrinkray setup`): it edits settings and never loosens a product rule.
11. **No secrets, no personal data** in a tracked file, a log, a test or a commit message. Local keys and tool config live under the gitignored `.opencode/`.
12. **No unsupported claim in a doc.** Every number, "supported" or code reference is checked against the code or the measurement it describes.

## Process: from issue to `main`

Open issues are the task list: work through them without stopping to ask what comes next. Every change takes this path; a one-line fix is not an exception. `main` is what a stranger clones and must be green at every commit. Never push to `main` directly — the repository protects it against force-pushes and deletion, but this rule, not a setting, is what keeps unreviewed work off it. The only lighter path: when the maintainer is in the session and explicitly asks for one, that instruction wins for that task.

1. **Resume or claim.** First look for work already in flight: `gh pr list --state open --search "<N>"` and `git branch -a --list "*/<N>-*"`. An open PR or branch for the issue is finished, not restarted — see "When a PR does not go through", and fix in that PR any mistake an earlier commit of it left. Agents may share one GitHub login, so the assignee alone does not say which session owns an issue. It is not yours if its latest claim comment comes from another session with no hand-off after it. If you cannot tell — assigned, no claim or hand-off comment, recent pushes — ask on the issue and take another one. Otherwise claim it with `gh issue edit <N> --add-assignee @me` and post the restating comment (How to work, 1), naming your branch; post it when resuming too. A PR you resume is yours in full — its body, its tests, its mistakes — but its scope is still the issue's. One issue at a time — an issue parked on `needs-decision`, or a PR waiting on `needs-human-review`, no longer counts, so you may take the next one.
2. **Branch from an up-to-date `main`** (unless resuming): `git switch main && git pull --ff-only && git switch -c <type>/<N>-<slug>` (e.g. `fix/38-test-pollutes-tree`).
3. **Do the work** to the standard above. The checks from "Done means done" pass **before every push**.
4. **Open the PR**: `git push -u origin <branch>`, then `gh pr create`, filling in every section of `.github/pull_request_template.md` (the reviewer brief, problem and root cause, change, verification, edge cases, risk) with `Closes #<N>`. A PR that cannot say how it was verified is not ready.
5. **Independent review, in a loop.** Spawn a subagent and wait for its result. Its prompt contains only the issue number and the PR number — not your summary, plan or opinion of the change. It reads `gh issue view <N> --comments`, `gh pr view <PR>` (the body and its comment thread) and `gh pr diff <PR>` itself and looks for correctness defects and violations of the hard invariants. Post its findings, numbered, as a PR comment headed `Independent review`, then answer every number in a follow-up comment: "fixed in `<sha>`", "not changed, because ..." — or, for a fix that only edits the PR body, the brief or any other section, "corrected (body edit, no code change)", because a body edit produces no commit SHA. A finding dismissed without a reason is not answered. The reviewer reads the brief **last**, after its findings are formed, and ends its comment with an unnumbered `Brief check`: observation lines only — what the diff touches (from `gh pr diff --name-only`) and which of the brief's pointers exist (a finding number against the PR's comment thread, a file+function against the diff; a command or link is echoed as named, since running it is the spot-check's own test) — never verdicts. A contradiction between the echo and the brief is a numbered finding like any other. Fix what it found; push only if the fix created a commit; spawn a fresh subagent the same way; repeat until one returns no findings — only then move to step 6. The loop is autonomous: it never pauses for a human reply between rounds, and an answer comment needs no acknowledgement — it ends only on a clean review or an escalation below. A clean review is a posted `Independent review` comment reporting no findings; a spawn that fails or returns nothing is not a clean review — retry the spawn once, then park the PR on `needs-human-review` with a note on what happened. Each spawn is a new session, never a continuation of a previous reviewer. A finding that requires a product choice (the `needs-decision` trigger in How to work): answer `not changed, needs-decision: <question>`, label the linked issue `needs-decision`, park the PR, take the next issue. A finding you can neither fix nor honestly dismiss and that is not a product choice: answer with the reason, label the PR `needs-human-review`, name it in the brief's why-field, stop. A bare re-raise of an already-reasoned dismissal: post one line naming the dispute, label the PR `needs-human-review`, stop; a re-raise adding evidence the answer did not address is a new finding — answer it normally. After five rounds ending in findings, park the PR on `needs-human-review` with a pointer to the open findings. Keep the brief current: a fix touching a brief claim updates the brief in the same push and says so in the answers comment; step 6's final-diff re-verification is the drift backstop.
6. **Merge — or hand to a human.** A `needs-human-review` PR carries a `For the human reviewer` brief at the top of its body (the template's blockquote): what the change does, why a human is needed, what to check first, one minutes-level spot-check with its expected outcome, and what was not verified — every claim backed by a pointer into evidence or carried under Not verified, never unsupported prose. Re-verify the brief against the final `git diff origin/main...HEAD` as the last act before applying the label — re-running the spot-check where one is declared, and re-checking a declared "no minutes-level check exists for this class" against the final diff — so the label means "brief verified against this diff"; an edit after the review gets a one-line PR comment saying so. Then label the PR `needs-human-review` and stop after the review if it changes the behaviour of any of the following — when unsure whether one applies, it applies: the replace, retain or rename path; the encode decision (eligibility gates, CRF search, VMAF target, encoder choice, crop); server authentication, path containment or routes; a database migration; a new dependency; CI, `tools/gate.sh`, the release workflow, `AGENTS.md` or `CONTRIBUTING.md`. Otherwise you may squash-merge your own PR once every CI check has passed (pending is not passed), it has no conflicts, the review loop ended in a clean review — a PR parked by any step-5 escalation (`needs-decision` on its issue or `needs-human-review` on the PR) is never merged by the agent — and every review finding is answered.
7. **Close the loop.** Delete the branch and confirm the issue closed; close it yourself if the reference did not.

**When a PR does not go through.** A red or conflicting PR is yours until it merges. Do not close it and open another, do not force-push a branch a reviewer may have read, and do not ask for review while CI is red. Merge `main` in — merge, do not rebase — fix it, and push the same PR:

```sh
gh pr checks <N>
git fetch origin && git merge origin/main   # resolve here; say in the PR what you resolved
tools/gate.sh && git push
```

## Conventions

- **uv** is the package manager: `uv add` / `uv remove`, never pip, never a hand-edited `uv.lock`, no `requirements.txt`. Python per `.python-version`; the minimum supported version is in `pyproject.toml`. Code in `src/shrinkray/`, entry point `shrinkray` or `python -m shrinkray`.
- **The gate is one script**, `tools/gate.sh`, run by CI, the release job (with `--no-cov`) and the dev image; CI adds only `uv audit --locked` and the `ui/` job around it. It runs mypy for both `linux` and `win32`, because a bare `mypy` checks only the host's view and cannot see a Windows-only API breaking a NAS user, or a POSIX-only import breaking a Windows contributor. `lint-imports` enforces the layering of `docs/decisions/0001-module-layout.md`. If you cannot make the gate pass, do not push around it; report the blocker.
- **Formatting is ruff's job**: `uv run ruff format` on touched files. **Types**: every module under `src/` is fully annotated under mypy's strict flags; new public functions get full hints and a Google-style docstring.
- **Module layout**: the import stack is `__main__ > (server | tui) > service > engine > tools > db > config > logger` — the two front ends are siblings that never import each other — and an upward import is a red gate. No new `helpers`/`utils`/`common` module — a function lives with its concern or its only caller. One implementation per external fact: one `tools/` module owns each binary's invocation and parsing, `engine/encoders.py` alone picks the encoder, `engine/crop.py` alone detects bars.
- **Tests** are plain `unittest` classes in a flat `tests/` (one module per concern; `tests/fixtures/` is the only subfolder), run by pytest. They need no ffmpeg, HandBrake or network — mock the subprocess seam. Filesystem tests use real `tempfile` directories, never fictional paths (`D:/Movies` is absolute on Windows, relative on Linux). They never write into the working tree and never depend on the wall clock or timing luck.
- **Real media** goes in `test_media/`, which is ignored and never committed (no `git add -f`, no removing the rule, no test reading it without a skip guard, no filename from it in code or docs). It is for manual end-to-end runs — `plan`, `encode`, `shrinkray retained` — the only way to catch what only real content shows: grain, letterboxing, HDR, an aspect-ratio change.
- **Commits** follow Conventional Commits (`feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`, `ci`, `build`, `style`) with a scope where it helps; the body explains why.
- **Markdown prose is one line per paragraph or list item** in issues, PRs and `CHANGELOG.md` — no hand-wrapped 80-column breaks.
- **Every PR that changes code or docs adds a `CHANGELOG.md` bullet** under `## [Unreleased]` (`Added` / `Changed` / `Fixed` / `Removed`), phrased as the user-visible change: `- **Short statement** (issue #N): ...`. It becomes the release note. A purely internal change gets one only if a release reader would care. **New bullets go at the BOTTOM of their section**, directly above the next section header — two PRs then append to different ends of the file and merge cleanly instead of colliding on the top line (the convention that ended the per-merge CHANGELOG conflicts).
- **`metadata.db` is user data.** Schema changes go through a migration in `db.py` (bump `SCHEMA_VERSION`; the v3 migration is the pattern). Never commit a `.db` file.
- **External binaries**: argument lists, a `timeout`, the expected artifact verified after a zero exit, and graceful degradation when the binary is missing (`find_handbrake()`). `tools/` is that boundary by contract but not yet by fact: short ffprobe reads in `engine/crop.py`, `engine/samples.py` and `engine/pipeline.py`, the unwatched encode when the stall watchdog is off (#62), and the dashboard's folder opener in `server/app.py` still call `subprocess` directly. A new read belongs in the matching `tools/` module, not in another copy.
- **Background work** you start (an encode, a watch loop) never blocks the session and comes with a note on how to stop it.

## Where to read more

| Question | Read |
|---|---|
| Layers, root modules, package conventions | `src/shrinkray/AGENTS.md` |
| CRF search, plan gates, encode/verify, bars, watchdog, lock | `src/shrinkray/engine/AGENTS.md` |
| Routes, token auth, path containment, jobs, SSE | `src/shrinkray/server/AGENTS.md` |
| The subprocess boundary, one module per binary | `src/shrinkray/tools/AGENTS.md` |
| The test suite, fixtures, patch seams | `tests/AGENTS.md` |
| The Svelte dashboard and its toolchain | `ui/AGENTS.md` |
| Product direction, modes, deployment target | `PRODUCT.md` |
| Every gate of the encode decision | `docs/encode-decision.md` |
| The plan cache | `docs/plan-reuse.md` |
| Measuring VMAF parity with ab-av1 by hand, on real content | `docs/vmaf-parity.md` |
| Crop measurements (its #131 two-measurement decision is superseded by #181's single timeline detector) | `docs/black-bar-spike.md` |
| The container deployment | `docs/docker.md` |
| The terminal control surface (`shrinkray setup`) | `docs/tui.md` |

**Precedence when documents disagree:** `docs/encode-decision.md` ("where the code disagrees with this document, the code is the defect") > this file > `docs/decisions/0001-module-layout.md` > PRODUCT.md. A disagreement you find is itself an issue to open.

Traps that are not obvious from the code:

- **Moving `validate_path` silently disables CodeQL's path sanitiser**: `.github/codeql/custom/ValidatePathSanitizer.qll` recognises it only in `src/shrinkray/server/app.py`.
- **The dashboard's API contract is duplicated by hand**: a FastAPI field not added to `ui/src/lib/api.ts` is invisible to the dashboard. `ui/dist` is build output — never edit it.
- **The container needs `/media` writable**, because the resident mode encodes in place; a read-only mount needs `$SHRINKRAY_WATCH_OUTPUT` on a writable volume. `.devcontainer/` ships an ffmpeg without libvmaf: lint and test there, not the smoke tests.

## Working alongside others

Several agents and humans work in this repository at once.

- **The assignment and the claim comment are the lock.** They are the only coordination that works across checkouts and sessions; a local branch is invisible to everyone else. Never start on an issue another person or session holds (Process, step 1).
- **Hand off explicitly.** If you stop mid-way, keep the assignment and comment what is done and what is left, so the next agent resumes instead of restarting. Unassign only when abandoning.
- **Uncommitted changes belong to whoever made them.** Check `git status` before `git add -A` or `git stash`, stage only your own files, and do not edit a file you have not read.
- **`main` moves.** After pulling, run the gate again before you continue; expect to merge `origin/main` into any PR that lives longer than a few hours.
