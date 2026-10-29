"""The gate's mypy steps survive a beta interpreter (issue #407).

The gate is one script shared by CI, the release job and the dev image; this
file pins the contract pieces that a silent edit could drop: the segfault
fallback's presence and shape. The segfault itself cannot be reproduced in a
test (it lives in mypyc vs the host interpreter), so the guard is textual -
the same drift-guard style tests/test_container.py uses for the scripts -
and the fallback's behaviour was demonstrated on the real affected host.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestGateMypyFallback(unittest.TestCase):
    def test_the_gate_retries_a_mypy_segfault_under_a_stable_interpreter(self) -> None:
        """Issue #407: the gate's mypy steps must not leave the gate unusable
        on a host whose .python-version resolves to a beta interpreter
        (mypy's compiled core exits 139 there before printing anything). The
        fallback must be LOUD - a warning naming the interpreters, and the
        re-run's own verdict deciding the gate - never a silent retry."""
        gate = (REPO / "tools" / "gate.sh").read_text(encoding="utf-8")
        self.assertIn("run_mypy linux", gate)
        self.assertIn("run_mypy win32", gate)
        # the segfault is distinguished from real failures: only 139 retries
        self.assertIn("-ne 139", gate)
        # the rc is captured before branching: after `if cmd; then` with no else,
        # $? is 0 even for a segfault (measured in issue #407's demonstration)
        self.assertIn('uv run mypy --platform "$platform" || rc=$?', gate)
        # the retry tries stable interpreters, newest first
        self.assertIn("for candidate in 3.14 3.13 3.12", gate)
        # the warning names the substituted interpreter (honesty, not silence)
        self.assertIn("not the project's .python-version interpreter", gate)
        # a fallback that finds nothing fails with the segfault's own code
        self.assertIn("return 139", gate)

    def test_the_gate_still_runs_both_mypy_platforms(self) -> None:
        """The two-platform contract (AGENTS.md): linux AND win32 both run,
        fallback or not."""
        gate = (REPO / "tools" / "gate.sh").read_text(encoding="utf-8")
        self.assertIn("run_mypy linux", gate)
        self.assertIn("run_mypy win32", gate)


if __name__ == "__main__":
    unittest.main()
