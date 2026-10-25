"""The missing-binary messages (issue #195): on WSL, a failed execve of an
absent binary surfaces as PermissionError [Errno 13], so the raw-OSError
interpolation told the operator "Permission denied" about a file that does
not exist. The fix names the absent binary and the install action; a binary
present but not executable still gets the permission cause.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.tools import ffprobe, vmaf


class TestMissingBinaryMessages(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_ffprobe_missing_names_the_install_action(self) -> None:
        with (
            patch(
                "shrinkray.tools.ffprobe.subprocess.run",
                side_effect=FileNotFoundError(2, "No such file or directory"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            ffprobe.run_ffprobe(self.movie)
        message = str(ctx.exception)
        self.assertIn("ffprobe not found", message)
        self.assertIn("Install ffmpeg", message)
        self.assertNotIn("Permission denied", message)

    def test_run_ffprobe_unexecutable_names_the_permission_cause(self) -> None:
        with (
            patch(
                "shrinkray.tools.ffprobe.subprocess.run",
                side_effect=PermissionError(13, "Permission denied"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            ffprobe.run_ffprobe(self.movie)
        message = str(ctx.exception)
        self.assertIn("executable", message)
        self.assertNotIn("ffprobe not found", message)

    def test_detect_vmaf_style_missing_names_the_install_action(self) -> None:
        saved = vmaf._vmaf_style
        vmaf._vmaf_style = None
        try:
            with (
                patch(
                    "shrinkray.tools.vmaf.subprocess.run",
                    side_effect=FileNotFoundError(2, "No such file or directory"),
                ),
                self.assertRaises(RuntimeError) as ctx,
            ):
                vmaf.detect_vmaf_style()
            message = str(ctx.exception)
            self.assertIn("not found", message)
            self.assertIn("Install a build with libvmaf support", message)
            self.assertNotIn("Permission denied", message)
        finally:
            vmaf._vmaf_style = saved

    def test_detect_vmaf_style_unexecutable_names_the_permission_cause(self) -> None:
        saved = vmaf._vmaf_style
        vmaf._vmaf_style = None
        try:
            with (
                patch(
                    "shrinkray.tools.vmaf.subprocess.run",
                    side_effect=PermissionError(13, "Permission denied"),
                ),
                self.assertRaises(RuntimeError) as ctx,
            ):
                vmaf.detect_vmaf_style()
            message = str(ctx.exception)
            self.assertIn("executable", message)
            self.assertNotIn("not found", message)
        finally:
            vmaf._vmaf_style = saved


if __name__ == "__main__":
    unittest.main()
