"""The gate's mypy steps survive a beta interpreter (issue #407).

The gate is one script shared by CI, the release job and the dev image; this
file pins the contract pieces that a silent edit could drop: the segfault
fallback's presence, its honesty (a candidate's verdict is returned, not
mislabelled), and its isolation (the retry must not re-point the project's
shared .venv). The segfault itself cannot be reproduced in a test (it lives
in mypyc vs the host interpreter), so the guard is textual - the same
drift-guard style tests/test_container.py uses for the scripts - and the
fallback's behaviour was demonstrated on the real affected host.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestGateMypyFallback(unittest.TestCase):
    def test_the_gate_retries_a_mypy_segfault_under_a_stable_interpreter(self) -> None:
        """Issue #407: the gate's mypy steps must not leave the gate unusable
        on a host whose .python-version resolves to a beta interpreter
        (mypy's compiled core exits 139 there before printing anything). The
        fallback must be LOUD and HONEST: the retry runs isolated, a
        candidate's non-139 verdict is returned as-is (a real type error is
        never mislabelled as a missing interpreter), and only an exhausted
        candidate list ends in 139."""
        gate_path = REPO / "tools" / "gate.sh"
        # issue #598: on 2026-11-08 CI read only the last 443 of this file's
        # 4539 bytes, from the page-aligned offset 4096, so the first
        # assertion below failed on a missing string that the file contains.
        # `Path.read_text` cannot start at an offset, no test patches a read
        # primitive (audited: every `Path.read_text` patch is a context
        # manager; there is no `setattr`, `builtins.open` or shared handle),
        # and the file is clean ASCII. So the short read came from outside the
        # suite (a runner-side partial materialization or an `io` defect in
        # CPython 3.15.2). The guard below names a short read with both byte
        # counts instead of leaving a missing-string assertion; it is not a retry.
        gate_bytes = gate_path.read_bytes()
        self.assertEqual(
            len(gate_bytes),
            gate_path.stat().st_size,
            f"the gate script was read short: {len(gate_bytes)} of "
            f"{gate_path.stat().st_size} bytes (issue #598)",
        )
        gate = gate_bytes.decode("utf-8")
        self.assertIn("run_mypy linux", gate)
        self.assertIn("run_mypy win32", gate)
        # the rc is captured before branching: after `if cmd; then` with no
        # else, $? is 0 even for a segfault (measured in issue #407's
        # demonstration)
        self.assertIn('uv run mypy --platform "$platform" || rc=$?', gate)
        # only 139 reaches the fallback; any other rc is returned as-is
        self.assertIn('if [ "$rc" -ne 139 ]; then', gate)
        self.assertIn('return "$rc"', gate)
        # the retry is isolated: it must not re-point the project's shared
        # .venv (review round 1, finding 2)
        self.assertIn("--isolated", gate)
        # command-shaped pins: a bare assertIn("FLAG", gate) is satisfied by
        # the flag's own explanatory comment (review round 4, finding 1 -
        # the round-3 answers' claim that the guard failed on a dropped flag
        # was false as committed)
        self.assertIn("UV_PYTHON_DOWNLOADS=never UV_OFFLINE=1 uv run --isolated", gate)
        # the retry captures its own rc too: a candidate's non-139 verdict is
        # returned, not silently advanced past (review round 1, finding 1)
        self.assertIn(
            'uv run --isolated --python "$candidate" mypy --platform "$platform")" || retry_rc=$?',
            gate,
        )
        self.assertIn('return "$retry_rc"', gate)
        # a candidate that is not installed is SKIPPED, not returned as a
        # verdict (review round 2, finding 1): the probe advances the ladder
        # so the exhausted-list outcome stays reachable
        self.assertIn('uv python find "$candidate"', gate)
        self.assertIn("is not installed; trying the next candidate", gate)
        # the exhausted-list error names the real conditions (candidates
        # segfaulted too, or none installed offline) - not "not installed"
        # alone (review round 1, finding 3)
        self.assertIn("segfaulted too or none is installed", gate)


if __name__ == "__main__":
    unittest.main()
