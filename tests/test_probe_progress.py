"""Probe-progress streaming: the web and console consumers of step events."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import samples as engine_samples
from shrinkray.engine import search as engine_search
from shrinkray.tools import ffprobe as tools_ffprobe
from shrinkray.tools import vmaf as tools_vmaf


class TestProbeProgress(unittest.TestCase):
    """Step events flow from the probing pipeline to the progress callback."""

    def test_extract_samples_reports_per_sample(self):
        """Each extracted sample emits one step event (n/count)."""
        events: list[dict] = []

        def fake_run(command, cancel=None):
            Path(command[-1]).write_bytes(b"x" * 8)  # the sample file
            return 0, ""

        with (
            patch(
                "shrinkray.engine.samples.subprocess.run",
                return_value=type(
                    "P", (), {"returncode": 0, "stdout": b'{"format": {"duration": 100.0}}'}
                )(),
            ),
            patch("shrinkray.engine.samples._run", side_effect=fake_run),
            tempfile.TemporaryDirectory() as tmp,
        ):
            engine_samples.extract_samples(
                Path("movie.mkv"),
                Path(tmp),
                3,
                10.0,
                cancel=None,
                progress=events.append,
            )
        self.assertEqual(
            [e["detail"] for e in events],
            ["extracting sample 1/3", "extracting sample 2/3", "extracting sample 3/3"],
        )
        self.assertEqual([e["done"] for e in events], [0, 1, 2])
        self.assertEqual(events[0]["total"], 3)

    def test_find_optimal_crf_reports_probe_steps(self):
        """The CRF bisect emits a start event per probe encode and a result
        event with the measured VMAF."""
        events: list[dict] = []
        with (
            patch.object(engine_search, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(engine_search, "probe_crf", side_effect=lambda *a, **k: 95.0),
            patch.object(engine_search, "probe_crf", side_effect=lambda *a, **k: 95.0),
        ):
            chosen, _achieved, probes = engine_search.find_optimal_crf(
                Path("movie.mkv"),
                Path("work"),
                "hb",
                target_vmaf=95.0,
                crf_range=(14, 32),
                sample_count=1,
                sample_seconds=10.0,
                progress=events.append,
            )
        # the mocked probe returns exactly the target, so the FIRST passing
        # round already sits within the early-stop tolerance (issue #313):
        # the search ends there instead of walking to the range's edge
        self.assertEqual(chosen, 23)
        details = [e["detail"] for e in events]
        self.assertTrue(any("probe encode" in d for d in details))
        self.assertTrue(all("VMAF" in d for d in details[1::2]))  # result events
        self.assertEqual(events[-1]["probes_done"], len(probes))

    def test_estimate_size_factor_reports_size_probe(self):
        """The skip-check size probe emits its step event."""
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "s0.mkv"
            sample.write_bytes(b"x" * 8)
            encoded = Path(tmp) / "size_probe_crf32.mkv"

            def fake_run(command, cancel=None):
                Path(command[-1]).write_bytes(b"y" * 4)  # the probe encode
                return 0, ""

            with (
                patch.object(
                    engine_search, "build_encode_command", return_value=["hb", "-o", str(encoded)]
                ),
                patch.object(engine_search, "_run", side_effect=fake_run),
                patch.object(engine_search, "measure_vmaf", return_value=None),
            ):
                factor = engine_search.estimate_size_factor(
                    [(sample, 10.0)],
                    Path(tmp),
                    "hb",
                    32,
                    sample,
                    10.0,
                    "x265",
                    "medium",
                    progress=events.append,
                )
        self.assertEqual(factor, 0.5)  # 4 bytes / 8 bytes
        self.assertEqual([e["detail"] for e in events], ["size probe (CRF 32)"])

    def test_plan_extracts_samples_exactly_once(self):
        """plan extracts the probe windows once, not once per phase.

        Regression: find_optimal_crf_and_skip extracted its own samples for
        the search and plan_quality extracted them again for the reported size
        factor, so every window was decoded and written twice per plan (three
        times before the size probe shared the search's list). The extraction is
        a decode plus a write per window, so the multiplier is the whole cost.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(
                    engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]
                ) as fake,
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "probe_crf", return_value=95.0),
                patch.object(engine_search, "probe_crf", return_value=95.0),
            ):
                engine_plan.plan_quality(src)
        self.assertEqual(fake.call_count, 1)

    def test_plan_quality_threads_progress_to_pipeline(self):
        """plan_quality passes its progress callback into the probe pipeline
        (regression: plan jobs emitted one event per movie and then went
        silent for the whole probe run)."""
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(
                    engine_plan, "find_optimal_crf_and_skip", return_value=(20, 95.0, [], False)
                ) as fake_search,
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(
                    engine_plan, "estimate_size_factor", return_value=0.5
                ) as fake_estimate,
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
            ):
                result = engine_plan.plan_quality(src, progress=events.append)
        self.assertTrue(result.success)
        fake_search.assert_called_once()
        self.assertIsNotNone(fake_search.call_args.kwargs.get("progress"))
        fake_estimate.assert_called_once()
        self.assertIsNotNone(fake_estimate.call_args.kwargs.get("progress"))

    def test_encode_with_quality_threads_progress_to_probes(self):
        """The encode's PROBE phase streams steps too (before HandBrake's
        own percent lines take over) — same shared pipeline as plan."""
        with (
            patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
            patch("shrinkray.engine.pipeline.check_disk_space", return_value=(True, "")),
            patch.object(engine_pipeline, "_source_snapshot", return_value=None),
            patch.object(
                engine_pipeline, "_probe_movie", return_value=(20, 95.0, [], None)
            ) as fake_probe,
            patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            src = Path(tmp) / "in.mkv"
            src.write_bytes(b"x" * 8)
            engine_pipeline.encode_with_quality(
                src,
                Path(tmp) / "out.mkv",
                progress=lambda data: None,
            )
        self.assertIsNotNone(fake_probe.call_args.kwargs.get("progress"))

    def test_encode_with_quality_forwards_thread_cap_to_probes(self):
        """A configured thread cap must reach the probe encodes, not just the
        final encode: the cap exists to keep cores free on a shared machine,
        and probing runs for minutes before the final encode starts."""
        with (
            patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
            patch("shrinkray.engine.pipeline.check_disk_space", return_value=(True, "")),
            patch.object(engine_pipeline, "_source_snapshot", return_value=None),
            patch.object(
                engine_pipeline, "_probe_movie", return_value=(20, 95.0, [], None)
            ) as fake_probe,
            patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            src = Path(tmp) / "in.mkv"
            src.write_bytes(b"x" * 8)
            engine_pipeline.encode_with_quality(
                src,
                Path(tmp) / "out.mkv",
                encoder_threads=4,
            )
        self.assertEqual(fake_probe.call_args.kwargs.get("encoder_threads"), 4)

    def test_plan_forwards_thread_cap_to_both_size_probes(self):
        """plan's report size probe must use the same cap as the CRF search
        and the encode that follows, or the estimate comes from a different
        thread configuration than the real encode."""
        with (
            patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
            patch.object(engine_plan, "find_handbrake", return_value="hb"),
            patch.object(
                engine_plan, "find_optimal_crf_and_skip", return_value=(20, 95.0, [], False)
            ) as fake_search,
            patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(engine_plan, "estimate_size_factor", return_value=0.5) as fake_estimate,
            patch.object(engine_search, "estimate_size_factor", return_value=0.5),
            tempfile.TemporaryDirectory() as tmp,
        ):
            src = Path(tmp) / "in.mkv"
            src.write_bytes(b"x" * 8)
            engine_plan.plan_quality(src, encoder_threads=4)
        self.assertEqual(fake_search.call_args.kwargs.get("encoder_threads"), 4)
        self.assertEqual(fake_estimate.call_args.kwargs.get("encoder_threads"), 4)

    def test_size_probe_does_not_measure_vmaf(self):
        """The size probe only needs a byte ratio; VMAF there is a full
        decode of both sides and the value is discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "s0.mkv"
            sample.write_bytes(b"x" * 8)
            encoded = Path(tmp) / "size_probe_crf32.mkv"

            def fake_run(command, cancel=None):
                Path(command[-1]).write_bytes(b"y" * 4)
                return 0, ""

            with (
                patch.object(
                    engine_search, "build_encode_command", return_value=["hb", "-o", str(encoded)]
                ),
                patch.object(engine_search, "_run", side_effect=fake_run),
                patch.object(engine_search, "measure_vmaf") as fake_measure,
            ):
                factor = engine_search.estimate_size_factor(
                    [(sample, 10.0)], Path(tmp), "hb", 32, sample, 10.0, "x265", "medium"
                )
        self.assertEqual(factor, 0.5)
        fake_measure.assert_not_called()

    def test_size_probe_works_without_libvmaf(self):
        """A size-probe skip decision must not require a working libvmaf:
        the ratio is a byte count, and a missing VMAF build used to make the
        whole probe fail."""
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "s0.mkv"
            sample.write_bytes(b"x" * 8)
            encoded = Path(tmp) / "size_probe_crf32.mkv"

            def fake_run(command, cancel=None):
                Path(command[-1]).write_bytes(b"y" * 4)
                return 0, ""

            with (
                patch.object(
                    engine_search, "build_encode_command", return_value=["hb", "-o", str(encoded)]
                ),
                patch.object(engine_search, "_run", side_effect=fake_run),
                patch.object(
                    tools_vmaf, "detect_vmaf_style", side_effect=RuntimeError("no libvmaf")
                ),
            ):
                factor = engine_search.estimate_size_factor(
                    [(sample, 10.0)], Path(tmp), "hb", 32, sample, 10.0, "x265", "medium"
                )
        self.assertEqual(factor, 0.5)
