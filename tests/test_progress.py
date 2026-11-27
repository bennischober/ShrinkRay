"""The structured progress event (issue #631): schema, merge and emission."""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import progress as engine_progress
from shrinkray.engine import samples as engine_samples
from shrinkray.engine import search as engine_search
from shrinkray.engine.progress import provenance as progress_provenance
from shrinkray.engine.search import SampleProbe
from shrinkray.server import jobs
from shrinkray.tools import handbrake as tools_handbrake


class TestHandBrakeProgressLine(unittest.TestCase):
    """One parser for HandBrake's own progress line (issue #631)."""

    def test_a_real_line_carries_percent_fps_and_eta(self) -> None:
        parsed = tools_handbrake.parse_progress_line(
            "Encoding: task 1 of 1, 42.31 % (123.45 fps, avg 120.00 fps, ETA 00h12m34s)"
        )
        self.assertEqual(
            parsed,
            {
                "percent": 42.31,
                "fps": 123.45,
                "avg_fps": 120.0,
                "eta_seconds": 12 * 60 + 34,
            },
        )

    def test_a_line_without_eta_has_null_fields_not_a_crash(self) -> None:
        parsed = tools_handbrake.parse_progress_line(
            "Encoding: task 1 of 1, 42.31 % (123.45 fps, avg 120.00 fps)"
        )
        self.assertEqual(parsed["percent"], 42.31)
        self.assertEqual(parsed["fps"], 123.45)
        self.assertEqual(parsed["avg_fps"], 120.0)
        self.assertIsNone(parsed["eta_seconds"])

    def test_a_percent_only_line_is_still_a_progress_line(self) -> None:
        parsed = tools_handbrake.parse_progress_line("Encoding: task 1 of 1, 42.31 %")
        self.assertEqual(parsed["percent"], 42.31)
        self.assertIsNone(parsed["fps"])
        self.assertIsNone(parsed["avg_fps"])
        self.assertIsNone(parsed["eta_seconds"])

    def test_a_malformed_line_is_not_a_progress_line(self) -> None:
        self.assertIsNone(tools_handbrake.parse_progress_line("Encoding: task 1 of 1, starting"))
        self.assertIsNone(tools_handbrake.parse_progress_line("[12:00:00] scan: 42.31 %"))


