"""HDR fixture tests (issue #99): the committed HDR10 clip is the regression
anchor for HDR detection and the HDR encoder path.

The fixture (`tests/fixtures/hdr10_gradient_320x180_2s.mp4`) is self-generated
with ffmpeg (libx265, 10-bit PQ, bt2020 primaries, HDR10 mastering display
metadata); the exact command is recorded in the module docstring below. It is
8 KB, license-safe, and needs no network.

The dict-level tests always run (no binary needed): they assert against the
RECORDED ffprobe dict (`hdr10_probe.json`, captured from the fixture) so a
regression in `media.is_hdr` or the encoder pick fails even on a machine
without ffprobe. The probe-level test re-runs ffprobe on the clip when the
binary exists and asserts the fixture still probes as recorded — that catches
fixture decay (re-encoding, corruption) instead of only testing the snapshot.

Generation command (ffmpeg with libx265; -tag:v hvc1 for QuickTime compat):

    ffmpeg -y -f lavfi -i "gradients=s=320x180:d=2:c0=#0B0B14:c1=#7B2D26:x0=0:y0=0:x1=320:y1=180" \
      -pix_fmt yuv420p10le -c:v libx265 -preset ultrafast \
      -x265-params "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:\
master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):\
max-cll=(1000,400):max-fall=(400,400)" -an \
      tests/fixtures/hdr10_gradient_320x180_2s.mp4
"""

import json
import shutil
import unittest
from pathlib import Path
from subprocess import run as run_subprocess
from unittest import skipUnless

from shrinkray import config as config_helper
from shrinkray.engine.encoders import DEFAULT_ENCODER, encoder_for
from shrinkray.tools import ffprobe as read_media

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLIP = FIXTURES / "hdr10_gradient_320x180_2s.mp4"
RECORDED = FIXTURES / "hdr10_probe.json"

# the shipped config.json's codec.hdr markers (the documented HDR signature):
# an empty config means "no HDR markers configured", so the tests construct the
# Conf explicitly instead of relying on defaults
HDR_CONF_DATA = {"codec": {"hdr": {"color_space": ["bt2020nc"], "color_transfer": ["smpte2084"]}}}


def _hdr_conf():
    return config_helper.Conf(HDR_CONF_DATA)


class TestHdrFixture(unittest.TestCase):
    """The committed HDR10 clip anchors HDR detection and the encoder path."""

    def setUp(self):
        self.assertTrue(
            CLIP.exists(), f"the HDR fixture is missing: {CLIP} (see the module docstring)"
        )
        self.recorded = json.loads(RECORDED.read_text(encoding="utf-8"))

    def test_recorded_probe_dict_is_the_hdr_signature(self):
        """The recorded dict IS HDR10: 10-bit HEVC, bt2020nc/smpte2084."""
        self.assertEqual(self.recorded["codec_name"], "hevc")
        self.assertEqual(self.recorded["pix_fmt"], "yuv420p10le")
        self.assertEqual(self.recorded["color_space"], "bt2020nc")
        self.assertEqual(self.recorded["color_transfer"], "smpte2084")
        self.assertEqual(self.recorded["color_primaries"], "bt2020")

    def test_is_hdr_true_on_the_recorded_dict(self):
        """is_hdr flags the fixture's stream dict (regression: detection logic
        can drift against the configured markers — this fails before any
        binary runs)."""
        video = dict(self.recorded)
        self.assertTrue(read_media.is_hdr(video, _hdr_conf()))

    def test_is_hdr_false_for_sdr_dict(self):
        """The same dict with SDR markers is NOT HDR (the negative control:
        guards against is_hdr ever degrading to always-true)."""
        sdr = dict(self.recorded)
        sdr["color_space"] = "bt709"
        sdr["color_transfer"] = "bt709"
        sdr["color_primaries"] = "bt709"
        self.assertFalse(read_media.is_hdr(sdr, _hdr_conf()))

    def test_is_hdr_respects_empty_marker_config(self):
        """An unconfigured (empty) marker list flags nothing — HDR detection
        is config-driven by design."""
        self.assertFalse(read_media.is_hdr(dict(self.recorded), config_helper.Conf({})))

    def test_encoder_for_hdr_picks_the_10bit_av1_encoder(self):
        """The HDR path picks svt_av1_10bit; SDR picks the default (issue #21:
        AV1-only output, one encoder decision per source)."""
        self.assertEqual(encoder_for(True), "svt_av1_10bit")
        self.assertEqual(encoder_for(False), DEFAULT_ENCODER)

    @skipUnless(shutil.which("ffprobe"), "ffprobe not on PATH")
    def test_fixture_still_probes_as_recorded(self):
        """Fixture decay check: re-probe the committed clip and assert the
        color signature still matches the recorded dict (catches accidental
        re-encoding or corruption of the binary fixture)."""
        proc = run_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-of",
                "json",
                str(CLIP),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stream = json.loads(proc.stdout)["streams"][0]
        for key in ("codec_name", "pix_fmt", "color_space", "color_transfer", "color_primaries"):
            self.assertEqual(
                stream.get(key),
                self.recorded[key],
                f"fixture decay: {key} changed (see the module docstring to regenerate)",
            )


if __name__ == "__main__":
    unittest.main()
