"""Job queue and API path validation tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shrinkray import db
from shrinkray.engine import search as engine_search
from shrinkray.server import jobs


def _eligible_decision(movie, **_kwargs):
    """A passing eligibility decision for the stage seams (issue #602)."""
    from types import SimpleNamespace

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


def _prediction(movie, **_kwargs):
    """A passing CRF-search prediction for the stage seams (issue #602)."""
    from types import SimpleNamespace

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
        crf_range=engine_search.DEFAULT_CRF_RANGE,
        sample_count=3,
        sample_seconds=10.0,
        crop_sample_count=10,
        vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
        hdr=False,
        encoder="svt_av1",
        input_mtime=None,
        probe_encoder_preset="5",
        encoder_preset="3",
        crop=None,
        vmaf_statistic="mean",
        target_vmaf=95.0,
        min_saving_percent=15,
        saved_percent=50.0,
        plan_created_at=None,
        crf_source="probe",
        encode_seconds=None,
        probe_seconds=None,
        input_width=None,
        input_height=None,
        input_codec=None,
        input_duration=None,
        input_bitrate=None,
        output_bitrate=None,
        input_bit_depth=None,
        tool_versions=None,
    )


class TestJobs(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")
        self.queue = jobs.JobQueue(self.connection)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def test_cancel_terminates_running_subprocess(self):
        """The cancel event terminates an in-flight subprocess (regression:
        cancel was only checked between movies, so a long probe or encode
        ran to completion despite the user cancelling)."""
        from shrinkray.tools.process import _run

        cancel = threading.Event()
        # a process that runs until killed (long enough to cancel mid-flight)
        command = ["ping", "-n", "30", "127.0.0.1"] if os.name == "nt" else ["sleep", "30"]
        result: list = []

        def runner():
            result.append(_run(command, cancel=cancel))

        thread = threading.Thread(target=runner)
        thread.start()
        threading.Event().wait(0)  # yield
        import time as _time

        _time.sleep(0.5)  # let the process start
        cancel.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "_run did not return after cancel")
        code, err = result[0]
        self.assertEqual(code, -1)
        self.assertIn("cancelled", err)

    def test_check_cancel_raises(self):
        """_check_cancel raises Cancelled when the event is set."""
        from shrinkray.engine.pipeline import _check_cancel
        from shrinkray.tools.process import Cancelled

        _check_cancel(None)  # disabled check never raises
        event = threading.Event()
        _check_cancel(event)  # not set: fine
        event.set()
        with self.assertRaises(Cancelled):
            _check_cancel(event)

    def test_quality_final_encode_always_streams(self):
        """Quality mode's final encode goes through the streamed loop even
        with progress=None (watch mode) - that loop is the disk watchdog;
        a blocking subprocess.run would encode unwatched. A watchdog
        cancellation surfaces as Cancelled with the engine's reason and the
        partial output already removed."""
        import tempfile

        from shrinkray.engine.pipeline import encode_with_quality
        from shrinkray.tools.process import Cancelled

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src = root / "in.mkv"
            out = root / "enc" / "in.mkv"
            (root / "enc").mkdir()
            src.write_bytes(b"x" * 8)
            stub = root / "hb_stub.cmd" if os.name == "nt" else root / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho Encoding: task 1 of 1, 42.31 %%\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho "Encoding: task 1 of 1, 42.31 %"\nexit 0\n')
                stub.chmod(0o755)

            def fake_probe(*args, **kwargs):
                return 20, 95.2, [], None

            with (
                mock.patch("shrinkray.engine.pipeline._probe_movie", side_effect=fake_probe),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=1_000_000),
                self.assertRaises(Cancelled) as ctx,
            ):
                encode_with_quality(src, out, progress=None)
            self.assertIn("disk space", str(ctx.exception))
            # the streamed engine removed the partial output itself
            self.assertFalse(out.exists())

    def test_plan_job_records_plans(self):
        """execute_plan persists each plan decision (the quicksave write
        path): a later encode of the same movie with the same settings
        finds a valid plan and skips probing."""
        from pathlib import Path as _Path
        from types import SimpleNamespace
        from unittest.mock import patch

        movie = _Path(self.tmp.name) / "Some Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        fake_result = SimpleNamespace(
            input=movie,
            output=movie,
            success=True,
            target_vmaf=95.0,
            min_saving_percent=15,
            chosen_crf=21,
            achieved_vmaf=95.47,
            probes=[],
            input_size=16,
            output_size=8,
            saved_percent=50.0,
            error=None,
            skipped_reason=None,
            encoder_preset="3",
            probe_encoder_preset="5",
            crop=None,
            crf_range=engine_search.DEFAULT_CRF_RANGE,
            sample_count=3,
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            hdr=False,
            encoder="svt_av1",
            input_mtime=None,  # a duck-typed result without the #436 field
        )
        with (
            patch("shrinkray.engine.plan.decide_quality", return_value=_eligible_decision(movie)),
            patch("shrinkray.engine.plan.search_quality", return_value=fake_result),
            patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            result = jobs.execute_plan(job, self.connection, threading.Event())
        self.assertEqual(result["plans"][0]["state"], "encode")
        row = self.connection.execute(
            "SELECT p.chosen_crf, f.path FROM plans p JOIN files f ON f.id = p.file_id"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["chosen_crf"], 21)
        self.assertEqual(row["path"], str(movie))

    def test_a_plan_job_records_the_per_file_stages(self):
        """Issue #602: a file's pipeline is its own job rows - the decision
        (the gates) and the CRF search - each with its own state and outcome,
        so the Jobs page can follow a file stage by stage."""
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Staged Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.search_quality",
                return_value=_prediction(movie),
            ),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_plan(job, self.connection, threading.Event(), queue=self.queue)
        rows = self.connection.execute(
            "SELECT kind, state, result FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual([row["kind"] for row in rows], ["decision", "crf-search"])
        self.assertEqual([row["state"] for row in rows], ["done", "done"])
        self.assertIn('"eligible": true', rows[0]["result"])
        self.assertIn('"crf": 21', rows[1]["result"])

    def test_a_rejected_file_records_no_search_row(self):
        """Issue #602: a decision that rejects the file is the whole pipeline
        for it - the decision row is done with its reason and no search row
        exists (nothing measured, nothing spent)."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Rejected Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        rejected = _NS(
            input=movie,
            eligible=False,
            skipped_reason="too short",
            error="duration 12s is below the 90s minimum",
            identity=_NS(size=16, mtime=None),
            hdr=False,
            encoder="svt_av1",
            crf_range=engine_search.DEFAULT_CRF_RANGE,
            sample_count=3,
            sample_seconds=10.0,
            duration=12.0,
        )
        with (
            _patch("shrinkray.engine.plan.decide_quality", return_value=rejected),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_plan(job, self.connection, threading.Event(), queue=self.queue)
        rows = self.connection.execute(
            "SELECT kind, state, result FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual([row["kind"] for row in rows], ["decision"])
        self.assertIn('"reason": "too short"', rows[0]["result"])
        # the rejection's plans row is written for real (issue #602): a later
        # encode reuses the verdict instead of re-probing the file
        plan = self.connection.execute(
            "SELECT skipped_reason FROM plans p JOIN files f ON f.id = p.file_id"
        ).fetchone()
        self.assertIsNotNone(plan)
        self.assertEqual(plan["skipped_reason"], "too short")

    def test_a_failed_plan_write_still_encodes(self):
        """Issue #602 review: a plan write that fails must never skip the
        encode - the encode boundary re-plans when nothing is stored."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Plan Write_t00.mkv"
        movie.write_bytes(b"x" * 16)
        out_dir = _Path(self.tmp.name) / "out"
        out_dir.mkdir()
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(out_dir),
                    "quality": True,
                },
            )
        )

        def fake_encode(movie, output, *args, **kwargs):
            output.write_bytes(b"y" * 8)
            return _NS(
                input_size=16,
                output_size=8,
                chosen_crf=21,
                achieved_vmaf=95.4,
                error=None,
                input_mtime=None,
            )

        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.search_quality",
                return_value=_prediction(movie),
            ),
            _patch(
                "shrinkray.service.record_plan",
                side_effect=RuntimeError("database is locked"),
            ),
            _patch(
                "shrinkray.engine.pipeline.encode_with_quality",
                side_effect=fake_encode,
            ) as engine,
            _patch(
                "shrinkray.service.record_quality_results",
                lambda *a, **k: _NS(encoded=True, skipped=False, error=None),
            ),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_encode(job, self.connection, threading.Event(), queue=self.queue)
        engine.assert_called_once()
        rows = self.connection.execute(
            "SELECT kind, state FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual(
            [(row["kind"], row["state"]) for row in rows],
            [("decision", "done"), ("crf-search", "done"), ("encode", "done")],
        )

    def test_a_search_stage_failure_skips_the_file(self):
        """Issue #602 review: a search-stage RuntimeError is a per-file skip -
        the row fails, the folder job continues, and no encode runs."""
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Broken Probe_t00.mkv"
        movie.write_bytes(b"x" * 16)
        out_dir = _Path(self.tmp.name) / "out"
        out_dir.mkdir()
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(out_dir),
                    "quality": True,
                },
            )
        )
        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.stored_plan_row",
                return_value=None,
            ),
            _patch(
                "shrinkray.engine.plan.search_quality",
                side_effect=RuntimeError("ffprobe could not read the sample"),
            ),
            _patch(
                "shrinkray.engine.pipeline.encode_with_quality",
                side_effect=AssertionError("a search failure must not encode"),
            ) as engine,
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            result = jobs.execute_encode(job, self.connection, threading.Event(), queue=self.queue)
        engine.assert_not_called()
        rows = self.connection.execute(
            "SELECT kind, state FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual(
            [(row["kind"], row["state"]) for row in rows],
            [("decision", "done"), ("crf-search", "failed")],
        )
        self.assertEqual(result["outcomes"][0]["state"], "skipped")
        self.assertIn("ffprobe could not read", result["outcomes"][0]["error"])

    def test_the_real_stored_plan_lookup_matches_a_recorded_plan(self):
        """Issue #602 review: the real `stored_plan_row` must find the plan
        `record_plan` stored - the encode job's reuse depends on it, and a
        patched lookup would hide a fingerprint drift."""
        from pathlib import Path as _Path

        from shrinkray.engine.identity import capture_run_identity
        from shrinkray.engine.plan import Decision, QualityResult, stored_plan_row
        from shrinkray.engine.search import DEFAULT_CRF_RANGE
        from shrinkray.service import record_plan
        from shrinkray.tools.vmaf import vmaf_recipe

        movie = _Path(self.tmp.name) / "Real Plan_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        recipe = vmaf_recipe(crop=None, width=1920, height=1080, bit_depth=10)
        stored = QualityResult(
            input=movie,
            output=movie,
            success=True,
            target_vmaf=95.0,
            min_saving_percent=15,
            chosen_crf=21,
            achieved_vmaf=95.4,
            input_size=16,
            output_size=8,
            encoder="svt_av1",
            probe_encoder_preset="5",
            encoder_preset="3",
            crop=None,
            crf_range=DEFAULT_CRF_RANGE,
            sample_count=3,
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_recipe=recipe.fingerprint(),
            hdr=False,
            input_mtime=None,
        )
        from shrinkray.config import load_config

        record_plan(self.connection, movie, stored, load_config())
        decision = Decision(
            input=movie,
            eligible=True,
            identity=capture_run_identity(movie),
            hdr=False,
            encoder="svt_av1",
            duration=7200.0,
            width=1920,
            height=1080,
            crf_range=DEFAULT_CRF_RANGE,
            sample_count=3,
            sample_seconds=10.0,
            bit_depth=10,
        )
        self.assertIsNotNone(
            stored_plan_row(
                movie,
                decision,
                target_vmaf=95.0,
                min_saving_percent=15,
                probe_encoder_preset="5",
                crop_sample_count=10,
                db_file=Path(self.tmp.name) / "test.db",
            )
        )

    def test_the_encode_stage_row_reports_the_live_progress(self):
        """Issue #639: the stage rows show the folder job's live progress -
        a row with no events reads "starting..." for the whole stage."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Live Progress_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        out_dir = _Path(self.tmp.name) / "out"
        out_dir.mkdir()
        bus = jobs.ProgressBus()
        queue = jobs.JobQueue(self.connection, bus)
        job = queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(out_dir),
                    "quality": True,
                },
            )
        )

        seen: dict[int, dict] = {}
        folder_events: list[dict] = []

        def fake_search(decision, *args, **kwargs):
            kwargs["progress"]({"stage": "probe", "percent": 10.0, "movie": decision.input.name})
            seen.update(bus.latest())
            return _prediction(movie)

        def fake_encode(movie, output, *args, **kwargs):
            kwargs["progress"]({"stage": "encode", "percent": 42.5, "movie": movie.name})
            # the seed cache drops a row's progress when it finishes, so the
            # event is captured while the encode is still running
            seen.update(bus.latest())
            output.write_bytes(b"y" * 8)
            return _NS(
                input_size=16,
                output_size=8,
                chosen_crf=21,
                achieved_vmaf=95.4,
                error=None,
                input_mtime=None,
            )

        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.stored_plan_row",
                return_value=None,
            ),
            _patch("shrinkray.engine.plan.search_quality", side_effect=fake_search),
            _patch("shrinkray.service.record_plan", lambda *a, **k: None),
            _patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_encode),
            _patch(
                "shrinkray.service.record_quality_results",
                lambda *a, **k: _NS(encoded=True, skipped=False, error=None),
            ),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_encode(
                job, self.connection, threading.Event(), progress=folder_events.append, queue=queue
            )
        rows = self.connection.execute(
            "SELECT id, kind, state FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        search_row = next(row for row in rows if row["kind"] == "crf-search")
        encode_row = next(row for row in rows if row["kind"] == "encode")
        # both stage rows receive the engine's events...
        self.assertIn(search_row["id"], seen)
        self.assertEqual(seen[search_row["id"]]["percent"], 10.0)
        self.assertIn(encode_row["id"], seen)
        self.assertEqual(seen[encode_row["id"]]["percent"], 42.5)
        # ...and the folder job keeps receiving them too
        folder_percents = [event.get("percent") for event in folder_events]
        self.assertIn(10.0, folder_percents)
        self.assertIn(42.5, folder_percents)

    def test_the_plan_path_search_row_reports_the_live_progress(self):
        """Issue #639: the plan job's crf-search row shows the search's live
        progress too - it is usually the longest stage of a plan run."""
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Plan Progress_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        bus = jobs.ProgressBus()
        queue = jobs.JobQueue(self.connection, bus)
        job = queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        seen: dict[int, dict] = {}

        def fake_search(decision, *args, **kwargs):
            kwargs["progress"]({"stage": "probe", "percent": 33.0, "movie": decision.input.name})
            seen.update(bus.latest())
            return _prediction(movie)

        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch("shrinkray.engine.plan.search_quality", side_effect=fake_search),
            _patch("shrinkray.service.record_plan", lambda *a, **k: None),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_plan(job, self.connection, threading.Event(), queue=queue)
        row = self.connection.execute(
            "SELECT id, kind FROM jobs WHERE id > ? AND kind = 'crf-search'", (job.id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row["id"], seen)
        self.assertEqual(seen[row["id"]]["percent"], 33.0)

    def test_a_stored_plan_skips_the_queue_path_search(self):
        """Issue #602: a valid stored plan means the search already ran for
        these bytes, so the encode job must not measure again - the search row
        reports the reuse and the search is never called."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Reused Plan_t00.mkv"
        movie.write_bytes(b"x" * 16)
        out_dir = _Path(self.tmp.name) / "out"
        out_dir.mkdir()
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(out_dir),
                    "quality": True,
                },
            )
        )

        def fake_encode(movie, output, *args, **kwargs):
            output.write_bytes(b"y" * 8)
            return _NS(
                input_size=16,
                output_size=8,
                chosen_crf=21,
                achieved_vmaf=95.4,
                error=None,
                input_mtime=None,
            )

        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.stored_plan_row",
                return_value={"chosen_crf": 21, "achieved_vmaf": 95.4, "skipped_reason": None},
            ),
            _patch(
                "shrinkray.engine.plan.search_quality",
                side_effect=AssertionError("the search must not run for a stored plan"),
            ) as search,
            _patch(
                "shrinkray.engine.pipeline.encode_with_quality",
                side_effect=fake_encode,
            ),
            _patch(
                "shrinkray.service.record_quality_results",
                lambda *a, **k: _NS(encoded=True, skipped=False, error=None),
            ),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_encode(job, self.connection, threading.Event(), queue=self.queue)
        search.assert_not_called()
        rows = self.connection.execute(
            "SELECT kind, state, result FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual(
            [(row["kind"], row["state"]) for row in rows],
            [("decision", "done"), ("crf-search", "done"), ("encode", "done")],
        )
        self.assertIn('"source": "plan"', rows[1]["result"])

    def test_a_cancel_during_the_encode_marks_its_stage_row_cancelled(self):
        """Issue #602 review: the encode stage row must read cancelled on a
        graceful stop, not failed - the worker marks the folder job cancelled."""
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch

        from shrinkray.tools.process import Cancelled

        movie = _Path(self.tmp.name) / "Cancelled Encode_t00.mkv"
        movie.write_bytes(b"x" * 16)
        out_dir = _Path(self.tmp.name) / "out"
        out_dir.mkdir()
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(out_dir),
                    "quality": True,
                },
            )
        )
        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.search_quality",
                return_value=_prediction(movie),
            ),
            _patch("shrinkray.service.record_plan", lambda *a, **k: None),
            _patch(
                "shrinkray.engine.pipeline.encode_with_quality",
                side_effect=Cancelled("cancelled during the encode"),
            ),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
            self.assertRaises(Cancelled),
        ):
            jobs.execute_encode(job, self.connection, threading.Event(), queue=self.queue)
        rows = self.connection.execute(
            "SELECT kind, state FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual(
            [(row["kind"], row["state"]) for row in rows],
            [("decision", "done"), ("crf-search", "done"), ("encode", "cancelled")],
        )

    def test_an_encode_job_records_the_three_stages(self):
        """Issue #602: the encode pipeline is three consecutive rows - the
        decision, the CRF search and the encode - so the Jobs page shows each
        stage with its own state and outcome."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Three Stages_t00.mkv"
        movie.write_bytes(b"x" * 16)
        out_dir = _Path(self.tmp.name) / "out"
        out_dir.mkdir()
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(out_dir),
                    "quality": True,
                },
            )
        )

        def fake_encode(movie, output, *args, **kwargs):
            output.write_bytes(b"y" * 8)
            return _NS(
                input_size=16,
                output_size=8,
                chosen_crf=21,
                achieved_vmaf=95.4,
                error=None,
                input_mtime=None,
            )

        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch(
                "shrinkray.engine.plan.search_quality",
                return_value=_prediction(movie),
            ),
            _patch("shrinkray.engine.pipeline.encode_with_quality", fake_encode),
            _patch("shrinkray.service.record_plan", lambda *a, **k: None),
            _patch(
                "shrinkray.service.record_quality_results",
                lambda *a, **k: _NS(encoded=True, skipped=False, error=None),
            ),
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_encode(job, self.connection, threading.Event(), queue=self.queue)
        rows = self.connection.execute(
            "SELECT kind, state FROM jobs WHERE id > ? ORDER BY id", (job.id,)
        ).fetchall()
        self.assertEqual([row["kind"] for row in rows], ["decision", "crf-search", "encode"])
        self.assertEqual([row["state"] for row in rows], ["done", "done", "done"])

    def test_plan_job_passes_encoder_preset(self):
        """The plan job threads the preset: a payload override wins, the
        config value is the fallback (issue #31: the preset was a constant
        no entry point could override)."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        movie = _Path(self.tmp.name) / "Preset Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        fake_result = _NS(
            input=movie,
            output=movie,
            success=True,
            target_vmaf=95.0,
            min_saving_percent=15,
            chosen_crf=21,
            achieved_vmaf=95.47,
            probes=[],
            input_size=16,
            output_size=8,
            saved_percent=50.0,
            error=None,
            skipped_reason=None,
            encoder_preset="3",
            probe_encoder_preset="5",
            crop=None,
            crf_range=(14, 32),
            sample_count=3,
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            hdr=False,
            encoder="svt_av1",
        )
        # a payload override reaches the engine...
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.PLAN,
                payload={"paths": [str(movie)], "encoder_preset": "8"},
            )
        )
        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch("shrinkray.engine.plan.search_quality", return_value=fake_result) as fake_plan,
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_plan(job, self.connection, threading.Event())
        self.assertEqual(fake_plan.call_args.kwargs.get("encoder_preset"), "8")
        # ...and without one, the config value does
        job2 = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch("shrinkray.engine.plan.search_quality", return_value=fake_result) as fake_plan2,
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_plan(job2, self.connection, threading.Event())
        self.assertEqual(fake_plan2.call_args.kwargs.get("encoder_preset"), "3")
        # the probe preset is a separate payload key (issue #22)
        job3 = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.PLAN,
                payload={"paths": [str(movie)], "probe_encoder_preset": "6"},
            )
        )
        with (
            _patch(
                "shrinkray.engine.plan.decide_quality",
                return_value=_eligible_decision(movie),
            ),
            _patch("shrinkray.engine.plan.search_quality", return_value=fake_result) as fake_plan3,
            _patch("shrinkray.server.jobs.cap_threads", return_value=1),
        ):
            jobs.execute_plan(job3, self.connection, threading.Event())
        self.assertEqual(fake_plan3.call_args.kwargs.get("probe_encoder_preset"), "6")

    def test_encode_single_file_payload(self):
        """A per-movie encode payload (a FILE path, as the UI's detail page
        sends) resolves to exactly one movie — regression: execute_encode
        scanned the file as a directory and reported done with 0 outcomes."""
        from pathlib import Path as _Path

        movie = _Path(self.tmp.name) / "Some Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)  # content irrelevant; resolution is mocked
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "paths": [str(movie)],
                    "output_dir": str(_Path(self.tmp.name) / "out"),
                    "quality": True,
                },
            )
        )
        outcomes: list = []

        def fake_encode_with_quality(movie, *args, **kwargs):
            outcomes.append({"movie": movie.name, "state": "encoded"})
            from types import SimpleNamespace

            return SimpleNamespace(
                input_size=1, output_size=1, chosen_crf=20, achieved_vmaf=95.0, error=None
            )

        from types import SimpleNamespace
        from unittest.mock import patch

        with (
            patch("shrinkray.engine.plan.decide_quality", side_effect=_eligible_decision),
            patch(
                "shrinkray.engine.plan.search_quality",
                side_effect=lambda movie, *a, **k: _prediction(movie),
            ),
            patch("shrinkray.engine.pipeline.encode_with_quality", fake_encode_with_quality),
            patch(
                "shrinkray.service.record_quality_results",
                lambda *a, **k: SimpleNamespace(encoded=True, skipped=False),
            ),
        ):
            result = jobs.execute_encode(job, self.connection, threading.Event())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["movie"], "Some Movie_t00.mkv")
        # the job's recorded outcomes carry the movie through (not empty)
        self.assertEqual(result["outcomes"][0]["movie"], "Some Movie_t00.mkv")

    def test_the_encode_job_resolves_the_probe_windows(self) -> None:
        """The resident ENCODE job resolves the probe windows with the one
        precedence (issue #526): density by default, a configured count when
        set (the reviewer's mutation: reverting this left the file green)."""
        from pathlib import Path as _Path
        from types import SimpleNamespace as _NS
        from unittest.mock import patch as _patch

        from shrinkray.config import Conf

        movie = _Path(self.tmp.name) / "Probe Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        captured: dict = {}

        def fake_encode(movie_in, output_file, **kwargs):
            captured["sample_every"] = kwargs.get("sample_every")
            captured["sample_count"] = kwargs.get("sample_count")
            return _NS(input_size=1, output_size=1, chosen_crf=20, achieved_vmaf=95.0, error=None)

        def run(config):
            job = self.queue.enqueue(
                jobs.Job(
                    kind=jobs.JobKind.ENCODE,
                    payload={
                        "paths": [str(movie)],
                        "output_dir": str(_Path(self.tmp.name) / "out"),
                        "quality": True,
                    },
                )
            )
            with (
                _patch("shrinkray.engine.plan.decide_quality", side_effect=_eligible_decision),
                _patch(
                    "shrinkray.engine.plan.search_quality",
                    side_effect=lambda movie, *a, **k: _prediction(movie),
                ),
                _patch("shrinkray.engine.pipeline.encode_with_quality", fake_encode),
                _patch(
                    "shrinkray.service.record_quality_results",
                    lambda *a, **k: _NS(encoded=True, skipped=False),
                ),
                _patch("shrinkray.config.load_config", return_value=config),
                _patch("shrinkray.config.refresh_config", return_value=config),
            ):
                jobs.execute_encode(job, self.connection, threading.Event())

        run(Conf())
        self.assertEqual(captured["sample_every"], 600.0)
        self.assertIsNone(captured["sample_count"])
        run(Conf({"quality": {"sample_count": 5}}))
        self.assertEqual(captured["sample_count"], 5)

    def test_an_in_place_job_never_swaps_an_encode_with_a_track_finding(self):
        """Review of #287: the job worker called swap_encode_into_place without
        the encode's verification finding, so a track-losing encode replaced
        the library file. In place (no output_dir), the REAL swap runs here."""
        from pathlib import Path as _Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from shrinkray.engine.plan import QualityResult

        library = _Path(self.tmp.name).resolve() / "lib"
        library.mkdir()
        movie = library / "Some Movie.mkv"
        movie.write_bytes(b"ORIGINAL" * 100)
        job = self.queue.enqueue(
            jobs.Job(kind=jobs.JobKind.ENCODE, payload={"paths": [str(movie)], "quality": True})
        )

        def fake_encode_with_quality(movie, output, *args, **kwargs):
            output.write_bytes(b"NEW")  # smaller: only the finding may decline
            return QualityResult(
                input=movie,
                output=output,
                success=True,
                chosen_crf=20,
                achieved_vmaf=95.0,
                input_size=800,
                output_size=3,
                error="output lost 1 audio track(s)",
            )

        with (
            patch("shrinkray.engine.plan.decide_quality", side_effect=_eligible_decision),
            patch(
                "shrinkray.engine.plan.search_quality",
                side_effect=lambda movie, *a, **k: _prediction(movie),
            ),
            patch("shrinkray.engine.pipeline.encode_with_quality", fake_encode_with_quality),
            patch(
                "shrinkray.service.record_quality_results",
                lambda *a, **k: SimpleNamespace(encoded=True, skipped=False, encode_id=None),
            ),
        ):
            result = jobs.execute_encode(job, self.connection, threading.Event())
        self.assertEqual(movie.read_bytes(), b"ORIGINAL" * 100)
        self.assertEqual(list(library.glob("*.shrinkray-original")), [])
        self.assertEqual(result["outcomes"][0]["state"], "skipped")

    def test_legacy_job_kind_reads_as_legacy(self):
        """A stored row of a removed kind does not crash the history.

        Removing a JobKind member must not make existing databases
        unreadable (issue #37): a user's jobs table keeps 'dedupe' rows
        written by an older build.
        """
        self.connection.execute(
            "INSERT INTO jobs (kind, payload, state) VALUES ('dedupe', '{}', 'done')"
        )
        self.connection.commit()
        listed = self.queue.all_jobs()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].kind, jobs.JobKind.LEGACY)
        self.assertEqual(listed[0].kind.value, "legacy")

    def test_queued_legacy_job_is_retired_not_executed(self):
        """A queued row of a removed kind fails at startup instead of hanging.

        The worker loads queued rows when it boots; an unknown kind must
        not raise there (which would stop the app from starting) and must
        not be executed either.
        """
        self.connection.execute(
            "INSERT INTO jobs (kind, payload, state) VALUES ('dedupe', '{}', 'queued')"
        )
        self.connection.commit()
        fresh = jobs.JobQueue(self.connection)
        self.assertEqual(fresh.all_jobs()[0].state, jobs.JobState.FAILED)
        self.assertIn("removed in this version", fresh.all_jobs()[0].error or "")
        self.assertIsNone(fresh.take_next())

    def test_enqueue_and_persist(self):
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={"path": "D:/Movies"}))
        self.assertIsNotNone(job.id)
        self.assertEqual(job.state, jobs.JobState.QUEUED)
        # persisted => a fresh queue loads it as pending
        fresh = jobs.JobQueue(self.connection)
        self.assertEqual(len(fresh.all_jobs()), 1)
        loaded = fresh.take_next()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.kind, jobs.JobKind.SCAN)
        self.assertEqual(loaded.payload["path"], "D:/Movies")

    def test_take_next_marks_running_and_finish_stores_result(self):
        self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={}))
        taken = self.queue.take_next()
        self.assertEqual(taken.state, jobs.JobState.RUNNING)
        self.queue.finish(taken, {"plans": []})
        reloaded = self.queue.get(taken.id)
        self.assertEqual(reloaded.state, jobs.JobState.DONE)
        self.assertEqual(reloaded.result, {"plans": []})

    def test_fail_stores_error(self):
        self.queue.enqueue(jobs.Job(kind=jobs.JobKind.ENCODE, payload={}))
        taken = self.queue.take_next()
        self.queue.fail(taken, "boom")
        reloaded = self.queue.get(taken.id)
        self.assertEqual(reloaded.state, jobs.JobState.FAILED)
        self.assertEqual(reloaded.error, "boom")

    def test_progress_never_persists_and_fans_out(self):
        """report_progress publishes to the bus only (regression for the
        DB-as-bus era: the running row's result column stays NULL — the
        database stores no live state — while the seed cache carries the
        latest event for SSE subscribers)."""
        bus = jobs.ProgressBus()
        q = jobs.JobQueue(self.connection, bus=bus)
        q.enqueue(jobs.Job(kind=jobs.JobKind.ENCODE, payload={}))
        taken = q.take_next()
        q.report_progress(taken, {"stage": "encode", "percent": 40})
        reloaded = q.get(taken.id)
        self.assertIsNone(reloaded.result)  # nothing persisted
        self.assertEqual(bus.latest().get(taken.id), {"stage": "encode", "percent": 40})
        # a subscriber present BEFORE the publish receives the event
        sid, box, seed = bus.subscribe()
        self.assertEqual(seed, [(taken.id, {"stage": "encode", "percent": 40})])
        q.report_progress(taken, {"stage": "encode", "percent": 60})
        self.assertEqual(box.get_nowait(), (taken.id, {"stage": "encode", "percent": 60}))
        bus.unsubscribe(sid)

    def test_bus_seed_cache_and_late_subscriber(self):
        """The bus seeds late subscribers with the latest event per job and
        forgets jobs on terminal writes (the cache must not grow forever)."""
        bus = jobs.ProgressBus()
        q = jobs.JobQueue(self.connection, bus=bus)
        q.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={}))
        taken = q.take_next()
        q.report_progress(taken, {"percent": 10})
        q.report_progress(taken, {"percent": 20})  # replaces, not accumulates
        sid, _box, seed = bus.subscribe()  # late subscriber
        self.assertEqual(seed, [(taken.id, {"percent": 20})])
        # terminal write drops the job from the seed cache
        q.finish(taken, {"movies": 1})
        self.assertEqual(bus.latest(), {})
        bus.unsubscribe(sid)

    def test_bus_mailbox_bounded_drop_oldest(self):
        """A slow consumer's mailbox drops the oldest event instead of
        growing or stalling the producer (progress is replaceable)."""
        bus = jobs.ProgressBus()
        sid, box, _ = bus.subscribe()
        self.addCleanup(bus.unsubscribe, sid)
        for p in range(100):
            bus.publish(1, {"percent": p})
        events = []
        while not box.empty():
            events.append(box.get_nowait())
        self.assertEqual(len(events), jobs.MAILBOX_SIZE)
        # the surviving events are the NEWEST ones
        self.assertEqual(events[-1], (1, {"percent": 99}))
        self.assertEqual(events[0], (1, {"percent": 100 - jobs.MAILBOX_SIZE}))

    def test_cancelled_job_keeps_partial_result(self):
        """A graceful cancel carries the engine's partial result — the UI
        renders outcomes on cancelled encode rows (App.svelte reads
        outcomes from done OR cancelled rows)."""
        self.queue.enqueue(jobs.Job(kind=jobs.JobKind.ENCODE, payload={}))
        taken = self.queue.take_next()
        self.queue.mark_cancelled(taken, {"outcomes": [{"movie": "a.mkv", "state": "encoded"}]})
        reloaded = self.queue.get(taken.id)
        self.assertEqual(reloaded.result["outcomes"][0]["movie"], "a.mkv")

    def test_history_pruned_to_retention_cap(self):
        """Terminal writes keep the newest JOB_RETENTION terminated rows and
        hard-delete older ones (the jobs table never had any retention —
        the library side did via prune_missing, this side grew forever).
        Queued/running rows are never touched."""
        from shrinkray.server.jobs import JOB_RETENTION

        total = JOB_RETENTION + 10
        for i in range(total):
            self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={"n": i}))
            taken = self.queue.take_next()
            self.queue.finish(taken, {"n": i})
        # one more, left queued: live rows never prune
        self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={"n": "keep"}))
        count = self.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(count, JOB_RETENTION + 1)
        # the OLDEST terminated rows went: survivors are the newest ids
        oldest = self.connection.execute(
            "SELECT MIN(id) FROM jobs WHERE state = 'done'"
        ).fetchone()[0]
        self.assertEqual(oldest, total - JOB_RETENTION + 1)

    def test_cancel_queued_job(self):
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={}))
        self.assertTrue(self.queue.cancel(job.id))
        reloaded = self.queue.get(job.id)
        self.assertEqual(reloaded.state, jobs.JobState.CANCELLED)
        # the queue no longer hands it out
        self.assertIsNone(self.queue.take_next())

    def test_cancel_running_job_signals_event(self):
        self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={}))
        taken = self.queue.take_next()
        event = threading.Event()
        self.queue.register_cancel_event(taken.id, event)
        self.assertTrue(self.queue.cancel(taken.id))
        self.assertTrue(event.is_set())
        self.queue.unregister_cancel_event(taken.id)

    def test_worker_executes_job(self):
        payload = {"path": str(Path(self.tmp.name) / "movies")}
        (Path(self.tmp.name) / "movies").mkdir()
        # a junk file: the scan handles it as an error, but the job succeeds
        (Path(self.tmp.name) / "movies" / "x.mkv").write_bytes(b"junk")
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload=payload))
        worker = jobs.JobWorker(self.connection, self.queue)
        worker._execute(job)  # direct execution => deterministic, no thread wait
        reloaded = self.queue.get(job.id)
        self.assertEqual(reloaded.state, jobs.JobState.DONE)
        self.assertEqual(reloaded.result["movies"], 1)
        self.assertGreaterEqual(len(reloaded.result["errors"]), 1)

    def test_worker_records_terminal_state_for_missing_path(self):
        # a nonexistent path inside a real temp dir: the scan finds nothing,
        # so posix completes with zero outcomes (windows may raise while
        # scanning instead) - the worker must reach a terminal state either
        # way. Real paths only: a drive-letter literal ('Z:/out') is absolute
        # on windows but RELATIVE on posix, where the job engine created a
        # literal 'Z:' directory in the caller's working tree (issue #38)
        root = Path(self.tmp.name)
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "path": str(root / "nowhere"),
                    "output_dir": str(root / "out"),
                    "quality": True,
                },
            )
        )
        worker = jobs.JobWorker(self.connection, self.queue)
        worker._execute(job)
        reloaded = self.queue.get(job.id)
        self.assertIn(reloaded.state, (jobs.JobState.FAILED, jobs.JobState.DONE))
        if reloaded.state == jobs.JobState.FAILED:
            self.assertIn("FileNotFoundError", reloaded.error)

    def test_worker_captures_engine_crash_as_failure(self):
        # deterministic crash: an engine exception must be caught by the
        # worker and recorded as a terminal FAILED state (never propagated,
        # never left RUNNING). On posix the missing-path case above completes
        # cleanly, so this is the only test of the crash path on every platform
        root = Path(self.tmp.name)
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "path": str(root / "movies"),
                    "output_dir": str(root / "out"),
                    "quality": True,
                },
            )
        )
        worker = jobs.JobWorker(self.connection, self.queue)
        with mock.patch(
            "shrinkray.server.jobs.resolve_movies", side_effect=RuntimeError("engine exploded")
        ):
            worker._execute(job)  # must not raise
        reloaded = self.queue.get(job.id)
        self.assertEqual(reloaded.state, jobs.JobState.FAILED)
        self.assertIn("RuntimeError: engine exploded", reloaded.error)

    def test_worker_stop_terminates_the_running_job_s_child(self):
        """Issue #431 (review round 2, finding 1): the measurement children run
        with the stall watchdog opted out, so a shutdown must set the running
        job's cancel event - the only kill path they have. stop() sets it
        while the job is executing, and _execute clears it in its finally."""
        root = Path(self.tmp.name)
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "path": str(root / "movies"),
                    "output_dir": str(root / "out"),
                    "quality": True,
                },
            )
        )
        from shrinkray.tools.process import Cancelled

        worker = jobs.JobWorker(self.connection, self.queue)
        worker._thread = threading.Thread(target=worker._run, name="job-worker", daemon=True)
        take_gate = threading.Event()
        release_gate = threading.Event()
        started = threading.Event()
        observed: dict = {}

        real_take_next = self.queue.take_next

        def gated_take_next():
            # hold the worker BEFORE it takes the job: stop() lands here
            take_gate.set()
            release_gate.wait(timeout=5.0)
            return real_take_next()

        def slow_encode(job, connection, cancel, progress=None, queue=None):
            started.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    observed["cancel_reached_job"] = True
                    raise Cancelled("cancelled by stop")
                time.sleep(0.05)
            observed["cancel_reached_job"] = False
            return {"cancelled": False}

        with (
            mock.patch("shrinkray.server.jobs.execute_encode", side_effect=slow_encode),
            mock.patch.object(self.queue, "take_next", side_effect=gated_take_next),
        ):
            worker._thread.start()
            self.assertTrue(take_gate.wait(timeout=5.0), "the worker never reached the take")
            # stop() lands BEFORE the job is taken: the event must already
            # exist and be set, so the job the worker then takes observes it
            # (the pre-round-3 code created the event inside _execute, after
            # take_next - this stop would have set nothing and the job ran
            # to completion)
            worker.stop(join_timeout=10.0)
            release_gate.set()
            self.assertTrue(started.wait(timeout=5.0), "the job never started")
            worker._thread.join(timeout=15.0)
        self.assertTrue(
            observed.get("cancel_reached_job"), "stop() did not reach the taken job's cancel event"
        )
        self.assertIsNone(worker._running_cancel, "the finally must clear the running event")
        reloaded = self.queue.get(job.id)
        self.assertEqual(reloaded.state, jobs.JobState.CANCELLED)

    def test_worker_request_stop_is_non_blocking_and_reaches_the_child(self):
        """Issue #431 (review round 6, finding 2): the server's exit hook runs
        inside a signal handler, so it must arm the shutdown without joining -
        request_stop sets the flags and returns; the worker ends itself and the
        running job's child terminates on its cancel event."""
        root = Path(self.tmp.name)
        job = self.queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={
                    "path": str(root / "movies"),
                    "output_dir": str(root / "out"),
                    "quality": True,
                },
            )
        )
        from shrinkray.tools.process import Cancelled

        worker = jobs.JobWorker(self.connection, self.queue)
        worker._thread = threading.Thread(target=worker._run, name="job-worker", daemon=True)
        started = threading.Event()
        observed: dict = {}

        def slow_encode(job, connection, cancel, progress=None, queue=None):
            started.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    observed["cancel_reached_job"] = True
                    raise Cancelled("cancelled by request_stop")
                time.sleep(0.05)
            observed["cancel_reached_job"] = False
            return {"cancelled": False}

        with mock.patch("shrinkray.server.jobs.execute_encode", side_effect=slow_encode):
            worker._thread.start()
            self.assertTrue(started.wait(timeout=5.0), "the job never started")
            t0 = time.monotonic()
            worker.request_stop()
            self.assertLess(time.monotonic() - t0, 2.0, "request_stop must not join")
            worker._thread.join(timeout=15.0)
        self.assertTrue(
            observed.get("cancel_reached_job"), "the cancel event did not reach the child"
        )
        self.assertFalse(worker._thread.is_alive(), "the worker did not end itself")
        self.assertIsNone(worker._running_cancel, "the finally must clear the running event")
        reloaded = self.queue.get(job.id)
        self.assertEqual(reloaded.state, jobs.JobState.CANCELLED)

    def test_next_poll_interval_helper_exists(self):
        # guard the cross-module dependency used by watch mode
        self.assertTrue(callable(jobs.JobQueue.wait_for_work))


