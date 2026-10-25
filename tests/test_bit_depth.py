"""Bit depth: the source's depth decides the output encoder (issue #234).

10-bit is not an HDR property — plenty of SDR masters ship as yuv420p10le —
and the old encoder_for(hdr) dropped a 10-bit SDR source to 8 bits, banding
gradients on the exact path the product exists for. The maintainer's decision
(2026-10-25, recorded in the issue): 10-bit stays 10-bit, 8-bit stays 8-bit,
HDR keeps its 10-bit override, and an unknown depth falls back to the HDR
answer instead of silently truncating an unreadable depth.
"""

import shutil
import unittest
from pathlib import Path
from subprocess import run as run_subprocess
from tempfile import TemporaryDirectory
from unittest import skipUnless

from shrinkray.engine.encoders import DEFAULT_ENCODER, encoder_for
from shrinkray.tools.ffprobe import bit_depth, get_data


class TestBitDepthParsing(unittest.TestCase):
    """The dict-level parser: no binary needed."""

    def test_bits_per_raw_sample_wins(self):
        self.assertEqual(bit_depth({"bits_per_raw_sample": "10"}), 10)
        self.assertEqual(bit_depth({"bits_per_raw_sample": "8"}), 8)
        self.assertEqual(bit_depth({"bits_per_raw_sample": "12"}), 12)

    def test_a_zero_or_garbage_raw_sample_falls_to_pix_fmt(self):
        # some muxers report 0 for bits_per_raw_sample
        self.assertEqual(bit_depth({"bits_per_raw_sample": "0", "pix_fmt": "yuv420p10le"}), 10)
        self.assertEqual(bit_depth({"bits_per_raw_sample": "x", "pix_fmt": "yuv420p10le"}), 10)

    def test_pix_fmt_trailing_depth(self):
        self.assertEqual(bit_depth({"pix_fmt": "yuv420p10le"}), 10)
        self.assertEqual(bit_depth({"pix_fmt": "yuv444p12le"}), 12)
        self.assertEqual(bit_depth({"pix_fmt": "yuv420p"}), None)
        self.assertEqual(bit_depth({"pix_fmt": "gray16le"}), 16)

    def test_no_information_is_none(self):
        self.assertIsNone(bit_depth({}))
        self.assertIsNone(bit_depth({"pix_fmt": ""}))


class TestEncoderFollowsBitDepth(unittest.TestCase):
    """The decision matrix: depth decides, HDR keeps its override."""

    def test_a_10bit_sdr_source_stays_10bit(self):
        self.assertEqual(encoder_for(10, False), "svt_av1_10bit")

    def test_an_8bit_sdr_source_stays_8bit(self):
        self.assertEqual(encoder_for(8, False), DEFAULT_ENCODER)

    def test_a_12bit_source_stays_high(self):
        self.assertEqual(encoder_for(12, False), "svt_av1_10bit")

    def test_an_unknown_depth_falls_back_to_the_hdr_answer(self):
        # never silently truncate a depth the probe failed to read
        self.assertEqual(encoder_for(None, False), DEFAULT_ENCODER)
        self.assertEqual(encoder_for(None, True), "svt_av1_10bit")

    def test_hdr_stays_10bit_regardless(self):
        self.assertEqual(encoder_for(10, True), "svt_av1_10bit")
        self.assertEqual(encoder_for(8, True), "svt_av1_10bit")


class TestRealProbeRoundTrip(unittest.TestCase):
    """A real 10-bit clip through get_data (runs wherever ffmpeg exists —
    the same opt-in shape as the HDR fixture's live probe)."""

    @skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    def test_a_synthetic_10bit_clip_probes_as_10bit(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "ten_bit.mp4"
            run_subprocess(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=duration=1:size=320x240:rate=25",
                    "-pix_fmt",
                    "yuv420p10le",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "18",
                    "-an",
                    str(clip),
                ],
                check=True,
                timeout=300,
            )
            data = get_data(clip)
            self.assertEqual(data["bit_depth"], 10)
            self.assertFalse(data["hdr"], "a 10-bit SDR clip must not be flagged HDR")
            # the decision: this source gets the 10-bit encoder although SDR
            self.assertEqual(encoder_for(data["bit_depth"], data["hdr"]), "svt_av1_10bit")

    @skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    def test_a_synthetic_8bit_clip_probes_as_8bit(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "eight_bit.mp4"
            run_subprocess(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=duration=1:size=320x240:rate=25",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "18",
                    "-an",
                    str(clip),
                ],
                check=True,
                timeout=300,
            )
            data = get_data(clip)
            self.assertEqual(data["bit_depth"], 8)
            self.assertEqual(encoder_for(data["bit_depth"], data["hdr"]), DEFAULT_ENCODER)


if __name__ == "__main__":
    unittest.main()
