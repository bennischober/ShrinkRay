# Contributing to ShrinkRay

Thanks for wanting to contribute. This project is built to be given to the
community — installed by strangers, pointed at video libraries that cannot be
re-acquired, run unattended. That standard, not "it works on my machine",
is what your contribution is held to. This file covers the human-facing part
(how to propose a change, what will not be accepted); the machine-facing part
lives in [AGENTS.md](AGENTS.md), which documents the same gate, conventions
and issue workflow for coding agents, together with the working method and
the hard invariants every change is held to. **The two must not drift**, so
when you change one, check the other.

## Before you start

1. **Check the issue tracker.** Work happens on issues. Pick an unassigned
   one (or propose a new one — see below). If you take one, assign it to
   yourself first; an assigned issue means work in progress.
2. **Read [PRODUCT.md](PRODUCT.md)** for the product contract, and the hard
   invariants in [AGENTS.md](AGENTS.md), which every change is held to. ShrinkRay changes a file's **bytes**,
   never its **identity**: no renaming, moving, organizing, deduplicating,
   acquiring, or playing. A feature whose usefulness depends on knowing what
   a file is *called* is out of scope and will be declined.
3. **Branch from an up-to-date `main`** — never work on `main` directly, and
   never push to it. Every change arrives through a pull request:

   ```sh
   git switch main && git pull --ff-only
   git switch -c <type>/<issue>-<slug>   # e.g. fix/38-test-pollutes-tree
   ```

## The gate (before every push)

```sh
tools/gate.sh
```

The gate is one script, and CI, the release job and the dev image run the same one, so this file deliberately does not copy its commands; read the script to see what it checks. It runs mypy for both Linux and Windows, because a bare `mypy` only checks the host's view and misses an API that exists on one platform only. Beyond the gate: if you changed `ui/`, also run `npm ci && npm run check && npm run test && npm run build` in `ui/`. If you changed `pyproject.toml`, re-lock with `uv lock` (CI rejects a stale `uv.lock`) and run `uv audit --locked`; CI runs the audit on every PR, so a new advisory can fail a PR that touched no dependency. If you changed the scan, probe, encode, watch or replace path, run the smoke tests against the real binaries (`SHRINKRAY_SMOKE=1 uv run pytest tests/test_smoke.py --no-cov` — a single file never reaches the coverage floor — needs ffmpeg with libvmaf and HandBrakeCLI), or say in the PR that you could not. If you cannot make the gate pass, stop and report the blocker; do not push around it.

## Test rules

- New behaviour ships with a test that would fail without it; a bug fix ships
  with a test that would have caught the bug.
- Tests must not require the ffmpeg/HandBrake binaries or network access —
  mock, or use `tmp_path`/`tempfile` fixtures.
- Tests that touch the filesystem use **real temp directories, never
  fictional paths**: `D:/Movies` is absolute on Windows but relative on
  Linux, and scope containment behaves differently — a test that passes on
  your machine may silently invert on CI.
- Tests never pollute the working tree. There is a conftest guard that fails
  the session if a test leaves files behind — this rule exists because that
  exact defect shipped once (issue #38).
- No dependence on wall-clock time or timing luck.

## Commit and PR conventions

- Conventional Commits (`feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`, `ci`, `build`, `style`), with the body explaining why.
- Fill in the pull request template: the problem and its root cause, the change, how it was verified (a PR that cannot state its verification is not ready), the edge cases considered, and what a reviewer should look at. Reference the issue (`Closes #N`) so it links and closes on merge.
- Every PR that changes code or docs adds a `CHANGELOG.md` entry under `## [Unreleased]`.
- **Every PR gets a reader who did not write it.** For a human contributor that is the maintainer's review; AGENTS.md describes the independent review agents run. A PR is merged only when every CI check has passed, it has no conflicts, and every review finding has been answered.
- **Some changes always wait for a maintainer**, whoever wrote them: anything that changes the behaviour of the replace, retain or rename path; the encode decision (eligibility gates, CRF search, VMAF target, encoder choice, crop); server authentication, path containment or routes; a database migration; a new dependency; or CI, the gate, the release workflow, AGENTS.md or this file. When unsure whether one applies, it applies. These carry the `needs-human-review` label; a maintainer adds it if you cannot, and the PR body then opens with the template's `For the human reviewer` brief — what the change does, why a human is needed, what to check first, one minutes-level spot-check with its expected outcome (or the honest declaration that none exists for this class), and what was not verified — so the maintainer gets into the loop in minutes without reading the whole diff.

## What a change to the quality decision requires

The question this tool answers per file — *is this file bigger than its
quality requires?* — is decided by measurement (VMAF), not opinion. A change
to how that decision is made (targets, presets, probe strategy) needs a
measurement or study behind it, not just a passing test suite. Say in the PR
what was measured and what changed.

## Proposing a change

Open an issue first for anything that changes the product's shape —
especially anything near the scope boundary (AGENTS.md, hard invariant "Bytes, never identity"), anything destructive
(there is no code path that deletes a source media file, and requests to add
one will be declined), and anything that changes the encode target (AV1-only,
VMAF-driven, per PRODUCT.md). As a human contributor, small, uncontroversial fixes (docs, typos, test gaps) can go straight to a PR that states the problem in the template; agents always start from an issue (AGENTS.md, Process).

## Security issues

Do not open a public issue for anything you believe is exploitable — see
[SECURITY.md](SECURITY.md) for the private disclosure path and the current
security posture.
