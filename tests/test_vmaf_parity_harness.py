"""The parity harness refuses to score a pair whose frames are not the same ones
(issue #408).

`scripts/vmaf_parity.py` reported a 1448-frame reference against a 1445-frame
HandBrake output as a legitimate result: VMAF 17.71, matched to four decimals by
ab-av1, and written into the results table as evidence. The two tools agreed
because both compared misaligned frames. A frame-count check is the only cheap
signal that catches this, and it must fail the run rather than print a number.

The pair is synthesised here (ffmpeg, no real media, no HandBrake): a reference
with N frames and a distorted side with N-3. The gate runs before any
measurement, so the test asserts on the gate and on the report, never on libvmaf.
"""

import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "vmaf_parity.py"
FFMPEG_OK = shutil.which("ffmpeg") is not None


def load_parity_module():
    """Imports scripts/vmaf_parity.py without adding the repo root to sys.path."""
    spec = importlib.util.spec_from_file_location("vmaf_parity_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestVideoFrameCount(unittest.TestCase):
    @unittest.skipUnless(FFMPEG_OK, "ffmpeg required to build the fixture")
    def test_it_counts_the_frames_it_built(self) -> None:
        parity = load_parity_module()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=64x64:rate=10:duration=5",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            self.assertEqual(parity.video_frame_count(clip), 50)

    def test_a_missing_file_is_none(self) -> None:
        parity = load_parity_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(parity.video_frame_count(Path(tmp) / "absent.mkv"))


class TestPairDimensions(unittest.TestCase):
    @unittest.skipUnless(FFMPEG_OK, "ffmpeg required to build the fixture")
    def test_a_4k_clip_selects_the_4k_model(self) -> None:
        parity = load_parity_module()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "uhd.mkv"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=3840x2160:rate=1:duration=1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            width, height, bit_depth = parity.pair_dimensions(clip)
            self.assertEqual((width, height), (3840, 2160))
            self.assertEqual(bit_depth, 8)
            # the recipe's model must follow the real geometry (issue #309):
            # without dimensions the harness scored 4K pairs with the 1080p model
            from shrinkray.tools.vmaf import vmaf_recipe

            with_dims = vmaf_recipe(width=width, height=height, bit_depth=bit_depth)
            without = vmaf_recipe()
            self.assertNotEqual(
                with_dims.model,
                without.model,
                "a 4K pair must not fall back to the 1080p model",
            )
            self.assertIn("4k", with_dims.model)

    def test_a_missing_file_yields_no_dimensions(self) -> None:
        parity = load_parity_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(parity.pair_dimensions(Path(tmp) / "absent.mkv"), (None, None, None))


class TestMismatchedPairIsNotScored(unittest.TestCase):
    @unittest.skipUnless(FFMPEG_OK, "ffmpeg required to build the fixture")
    def test_a_frame_count_mismatch_fails_without_measuring(self) -> None:
        parity = load_parity_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "ref.mkv"
            distorted = root / "dis.mkv"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=64x64:rate=10:duration=5",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(reference),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(reference),
                    "-vf",
                    "trim=start_frame=3,setpts=PTS-STARTPTS",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(distorted),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            self.assertNotEqual(
                parity.video_frame_count(reference),
                parity.video_frame_count(distorted),
                "the fixture must differ by frames for this test to mean anything",
            )

            results = root / "results.md"
            argv = [
                "vmaf_parity.py",
                "--pair",
                str(reference),
                str(distorted),
                "--label",
                "misaligned",
            ]
            captured = io.StringIO()
            with (
                mock.patch.object(parity, "RESULT_FILE", results),
                mock.patch.object(parity, "measure_ours") as measure,
                mock.patch.object(parity, "ab_av1_vmaf") as ab_av1,
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(captured),
            ):
                code = parity.main()

            self.assertEqual(code, 1, "a misaligned pair must fail the run")
            measure.assert_not_called()
            ab_av1.assert_not_called()
            table = results.read_text()
            self.assertIn("misaligned", table)
            self.assertNotIn("PASS", table)
            # the pair was never compared, so the summary must not report it as
            # outside the tolerance
            printed = captured.getvalue()
            self.assertIn("1 pair(s) did not produce a usable comparison", printed)
            self.assertIn("0 outside the 0.1 tolerance", printed)


if __name__ == "__main__":
    unittest.main()
