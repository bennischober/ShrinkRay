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

    def test_conf_without_hdr_section_uses_the_code_defaults(self):
        """Issue #236, the maintainer's decision: HDR detection defaults in
        code. A config that says nothing about HDR still classifies the
        documented signature (bt2020nc/smpte2084) as HDR — the container
        must not depend on its baked config.json for detection."""
        conf = config_helper.Conf({})
        self.assertEqual(conf.hdr_color_spaces, ["bt2020nc"])
        self.assertEqual(conf.hdr_color_transfers, ["smpte2084"])
        self.assertTrue(read_media.is_hdr(dict(self.recorded), conf))

    def test_an_explicitly_empty_marker_list_flags_nothing(self):
        """The opt-OUT stays possible: a config that EXPLICITLY empties the
        marker lists flags nothing (an operator narrowing detection is
        honored — the defaults are overridable, the decision is theirs)."""
        conf = config_helper.Conf({"codec": {"hdr": {"color_space": [], "color_transfer": []}}})
        self.assertFalse(read_media.is_hdr(dict(self.recorded), conf))

    def test_encoder_for_hdr_picks_the_10bit_av1_encoder(self):
        """The HDR path picks svt_av1_10bit; SDR picks the default (issue #21:
        AV1-only output, one encoder decision per source)."""
        self.assertEqual(encoder_for(10, True), "svt_av1_10bit")
        self.assertEqual(encoder_for(None, True), "svt_av1_10bit")
        self.assertEqual(encoder_for(8, False), DEFAULT_ENCODER)

    def test_encoder_for_follows_the_source_bit_depth(self):
        """Issue #234: the SOURCE's bit depth decides, not an HDR guess. A
        10-bit SDR master keeps its depth; an 8-bit source stays 8-bit even
        when HDR flags would suggest more; an unknown depth falls back to
        the HDR answer instead of silently truncating an unreadable depth."""
        self.assertEqual(encoder_for(10, False), "svt_av1_10bit")
        self.assertEqual(encoder_for(12, False), "svt_av1_10bit")
        self.assertEqual(encoder_for(8, False), DEFAULT_ENCODER)
        self.assertEqual(encoder_for(None, False), DEFAULT_ENCODER)
        self.assertEqual(encoder_for(8, True), "svt_av1_10bit")

    @skipUnless(shutil.which("ffprobe"), "ffprobe not on PATH")
    def test_fixture_still_probes_as_recorded(self):
        """Fixture decay check: re-probe the committed clip and assert the
        color signature still matches the recorded dict (catches accidental
        re-encoding or corruption of the binary fixture).

        The probe is retried once on a truncated/garbled stdout: under heavy
        load (WSL2/drvfs, parallel test runs) ffprobe occasionally returns a
        partial stream with exit 0 - a probe of a static file is idempotent,
        so a retry measures the same thing (observed ~1 in 3 full-suite runs
        locally; never in CI)."""
        stdout = ""
        for attempt in range(2):
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
            stdout = proc.stdout
            try:
                stream = json.loads(stdout)["streams"][0]
                break
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                if attempt == 1:
                    self.fail(f"ffprobe returned unusable stdout twice: {e!r}: {stdout[:80]!r}")
        for key in ("codec_name", "pix_fmt", "color_space", "color_transfer", "color_primaries"):
            self.assertEqual(
                stream.get(key),
                self.recorded[key],
                f"fixture decay: {key} changed (see the module docstring to regenerate)",
            )


if __name__ == "__main__":
    unittest.main()