class TestProgressMerge(unittest.TestCase):
    """The bus merge: a tick updates its fields, never wipes the others."""

    def test_a_percent_tick_does_not_erase_run_or_file(self) -> None:
        last = {
            "v": 1,
            "stage": "crf-search",
            "run": {"done": 3, "total": 12},
            "file": {
                "movie": "a.mkv",
                "path": "/media/a.mkv",
                "stage": "crf-search",
                "step": 4,
                "steps": 7,
            },
            "probe": {"index": 2, "count": 5, "crf": 27, "vmaf": 95.9, "passed": False},
        }
        merged = engine_progress.merge(
            last,
            {
                "v": 1,
                "stage": "encode",
                "file": {"stage": "encode", "step": 5},
                "encode": {
                    "percent": 42.5,
                    "fps": 12.3,
                    "avg_fps": 12.1,
                    "eta_seconds": 60,
                },
            },
        )
        self.assertEqual(merged["run"], {"done": 3, "total": 12})
        self.assertEqual(merged["file"]["movie"], "a.mkv")
        self.assertEqual(merged["file"]["step"], 5)
        self.assertEqual(merged["encode"]["percent"], 42.5)
        # the probe trail stays: the encode is still the same file's work
        self.assertEqual(merged["probe"]["crf"], 27)
        self.assertEqual(len(merged["probes"]), 1)

    def test_a_new_file_resets_the_previous_probes_and_chosen(self) -> None:
        last = {
            "v": 1,
            "stage": "encode",
            "file": {"movie": "a.mkv", "path": "/a.mkv", "stage": "encode"},
            "probe": {"index": 2, "count": 5, "crf": 27, "vmaf": 95.9, "passed": False},
            "probes": [
                {"index": 1, "count": 5, "crf": 30, "vmaf": 94.8, "passed": False},
                {"index": 2, "count": 5, "crf": 27, "vmaf": 95.9, "passed": False},
            ],
            "chosen": {"crf": 27, "vmaf": 95.9, "predicted_bytes": 10, "reason": None},
            "encode": {"percent": 40.0},
        }
        merged = engine_progress.merge(
            last,
            {
                "v": 1,
                "stage": "decision",
                "file": {"movie": "b.mkv", "path": "/b.mkv", "stage": "decision"},
            },
        )
        self.assertEqual(merged["file"]["movie"], "b.mkv")
        self.assertNotIn("probes", merged)
        self.assertNotIn("probe", merged)
        self.assertNotIn("chosen", merged)
        self.assertNotIn("encode", merged)

    def test_provenance_maps_measured_cache_and_plan(self) -> None:
        """The stage result's source is measured | cache | plan (issue #631)."""
        live = [SampleProbe(crf=21, vmaf=95.0)]
        cached = [
            SampleProbe(
                crf=21,
                vmaf=95.0,
                cached=True,
                measured_at="2026-11-14 08:12:00",
            )
        ]
        self.assertEqual(progress_provenance(live), ("measured", None))
        self.assertEqual(progress_provenance(cached), ("cache", "2026-11-14 08:12:00"))
        self.assertEqual(
            progress_provenance(
                cached,
                crf_source="plan",
                plan_created_at="2026-11-15 10:00:00",
            ),
            ("plan", "2026-11-15 10:00:00"),
        )

    def test_merge_keeps_probe_cache_provenance(self) -> None:
        """A cached replay's probe/source/measured_at survive the bus merge
        (maintainer 2026-11-14, issue #631)."""
        merged = engine_progress.merge(
            None,
            {
                "v": 1,
                "stage": "crf-search",
                "probe": {
                    "index": 1,
                    "count": 1,
                    "crf": 24,
                    "vmaf": 96.6,
                    "predicted_bytes": None,
                    "passed": True,
                    "cached": True,
                    "measured_at": "2026-11-14 08:12:00",
                },
                "chosen": {
                    "crf": 24,
                    "vmaf": 96.6,
                    "predicted_bytes": 1_000,
                    "reason": None,
                    "source": "cache",
                },
                "source": "cache",
                "measured_at": "2026-11-14 08:12:00",
            },
        )
        self.assertTrue(merged["probe"]["cached"])
        self.assertEqual(merged["probe"]["measured_at"], "2026-11-14 08:12:00")
        self.assertEqual(merged["chosen"]["source"], "cache")
        self.assertEqual(merged["source"], "cache")
        self.assertEqual(merged["measured_at"], "2026-11-14 08:12:00")

    def test_a_completed_probe_accumulates_by_index(self) -> None:
        merged = engine_progress.merge(None, {"probe": {"index": 1, "crf": 30, "vmaf": 94.8}})
        merged = engine_progress.merge(merged, {"probe": {"index": 2, "crf": 24, "vmaf": 96.6}})
        merged = engine_progress.merge(merged, {"probe": {"index": 1, "crf": 30, "vmaf": 94.9}})
        self.assertEqual(
            [(p["index"], p["crf"], p["vmaf"]) for p in merged["probes"]],
            [(1, 30, 94.9), (2, 24, 96.6)],
        )


