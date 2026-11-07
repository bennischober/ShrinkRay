"""The clamped target is the recorded floor, and the cache keys on it (issue #584).

Round 2 of the review measured the opposite: a search aiming at 95.46 recorded
`target_vmaf == 96.0`, so an absolute-target request could reuse a plan that was
only measured against the clamped floor - the unsafe direction. These pins are that
measurement turned into tests.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import search as engine_search


class TestTheClampedFloorIsRecorded(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x" * 200)
        self.db_file = Path(self.tmp.name) / "plans.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _plan(self, margin: float):
        with (
            patch.object(engine_search, "source_vmaf_ceiling", return_value=98.46),
            patch.object(engine_search, "probe_crf", return_value=95.6),
            patch.object(engine_search, "estimate_size_factor", return_value=0.5),
            patch.object(engine_plan, "find_handbrake", return_value="hb"),
            patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(engine_plan, "detect_crop_summary_cached", return_value=None),
            patch.object(engine_plan, "detect_crop_rect", return_value=None),
            patch.object(engine_plan, "source_codec", return_value="h264"),
            patch.object(engine_plan, "source_bit_depth", return_value=None),
            patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
            patch.object(
                engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
            ),
        ):
            return engine_plan.plan_quality(self.movie, vmaf_margin=margin, db_file=self.db_file)

    def test_a_clamped_plan_records_the_effective_floor(self) -> None:
        result = self._plan(margin=3.0)
        self.assertAlmostEqual(result.target_vmaf, 95.46)

    def test_a_zero_margin_records_the_absolute_floor(self) -> None:
        result = self._plan(margin=0.0)
        self.assertAlmostEqual(result.target_vmaf, 96.0)

    def test_the_recorded_row_carries_the_effective_floor(self) -> None:
        """The row stores 95.46, not 96: an absolute-target lookup asks for 96
        and so cannot match it - the plan re-probes instead of being reused."""
        result = self._plan(margin=3.0)
        connection = db.connect(self.db_file)
        try:
            from shrinkray.service import record_plan

            db.upsert_file(
                connection,
                db.MovieRecord(
                    path=self.movie,
                    mtime=self.movie.stat().st_mtime,
                    size=self.movie.stat().st_size,
                    name=self.movie.name,
                    category="Cat",
                ),
            )
            connection.commit()
            record_plan(connection, self.movie, result, _config())
            row = connection.execute(
                "SELECT target_vmaf FROM plans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row, "the plan row must exist")
        self.assertAlmostEqual(float(row[0]), 95.46)


class TestTheSkipPathRecordsTheFloor(unittest.TestCase):
    """An encode skip must record the floor the search used (issue #584).

    Round 4 measured the opposite: all three SkipEncode verdicts recorded 96.0
    while the search had aimed at 95.46 - the exception bypassed the clamp, so
    the handler read the un-clamped local.
    """

    def test_a_skip_records_the_floors_the_search_used(self) -> None:
        from shrinkray.engine.pipeline import encode_with_quality
        from shrinkray.engine.search import SkipEncode

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        movie = Path(tmp.name) / "movie.mkv"
        movie.write_bytes(b"x" * 200)
        for verdict in (
            "re-encode would not shrink the file",
            "saving below minimum",
            "target VMAF 95.46 unreachable (best 95.2 at CRF 14)",
        ):
            with (
                patch(
                    "shrinkray.engine.pipeline._probe_movie",
                    side_effect=SkipEncode(verdict, target_vmaf=95.46),
                ),
                patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
            ):
                result = encode_with_quality(
                    movie,
                    Path(tmp.name) / "out.mkv",
                    vmaf_margin=3.0,
                    sample_count=1,
                    db_file=None,
                )
            self.assertAlmostEqual(
                result.target_vmaf, 95.46, msg=f"{verdict} recorded the configured floor"
            )


class TestThePlanCacheKeysOnTheMargin(unittest.TestCase):
    """A margin-0 plan must not be reused by a margin-3 run (issue #584).

    Round 5 measured the opposite: the margin never reached the plan lookup, so a
    pre-upgrade "target unreachable" row at 96 was reused and the animation the
    change exists for stayed skipped.
    """

    def test_the_fingerprint_carries_the_margin(self) -> None:
        from shrinkray.engine.plan import _plan_fingerprint
        from shrinkray.tools.vmaf import vmaf_recipe

        recipe = vmaf_recipe()
        self.assertNotEqual(
            _plan_fingerprint(recipe, 0.0),
            _plan_fingerprint(recipe, 3.0),
            "a margin change must invalidate the stored plan",
        )
        self.assertIn("margin=3", _plan_fingerprint(recipe, 3.0))


def _config():
    from shrinkray.config import Conf

    return Conf()


if __name__ == "__main__":
    unittest.main()
