# test_media/

Local video files for manual runs and the opt-in smoke tests. **Nothing in
this folder is ever committed** except this README: the media is copyrighted
and far too large for git. `.gitignore` ignores everything else in here, at
any depth.

Unit tests never read this folder (they use temporary directories and mocks,
see AGENTS.md). Anything that needs real media is a smoke test and must skip
cleanly when the files are absent.

`tools/generate-test-media.sh` generates tiny synthetic fixtures into
`synthetic/` (one per gate boundary: bars, short, sub-1080p, old codec,
tiny size) so a full doctor/plan/encode E2E runs in minutes, not hours
(issue #334); the fixture table's skip verdicts are the point.