class TestStructuredEventSequence(unittest.TestCase):
    """The engine emits one structured event per stage for one file."""

    def test_search_quality_emits_bars_samples_probes_and_chosen(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x" * 16)
            sample = Path(tmp) / "sample_0.mkv"
            sample.write_bytes(b"x" * 8)

            def fake_extract(movie_path, workdir, count, seconds, cancel=None, progress=None):
                return engine_samples.extract_samples(
                    movie_path, workdir, count, seconds, cancel=cancel, progress=progress
                )

            def fake_run(command, cancel=None):
                Path(command[-1]).write_bytes(b"x" * 8)
                return 0, ""

            decision = SimpleNamespace(
                input=movie,
                eligible=True,
                identity=SimpleNamespace(size=16, mtime=0.0),
                width=1920,
                height=1080,
                bit_depth=8,
                hdr=False,
                encoder="svt_av1",
                duration=100.0,
                crf_range=engine_search.DEFAULT_CRF_RANGE,
                sample_count=1,
                sample_seconds=10.0,
            )
            with (
                patch.object(engine_plan, "detect_crop_summary_cached", return_value=object()),
                patch.object(engine_plan, "crop_bars_or_fallback", return_value=None),
                patch.object(engine_plan, "source_bit_depth", return_value=8),
                patch.object(engine_plan, "extract_samples", side_effect=fake_extract),
                patch.object(engine_plan, "ensure_unchanged"),
                patch.object(
                    engine_plan,
                    "vmaf_recipe",
                    return_value=SimpleNamespace(
                        fingerprint=lambda: "recipe", identifier=lambda: "recipe"
                    ),
                ),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search,
                    "probe_crf",
                    side_effect=lambda samples, crf, *a, **k: 96.6 if crf >= 24 else 94.8,
                ),
                patch.object(
                    engine_samples.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout=b'{"format": {"duration": "100.0"}}', stderr=b""
                    ),
                ),
                patch.object(engine_samples, "_run", side_effect=fake_run),
            ):
                result = engine_plan.search_quality(
                    decision,
                    target_vmaf=96.0,
                    min_saving_percent=15,
                    db_file=None,
                    progress=events.append,
                )
        self.assertTrue(result.success)
        stages = [e["stage"] for e in events]
        self.assertEqual(stages[0], "bars")
        self.assertIn("samples", stages)
        self.assertIn("crf-search", stages)
        self.assertLess(stages.index("bars"), stages.index("samples"))
        self.assertLess(stages.index("samples"), stages.index("crf-search"))
        self.assertEqual(stages[-1], "crf-search")
        self.assertTrue(events[-1]["chosen"])
        # samples never ride done/total (the folder counter's fields)
        sample_event = next(e for e in events if e["stage"] == "samples")
        self.assertEqual(sample_event["sample"], {"index": 1, "count": 1})
        self.assertNotIn("done", sample_event)
        self.assertNotIn("total", sample_event)
        # completed probes carry numeric crf/vmaf and a verdict
        probes = [e["probe"] for e in events if e.get("probe") and e["probe"]["vmaf"] is not None]
        self.assertTrue(probes)
        self.assertTrue(all(isinstance(p["crf"], int) for p in probes))
        self.assertTrue(all(isinstance(p["vmaf"], float) for p in probes))
        self.assertTrue(all(isinstance(p["passed"], bool) for p in probes))

    def test_the_stored_plan_reuse_path_emits_the_crf_search_stage(self) -> None:
        """A valid stored plan is a CRF-search decision reused, not a `probe`
        stage (issue #631): the event carries the new shape and the chosen
        result; a skipped stored plan carries its reason and no encode."""
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x" * 16)
            output = Path(tmp) / "out.mkv"
            events: list[dict] = []

            def fake_encode(movie_path, out, *args, **kwargs):
                out.write_bytes(b"y" * 8)
                kwargs["progress"](
                    engine_progress.event(
                        "encode",
                        file={"movie": movie_path.name, "path": str(movie_path), "stage": "encode"},
                        encode={"percent": 10.0},
                    )
                )
                return SimpleNamespace(
                    input_size=16,
                    output_size=8,
                    chosen_crf=21,
                    achieved_vmaf=95.4,
                    error=None,
                    input_mtime=None,
                    success=True,
                    cancelled=False,
                )

            valid_plan = {
                "chosen_crf": 21,
                "achieved_vmaf": 95.4,
                "skipped_reason": None,
                "crop": None,
                "probes": json.dumps(
                    [
                        (30, 94.8, "2026-11-14 08:12:00", True),
                        (24, 96.6, "2026-11-14 08:12:00", True),
                    ]
                ),
                "estimated_size": 1_000_000,
                "created_at": "2026-11-25 12:00:00",
                "encoder_preset": "5",
            }
            skipped_plan = {
                **valid_plan,
                "chosen_crf": 14,
                "achieved_vmaf": 92.8,
                "skipped_reason": "target unreachable",
                "estimated_size": None,
            }
            stack = contextlib.ExitStack()
            self.addCleanup(stack.close)
            for patcher in (
                patch.object(engine_pipeline, "_lookup_plan", return_value=valid_plan),
                patch.object(
                    engine_pipeline,
                    "_source_snapshot",
                    return_value={
                        "width": 1920,
                        "height": 1080,
                        "duration": 100.0,
                        "codec": "h264",
                        "bit_depth": 8,
                        "bitrate": None,
                    },
                ),
                patch.object(engine_plan, "is_hdr_file", return_value=False),
                patch.object(engine_pipeline, "source_bit_depth", return_value=8),
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=100.0),
                patch.object(engine_pipeline, "check_disk_space", return_value=(True, "")),
                patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
                patch.object(engine_pipeline, "build_encode_command", return_value=["hb"]),
                patch.object(engine_pipeline, "_encode_with_command", side_effect=fake_encode),
                patch.object(engine_pipeline, "durations_match", return_value=True),
                patch.object(engine_pipeline, "tracks_survived", return_value=(True, "ok")),
                patch.object(engine_pipeline, "_output_bitrate", return_value=None),
            ):
                stack.enter_context(patcher)
            result = engine_pipeline.encode_with_quality(
                movie,
                output,
                progress=events.append,
                db_file=Path(tmp) / "metadata.db",
            )
            self.assertTrue(result.success)
            self.assertEqual(events[0]["v"], 1)
            self.assertEqual(events[0]["stage"], "crf-search")
            self.assertEqual(events[0]["file"]["stage"], "crf-search")
            self.assertEqual(events[0]["file"]["step"], 4)
            self.assertEqual(events[0]["chosen"]["crf"], 21)
            self.assertEqual(events[0]["chosen"]["vmaf"], 95.4)
            self.assertEqual(events[0]["chosen"]["predicted_bytes"], 1_000_000)
            self.assertIsNone(events[0]["chosen"]["reason"])
            self.assertEqual(events[0]["chosen"]["source"], "plan")
            self.assertEqual(events[0]["source"], "plan")
            self.assertEqual(events[0]["measured_at"], "2026-11-25 12:00:00")
            self.assertTrue(events[0]["probes"])
            self.assertTrue(all(probe["cached"] for probe in events[0]["probes"]))
            self.assertTrue(
                all(probe["measured_at"] == "2026-11-14 08:12:00" for probe in events[0]["probes"])
            )
            self.assertEqual(events[0]["probe"]["cached"], True)
            self.assertIn("encode", [event["stage"] for event in events])

            events.clear()
            stack.pop_all().close()
            stack = contextlib.ExitStack()
            self.addCleanup(stack.close)
            for patcher in (
                patch.object(engine_pipeline, "_lookup_plan", return_value=skipped_plan),
                patch.object(
                    engine_pipeline,
                    "_source_snapshot",
                    return_value={
                        "width": 1920,
                        "height": 1080,
                        "duration": 100.0,
                        "codec": "h264",
                        "bit_depth": 8,
                        "bitrate": None,
                    },
                ),
                patch.object(engine_plan, "is_hdr_file", return_value=False),
                patch.object(engine_pipeline, "source_bit_depth", return_value=8),
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=100.0),
                patch.object(engine_pipeline, "check_disk_space", return_value=(True, "")),
            ):
                stack.enter_context(patcher)
            skipped = engine_pipeline.encode_with_quality(
                movie,
                output,
                progress=events.append,
                db_file=Path(tmp) / "metadata.db",
            )
            self.assertFalse(skipped.success)
            self.assertEqual(events[0]["stage"], "crf-search")
            self.assertEqual(events[0]["chosen"]["reason"], "target unreachable")
            self.assertEqual([event["stage"] for event in events], ["crf-search"])

    def test_the_full_event_sequence_for_one_file(self) -> None:
        """One resident encode emits decision -> bars -> samples -> probes ->
        chosen -> encode -> verify -> swap, with the folder job carrying the
        stage row's id and the stage row keeping its own job_id (#631/#639)."""
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x" * 16)
            os.utime(movie, (0, 0))  # old enough for the witness (#586)
            db_file = Path(tmp) / "metadata.db"
            from shrinkray import db

            connection = db.connect(db_file)
            self.addCleanup(connection.close)
            db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie,
                    mtime=movie.stat().st_mtime,
                    size=movie.stat().st_size,
                    name=movie.name,
                    category="Cat",
                ),
            )
            connection.commit()
            bus = jobs.ProgressBus()
            queue = jobs.JobQueue(connection, bus)
            job = queue.enqueue(
                jobs.Job(
                    kind=jobs.JobKind.ENCODE,
                    payload={"paths": [str(movie)], "quality": True},
                )
            )
            seen: list[dict] = []

            def fake_extract(movie_path, workdir, count, seconds, cancel=None, progress=None):
                return engine_samples.extract_samples(
                    movie_path, workdir, count, seconds, cancel=cancel, progress=progress
                )

            def fake_run(command, cancel=None):
                Path(command[-1]).write_bytes(b"x" * 8)
                return 0, ""

            def fake_encode(movie_path, output, *args, **kwargs):
                output.write_bytes(b"y" * 8)
                kwargs["progress"](
                    engine_progress.event(
                        "encode",
                        file={
                            "movie": movie_path.name,
                            "path": str(movie_path),
                            "stage": "encode",
                            "step": 5,
                            "steps": 7,
                        },
                        encode={
                            "percent": 42.31,
                            "fps": 12.3,
                            "avg_fps": 12.1,
                            "eta_seconds": 60,
                        },
                    )
                )
                return SimpleNamespace(
                    input_size=16,
                    output_size=8,
                    chosen_crf=21,
                    achieved_vmaf=95.4,
                    error=None,
                    input_mtime=None,
                    success=True,
                    cancelled=False,
                )

            plan_row = {
                "chosen_crf": 21,
                "achieved_vmaf": 95.4,
                "skipped_reason": None,
                "crop": None,
                "probes": json.dumps(
                    [
                        (30, 94.8, "2026-11-14 08:12:00", True),
                        (24, 96.6, "2026-11-14 08:12:00", True),
                    ]
                ),
                "created_at": "2026-11-25 12:00:00",
                "encoder_preset": "5",
                "estimated_size": 1_000_000,
            }
            stack = contextlib.ExitStack()
            self.addCleanup(stack.close)
            for patcher in (
                patch.object(engine_plan, "decide_quality", return_value=_eligible_decision(movie)),
                patch.object(engine_plan, "stored_plan_row", return_value=None),
                patch.object(engine_plan, "detect_crop_summary_cached", return_value=object()),
                patch.object(engine_plan, "crop_bars_or_fallback", return_value=None),
                patch.object(engine_plan, "source_bit_depth", return_value=8),
                patch.object(engine_plan, "extract_samples", side_effect=fake_extract),
                patch.object(engine_plan, "ensure_unchanged"),
                patch.object(
                    engine_plan,
                    "vmaf_recipe",
                    return_value=SimpleNamespace(
                        fingerprint=lambda: "recipe", identifier=lambda: "recipe"
                    ),
                ),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "is_hdr_file", return_value=False),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search,
                    "probe_crf",
                    side_effect=lambda samples, crf, *a, **k: 96.6 if crf >= 24 else 94.8,
                ),
                patch.object(
                    engine_samples.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout=b'{"format": {"duration": "100.0"}}', stderr=b""
                    ),
                ),
                patch.object(engine_samples, "_run", side_effect=fake_run),
                patch.object(engine_pipeline, "_lookup_plan", return_value=plan_row),
                patch.object(
                    engine_pipeline,
                    "_source_snapshot",
                    return_value={
                        "width": 1920,
                        "height": 1080,
                        "duration": 100.0,
                        "codec": "h264",
                        "bit_depth": 8,
                        "bitrate": None,
                    },
                ),
                patch.object(engine_pipeline, "check_disk_space", return_value=(True, "")),
                patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
                patch.object(engine_pipeline, "source_bit_depth", return_value=8),
                patch.object(engine_pipeline, "source_codec", return_value="h264"),
                patch.object(engine_pipeline, "build_encode_command", return_value=["hb"]),
                patch.object(engine_pipeline, "_encode_with_command", side_effect=fake_encode),
                patch.object(engine_pipeline, "durations_match", return_value=True),
                patch.object(engine_pipeline, "tracks_survived", return_value=(True, "ok")),
                patch.object(engine_pipeline, "_output_bitrate", return_value=None),
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=100.0),
                patch("shrinkray.service.record_plan", lambda *a, **k: None),
                patch(
                    "shrinkray.service.record_quality_results",
                    lambda *a, **k: SimpleNamespace(
                        encoded=True, skipped=False, error=None, encode_id=1
                    ),
                ),
                patch(
                    "shrinkray.service.swap_encode_into_place",
                    return_value=SimpleNamespace(replaced=True, reason=None),
                ),
                patch("shrinkray.server.jobs.cap_threads", return_value=1),
            ):
                stack.enter_context(patcher)

            def report(data: dict) -> None:
                seen.append(data)
                queue.report_progress(job, data)

            jobs.execute_encode(
                job,
                connection,
                __import__("threading").Event(),
                progress=report,
                queue=queue,
            )
        stages = [event["stage"] for event in seen]
        self.assertEqual(stages[0], "decision")
        for earlier, later in (
            ("decision", "bars"),
            ("bars", "samples"),
            ("samples", "crf-search"),
            ("crf-search", "encode"),
            ("encode", "verify"),
            ("verify", "swap"),
        ):
            self.assertLess(stages.index(earlier), stages.index(later), stages)
        # the file's own completion follows the swap
        self.assertEqual(stages[-1], "encode")
        # the folder job's events name the stage row; the stage row's own
        # event keeps its own job_id (#639)
        stage_rows = {
            event["stage_job_id"] for event in seen if event.get("stage_job_id") is not None
        }
        self.assertEqual(len(stage_rows), 3)
        self.assertIn(job.id, bus.latest())
        # a percent tick did not erase the folder's file counter
        for event in seen:
            if event["stage"] == "encode" and event.get("encode"):
                self.assertEqual(event["run"], {"done": 0, "total": 1})
                break
        else:
            self.fail("no encode event carried the run counter")

    def test_encode_command_reports_the_parsed_line(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "handbrake-stub.sh"
            output = Path(tmp) / "out.mkv"
            input_file = Path(tmp) / "in.mkv"
            input_file.write_bytes(b"x" * 16)
            script.write_text(
                "#!/bin/sh\n"
                "printf 'Encoding: task 1 of 1, 42.31 %% "
                "(12.30 fps, avg 12.10 fps, ETA 00h01m00s)\\n'\n"
                'printf x > "$1"\n'
            )
            script.chmod(0o755)
            with (
                patch.object(engine_pipeline, "durations_match", return_value=True),
                patch.object(
                    engine_pipeline,
                    "_source_snapshot",
                    return_value={
                        "width": 1920,
                        "height": 1080,
                        "duration": 100.0,
                        "codec": "h264",
                        "bit_depth": 8,
                        "bitrate": None,
                    },
                ),
            ):
                result = engine_pipeline._encode_with_command(
                    input_file,
                    output,
                    ["sh", str(script), str(output)],
                    progress=events.append,
                )
        self.assertTrue(result.success)
        self.assertTrue(events)
        self.assertEqual(events[-1]["stage"], "encode")
        self.assertEqual(events[-1]["encode"]["percent"], 42.31)
        self.assertEqual(events[-1]["encode"]["fps"], 12.3)
        self.assertEqual(events[-1]["encode"]["avg_fps"], 12.1)
        self.assertEqual(events[-1]["encode"]["eta_seconds"], 60)
        self.assertEqual(events[-1]["file"]["movie"], "in.mkv")

    def test_the_bus_seed_keeps_the_merged_probe_trail(self) -> None:
        bus = jobs.ProgressBus()
        for index, (crf, vmaf) in enumerate([(30, 94.8), (24, 96.6)], start=1):
            bus.publish(
                1,
                {
                    "v": 1,
                    "stage": "crf-search",
                    "run": {"done": 3, "total": 12},
                    "file": {
                        "movie": "a.mkv",
                        "path": "/a.mkv",
                        "stage": "crf-search",
                        "step": 4,
                        "steps": 7,
                    },
                    "probe": {
                        "index": index,
                        "count": 3,
                        "crf": crf,
                        "vmaf": vmaf,
                        "predicted_bytes": None,
                        "passed": vmaf >= 96.0,
                    },
                },
            )
        sid, _box, seed = bus.subscribe()
        self.addCleanup(bus.unsubscribe, sid)
        self.assertEqual(len(seed), 1)
        merged = seed[0][1]
        self.assertEqual(merged["run"], {"done": 3, "total": 12})
        self.assertEqual(
            [(p["crf"], p["vmaf"]) for p in merged["probes"]],
            [(30, 94.8), (24, 96.6)],
        )


class TestSwapEvent(unittest.TestCase):
    """The swap stage is emitted around the in-place replace."""

    def test_the_encode_job_emits_swap_for_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x" * 16)
            os.utime(movie, (0, 0))  # old enough for the witness (#586)
            db_file = Path(tmp) / "metadata.db"
            from shrinkray import db

            connection = db.connect(db_file)
            self.addCleanup(connection.close)
            db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie,
                    mtime=movie.stat().st_mtime,
                    size=movie.stat().st_size,
                    name=movie.name,
                    category="Cat",
                ),
            )
            connection.commit()
            bus = jobs.ProgressBus()
            queue = jobs.JobQueue(connection, bus)
            job = queue.enqueue(
                jobs.Job(
                    kind=jobs.JobKind.ENCODE,
                    payload={"paths": [str(movie)], "quality": True},
                )
            )
            seen: list[dict] = []

            def fake_search(decision, *args, **kwargs):
                return SimpleNamespace(
                    input=movie,
                    output=movie,
                    success=True,
                    skipped_reason=None,
                    error=None,
                    chosen_crf=21,
                    achieved_vmaf=95.4,
                    probes=[],
                    input_size=16,
                    output_size=8,
                )

            def fake_encode(movie_path, output, *args, **kwargs):
                kwargs["progress"](engine_progress.event("verify"))
                output.write_bytes(b"y" * 8)
                return SimpleNamespace(
                    input_size=16,
                    output_size=8,
                    chosen_crf=21,
                    achieved_vmaf=95.4,
                    error=None,
                    input_mtime=None,
                )

            with (
                patch.object(engine_plan, "decide_quality", return_value=_eligible_decision(movie)),
                patch.object(engine_plan, "stored_plan_row", return_value=None),
                patch.object(engine_plan, "search_quality", side_effect=fake_search),
                patch.object(engine_pipeline, "encode_with_quality", side_effect=fake_encode),
                patch("shrinkray.service.record_plan", lambda *a, **k: None),
                patch(
                    "shrinkray.service.record_quality_results",
                    lambda *a, **k: SimpleNamespace(
                        encoded=True, skipped=False, error=None, encode_id=1
                    ),
                ),
                patch(
                    "shrinkray.service.swap_encode_into_place",
                    return_value=SimpleNamespace(replaced=True, reason=None),
                ),
                patch("shrinkray.server.jobs.cap_threads", return_value=1),
            ):
                jobs.execute_encode(
                    job,
                    connection,
                    __import__("threading").Event(),
                    progress=seen.append,
                    queue=queue,
                )
        stages = [event["stage"] for event in seen]
        self.assertEqual(stages[0], "decision")
        self.assertIn("verify", stages)
        self.assertIn("swap", stages)
        self.assertGreater(stages.index("swap"), stages.index("verify"))
        # the file's own completion tick follows the swap
        self.assertEqual(stages[-1], "encode")


def _eligible_decision(movie: Path) -> SimpleNamespace:
    return SimpleNamespace(
        input=movie,
        eligible=True,
        skipped_reason=None,
        error=None,
        identity=SimpleNamespace(size=16, mtime=None),
        hdr=False,
        encoder="svt_av1",
        duration=7200.0,
        width=1920,
        height=1080,
        crf_range=engine_search.DEFAULT_CRF_RANGE,
        sample_count=3,
        sample_seconds=10.0,
        bit_depth=None,
    )


if __name__ == "__main__":
    unittest.main()
