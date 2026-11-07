"""The margin must reach the engine from every entry point (issue #584).

Resolving `quality.vmaf_margin` in the command is not enough: the plan and the
encode path each call `find_optimal_crf_and_skip`, and a dropped keyword makes
the whole setting inert. The review of PR #588 found exactly that.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine import plan as engine_plan


class TestTheMarginReachesTheEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x" * 100)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_plan_path_forwards_the_margin(self) -> None:
        with (
            patch.object(
                engine_plan,
                "find_optimal_crf_and_skip",
                return_value=(20, 96.0, [], False, 0.5, 96.0),
            ) as fake_search,
            patch("shrinkray.engine.plan.extract_samples", return_value=[]),
            patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.plan.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
        ):
            engine_plan.plan_quality(
                self.movie,
                vmaf_margin=7.0,
                sample_count=1,
                db_file=None,
            )
        self.assertEqual(fake_search.call_args.kwargs["vmaf_margin"], 7.0)

    def test_the_encode_path_forwards_the_margin(self) -> None:
        with (
            patch.object(
                engine_pipeline,
                "_probe_movie",
                side_effect=RuntimeError("stop after the probe"),
            ) as fake_probe,
            patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
            self.assertRaises(RuntimeError),
        ):
            engine_pipeline.encode_with_quality(
                self.movie,
                Path(self.tmp.name) / "out.mkv",
                vmaf_margin=7.0,
                sample_count=1,
                db_file=None,
            )
        self.assertEqual(fake_probe.call_args.kwargs.get("vmaf_margin"), 7.0)


if __name__ == "__main__":
    unittest.main()
