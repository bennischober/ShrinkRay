"""Regression tests for the HandBrake capability probe (issues #165, #654).

Issue #165: HandBrakeCLI 1.11.2 writes the `--encoder-preset-list` output to
STDERR (stdout stays empty), so a probe that reads only stdout reports a
perfectly good toolchain as unusable and the resident app refuses to start.
These tests pin the merged-stream behavior.

Issue #654: the tests used to write and exec a real stub shell script, and on
the Python 3.15 CI job a freshly written script intermittently failed to exec,
so the same tree went red then green on a rerun. The probe is a parse of the
child's output (`subprocess.run` drains both pipes to EOF), so the tests fake
`subprocess.run` at the module's own seam; no child spawns and the verdict no
longer depends on the environment.
"""

import subprocess
import unittest
from unittest.mock import patch

from shrinkray.tools.handbrake import supports_svt_av1

PRESET_LIST = b"banner\n    10\n    -1\n"


def _completed(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestSupportsSvtAv1(unittest.TestCase):
    def _probe(self, completed: subprocess.CompletedProcess) -> bool:
        with patch("shrinkray.tools.handbrake.subprocess.run", return_value=completed) as run:
            supported = supports_svt_av1("HandBrakeCLI")
        self.assertEqual(run.call_count, 1, "the probe must not spawn a real child")
        self.assertEqual(
            run.call_args.args[0],
            ["HandBrakeCLI", "--encoder-preset-list", "svt_av1"],
        )
        return supported

    def test_a_stderr_only_preset_list_counts_as_supported(self) -> None:
        """The list on stderr (stdout empty) means supported (issue #165)."""
        self.assertTrue(self._probe(_completed(stderr=PRESET_LIST)))

    def test_a_stdout_only_preset_list_also_counts(self) -> None:
        """Older builds may write the list to stdout; both are supported."""
        self.assertTrue(self._probe(_completed(stdout=PRESET_LIST)))

    def test_an_unknown_encoder_answers_only_auto(self) -> None:
        """Only the generic 'auto' preset means NOT supported."""
        self.assertFalse(self._probe(_completed(stderr=b"    auto\n")))

    def test_a_failing_probe_is_not_supported(self) -> None:
        """A nonzero exit is not supported, even with the list printed."""
        self.assertFalse(self._probe(_completed(returncode=3, stderr=PRESET_LIST)))


if __name__ == "__main__":
    unittest.main()
