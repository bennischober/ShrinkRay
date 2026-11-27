"""Probe-progress streaming: the web and console consumers of step events."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db as shrinkray_db
from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import progress as engine_progress
from shrinkray.engine import samples as engine_samples
from shrinkray.engine import search as engine_search
from shrinkray.tools import ffprobe as tools_ffprobe
from shrinkray.tools import vmaf as tools_vmaf


class TestProbeProgress(unittest.TestCase):
    """Step events flow from the probing pipeline to the progress callback."""

    def test_an_unspawnable_binary_raises_the_step_shape(self):
        """Issue #458: a spawn failure at the sample-extraction seam raises
        the step's documented RuntimeError (ffmpeg named), not a raw
        OSError traceback; the sample path fails loudly and per file."""

        def broken_run(command, cancel=None):
            raise FileNotFoundError(2, "No such file or directory", command[0])

        with (
            patch(
                "shrinkray.engine.samples.subprocess.run",
                return_value=type(
                    "P", (), {"returncode": 0, "stdout": b'{"format": {"duration": 100.0}}'}
                )(),
            ),
            patch("shrinkray.engine.samples._run", side_effect=broken_run),
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(RuntimeError) as ctx,
        ):
            engine_samples.extract_samples(
                Path("movie.mkv"),
                Path(tmp),
                1,
                10.0,
                cancel=None,
                progress=lambda event: None,
            )
        # the FULL message (the guard's own interpolation, not the chained
        # cause's): the pin catches a guard dropping the binary name
        self.assertIn("sample extraction cannot run 'ffmpeg'", str(ctx.exception))

    def test_the_ffprobe_duration_read_is_pinned_too(self):
        """Issue #495: the ffprobe duration read that opens extract_samples is
        a bare subprocess.run whose handler catches only SubprocessError (#62's
        timeout class). A missing or unspawnable ffprobe is an OSError, not a
        SubprocessError, so it escaped as a raw traceback; it must raise the
        step's documented RuntimeError (ffprobe named), like the function's
        two _run-site guards (#458) and the crop step (#401)."""

        def broken_run(command, **kwargs):
            raise OSError(24, "Too many open files")

        with (
            patch(
                "shrinkray.engine.samples.subprocess.run",
                side_effect=broken_run,
            ),
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(RuntimeError) as ctx,
        ):
            engine_samples.extract_samples(
                Path("movie.mkv"),
                Path(tmp),
                1,
                10.0,
                cancel=None,
                progress=lambda event: None,
            )
        # the FULL message (the guard's own interpolation, not the chained
        # cause's): the pin catches a guard dropping the binary name
        self.assertIn("cannot run 'ffprobe'", str(ctx.exception))
        self.assertIn("Too many open files", str(ctx.exception))

    def test_the_reextract_guard_is_pinned_too(self):
        """Issue #458's per-site criterion: the LOSSLESS RE-EXTRACT's guard
        (the second _run inside the stream-copy fallback) also converts a
        spawn failure to the step shape - the first _run fails the
        stream-copy LEGITIMATELY (exit 28, a codec the container cannot
        copy), forcing the re-extract, whose spawn then fails."""

        calls: list = []

        def first_fails_second_unspawnable(command, cancel=None):
            calls.append(command)
            if "-c:v" in command and "copy" in command:
                return 28, "codec error"  # a legitimate stream-copy failure
            raise FileNotFoundError(2, "No such file or directory", command[0])

        with (
            patch(
                "shrinkray.engine.samples.subprocess.run",
                return_value=type(
                    "P", (), {"returncode": 0, "stdout": b'{"format": {"duration": 100.0}}'}
                )(),
            ),
            patch("shrinkray.engine.samples._run", side_effect=first_fails_second_unspawnable),
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(RuntimeError) as ctx,
        ):
            engine_samples.extract_samples(
                Path("movie.mkv"),
                Path(tmp),
                1,
                10.0,
                cancel=None,
                progress=lambda event: None,
            )
        self.assertIn("sample re-extract cannot run 'ffmpeg'", str(ctx.exception))
        self.assertEqual(len(calls), 2, "the fallback path ran")

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
        self.assertEqual(
            [e["sample"] for e in events],
            [
                {"index": 1, "count": 3},
                {"index": 2, "count": 3},
                {"index": 3, "count": 3},
            ],
        )
        # samples never ride done/total: that counter is the folder job's
        self.assertNotIn("done", events[0])
        self.assertNotIn("total", events[0])
        self.assertEqual(events[0]["file"]["stage"], "samples")

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
        # the mocked probe returns exactly the target on every CRF: the early
        # stop never fires (review round 2's gate - no failing bound ever
        # exists on a constant curve), so the search walks to the range's
        # top and answers there
        self.assertEqual(chosen, 31)
        details = [e["detail"] for e in events]
        self.assertTrue(any("probe encode" in d for d in details))
        self.assertTrue(all("VMAF" in d for d in details[1::2]))  # result events
        # every completed probe carries its numeric crf/vmaf, its position and
        # the search's known step count (issue #631)
        self.assertEqual(events[-1]["probe"]["index"], len(probes))
        self.assertEqual(events[-1]["probe"]["vmaf"], probes[-1].vmaf)
        self.assertEqual(events[-1]["probe"]["crf"], probes[-1].crf)
        self.assertIsInstance(events[-1]["probe"]["passed"], bool)
        self.assertEqual(events[-1]["file"]["stage"], "crf-search")
        self.assertEqual(events[-1]["file"]["step"], 4)
        self.assertEqual(events[-1]["probe"]["count"], len(probes))

    def test_probe_count_is_the_searchs_actual_step_count(self):
        """Issue #631: the count is the search's actual step sequence, not the
        remaining CRF bracket. The search knows the total only when it finishes;
        the live events carry null, and the final event carries the real count."""
        events: list[dict] = []
        with (
            patch.object(engine_search, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(
                engine_search,
                "probe_crf",
                side_effect=lambda samples, crf, *a, **k: 96.6 if crf in (23, 26, 27) else 95.9,
            ),
        ):
            chosen, achieved, probes = engine_search.find_optimal_crf(
                Path("movie.mkv"),
                Path("work"),
                "hb",
                target_vmaf=96.0,
                crf_range=(14, 32),
                sample_count=1,
                sample_seconds=10.0,
                progress=events.append,
            )
        self.assertEqual([p.crf for p in probes], [23, 28, 25, 24])
        self.assertEqual(chosen, 23)
        self.assertEqual(len(probes), 4)
        self.assertEqual(events[-1]["probe"]["count"], len(probes))
        self.assertEqual(events[-1]["probe"]["index"], len(probes))
        # the final probe row is the REAL last probe (24), never the chosen
        # decision (23): the merge replaces probes by index, so reusing the
        # chosen row here would delete the last measured probe (issue #631)
        self.assertEqual(events[-1]["probe"]["crf"], probes[-1].crf)
        self.assertEqual(events[-1]["probe"]["vmaf"], probes[-1].vmaf)
        self.assertEqual(events[-1]["probe"]["crf"], 24)
        self.assertNotEqual(events[-1]["probe"]["crf"], chosen)
        # the decision rides ONLY in the chosen field
        self.assertEqual(events[-1]["chosen"]["crf"], chosen)
        self.assertEqual(events[-1]["chosen"]["vmaf"], achieved)
        live = [e for e in events if e["detail"].startswith("probe encode")]
        self.assertTrue(live)
        self.assertTrue(all(e["probe"]["count"] is None for e in live))

    def test_the_merged_probe_trail_ends_with_the_real_last_probe(self):
        """Issue #631: merging the real-bracket event sequence must produce the
        trail [(1,23),(2,28),(3,25),(4,24)] and chosen 23, not duplicate 23 at
        the last row (the old final-event bug)."""
        events: list[dict] = []
        with (
            patch.object(engine_search, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(
                engine_search,
                "probe_crf",
                side_effect=lambda samples, crf, *a, **k: 96.6 if crf in (23, 26, 27) else 95.9,
            ),
        ):
            _chosen, _achieved, _probes = engine_search.find_optimal_crf(
                Path("movie.mkv"),
                Path("work"),
                "hb",
                target_vmaf=96.0,
                crf_range=(14, 32),
                sample_count=1,
                sample_seconds=10.0,
                progress=events.append,
            )
        merged = None
        for event in events:
            merged = engine_progress.merge(merged, event)
        self.assertIsNotNone(merged)
        self.assertEqual(
            [(p["index"], p["crf"], p["vmaf"]) for p in merged["probes"]],
            [(1, 23, 96.6), (2, 28, 95.9), (3, 25, 95.9), (4, 24, 95.9)],
        )
        self.assertEqual(merged["probe"]["crf"], 24)
        self.assertEqual(merged["probe"]["vmaf"], 95.9)
        self.assertEqual(merged["chosen"]["crf"], 23)
        self.assertEqual(merged["chosen"]["vmaf"], 96.6)

    def test_a_real_probe_cache_hit_reaches_cache_status(self):
        """A seeded probe_cache row is read through the real _cached_or_none
        path: no fabricated collector, no probe encode (review M4, #631)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / "movie.mkv"
            movie.write_bytes(b"movie")
            sample = root / "sample.mkv"
            sample.write_bytes(b"sample")
            db_file = root / "shrinkray.db"
            connection = shrinkray_db.connect(db_file)
            try:
                recipe = tools_vmaf.vmaf_recipe(crop=None)
                key = engine_search.probe_cache_key(
                    movie,
                    0.0,
                    10.0,
                    24,
                    "svt_av1",
                    "5",
                    None,
                    None,
                    tools_vmaf._tool_versions("HandBrakeCLI"),
                    recipe.identifier(),
                )
                shrinkray_db.save_probe_score(connection, key, 95.5, 123)
                measured_at = shrinkray_db.get_probe_measured_at(connection, key)
            finally:
                connection.close()
            self.assertIsNotNone(measured_at)
            statuses: list[tuple[bool, str | None]] = []
            events: list[dict] = []
            score = engine_search.probe_crf(
                [(sample, 0.0)],
                24,
                root,
                "HandBrakeCLI",
                movie,
                10.0,
                "svt_av1",
                "5",
                recipe=recipe,
                db_file=db_file,
                cache_status=statuses,
                progress=events.append,
            )
            self.assertEqual(score, 95.5)
            self.assertEqual(statuses, [(True, measured_at)])
            self.assertTrue(events)

    def test_cached_replay_reports_cache_provenance(self):
        """A cached probe replay is source=cache with the stored measured_at, and
        the final probe row is the cached row (maintainer 2026-11-14, #631)."""
        events: list[dict] = []
        measured_at = "2026-11-14 08:12:00"

        def cached_probe(samples, crf, *args, **kwargs):
            kwargs["cache_status"].append((True, measured_at))
            return 96.2

        with (
            patch.object(engine_search, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(engine_search, "probe_crf", side_effect=cached_probe),
        ):
            chosen, achieved, probes = engine_search.find_optimal_crf(
                Path("movie.mkv"),
                Path("work"),
                "hb",
                target_vmaf=96.0,
                crf_range=(14, 32),
                sample_count=1,
                sample_seconds=10.0,
                progress=events.append,
            )
        self.assertEqual(chosen, probes[-1].crf)
        self.assertEqual(achieved, probes[-1].vmaf)
        self.assertTrue(all(p.cached for p in probes))
        self.assertTrue(all(p.measured_at == measured_at for p in probes))
        self.assertEqual(events[-1]["chosen"]["source"], "cache")
        self.assertEqual(events[-1]["source"], "cache")
        self.assertEqual(events[-1]["measured_at"], measured_at)
        self.assertTrue(events[-1]["probe"]["cached"])
        self.assertEqual(events[-1]["probe"]["measured_at"], measured_at)
        self.assertTrue(
            all(
                e["probe"].get("cached")
                for e in events
                if e.get("probe") and str(e.get("detail", "")).startswith("CRF ")
            )
        )

    def test_live_probe_reports_measured_provenance(self):
        """A live probe is source=measured with no measured_at, never cache."""
        events: list[dict] = []

        def live_probe(samples, crf, *args, **kwargs):
            kwargs["cache_status"].append((False, None))
            return 95.4

        with (
            patch.object(engine_search, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(engine_search, "probe_crf", side_effect=live_probe),
        ):
            _chosen, _achieved, probes = engine_search.find_optimal_crf(
                Path("movie.mkv"),
                Path("work"),
                "hb",
                target_vmaf=96.0,
                crf_range=(14, 32),
                sample_count=1,
                sample_seconds=10.0,
                progress=events.append,
            )
        self.assertTrue(probes)
        self.assertTrue(all(not p.cached for p in probes))
        self.assertTrue(all(p.measured_at is None for p in probes))
        self.assertEqual(events[-1]["chosen"]["source"], "measured")
        self.assertEqual(events[-1]["source"], "measured")
        self.assertIsNone(events[-1]["measured_at"])
        self.assertFalse(events[-1]["probe"]["cached"])
        self.assertIsNone(events[-1]["probe"]["measured_at"])

    def test_probe_crf_reports_each_sample(self):
        """Issue #690: probe_crf emits one event per sample, naming the
        within-probe sample in `detail`. This direct call passes no probe_index,
        so `probe.index` is None; find_optimal_crf supplies the search
        position (see test_find_optimal_crf_reports_probe_steps)."""
        events: list[dict] = []

        def fake_run(command, cancel=None):
            Path(command[-1]).write_bytes(b"x" * 8)
            return 0, ""

        with (
            patch.object(engine_search, "probe_cache_key", return_value="k"),
            patch.object(engine_search, "_cached_or_none", return_value=(None, None, None, None)),
            patch.object(
                engine_search,
                "build_encode_command",
                side_effect=lambda hb, sample, out, *a, **k: ["hb", str(out)],
            ),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", return_value=95.0),
            patch.object(engine_search, "_store_probe"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            engine_search.probe_crf(
                [(Path("s0.mkv"), 0.0), (Path("s1.mkv"), 10.0)],
                30,
                Path(tmp),
                "hb",
                Path("movie.mkv"),
                10.0,
                "svt_av1",
                "5",
                progress=events.append,
            )
        self.assertEqual(
            [e["detail"] for e in events],
            ["sample 1/2 (CRF 30)", "sample 2/2 (CRF 30)"],
        )
        self.assertEqual([e["probe"]["index"] for e in events], [None, None])
        self.assertEqual([e["probe"]["crf"] for e in events], [30, 30])
        # a probe's per-sample tick has no measured VMAF yet; the completed
        # probe event (find_optimal_crf's) carries it
        self.assertIsNone(events[0]["probe"]["vmaf"])
        self.assertEqual(events[0]["file"]["stage"], "crf-search")

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
                    engine_plan,
                    "find_optimal_crf_and_skip",
                    return_value=(20, 95.0, [], False, 0.5),
                ) as fake_search,
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
            ):
                result = engine_plan.plan_quality(src, progress=events.append)
        self.assertTrue(result.success)
        fake_search.assert_called_once()
        self.assertIsNotNone(fake_search.call_args.kwargs.get("progress"))

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
        """The thread cap reaches BOTH size probes through the search (issue
        #388: the plan reports the decision's factor, so the decision's own
        probe calls are the only size encodes left): the search's bound probe
        AND its chosen-CRF check each receive the plan's cap."""
        with (
            patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
            patch.object(engine_plan, "find_handbrake", return_value="hb"),
            patch.object(engine_plan, "source_codec", return_value="h264"),
            patch.object(engine_plan, "source_bit_depth", return_value=None),
            patch.object(
                engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
            ),
            patch.object(engine_plan, "detect_crop_summary_cached", return_value=None),
            patch.object(engine_plan, "detect_crop_rect", return_value=None),
            patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
            patch.object(engine_search, "estimate_size_factor", return_value=0.5) as fake_estimate,
            patch.object(engine_search, "find_optimal_crf", return_value=(22, 95.5, [])),
            patch.object(engine_search, "measure_vmaf", return_value=95.5),
            tempfile.TemporaryDirectory() as tmp,
        ):
            src = Path(tmp) / "in.mkv"
            src.write_bytes(b"x" * 8)
            engine_plan.plan_quality(src, encoder_threads=4)
        self.assertEqual(fake_estimate.call_count, 2, "the bound + the chosen-CRF check")
        for call in fake_estimate.call_args_list:
            self.assertEqual(call.kwargs.get("encoder_threads"), 4)

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
