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
        gate = (REPO / "tools" / "gate.sh").read_text(encoding="utf-8")
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
        self.assertIn("UV_PYTHON_DOWNLOADS=never", gate)
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
