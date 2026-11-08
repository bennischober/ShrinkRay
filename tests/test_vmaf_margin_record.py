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

    def _record(self, result: engine_plan.QualityResult) -> None:
        from shrinkray.service import record_plan

        connection = db.connect(self.db_file)
        try:
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
        finally:
            connection.close()

    def _lookup(self, result: engine_plan.QualityResult, target: float, margin: float):
        """The encode path's lookup, with the fingerprint built the way it builds it."""
        from shrinkray.engine.pipeline import _lookup_plan, _plan_fingerprint
        from shrinkray.tools.vmaf import vmaf_recipe

        return _lookup_plan(
            self.db_file,
            self.movie,
            encoder=result.encoder,
            encoder_preset=result.probe_encoder_preset,
            target_vmaf=target,
            crf_range=result.crf_range,
            sample_count=result.sample_count,
            sample_seconds=result.sample_seconds,
            crop_sample_count=result.crop_sample_count,
            vmaf_recipe=_plan_fingerprint(
                vmaf_recipe(width=1920, height=1080, bit_depth=None), margin, target
            ),
            min_saving_percent=result.min_saving_percent,
        )

    def test_the_recorded_row_carries_the_effective_floor(self) -> None:
        """The row stores 95.46, not 96: the floor the search aimed at."""
        self._record(self._plan(margin=3.0))
        connection = db.connect(self.db_file)
        try:
            row = connection.execute(
                "SELECT target_vmaf FROM plans ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row, "the plan row must exist")
        self.assertAlmostEqual(float(row[0]), 95.46)

    def test_the_same_request_reuses_a_clamped_plan(self) -> None:
        """plan stores, encode looks up: the request that clamped it must hit.

        Before the requested target entered the fingerprint, the lookup asked for
        96 against the stored 95.46 and missed, so every file the margin rescued
        re-ran its whole search on encode.
        """
        result = self._plan(margin=3.0)
        self._record(result)
        row = self._lookup(result, target=96.0, margin=3.0)
        self.assertIsNotNone(row, "the request that produced the clamped plan must reuse it")
        self.assertAlmostEqual(float(row["target_vmaf"]), 95.46)

    def test_another_request_never_reuses_a_clamped_plan(self) -> None:
        result = self._plan(margin=3.0)
        self._record(result)
        self.assertIsNone(
            self._lookup(result, target=96.0, margin=0.0),
            "an absolute-target request must not reuse a plan aimed below it",
        )
        self.assertIsNone(
            self._lookup(result, target=95.46, margin=3.0),
            "a request for 95.46 never searched 96 first: a different decision",
        )
        self.assertIsNone(
            self._lookup(result, target=97.0, margin=3.0),
            "a higher request must re-probe",
        )

    def test_a_reachable_plan_is_reused_at_its_own_target(self) -> None:
        result = self._plan(margin=0.0)
        self._record(result)
        self.assertIsNotNone(self._lookup(result, target=96.0, margin=0.0))


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
            _plan_fingerprint(recipe, 0.0, 96.0),
            _plan_fingerprint(recipe, 3.0, 96.0),
            "a margin change must invalidate the stored plan",
        )
        self.assertIn("margin=3", _plan_fingerprint(recipe, 3.0, 96.0))

    def test_the_fingerprint_carries_the_requested_target(self) -> None:
        """A clamped row stores the floor, so only the key still names the request."""
        from shrinkray.engine.plan import _plan_fingerprint
        from shrinkray.tools.vmaf import vmaf_recipe

        recipe = vmaf_recipe()
        self.assertNotEqual(
            _plan_fingerprint(recipe, 3.0, 96.0), _plan_fingerprint(recipe, 3.0, 95.46)
        )
        self.assertIn("target=96", _plan_fingerprint(recipe, 3.0, 96.0))


def _config():
    from shrinkray.config import Conf

    return Conf()


if __name__ == "__main__":
    unittest.main()