class TestApiPathValidation(unittest.TestCase):
    """Tests for the path allowlist of the resident app API."""

    def setUp(self):
        import tempfile

        from shrinkray.server import app as app_module

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        app_module.allow_roots([self.root])

    def tearDown(self):
        from shrinkray.server import app as app_module

        app_module._allowed_roots = []
        self.tmp.cleanup()

    def test_validate_path_accepts_inside_root(self):
        from shrinkray.server.app import validate_path

        path = validate_path(str(self.root / "movies"))
        self.assertEqual(path, self.root / "movies")

    def test_validate_path_accepts_root_itself(self):
        from shrinkray.server.app import validate_path

        self.assertEqual(validate_path(str(self.root)), self.root)

    def test_validate_path_rejects_outside_root(self):
        from fastapi import HTTPException

        from shrinkray.server.app import validate_path

        # a real directory outside the allowed root (the system temp dir that
        # holds it): a drive-letter literal is relative on posix and would
        # reject for the wrong reason - containment, not relativity
        with self.assertRaises(HTTPException) as ctx:
            validate_path(str(self.root.parent))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_validate_path_rejects_escape_via_dotdot(self):
        from fastapi import HTTPException

        from shrinkray.server.app import validate_path

        # .. inside the root resolves outside => must be rejected
        with self.assertRaises(HTTPException) as ctx:
            validate_path(str(self.root / ".." / "elsewhere"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_validate_path_rejects_symlink_escape(self):
        """A symlink INSIDE an allowed root pointing OUTSIDE it must be
        rejected: realpath resolves the link first, so the containment check
        sees the real target (the case a real attacker would use)."""
        from fastapi import HTTPException

        from shrinkray.server.app import validate_path

        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        target_dir = Path(self.root.parent) / "outside-target"
        target_dir.mkdir(exist_ok=True)
        secret = target_dir / "secret.txt"
        secret.write_bytes(b"x")
        link = self.root / "innocent.txt"
        try:
            link.symlink_to(secret)
        except OSError:  # e.g. no symlink privilege on windows CI
            self.skipTest("cannot create symlinks on this platform")
        with self.assertRaises(HTTPException) as ctx:
            validate_path(str(link))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_validate_path_rejects_empty(self):
        from fastapi import HTTPException

        from shrinkray.server.app import validate_path

        with self.assertRaises(HTTPException) as ctx:
            validate_path("   ")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_validate_path_rejects_relative(self):
        from fastapi import HTTPException

        from shrinkray.server.app import validate_path

        # relative paths resolve against the CWD, which is not an allowed root
        with self.assertRaises(HTTPException) as ctx:
            validate_path("movies")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_validate_path_accepts_root_itself_and_children(self):
        import tempfile

        from shrinkray.server.app import allow_roots, validate_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            allow_roots([root])
            # the root itself and direct children are allowed
            self.assertEqual(validate_path(str(root)), root)
            self.assertEqual(validate_path(str(root / "sub")), root / "sub")
            # '..' escaping the root is rejected
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as ctx:
                validate_path(str(root / ".." / "elsewhere"))
            self.assertEqual(ctx.exception.status_code, 403)

    def test_ui_asset_containment(self):
        import tempfile

        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        with tempfile.TemporaryDirectory() as tmp:
            app_module.state = AppState(Path(tmp) / "test.db")
            try:
                client = TestClient(fast_app)
                # traversal attempts are rejected with 404 (not served)
                response = client.get("/assets/../metadata.db")
                self.assertEqual(response.status_code, 404)
                response = client.get("/assets/..%2F..%2Fpyproject.toml")
                self.assertEqual(response.status_code, 404)
                # a real asset inside dist is served (if the UI is built)
                dist = Path(__file__).parent.parent / "ui" / "dist" / "assets"
                if dist.is_dir() and any(dist.iterdir()):
                    asset_name = next(dist.iterdir()).name
                    ok = client.get(f"/assets/{asset_name}")
                    self.assertEqual(ok.status_code, 200)
                    # hashed assets are immutable: the browser never needs to
                    # revalidate them (the entry page is the freshness point)
                    self.assertEqual(
                        ok.headers.get("cache-control"),
                        "public, max-age=31536000, immutable",
                    )
            finally:
                app_module.state.shutdown()
                app_module.state = None

    def test_index_no_cache(self):
        """The entry page revalidates on every load (no-cache): a rebuilt UI
        must reach an open tab with a plain reload — a resident app's UI
        updates underneath the tab, and a hard-refresh ritual is a bug."""
        from fastapi.testclient import TestClient

        import shrinkray.server.app as app_module
        from shrinkray.server.app import AppState
        from shrinkray.server.app import app as fast_app

        dist = Path(__file__).parent.parent / "ui" / "dist"
        if not (dist / "index.html").is_file():
            self.skipTest("UI not built")
        app_module.state = AppState(Path(tempfile.gettempdir()) / "shrinkray-test-cache.db")
        try:
            client = TestClient(fast_app)
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("cache-control"), "no-cache")
        finally:
            app_module.state.shutdown()
            app_module.state = None


class TestPayloadPaths(unittest.TestCase):
    """payload_paths resolves a job's movie targets (issue #100 regression:
    the payload form the UI submits is path=<first target>,
    paths=<remaining targets>; a resolver that prefers 'paths' and drops
    'path' silently skips the FIRST target of every multi-target batch —
    in resolve_movies that meant plan/encode never saw folder one)."""

    def test_paths_and_path_are_unioned(self):
        resolved = jobs.payload_paths({"path": "/m/a", "paths": ["/m/b", "/m/c"]})
        self.assertEqual(sorted(resolved), [Path("/m/a"), Path("/m/b"), Path("/m/c")])

    def test_path_only_payload(self):
        resolved = jobs.payload_paths({"path": "/m/a"})
        self.assertEqual(resolved, [Path("/m/a")])

    def test_paths_only_payload(self):
        resolved = jobs.payload_paths({"paths": ["/m/b", "/m/c"]})
        self.assertEqual(resolved, [Path("/m/b"), Path("/m/c")])

    def test_empty_paths_falls_back_to_path(self):
        resolved = jobs.payload_paths({"path": "/m/a", "paths": []})
        self.assertEqual(resolved, [Path("/m/a")])

    def test_duplicate_targets_are_deduplicated(self):
        # a UI retry or double-submit can produce the same folder in both
        # keys; resolving it twice would scan/encode it twice
        resolved = jobs.payload_paths({"path": "/m/a", "paths": ["/m/a"]})
        self.assertEqual(resolved, [Path("/m/a")])


class TestTranscriptBoundaries(unittest.TestCase):
    """Issue #471: job start/finish lines with a measured duration, the
    plan's verdict line, and the [i/N] counter on a single-file payload."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")
        self.queue = jobs.JobQueue(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_boundaries_verdict_and_counter(self) -> None:
        from loguru import logger

        movie = Path(self.tmp.name) / "movie.mkv"
        movie.write_bytes(b"x" * 64)
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        worker = jobs.JobWorker(self.connection, self.queue)
        captured: list[str] = []
        sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            # the too-short gate fires on a mocked small duration: no probe
            # subprocess runs, the verdict line is the plan's own
            with mock.patch("shrinkray.engine.plan.probe_duration_seconds", return_value=10.0):
                worker._execute(job)
        finally:
            logger.remove(sink_id)
        text = "".join(captured)
        self.assertIn(f"Job #{job.id} (plan) started", text, text)
        # the plan's verdict line, counter-prefixed over the 1-movie payload
        self.assertIn("[1/1] Plan for 'movie.mkv': skip (too short)", text, text)
        self.assertIn(f"Job #{job.id} (plan) finished in", text, text)

    def test_the_end_line_counts_use_each_paths_own_words(self) -> None:
        """Issue #471 round 3, finding 2: the counts branch is pinned — a plan
        job's already-planned files read 'to encode', an encode job's read
        'encoded', and cancelled files get their own bucket."""
        counts = jobs.JobWorker._outcome_counts
        self.assertEqual(
            counts(
                {
                    "plans": [
                        {"state": "encode"},
                        {"state": "skip"},
                        {"state": "error"},
                        {"state": "cancelled"},
                    ]
                }
            ),
            "1 to encode, 1 skipped, 1 failed, 1 cancelled",
        )
        self.assertEqual(
            counts(
                {
                    "outcomes": [
                        {"state": "encoded"},
                        {"state": "already_encoded"},
                        {"state": "skipped"},
                        {"state": "cancelled"},
                        {"state": "cancelled"},
                    ]
                }
            ),
            "2 encoded, 1 skipped, 0 failed, 2 cancelled",
        )
        self.assertEqual(
            counts({"movies": 3, "errors": ["a", "b"]}),
            "3 scanned, 2 errors",
        )


class TestEncodeWindowGate(unittest.TestCase):
    """Issue #434: the worker holds plan/encode jobs while outside the
    window; scan jobs proceed; the queue's kind filter does the holding."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.tmp.name) / "test.db")
        self.queue = jobs.JobQueue(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_the_kind_filter_holds_plan_and_encode(self) -> None:
        plan_job = self.queue.enqueue(
            jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": ["D:/Movies"]})
        )
        self.queue.enqueue(jobs.Job(kind=jobs.JobKind.SCAN, payload={"path": "D:/Movies"}))
        # gated: only scan jobs are taken
        taken = self.queue.take_next(only_kinds={jobs.JobKind.SCAN})
        self.assertIsNotNone(taken)
        self.assertEqual(taken.kind, jobs.JobKind.SCAN)
        # the plan job is still queued
        self.assertEqual(self.queue.take_next(only_kinds={jobs.JobKind.SCAN}), None)
        # ungated: the plan job comes out
        taken = self.queue.take_next()
        self.assertEqual(taken.id, plan_job.id)

    def test_the_worker_holds_encode_jobs_outside_the_window(self) -> None:
        from unittest.mock import patch

        movie = Path(self.tmp.name) / "movie.mkv"
        movie.write_bytes(b"x" * 64)
        job = self.queue.enqueue(
            jobs.Job(kind=jobs.JobKind.ENCODE, payload={"paths": [str(movie)], "quality": True})
        )
        worker = jobs.JobWorker(self.connection, self.queue)

        def stop_after_first_wait(timeout: float) -> bool:
            worker.request_stop()
            return True

        # outside the window: the worker takes only scan jobs - the encode
        # job stays queued (issue #434)
        with (
            patch("shrinkray.server.jobs.encode_window_open", return_value=False),
            patch.object(self.queue, "wait_for_work", side_effect=stop_after_first_wait),
        ):
            worker._run()
        reloaded = self.queue.get(job.id)
        self.assertEqual(
            reloaded.state, jobs.JobState.QUEUED, "the encode job was taken outside the window"
        )

        # inside the window: the job is taken (its execution is patched out —
        # the assertion is the TAKE, not the toolchain's outcome). A fresh
        # worker: the first phase's request_stop latched its stop event
        worker = jobs.JobWorker(self.connection, self.queue)
        with (
            patch("shrinkray.server.jobs.encode_window_open", return_value=True),
            patch.object(self.queue, "wait_for_work", side_effect=stop_after_first_wait),
            patch.object(jobs.JobWorker, "_execute", lambda self, job, cancel=None: None),
        ):
            worker._run()
        reloaded = self.queue.get(job.id)
        self.assertEqual(
            reloaded.state,
            jobs.JobState.RUNNING,
            "the encode job was never taken inside the window",
        )


if __name__ == "__main__":
    unittest.main()
