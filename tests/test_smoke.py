"""Opt-in smoke tests against the REAL toolchain (ffmpeg + HandBrakeCLI).

Skipped by default everywhere (CI, local gate): the suite's contract is
no binaries required. Run explicitly with real binaries on PATH:

    SHRINKRAY_SMOKE=1 uv run pytest tests/test_smoke.py -v

Why this exists: the stall-watchdog false positive (8e86d82) passed
every stub-based test while killing real encodes - stub behavior is not
binary behavior. These tests run the plan -> encode -> reuse cycle with
the real HandBrakeCLI/ffmpeg so a change that breaks real-binary
behavior surfaces here, not in the user's library.

Cost: a synthetic 100s clip at 320x240 keeps a full cycle in the
minutes range (plan ~1 min, reused encode well under that).
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

BINARIES_OK = shutil.which("HandBrakeCLI") is not None and shutil.which("ffmpeg") is not None


@unittest.skipUnless(os.environ.get("SHRINKRAY_SMOKE"), "smoke: opt-in (SHRINKRAY_SMOKE=1)")
@unittest.skipUnless(BINARIES_OK, "real ffmpeg + HandBrakeCLI required on PATH")
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
        self.config = SimpleNamespace(handbrake_encoder="x265")

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    @staticmethod
    def _make_clip(path: Path, seconds: int) -> None:
        """Generates a REAL media file: synthetic video + tone audio.

        h264 crf 18 keeps the source compressible-but-not-degenerate so
        the x265 re-encode actually finds a size gain (a 'no size gain'
        plan would skip the reuse path under test).
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
                f"testsrc=duration={seconds}:size=320x240:rate=25",
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
            "(the smoke source must leave room for an x265 size gain)",
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
                target_vmaf=90.0,  # differs from the plan's 95.0
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


if __name__ == "__main__":
    unittest.main()
