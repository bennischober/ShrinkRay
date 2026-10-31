"""The timeline crop detector (issue #181, the #131 spike's follow-up).

One keyframe-only cropdetect pass over the whole runtime replaces the two
independent measurements the code had before: the plan's ten 2-second windows
(detect_crop_rect) and the scan's separate 10-frame flag probe
(has_black_bars). The pass's readings are filtered to SUSTAINED runs (a
run-length in SECONDS, never readings — the keyframe interval varies by
encoder — with a floor of 3 readings), the safe crop is the union of the
sustained picture areas (only bars every sustained segment shares), and the
summary is cached per file version in the `crops` table. Classification by
runtime share is report-only; the crop decision never uses it.
"""

import contextlib
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db
from shrinkray.engine.crop import (
    CropReading,
    CropSummary,
    crop_summary_from_timeline,
    detect_crop_summary_cached,
    sustained_runs,
)
from shrinkray.tools.process import _run


def _reading(timestamp: float, w: int, h: int, x: int = 0, y: int = 0) -> CropReading:
    return CropReading(timestamp=timestamp, width=w, height=h, x=x, y=y)


class TestSustainedRuns(unittest.TestCase):
    """The run-length filter: seconds of runtime, with a readings floor."""

    def test_a_sustained_segment_survives(self) -> None:
        # a 40 s segment read every ~1 s: 41 readings, span 40 s
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        sustained = sustained_runs(readings)
        self.assertEqual(len(sustained), 41)
        self.assertEqual(sustained[0].rect(), (1920, 800, 0, 140))

    def test_a_short_run_is_noise_and_is_dropped(self) -> None:
        # a 4 s dark-scene flash: 5 readings, span 4 s < MIN_RUN_SECONDS
        readings = [_reading(100.0 + float(i), 1920, 800, 0, 140) for i in range(5)]
        sustained = sustained_runs(readings)
        self.assertEqual(sustained, [])

    def test_the_readings_floor_rejects_a_sparse_satisfier(self) -> None:
        # two readings 60 s apart could satisfy the seconds rule by luck on a
        # file with 30 s keyframes; the 3-reading floor must reject it
        readings = [_reading(0.0, 1920, 800, 0, 140), _reading(60.0, 1920, 800, 0, 140)]
        self.assertEqual(sustained_runs(readings), [])
        # and one reading alone can never be sustained
        self.assertEqual(sustained_runs([_reading(0.0, 1920, 800, 0, 140)]), [])

    def test_a_sparse_stream_with_enough_readings_is_kept(self) -> None:
        # 4 readings across 45 s (30 s+ intervals): seconds AND floor pass —
        # the filter must be invariant to the keyframe interval
        readings = [_reading(0.0, 1920, 800, 0, 140)]
        for i in range(1, 4):
            readings.append(_reading(float(i) * 15.0, 1920, 800, 0, 140))
        sustained = sustained_runs(readings)
        self.assertEqual(len(sustained), 4)

    def test_two_runs_are_filtered_independently(self) -> None:
        # 40 s of bars, 5 s of dark noise, 40 s of bars again: both sustained
        # segments survive, the middle does not
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        readings += [_reading(40.0 + float(i), 1920, 1080, 0, 0) for i in range(6)]
        readings += [_reading(46.0 + float(i), 1920, 800, 0, 140) for i in range(41)]
        sustained = sustained_runs(readings)
        self.assertEqual(len(sustained), 41 + 41)


