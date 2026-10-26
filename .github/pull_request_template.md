> **For the human reviewer** (a claim is backed by a pointer — a command, a link, a finding number, a file+function — or belongs under Not verified; a `needs-human-review` PR is not ready without this block)
> - **Does:** <one sentence, artifact not effect: "changes the rename-aside step in `replace.py`", not "makes replace safe">
> - **Why a human:** <the trigger area, named: the replace/retain/rename path, the encode decision, server authentication, path containment or routes, a migration, a dependency, CI/gate/release/AGENTS.md/CONTRIBUTING.md — or "n/a" on a PR that needs no label>
> - **Check first:** <the review finding or open dispute that outranks everything else, or "none">
> - **Spot-check:** <one minutes-level action with its expected outcome — a command, a CI run link, a declared reading task — or the honest "no minutes-level check exists for this class">
> - **Not verified:** <what you could not run, and why>

<!-- A claim is backed by a pointer (a command, a link, a finding number, a file+function) or belongs under Not verified — "Why a human" names an area, "Not verified" states a reason. Re-check this block against the final diff as the last act before applying `needs-human-review` (AGENTS.md, step 6); an edit after the review gets a one-line PR comment. -->

Closes #

## Problem and root cause

<!-- What was wrong, in your own words, and WHY it happened, not only where. For a new feature: the need and the constraint that shaped the design. -->

## Change

<!-- What changed and why this approach. Decisions you made on your own and the trade-offs you weighed. -->

## Verification

<!-- "Verified" means you ran it and saw the result. Say what you did not run and why. -->

- [ ] `tools/gate.sh` passes
- [ ] A test fails without this change and passes with it (or: why no test can)
- [ ] `ui/` checks (`npm ci && npm run check && npm run test && npm run build`), or n/a
- [ ] Smoke tests (`SHRINKRAY_SMOKE=1`) if the scan, probe, encode, watch or replace path changed, or why not
- [ ] `uv audit --locked` if dependencies changed, or n/a
- [ ] `CHANGELOG.md` entry, docs updated

## Edge cases considered

<!-- Which apply and how they are handled: empty input, unicode and long paths, Windows vs POSIX, a crash mid-write, a full disk, a missing binary, huge files, concurrent encodes, HDR/10-bit. -->

## Risk: what a reviewer should look at

<!-- The part most likely to be wrong, beyond what the `For the human reviewer` brief at the top already says. Anything touching the replace, retain or rename path, the encode decision, server authentication, path containment or routes, a migration, a dependency, CI, the gate, the release workflow, AGENTS.md or CONTRIBUTING.md gets the `needs-human-review` label. -->

## Independent review

<!-- Link the "Independent review" comment and the reply that answers every finding. -->
