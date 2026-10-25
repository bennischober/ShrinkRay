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
- [ ] Smoke tests (`SHRINKRAY_SMOKE=1`) if the probe, encode, watch or replace path changed, or why not
- [ ] `uv audit --locked` if dependencies changed, or n/a
- [ ] `CHANGELOG.md` entry, docs updated

## Edge cases considered

<!-- Which apply and how they are handled: empty input, unicode and long paths, Windows vs POSIX, a crash mid-write, a full disk, a missing binary, huge files, concurrent encodes, HDR/10-bit. -->

## Risk: what a reviewer should look at

<!-- The part most likely to be wrong. Anything touching the replace/retain path, the encode decision, auth/path containment, a migration, a dependency or CI gets the `needs-human-review` label. -->

## Independent review

<!-- Link the "Independent review" comment; every finding answered in its thread. -->
