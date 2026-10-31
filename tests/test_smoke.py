"""Opt-in smoke tests against the REAL toolchain (ffmpeg + HandBrakeCLI).

Skipped by default everywhere (CI, local gate): the suite's contract is
no binaries required. Run explicitly with real binaries on PATH:

    SHRINKRAY_SMOKE=1 uv run pytest tests/test_smoke.py -v --no-cov

--no-cov because pyproject's addopts enforce the suite-wide coverage floor,
which one file never reaches: without it the run fails even when every
smoke test passes (issue #295).

Why this exists: the stall-watchdog false positive (8e86d82) passed
every stub-based test while killing real encodes - stub behavior is not
binary behavior. These tests run the plan -> encode -> reuse cycle with
the real HandBrakeCLI/ffmpeg so a change that breaks real-binary
behavior surfaces here, not in the user's library.

Cost: a synthetic 100s clip at 1920x1080 keeps a full cycle in the
minutes range (the measured cycle: ~80 s per test, 2m41s for the pair).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shrinkray import db, service
from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine.pipeline import encode_with_quality, plan_quality

BINARIES_OK = (
    shutil.which("HandBrakeCLI") is not None
    and shutil.which("ffmpeg") is not None
    and shutil.which("ffprobe") is not None
)


@unittest.skipUnless(os.environ.get("SHRINKRAY_SMOKE"), "smoke: opt-in (SHRINKRAY_SMOKE=1)")
@unittest.skipUnless(BINARIES_OK, "real ffmpeg + HandBrakeCLI + ffprobe required on PATH")
class TestSmokePlanReuse(unittest.TestCase):
    """plan -> record -> encode reuses the plan (real binaries end-to-end)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db_file = self.root / "test.db"
        self.clip = self.root / "clip.mkv"
        self._make_clip(self.clip, seconds=100)
        self.connection = db.connect(self.db_file)
        # the plan lookup is keyed by the scanned library's file row
        service.ensure_file_row(self.connection, self.clip)
        # record_plan reads config.handbrake_encoder; explicit (no user config)
        self.config = SimpleNamespace(handbrake_encoder="svt_av1")

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    @staticmethod
    def _make_clip(path: Path, seconds: int) -> None:
        """Generates a REAL media file: synthetic video + tone audio.

        1920x1080 keeps the source inside the encode decision's 1080p-class
        floor (issue #233): a below-floor source is skipped as 'resolution
        too low' before any plan runs, so the reuse path under test never
        starts (issue #442). smptehdbars is a synthetic source the plan's
        svt_av1 search can actually reach the default 96.0 VMAF target on -
        testsrc's moving geometry measured a ~82 ceiling in the real probe
        pipeline, so the plan failed with 'target unreachable' before the
        reuse path under test could start (measured, issue #442). h264 crf
        18 keeps the source compressible-but-not-degenerate so the re-encode
        actually finds a size gain (a 'no size gain' plan would skip the
        reuse path).
        """
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"smptehdbars=duration={seconds}:size=1920x1080:rate=25",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-shortest",
                str(path),
            ],
            check=True,
            timeout=300,
        )

    def _plan_probe_spy(self, calls: list):
        """Wraps the real _probe_movie: counts calls, never replaces it.

        The real function is captured BEFORE patching (calling the module
        attribute inside the spy would recurse into the mock itself)."""

        def spy(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("input_file"))
            return real_probe(*args, **kwargs)

        real_probe = engine_pipeline._probe_movie
        return spy

    def test_plan_run_then_encode_skips_probing(self):
        """The full cycle: a recorded plan makes the encode start at the
        planned CRF without running a single probe encode."""

        plan_result = plan_quality(self.clip, db_file=self.db_file)
        self.assertTrue(
            plan_result.success,
            f"plan skipped the movie: {plan_result.skipped_reason!r} "
            "(the smoke source must leave room for an svt_av1 size gain)",
        )
        service.record_plan(self.connection, self.clip, plan_result, self.config)
        plans = self.connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        self.assertEqual(plans, 1, "the plan run must record its decision")

        calls: list = []
        with mock.patch.object(
            engine_pipeline, "_probe_movie", side_effect=self._plan_probe_spy(calls)
        ):
            result = encode_with_quality(
                self.clip,
                self.root / "out1" / "clip.mkv",
                db_file=self.db_file,
            )
        self.assertEqual(calls, [], "a valid plan must replace the probe pipeline")
        self.assertTrue(result.success, f"encode failed: {result.error!r}")
        self.assertEqual(result.crf_source, "plan")
        self.assertEqual(result.chosen_crf, plan_result.chosen_crf)
        # persist like the job worker/CLI do, then check provenance lands
        service.record_quality_results(self.connection, self.clip, result, self.config)
        row = self.connection.execute(
            "SELECT crf_source FROM encodes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["crf_source"], "plan")

    def test_changed_target_reprobes(self):
        """A fingerprint mismatch (different target VMAF) must fall back to
        the real probe pipeline - never a silent stale reuse."""

        plan_result = plan_quality(self.clip, db_file=self.db_file)
        self.assertTrue(plan_result.success, plan_result.skipped_reason)
        service.record_plan(self.connection, self.clip, plan_result, self.config)

        calls: list = []
        with mock.patch.object(
            engine_pipeline, "_probe_movie", side_effect=self._plan_probe_spy(calls)
        ):
            result = encode_with_quality(
                self.clip,
                self.root / "out2" / "clip.mkv",
                target_vmaf=90.0,  # differs from the plan's 96.0 (the default target)
                db_file=self.db_file,
            )
        self.assertEqual(len(calls), 1, "changed settings must trigger a real re-probe")
        self.assertTrue(result.success, f"encode failed: {result.error!r}")
        self.assertEqual(result.crf_source, "probe")
        service.record_quality_results(self.connection, self.clip, result, self.config)
        row = self.connection.execute(
            "SELECT crf_source FROM encodes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["crf_source"], "probe")


@unittest.skipUnless(os.environ.get("SHRINKRAY_SMOKE"), "smoke: opt-in (SHRINKRAY_SMOKE=1)")
@unittest.skipUnless(BINARIES_OK, "real ffmpeg + HandBrakeCLI + ffprobe required on PATH")
class TestSmokeColourRetag(unittest.TestCase):
    """An untagged source's unset colour components come out tagged BT.709 (issue #258).

    Pins the documented re-tagging: what HandBrake writes for tags is a binary
    behaviour, so a stub cannot catch a HandBrake change - this test can."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.clip = self.root / "untagged.mkv"
        self._make_untagged_clip(self.clip)
        self.enc = self.root / "untagged-enc.mkv"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _probe_tags(path: Path) -> dict:
        """Reads the video stream's colour tags with ffprobe."""
        import json as _json

        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=pix_fmt,color_range,color_space,color_transfer,color_primaries",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _json.loads(out.stdout)["streams"][0]

    @staticmethod
    def _make_untagged_clip(path: Path) -> None:
        """A real 10 s 1080p-class clip with NO colour tags.

        lavfi's testsrc encodes without colour tags by default - nothing strips
        tags here, and the setup verifies the premise (fail loudly if ffmpeg
        starts tagging its output - the test's premise would be gone). The
        fixture is 1080p-class because HandBrake's assumed colour profile is
        resolution-dependent (at SD sizes the matrix and primaries come out
        bt601-family while the transfer tag is bt709 at every measured size;
        PR #441's review thread comment `6460170853` posts the tag triple per
        size), and ShrinkRay only encodes 1080p+."""
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=10:size=1920x1080:rate=25",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(path),
            ],
            check=True,
            timeout=120,
        )
        tags = TestSmokeColourRetag._probe_tags(path)
        unset = [
            k
            for k in ("color_space", "color_transfer", "color_primaries")
            if tags.get(k) in (None, "unknown")
        ]
        if len(unset) != 3:
            raise AssertionError(f"premise: the generated clip must be fully untagged, got {tags}")

    def test_unset_components_come_out_bt709(self):
        """The re-tagging (issue #258): unset in -> bt709 out.

        The set-components case is the sibling test below; this fixture is fully
        untagged by construction and cannot observe it."""
        from shrinkray.tools.handbrake import build_encode_command, find_handbrake

        handbrake = find_handbrake()
        self.assertIsNotNone(handbrake, "HandBrakeCLI not found")
        command = build_encode_command(handbrake, self.clip, self.enc, 28, "svt_av1", "10")
        subprocess.run(command, check=True, capture_output=True, timeout=300)

        src = self._probe_tags(self.clip)
        out = self._probe_tags(self.enc)
        self.assertEqual(out["pix_fmt"], "yuv420p")
        for key in ("color_space", "color_transfer", "color_primaries"):
            self.assertEqual(
                out.get(key),
                "bt709",
                f"the unset '{key}' must come out as bt709 (source had {src.get(key)})",
            )

    def test_set_components_pass_through(self):
        """The doc's other claim: already-set components survive the encode.

        All three components are set to values the unset-fill at this
        fixture's 1080p size would NOT write itself (smpte170m space and
        transfer, bt470bg primaries; the 1080p fill is all-bt709, while at
        SD sizes the fill writes smpte170m/bt470bg itself -
        docs/encode-settings.md): survival of THOSE values proves
        pass-through for every component; a forced rewrite would produce
        bt709."""
        from shrinkray.tools.handbrake import build_encode_command, find_handbrake

        # all three components set to non-bt709 values: a stream copy from the
        # untagged fixture with explicit tags writes exactly those
        partial = self.root / "partial.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(self.clip),
                "-c:v",
                "copy",
                "-colorspace",
                "smpte170m",
                "-color_trc",
                "smpte170m",
                "-color_primaries",
                "bt470bg",
                str(partial),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        tags = self._probe_tags(partial)
        self.assertEqual(tags.get("color_space"), "smpte170m", "premise: space tagged smpte170m")
        self.assertEqual(
            tags.get("color_transfer"), "smpte170m", "premise: transfer tagged smpte170m"
        )
        self.assertEqual(
            tags.get("color_primaries"), "bt470bg", "premise: primaries tagged bt470bg"
        )

        handbrake = find_handbrake()
        self.assertIsNotNone(handbrake)
        enc = self.root / "partial-enc.mkv"
        command = build_encode_command(handbrake, partial, enc, 28, "svt_av1", "10")
        subprocess.run(command, check=True, capture_output=True, timeout=300)

        out = self._probe_tags(enc)
        for key, val in (
            ("color_space", "smpte170m"),
            ("color_transfer", "smpte170m"),
            ("color_primaries", "bt470bg"),
        ):
            self.assertEqual(
                out.get(key), val, f"the set '{key}' must SURVIVE (a rewrite would emit bt709)"
            )


if __name__ == "__main__":
    unittest.main()
