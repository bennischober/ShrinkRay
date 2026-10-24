"""Regression tests for the HandBrake capability probe (issue #165).

The container smoke test found a real defect: HandBrakeCLI 1.11.2 writes
the `--encoder-preset-list` output to STDERR (stdout stays empty), so a
probe that reads only stdout reports a perfectly good toolchain as unusable
and the resident app refuses to start. These tests pin the merged-stream
behavior with a stub binary, no real HandBrake required.
"""

import stat
import tempfile
import unittest
from pathlib import Path

from shrinkray.tools.handbrake import supports_svt_av1

STUB_BODY = """#!/bin/sh
# emit the preset list the way HandBrakeCLI 1.11.2 does: on STDERR
if [ "$1" = "--encoder-preset-list" ] && [ "$2" = "svt_av1" ]; then
    echo "banner" >&2
    echo "    10" >&2
    echo "    -1" >&2
    exit 0
fi
exit 1
"""


def _write_stub(tmp: str, body: str) -> str:
    stub = Path(tmp) / "hb_stub.sh"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


class TestSupportsSvtAv1(unittest.TestCase):
    def test_a_stderr_only_preset_list_counts_as_supported(self) -> None:
        """The list on stderr (stdout empty) means supported (issue #165)."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_stub(tmp, STUB_BODY)
            self.assertTrue(supports_svt_av1(binary))

    def test_a_stdout_only_preset_list_also_counts(self) -> None:
        """Older builds may write the list to stdout; both are supported."""
        body = STUB_BODY.replace(">&2", "")
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_stub(tmp, body)
            self.assertTrue(supports_svt_av1(binary))

    def test_an_unknown_encoder_answers_only_auto(self) -> None:
        """Only the generic 'auto' preset means NOT supported."""
        body = """#!/bin/sh
echo "    auto" >&2
exit 0
"""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_stub(tmp, body)
            self.assertFalse(supports_svt_av1(binary))

    def test_a_failing_probe_is_not_supported(self) -> None:
        """A nonzero exit is not supported (the honest default)."""
        body = """#!/bin/sh
exit 3
"""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_stub(tmp, body)
            self.assertFalse(supports_svt_av1(binary))


if __name__ == "__main__":
    unittest.main()