class TestCropSummaryFromTimeline(unittest.TestCase):
    """The reduction: safe union crop + the report-only ratio table."""

    def test_the_safe_crop_is_the_union_of_sustained_pictures(self) -> None:
        # two sustained letterboxed segments, 140 px and 200 px of bars: the
        # crop keeps only the bars BOTH share => 140 px top and bottom
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        readings += [_reading(40.0 + float(i), 1920, 680, 0, 200) for i in range(41)]
        summary = crop_summary_from_timeline(readings, (1920, 1080))
        self.assertEqual(summary.bars, (140, 140, 0, 0))

    def test_a_bar_free_sustained_segment_vetoes_the_crop(self) -> None:
        # the spike's safe-union rule: one sustained full-frame segment keeps
        # the whole picture, so bars = None
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        readings += [_reading(40.0 + float(i), 1920, 1080, 0, 0) for i in range(41)]
        summary = crop_summary_from_timeline(readings, (1920, 1080))
        self.assertIsNone(summary.bars)
        self.assertEqual(summary.flag, 0)

    def test_the_ratio_table_records_shares_for_the_report(self) -> None:
        # 40 s at 2.39:1 then 40 s bar-free => 50/50 shares; report-only data
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        readings += [_reading(40.0 + float(i), 1920, 1080, 0, 0) for i in range(41)]
        summary = crop_summary_from_timeline(readings, (1920, 1080))
        shares = sorted(round(v, 2) for v in summary.ratios.values())
        self.assertEqual(shares, [0.5, 0.5])
        rects = list(summary.ratios.keys())
        self.assertIn((1920, 800, 0, 140), rects)

    def test_no_sustained_segments_yield_no_crop(self) -> None:
        # everything noise (isolated dark frames): no crop, no ratios
        readings = [_reading(float(i) * 45.0, 1920, 1070, 0, 5) for i in range(2)]
        summary = crop_summary_from_timeline(readings, (1920, 1080))
        self.assertIsNone(summary.bars)
        self.assertEqual(summary.ratios, {})
        self.assertEqual(summary.readings, 2)
        self.assertEqual(summary.sustained_readings, 0)

    def test_the_flag_projects_a_bars_summary(self) -> None:
        summary = crop_summary_from_timeline(
            [_reading(float(i), 1920, 800, 0, 140) for i in range(41)], (1920, 1080)
        )
        self.assertEqual(summary.flag, 1)


class TestCropCache(unittest.TestCase):
    """The crops table: keyed by size + mtime, one row per file version."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db_file = self.root / "test.db"
        self.connection = db.connect(self.db_file)
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"x" * 8)
        stat = self.movie.stat()
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=self.movie, mtime=stat.st_mtime, size=stat.st_size, name="movie", category="C"
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _timeline(self, readings: list[CropReading], frame: tuple[int, int]) -> None:
        def fake_run(movie: Path, limit: int = 24, cancel=None):
            return readings, frame

        return fake_run

    def test_a_computed_summary_is_cached_and_reused(self) -> None:
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        fake = self._timeline(readings, (1920, 1080))
        with patch("shrinkray.engine.crop.run_crop_timeline", side_effect=fake):
            first = detect_crop_summary_cached(self.movie, self.db_file)
            self.assertIsNotNone(first)
            self.assertEqual(first.bars, (140, 140, 0, 0))
            # second call: served from the db, the pass must not run again
            with patch(
                "shrinkray.engine.crop.run_crop_timeline",
                side_effect=AssertionError("the pass ran although a cache row exists"),
            ):
                second = detect_crop_summary_cached(self.movie, self.db_file)
        self.assertEqual(second.bars, (140, 140, 0, 0))
        # one row per file version, keyed by size + mtime
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM crops").fetchone()[0], 1)

    def _opened_connections(self) -> tuple[list[sqlite3.Connection], object]:
        """Patches db.connect to record every connection the cache opens."""
        opened: list[sqlite3.Connection] = []
        real_connect = db.connect

        def recording_connect(db_path: Path) -> sqlite3.Connection:
            connection = real_connect(db_path)
            opened.append(connection)
            return connection

        return opened, patch("shrinkray.db.connect", side_effect=recording_connect)

    def assert_closed(self, connections: list[sqlite3.Connection]) -> None:
        self.assertTrue(connections, "the cache opened no connection to check")
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError, msg="the connection was left open"):
                connection.execute("SELECT 1")

    def test_a_cache_hit_closes_its_connection(self) -> None:
        """Issue #480: the hit returned before the finally that closed the
        connection, leaving it to the garbage collector (a ResourceWarning on
        Python >= 3.13, an open metadata.db handle in the resident server)."""
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        with patch(
            "shrinkray.engine.crop.run_crop_timeline",
            side_effect=self._timeline(readings, (1920, 1080)),
        ):
            detect_crop_summary_cached(self.movie, self.db_file)
        opened, recording = self._opened_connections()
        with (
            recording,
            patch(
                "shrinkray.engine.crop.run_crop_timeline",
                side_effect=AssertionError("the pass ran although a cache row exists"),
            ),
        ):
            summary = detect_crop_summary_cached(self.movie, self.db_file)
        self.assertEqual(summary.bars, (140, 140, 0, 0))
        self.assert_closed(opened)

    def test_a_failing_cache_read_closes_its_connection(self) -> None:
        """An error the cache does not absorb still closes the connection."""

        class _CacheReadFailed(Exception):
            pass

        opened, recording = self._opened_connections()
        with (
            recording,
            patch(
                "shrinkray.engine.crop.get_crop_summary",
                side_effect=_CacheReadFailed("read failed"),
            ),
            self.assertRaises(_CacheReadFailed),
        ):
            detect_crop_summary_cached(self.movie, self.db_file)
        self.assert_closed(opened)

    def test_a_corrupt_database_computes_uncached(self) -> None:
        """Issue #486: a damaged metadata.db raises sqlite3.DatabaseError, which
        the fallback did not catch, so the cache that only saves time stopped
        the encode."""
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not a database, just garbage bytes" * 128)
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        with (
            patch(
                "shrinkray.engine.crop.run_crop_timeline",
                side_effect=self._timeline(readings, (1920, 1080)),
            ),
            patch("shrinkray.engine.crop.log") as log,
        ):
            summary = detect_crop_summary_cached(self.movie, corrupt)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.bars, (140, 140, 0, 0))
        logged = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn("crop cache is unavailable", logged)

    def test_a_failing_cache_write_keeps_the_measured_summary(self) -> None:
        """Issue #486: a database that opens but cannot store the row (locked,
        full disk) must not discard a crop that was already measured."""
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        opened, recording = self._opened_connections()
        with (
            recording,
            patch(
                "shrinkray.engine.crop.run_crop_timeline",
                side_effect=self._timeline(readings, (1920, 1080)),
            ),
            patch(
                "shrinkray.db.save_crop_summary",
                side_effect=sqlite3.OperationalError("database or disk is full"),
            ),
            patch("shrinkray.engine.crop.log") as log,
        ):
            summary = detect_crop_summary_cached(self.movie, self.db_file)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.bars, (140, 140, 0, 0))
        logged = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn("could not be cached", logged)
        self.assert_closed(opened)

    def test_a_changed_file_is_remeasured(self) -> None:
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        fake = self._timeline(readings, (1920, 1080))
        with patch("shrinkray.engine.crop.run_crop_timeline", side_effect=fake):
            detect_crop_summary_cached(self.movie, self.db_file)
        # the file changed: identity (size + mtime) breaks, the cache misses
        self.movie.write_bytes(b"y" * 99)
        with patch("shrinkray.engine.crop.run_crop_timeline", side_effect=fake) as mock_run:
            summary = detect_crop_summary_cached(self.movie, self.db_file)
        self.assertEqual(mock_run.call_count, 1, "a changed file must be re-measured")
        self.assertIsNotNone(summary)

    def test_a_failed_pass_caches_nothing(self) -> None:
        with patch(
            "shrinkray.engine.crop.run_crop_timeline",
            return_value=([], None),
        ):
            summary = detect_crop_summary_cached(self.movie, self.db_file)
        self.assertIsNone(summary)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM crops").fetchone()[0], 0)

    def _interrupted_pass(self, run_result: object) -> str:
        """Drives the real run_crop_timeline with a pass that did not
        complete, asserts nothing is measured or cached, and returns what
        was logged."""
        with (
            patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
            patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=300.0),
            patch("shrinkray.engine.crop._run", **run_result),
            patch("shrinkray.engine.crop.log") as log,
        ):
            summary = detect_crop_summary_cached(self.movie, self.db_file)
        self.assertIsNone(summary)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM crops").fetchone()[0], 0)
        return " ".join(str(call.args[0]) for call in log.call_args_list)

    def test_an_interrupted_pass_caches_nothing(self) -> None:
        """An ffmpeg that exits early (ctrl+c or a closed terminal reach it
        in the CLI's process group, issue #326) printed readings for part of
        the runtime only: caching them would crop every later run of the
        file to the first part's bars."""
        partial = "".join(
            f"[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:140 y2:939 w:1920 h:800 "
            f"x:0 y:140 pts:{i} t:{i * 5}.000000 crop=1920:800:0:140\n"
            for i in range(20)
        ).encode()
        self._interrupted_pass({"return_value": (255, partial)})

    def test_a_timed_out_pass_caches_nothing_and_says_why(self) -> None:
        # the runner's code for a child it terminated at the ceiling; the log
        # carries its reason, not a bare "-2" that reads like a crash code
        logged = self._interrupted_pass(
            {"return_value": (-2, "timed out after 1800s (terminated)")}
        )
        self.assertIn("timed out after 1800s", logged)

    def test_a_hangup_from_outside_is_a_logged_failure_not_a_cancel(self) -> None:
        # Popen reports a child that died of SIGHUP as -1, the runner's
        # cancel code; with no cancel set it is a failure and must be said
        logged = self._interrupted_pass({"return_value": (-1, "")})
        self.assertIn("ffmpeg exited -1", logged)

    def test_an_interrupt_from_outside_is_not_read_as_the_runners_ceiling(self) -> None:
        # a child that died of SIGINT also returns -2; its stderr is ffmpeg's
        # readings, which must not be logged as the reason
        readings = "[Parsed_cropdetect_0 @ 0x1] w:1920 h:800 x:0 y:140 crop=1920:800:0:140\n"
        logged = self._interrupted_pass({"return_value": (-2, readings * 50)})
        self.assertIn("ffmpeg exited -2", logged)
        self.assertNotIn("Parsed_cropdetect", logged)

    def test_an_unstartable_ffmpeg_fails_the_pass_without_raising(self) -> None:
        logged = self._interrupted_pass({"side_effect": FileNotFoundError(2, "No such file")})
        self.assertIn("could not start", logged)

    def test_a_cancel_ends_the_timeline_pass_and_caches_nothing(self) -> None:
        """Issue #384: a cancel during the pass terminates the child within
        a poll tick and caches nothing. The real runner drives a real silent
        child in place of ffmpeg, which would otherwise sleep 10 s; the bound
        is counted from the moment the event is set.
        """
        child = ["cmd", "/c", "ping -n 10 127.0.0.1 >nul"] if os.name == "nt" else ["sleep", "10"]
        cancel = threading.Event()
        set_at: list[float] = []

        def run_child(
            command: list[str],
            cancel: threading.Event | None = None,
            timeout: float | None = None,
        ) -> tuple[int, str]:
            self.assertIsNotNone(cancel, "the pass must hand the runner its cancel event")
            return _run(child, cancel=cancel, timeout=timeout)

        def fire() -> None:
            set_at.append(time.monotonic())
            cancel.set()

        with (
            patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
            patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.crop._run", side_effect=run_child),
        ):
            threading.Timer(0.3, fire).start()
            summary = detect_crop_summary_cached(self.movie, self.db_file, cancel=cancel)
            elapsed = time.monotonic() - set_at[0]
        self.assertIsNone(summary)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM crops").fetchone()[0], 0)
        self.assertLess(elapsed, 1.0, f"the cancel took {elapsed:.1f}s to end the pass")

    def test_a_stateless_run_computes_without_a_cache(self) -> None:
        readings = [_reading(float(i), 1920, 800, 0, 140) for i in range(41)]
        fake = self._timeline(readings, (1920, 1080))
        with patch("shrinkray.engine.crop.run_crop_timeline", side_effect=fake) as mock_run:
            summary = detect_crop_summary_cached(self.movie, None)
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(summary.bars, (140, 140, 0, 0))
        # no db involvement: nothing was stored
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM crops").fetchone()[0], 0)


class TestScanFlagProjection(unittest.TestCase):
    """scan's flag is the cached summary's projection when one exists.

    scan NEVER runs the timeline pass itself (a library scan must stay fast);
    the flag stays the cheap 10-frame probe until a plan/encode has cached a
    summary for this exact file version.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db_file = self.root / "test.db"
        self.connection = db.connect(self.db_file)
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"x" * 8)
        stat = self.movie.stat()
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=self.movie, mtime=stat.st_mtime, size=stat.st_size, name="movie", category="C"
            ),
        )
        self.summary = CropSummary(
            bars=(140, 140, 0, 0),
            frame_width=1920,
            frame_height=1080,
            readings=120,
            sustained_readings=118,
            ratios={},
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_a_cached_summary_drives_the_flag(self) -> None:
        from shrinkray.engine.scan import process_movie

        db.save_crop_summary(self.connection, self.movie, self.summary)
        with (
            patch("shrinkray.engine.scan.get_data", return_value=_fake_probe_data()),
            patch(
                "shrinkray.engine.scan.has_black_bars",
                side_effect=AssertionError("the cheap probe ran although a summary exists"),
            ) as probe,
        ):
            data, error = process_movie(self.movie, self.root, _config(), db_file=self.db_file)
        self.assertIsNone(error)
        self.assertEqual(data["crop"], 1)
        probe.assert_not_called()

    def test_without_a_summary_the_cheap_probe_runs(self) -> None:
        from shrinkray.engine.scan import process_movie

        with (
            patch("shrinkray.engine.scan.get_data", return_value=_fake_probe_data()),
            patch("shrinkray.engine.scan.has_black_bars", return_value=0) as probe,
        ):
            data, error = process_movie(self.movie, self.root, _config(), db_file=self.db_file)
        self.assertIsNone(error)
        self.assertEqual(data["crop"], 0)
        probe.assert_called_once()

    def test_a_broken_db_falls_back_to_the_cheap_probe(self) -> None:
        from shrinkray.engine.scan import process_movie

        with (
            patch("shrinkray.engine.scan.get_data", return_value=_fake_probe_data()),
            patch("shrinkray.engine.scan.has_black_bars", return_value=0) as probe,
        ):
            data, error = process_movie(
                self.movie, self.root, _config(), db_file=self.root / "missing.db"
            )
        self.assertIsNone(error)
        self.assertEqual(data["crop"], 0)
        probe.assert_called_once()


class TestCropCancelReachesBothPasses(unittest.TestCase):
    """Issue #384: the sampled fallback honours the cancel, and both engine
    entry points hand their cancel event to both crop passes."""

    def test_a_cancel_ends_the_sampled_fallback_within_a_tick(self) -> None:
        from shrinkray.engine.crop import detect_crop_rect

        child = ["cmd", "/c", "ping -n 10 127.0.0.1 >nul"] if os.name == "nt" else ["sleep", "10"]
        cancel = threading.Event()
        windows: list[list[str]] = []
        set_at: list[float] = []

        def run_child(
            command: list[str],
            cancel: threading.Event | None = None,
            timeout: float | None = None,
        ) -> tuple[int, str]:
            windows.append(command)
            return _run(child, cancel=cancel, timeout=timeout)

        def fire() -> None:
            set_at.append(time.monotonic())
            cancel.set()

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            with (
                patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
                patch("shrinkray.engine.crop._run", side_effect=run_child),
            ):
                threading.Timer(0.3, fire).start()
                rect = detect_crop_rect(movie, Path(tmp), count=3, cancel=cancel)
                elapsed = time.monotonic() - set_at[0]
        self.assertIsNone(rect)
        self.assertEqual(len(windows), 1, "the windows after the cancel must not start")
        self.assertLess(elapsed, 1.0, f"the cancel took {elapsed:.1f}s to end the fallback")

    def test_a_window_killed_by_a_signal_leaves_the_file_uncropped(self) -> None:
        """A window whose ffmpeg died of a signal (here SIGKILL, -9, with no
        cancel set) measured nothing; cropping from the other windows would
        lose its veto, so the fallback measures no crop and says why."""
        from shrinkray.engine.crop import detect_crop_rect

        bars = (0, "[Parsed_cropdetect_0 @ 0x1] crop=1920:800:0:140\n")
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            with (
                patch("shrinkray.engine.crop.source_frame", return_value=(1920, 1080)),
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=3600.0),
                patch("shrinkray.engine.crop._run", side_effect=[bars, (-9, ""), bars]),
                patch("shrinkray.engine.crop.log") as log,
            ):
                rect = detect_crop_rect(movie, Path(tmp), count=3)
        self.assertIsNone(rect)
        self.assertIn("ffmpeg exited -9", " ".join(str(c.args[0]) for c in log.call_args_list))

    def test_a_file_without_a_video_stream_says_so(self) -> None:
        import subprocess

        from shrinkray.engine.crop import source_frame

        audio_only = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'{"streams": [{"codec_type": "audio"}]}', stderr=b""
        )
        with (
            patch("shrinkray.engine.crop.subprocess.run", return_value=audio_only),
            patch("shrinkray.engine.crop.log") as log,
        ):
            self.assertIsNone(source_frame(Path("movie.mkv")))
        self.assertIn("no video stream", log.call_args.args[0])

    def _passes_seen(self, module: str, stack: contextlib.ExitStack) -> dict[str, object]:
        """Patches both crop passes in `module`; the fallback raises
        _ReachedFallback so the caller stops there. Returns the cancel each
        pass received."""
        seen: dict[str, object] = {}

        def timeline(movie: Path, db_file: Path | None, cancel: object = None) -> None:
            seen["timeline"] = cancel

        def fallback(movie: Path, workdir: Path, count: int = 10, cancel: object = None) -> None:
            seen["fallback"] = cancel
            raise _ReachedFallback

        stack.enter_context(
            patch(f"shrinkray.engine.{module}.detect_crop_summary_cached", side_effect=timeline)
        )
        stack.enter_context(
            patch(f"shrinkray.engine.{module}.detect_crop_rect", side_effect=fallback)
        )
        stack.enter_context(patch(f"shrinkray.engine.{module}.find_handbrake", return_value="hb"))
        return seen

    def test_plan_quality_hands_its_cancel_to_both_passes(self) -> None:
        import shrinkray.engine.plan as plan_module

        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x" * 8)
            seen = self._passes_seen("plan", stack)
            stack.enter_context(
                patch("shrinkray.engine.plan.get_data", return_value={"hdr": False})
            )
            with self.assertRaises(_ReachedFallback):
                plan_module.plan_quality(movie, cancel=cancel)
        self.assertIs(seen.get("timeline"), cancel, "the timeline pass lost the cancel")
        self.assertIs(seen.get("fallback"), cancel, "the fallback lost the cancel")

    def test_the_encode_probe_hands_its_cancel_to_both_passes(self) -> None:
        import shrinkray.engine.pipeline as pipeline_module

        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x" * 8)
            seen = self._passes_seen("pipeline", stack)
            with self.assertRaises(_ReachedFallback):
                pipeline_module._probe_movie(
                    movie,
                    target_vmaf=96.0,
                    crf_range=(14, 45),
                    sample_count=3,
                    sample_seconds=2.0,
                    encoder="svt_av1",
                    probe_encoder_preset="8",
                    cancel=cancel,
                )
        self.assertIs(seen.get("timeline"), cancel, "the timeline pass lost the cancel")
        self.assertIs(seen.get("fallback"), cancel, "the fallback lost the cancel")


class _ReachedFallback(Exception):
    """Stops an engine entry point once it has called the sampled fallback."""


class TestPlanUsesTheTimelineDetector(unittest.TestCase):
    """plan_quality reads the cached summary before the sampled fallback."""

    def test_plan_quality_uses_the_cached_summary(self) -> None:
        import shrinkray.engine.plan as plan_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x" * 8)
            db_file = root / "test.db"
            connection = db.connect(db_file)
            stat = movie.stat()
            db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie, mtime=stat.st_mtime, size=stat.st_size, name="movie", category="C"
                ),
            )
            summary = CropSummary(
                bars=(140, 140, 0, 0),
                frame_width=1920,
                frame_height=1080,
                readings=120,
                sustained_readings=118,
                ratios={},
            )
            db.save_crop_summary(connection, movie, summary)
            connection.close()

            def fake_find(movie, workdir, handbrake, **kwargs):
                assert kwargs["crop"] == (140, 140, 0, 0), kwargs["crop"]
                # a real skip result: the search measures nothing further
                return (20, 95.0, [], True)

            with (
                patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=summary),
                patch(
                    "shrinkray.engine.plan.detect_crop_rect",
                    side_effect=AssertionError(
                        "the sampled fallback ran although a summary exists"
                    ),
                ),
                patch("shrinkray.engine.plan.extract_samples", return_value=[]),
                patch("shrinkray.engine.plan.estimate_size_factor", return_value=0.5),
                patch(
                    "shrinkray.engine.plan.find_optimal_crf_and_skip",
                    side_effect=fake_find,
                ) as find,
                patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
                patch("shrinkray.engine.plan.get_data", return_value={"hdr": False}),
            ):
                result = plan_module.plan_quality(movie, db_file=db_file)
            find.assert_called_once()
            self.assertEqual(result.crop, (140, 140, 0, 0))


def _fake_probe_data() -> dict:
    """The get_data shape process_movie consumes (all read keys present)."""
    return {
        "duration": "00:00:01",
        "duration_raw": 1.0,
        "dimensions": {"width": 1920, "height": 1080},
        "codec": "h264",
        "hdr": False,
        "other_codecs": [],
    }


def _config():
    from shrinkray.config import load_config

    return load_config()


if __name__ == "__main__":
    unittest.main()
