"""Watch-mode tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from shrinkray.engine import plan as engine_plan
from shrinkray.engine import watch as watch_mod

# the module's own value, read before any test patches it: _isolated_lock
# compares against it to tell "a test set its own interval" from "unpatched"
_REAL_STABLE_CHECK_INTERVAL = watch_mod.STABLE_CHECK_INTERVAL


def _watch_scan_data(movie: Path, root: Path) -> dict:
    """The get_data shape movie_record_from_data persists (issue #574: the
    watcher's scan records the row, so a scan fake must return the full
    shape), with the category the real get_category computes from the root."""
    from shrinkray.engine.scan import get_category

    return {
        "name": movie.stem,
        "category": get_category(movie, root),
        "duration": 12.0,
        "duration_raw": 12.0,
        "fps": 25.0,
        "codec": "h264",
        "bitrate": 1000,
        "aspect_ratio": "16:9",
        "dimensions": {"width": 1920, "height": 1080},
        "crop": None,
        "hdr": False,
        "other_codecs": "",
        "audio_languages": "",
        "hash": None,
    }


def _isolated_lock(stack, tmp) -> None:
    """Points the watch loop's single-instance lock at this test's tmp dir.

    The real lock lives in the shared /tmp; a leftover lock whose pid was
    reused by another process made watch() refuse to run mid-suite (six
    empty-result failures in one CI run, issue #176's flake follow-up).
    Accepts a Path or a str (test sites pass either).

    Also pins the pass guard's free-space reading: a busy CI runner's disk
    can dip below the floor mid-suite, and the guard's skip-the-pass
    behaviour (correct in production) then eats the pass and the test's
    expectations come back empty (the #303 CI face of the #291 family).

    Also zeroes STABLE_CHECK_INTERVAL unless the test already patched it: a
    watch test's subject is not the stability wait, and three tests that
    forgot the per-test patch slept 120 s per run (issue #478). Since #479 the
    watcher's witness no longer sleeps the interval at all; the patch keeps a
    future pacing change from putting the sleep back into these tests. STABLE_MIN_AGE is left alone - tests that
    want a stable file backdate its mtime, tests that want "never stable"
    patch the floor themselves. A test that needs its own interval patches
    it before this call or enters its patch after it; either way its value
    wins.
    """
    stack.enter_context(
        mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "shrinkray_watch.lock")
    )
    stack.enter_context(mock.patch.object(watch_mod, "disk_free_bytes", return_value=1 << 40))
    if watch_mod.STABLE_CHECK_INTERVAL == _REAL_STABLE_CHECK_INTERVAL:
        stack.enter_context(mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0))


class TestFsWaiterStop(unittest.TestCase):
    """The drain thread must be out of select() before stop() closes the
    inotify descriptor (issue #642): the race let select run on a closed
    fd - EBADF killed the drain thread - and left the closed number free
    for reuse under a still-running read."""

    def test_stop_joins_the_drain_before_closing_the_fd(self) -> None:
        import select as select_mod

        with tempfile.TemporaryDirectory() as tmp:
            waiter = watch_mod._FsWaiter([Path(tmp)])
            waiter._start_linux()
            self.addCleanup(waiter.stop)

            real_select = select_mod.select
            in_select = threading.Event()
            release = threading.Event()

            def parked_select(rlist, wlist, xlist, timeout):
                # the deterministic park: the drain stays INSIDE select
                # until the test releases it, whichever stop() does
                in_select.set()
                release.wait(5.0)
                return real_select(rlist, wlist, xlist, 0.05)

            hook = mock.Mock()
            old_hook = threading.excepthook
            threading.excepthook = hook
            try:
                with mock.patch("select.select", parked_select):
                    self.assertTrue(in_select.wait(5.0), "the drain never parked in select")
                    stop_finished = threading.Event()

                    def do_stop():
                        waiter.stop()
                        stop_finished.set()

                    stopper = threading.Thread(target=do_stop)
                    stopper.start()
                    # a beat for stop() to reach the close (main) or the
                    # join (fixed) while the drain is parked
                    stopper.join(timeout=1.0)
                    release.set()
                    stopper.join(5.0)
                    waiter._thread.join(5.0)
                # the drain must have exited without an unhandled exception
                self.assertFalse(
                    hook.called,
                    f"an unhandled thread exception escaped the drain: {hook.call_args}",
                )
                self.assertTrue(stop_finished.is_set())
            finally:
                threading.excepthook = old_hook


class TestWatch(unittest.TestCase):
    def test_a_library_below_a_shrinkray_named_ancestor_is_visible(self):
        """Issue #286: the staging filter scoped to the walk root - a library
        that lives below an ancestor named .shrinkray-* (above the root) is
        a library, not working state. Fails on main's absolute-path check."""

        from shrinkray.engine.scan import find_movies
        from shrinkray.engine.watch import find_new_movies
        from shrinkray.replace import is_staging_path

        with tempfile.TemporaryDirectory() as tmp:
            # resolved: find_movies returns resolved paths, and a temp dir can
            # sit behind a symlink (/var -> /private/var on macOS)
            base = Path(tmp).resolve()
            library = base / ".shrinkray-archive" / "library"
            library.mkdir(parents=True)
            movie = library / "Movie (2001).mkv"
            movie.write_bytes(b"x")

            # the predicate with the root: visible
            self.assertFalse(is_staging_path(movie, library))
            # both walkers find it
            self.assertEqual(find_movies(library, [".mkv"]), [movie])
            self.assertEqual(find_new_movies(library, [".mkv"], set()), [movie])
            # the actual staging state BELOW the root is still filtered
            staging = library / ".shrinkray-inplace-123" / "Movie (2001).mkv"
            staging.parent.mkdir()
            staging.write_bytes(b"x")
            self.assertTrue(is_staging_path(staging, library))
            self.assertEqual(find_movies(library, [".mkv"]), [movie])

    def test_one_unreadable_file_does_not_end_the_watch(self):
        """Issue #260: a per-file failure (RuntimeError from the probe) is
        logged with the file's name and the watcher continues - the loop
        runs unattended for months, so one corrupt file must not end it.
        The failing file stays in known (no retry rule invented here;
        #231 owns the failure memory); the NEXT candidate is processed."""

        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            broken = inbox / "broken.mkv"
            broken.write_bytes(b"not a movie")
            good = inbox / "good.mkv"
            good.write_bytes(b"movie")
            old = time.time() - 7200
            for f in (broken, good):
                os.utime(f, (old, old))  # stable
            processed: list[str] = []

            def fake_quality(m, o, **kwargs):
                processed.append(m.name)
                if m.name == "broken.mkv":
                    raise RuntimeError("ffprobe failed for 'broken.mkv'")
                return engine_plan.QualityResult(
                    input=m, output=o, success=False, skipped_reason="no size gain"
                )

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch([inbox], out, once=True)
            # the candidates' order is the readdir's, not mine: the behavior
            # under test is BOTH processed (the broken one's failure did not
            # end the loop), not an order
            self.assertEqual(
                sorted(processed),
                ["broken.mkv", "good.mkv"],
                "the loop died on the broken file; the good one was never processed",
            )

    def test_unstable_movie_is_retried_next_pass(self):
        """A file that fails the stability check stays eligible: the old code
        marked it `known` BEFORE the stability check, so every normal copy
        arrival (seen while younger than the grace period) was logged 'later'
        and then never reconsidered - silent starvation."""

        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")  # fresh mtime => unstable
            seen: list = []

            def fake_quality(m, o, **kwargs):
                seen.append(m)
                return engine_plan.QualityResult(
                    input=m, output=o, success=False, skipped_reason="no size gain"
                )

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
                mock.patch("shrinkray.engine.watch.STABLE_MIN_AGE", 3600.0),  # never stable
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch([inbox], out, once=True)
                    self.assertEqual(seen, [])  # not stable => not encoded yet
                # the movie must NOT be in the watcher's known set: simulate
                # the next pass after the copy finished (mtime backdated)
                old = time.time() - 7200
                os.utime(movie, (old, old))
            # a fresh watcher instance now encodes it (the real loop keeps
            # `known` in memory across passes; the set is per-instance, so
            # the guarantee under test is: unstable files never ENTER it)
            self.assertEqual(seen, [])

    def test_unstable_movie_never_enters_known(self):
        """The fix's core invariant: after a pass where the file was unstable,
        the loop's `known` set does not contain it - so the next pass still
        sees the file (the old code consumed it on the first pass)."""

        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")  # fresh mtime => unstable

            captured: dict = {}
            real_find = watch_mod.find_new_movies

            def spy(inbox_dir, extensions, known):
                captured["known"] = known
                return real_find(inbox_dir, extensions, known)

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.STABLE_MIN_AGE", 3600.0),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
                mock.patch("shrinkray.engine.watch.find_new_movies", side_effect=spy),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch([inbox], out, once=True)
            self.assertIn("known", captured)
            # the key is "path:mtime:size", so a bare path is never a member;
            # assert on the path PREFIX to prove this movie specifically is absent
            self.assertFalse(
                [k for k in captured["known"] if k.startswith(str(movie.resolve()) + ":")],
                "an unstable movie entered the known set",
            )

    def test_pass_skipped_when_output_drive_full(self):
        """When the output drive is below the floor, the pass is skipped
        BEFORE any movie enters `known` (no probe-burning retry loop) and
        the movies stay eligible for a later pass."""

        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            (inbox / "movie.mkv").write_bytes(b"movie")
            probed: list = []

            def fake_quality(m, o, **kwargs):
                probed.append(m)
                return engine_plan.QualityResult(input=m, output=o, success=True)

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
                mock.patch("shrinkray.engine.watch.disk_free_bytes", return_value=1_000_000),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch([inbox], out, once=True)
            # nothing was probed, nothing consumed
            self.assertEqual(probed, [])

    def test_watch_refuses_overlapping_dirs(self):
        """inbox == output_dir (or nested) would make the watcher re-encode
        its own output (worst case: HandBrake writes its input's path) =>
        refused loudly at start."""

        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inbox = root / "in"
            inbox.mkdir()
            with mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "watch.lock"):
                with self.assertRaises(ValueError):
                    watch_mod.watch([inbox], inbox, once=True)
                with self.assertRaises(ValueError):
                    watch_mod.watch([inbox], inbox / "sub", once=True)
                with self.assertRaises(ValueError):
                    watch_mod.watch([inbox / "sub"], inbox, once=True)

    def test_watch_stop_event_ends_loop(self):
        """The app's stop event ends the loop promptly (between passes and
        during the sleep) instead of leaving a daemon thread until exit."""

        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            stop = threading.Event()
            stop.set()
            # must return immediately (checked at the top of the pass)
            with mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "watch.lock"):
                watch_mod.watch([inbox], out, once=False, stop_event=stop)

    def test_find_new_movies(self):

        from shrinkray.engine.watch import _identity, find_new_movies

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp)
            (inbox / "a.mkv").write_bytes(b"x")
            (inbox / "b.MKV").write_bytes(b"x")
            (inbox / "c.txt").write_bytes(b"x")
            (inbox / "sub").mkdir()
            (inbox / "sub" / "d.mkv").write_bytes(b"x")

            new = find_new_movies(inbox, [".mkv"], set())
            names = {m.name for m in new}
            self.assertEqual(names, {"a.mkv", "b.MKV", "d.mkv"})

            known = {_identity(m) for m in new}
            self.assertEqual(find_new_movies(inbox, [".mkv"], known), [])

            changed = inbox / "a.mkv"
            changed.write_bytes(b"x" * 16)
            self.assertEqual([m.name for m in find_new_movies(inbox, [".mkv"], known)], ["a.mkv"])

    def test_acquire_and_release_lock(self):

        from shrinkray.engine.watch import acquire_lock, release_lock

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "watch.lock"
            self.assertTrue(acquire_lock(lock))
            # second acquire fails (our own pid is alive)
            self.assertFalse(acquire_lock(lock))
            release_lock(lock)
            self.assertFalse(lock.exists())
            # stale lock (dead pid) => take over
            lock.write_text("999999999", encoding="utf-8")
            self.assertTrue(acquire_lock(lock))
            release_lock(lock)

    def test_check_stability_min_age(self):

        from shrinkray.engine.watch import check_stability

        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh.mkv"
            fresh.write_bytes(b"x")
            # just created => below the minimum age => not settled
            self.assertFalse(check_stability(fresh).settled)

    def test_decide_too_short(self):

        from shrinkray.engine.watch import decide

        with tempfile.TemporaryDirectory() as tmp:
            # a real, very short movie file (valid media, below the minimum)
            clip = Path(tmp) / "clip.mkv"
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                self.skipTest("ffmpeg not available")
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=duration=1:size=320x240:rate=10",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    str(clip),
                ],
                capture_output=True,
                check=True,
            )
            self.assertEqual(decide(clip, 95.0, "x265"), "too short")

        with tempfile.TemporaryDirectory() as tmp:
            # junk file (invalid media) => must not crash the decision
            junk = Path(tmp) / "junk.mkv"
            junk.write_bytes(b"not a movie")
            self.assertEqual(decide(junk, 95.0, "x265"), "encode")

    def test_watch_pass_encodes_with_quality(self):
        """The watch loop encodes via the quality engine (same skip rules,
        same CRF search as CLI/jobs) - not the old fixed-preset path."""

        from shrinkray.engine import watch as watch_mod
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            # past the stability grace period (the watch loop would defer it)
            old = time.time() - 3600
            os.utime(movie, (old, old))
            captured: dict = {}

            def fake_quality(movie, output_file, **kwargs):
                captured["kwargs"] = kwargs
                captured["output"] = output_file
                return QualityResult(
                    input=movie,
                    output=output_file,
                    success=True,
                    chosen_crf=22,
                    achieved_vmaf=95.2,
                    input_size=100,
                    output_size=40,
                )

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch([inbox], out, once=True)
            # quality engine got the movie with the thread cap and wrote
            # into the watch output dir
            self.assertEqual(captured["output"], out / "movie.mkv")
            self.assertIn("encoder_threads", captured["kwargs"])

    def test_a_second_root_movie_is_scanned_under_its_own_root(self):
        """Issue #404: a movie found under root B is recorded with a category
        relative to root B, not root A (the submit branch passed roots[0] to
        the scan, so a multi-root library filed second-root movies under
        whatever get_category(root_a, movie) produced)."""

        from shrinkray import db
        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "media-a"
            root_b = Path(tmp) / "media-b"
            root_a.mkdir()
            root_b.mkdir()
            # root B carries the movie in its own Title subfolder; root A
            # carries a DIFFERENT title subfolder (so roots[0]'s category
            # would be wrong, not merely equal)
            title_b = root_b / "TitleB"
            title_b.mkdir()
            movie_b = title_b / "film.mkv"
            movie_b.write_bytes(b"movie-b")
            (root_a / "TitleA").mkdir()
            old = time.time() - 3600
            os.utime(movie_b, (old, old))

            db_file = Path(tmp) / "metadata.db"
            connection = db.connect(db_file)
            try:
                submit_calls: list = []

                def fake_submit(movie):
                    submit_calls.append(movie)
                    return True

                scan_roots: list = []

                def fake_scan(movie, root, config, db_file=None):
                    # the scan's category computation, performed on the root
                    # the watcher handed over - PRODUCTION's own get_category
                    # (with its Uncategorized fallback), so restoring roots[0]
                    # fails cleanly on the assert below instead of a
                    # ValueError inside this fake, and the recorded row is a
                    # pin on production behavior, not this test's arithmetic
                    scan_roots.append((movie, root))
                    # the scan RECORDS now (issue #574): the fake returns the
                    # full data shape and _scan_into_db's write-back makes the
                    # row - the fake's own manual upsert was the stand-in for
                    # the recording this fix adds
                    return _watch_scan_data(movie, root), None

                stack = ExitStack()
                with (
                    mock.patch(
                        "shrinkray.engine.watch.encode_with_quality"
                    ),  # not reached: submit path owns the work
                    mock.patch("shrinkray.engine.watch.process_movie", side_effect=fake_scan),
                    # decide()'s duration read is a real subprocess seam
                    # (neighbours mock it too); this test's subject is the
                    # scan's ROOT argument, not the decision
                    mock.patch(
                        "shrinkray.engine.watch.probe_duration_seconds",
                        return_value=600.0,
                    ),
                    # the decision needs a toolchain to reach 'encode'
                    mock.patch("shrinkray.engine.watch.find_handbrake", return_value="hb"),
                    mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
                ):
                    _isolated_lock(stack, tmp)
                    with stack:
                        watch_mod.watch(
                            [root_a, root_b], None, once=True, submit=fake_submit, db_file=db_file
                        )
                # the scan received the movie's OWN root, not roots[0]
                self.assertEqual(scan_roots, [(movie_b, root_b)])
                self.assertEqual(len(submit_calls), 1)
                row = connection.execute(
                    "SELECT f.category FROM files f WHERE f.path = ?",
                    (str(movie_b),),
                ).fetchone()
                self.assertIsNotNone(row, "the movie was scanned into the db")
                self.assertEqual(row["category"], "TitleB")
            finally:
                connection.close()

    def test_watch_pass_reports_quality_skip(self):
        """A quality skip (no size gain / unreachable) is surfaced as a
        skip log, not an encode error."""

        from shrinkray.engine import watch as watch_mod
        from shrinkray.engine.plan import QualityResult

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            # past the stability grace period (the watch loop would defer it)
            old = time.time() - 3600
            os.utime(movie, (old, old))

            def fake_quality(movie, output_file, **kwargs):
                return QualityResult(
                    input=movie,
                    output=output_file,
                    success=False,
                    skipped_reason="no size gain",
                )

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch([inbox], out, once=True)

    def test_watch_pass_retries_when_busy(self):
        """A busy encode slot (CLI/job running) retries the movie on the
        next pass instead of failing it."""

        from shrinkray.engine import watch as watch_mod
        from shrinkray.engine.lock import EncodeBusyError

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            # past the stability grace period (the watch loop would defer it)
            old = time.time() - 3600
            os.utime(movie, (old, old))

            def busy(movie, output_file, **kwargs):
                raise EncodeBusyError(f"another encode is running in '{out}'")

            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=busy),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    # must not raise: the movie is eligible again next pass
                    watch_mod.watch([inbox], out, once=True)

    def test_db_file_reaches_encode_with_quality(self):
        """The db path given to watch() reaches encode_with_quality as
        db_file= (stall-telemetry wiring; the watchdog fix scoped it to
        the quality path only, watch mode was left without telemetry)."""
        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            seen: list = []

            def fake_quality(m, o, **kwargs):
                seen.append(kwargs.get("db_file"))
                return engine_plan.QualityResult(input=m, output=o, success=True)

            # backdate the mtime: a fresh file is below STABLE_MIN_AGE and
            # would never reach the encode call
            old = time.time() - 7200
            os.utime(movie, (old, old))

            stack = ExitStack()
            with mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch(
                        [inbox],
                        out,
                        once=True,
                        db_file=Path(tmp) / "test.db",
                        probe_encoder_preset="2",
                        encoder_preset="8",
                    )
            self.assertEqual(seen, [Path(tmp) / "test.db"])

    def test_presets_reach_encode_with_quality(self):
        """Both presets reach the engine from watch() (issue #22: the probe
        preset is a measurement setting, the final preset an output one)."""
        from shrinkray.engine import watch as watch_mod

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            movie = inbox / "movie.mkv"
            movie.write_bytes(b"movie")
            seen: list = []

            def fake_quality(m, o, **kwargs):
                seen.append((kwargs.get("probe_encoder_preset"), kwargs.get("encoder_preset")))
                return engine_plan.QualityResult(input=m, output=o, success=True)

            old = time.time() - 7200
            os.utime(movie, (old, old))

            stack = ExitStack()
            with mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch(
                        [inbox],
                        out,
                        once=True,
                        probe_encoder_preset="2",
                        encoder_preset="8",
                    )
            self.assertEqual(seen, [("2", "8")])


class TestAdaptivePoll(unittest.TestCase):
    def test_idle_growth_caps_at_max(self):
        from shrinkray.engine.watch import next_poll_interval

        # idle polls double the interval up to 4x the base: 60 -> 120 -> 240 -> 240
        interval = 60.0
        interval = next_poll_interval(interval, 60.0, False)
        self.assertEqual(interval, 120.0)
        interval = next_poll_interval(interval, 60.0, False)
        self.assertEqual(interval, 240.0)
        interval = next_poll_interval(interval, 60.0, False)
        self.assertEqual(interval, 240.0)
        interval = next_poll_interval(interval, 60.0, False)
        self.assertEqual(interval, 240.0)

    def test_activity_resets_to_base(self):
        from shrinkray.engine.watch import next_poll_interval

        # any new candidate (even skipped/unstable ones) resets to the base
        self.assertEqual(next_poll_interval(240.0, 60.0, True), 60.0)
        self.assertEqual(next_poll_interval(60.0, 60.0, True), 60.0)

    def test_non_default_base(self):
        from shrinkray.engine.watch import next_poll_interval

        # growth is capped relative to the given base, not the default
        interval = 5.0
        interval = next_poll_interval(interval, 5.0, False)
        self.assertEqual(interval, 10.0)
        interval = next_poll_interval(interval, 5.0, False)
        self.assertEqual(interval, 20.0)
        interval = next_poll_interval(interval, 5.0, False)
        self.assertEqual(interval, 20.0)
        # reset also works with a non-default base
        self.assertEqual(next_poll_interval(20.0, 5.0, True), 5.0)


class TestScanIntoDbRecords(unittest.TestCase):
    """Issue #574: the watcher's scan-into-db half RECORDS the row - a file
    the watchdog saw but whose encode job never ran is still catalogued,
    under the watch root's category (the #231 contract's scan half)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "library"
        self.root.mkdir()
        (self.root / "Title").mkdir()
        self.movie = self.root / "Title" / "film.mkv"
        self.movie.write_bytes(b"x" * 64)
        self.db_file = Path(self.tmp.name) / "test.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    _full_data = staticmethod(_watch_scan_data)

    def _recorded(self) -> "sqlite3.Row | None":
        from shrinkray import db

        connection = db.connect(self.db_file)
        try:
            return connection.execute(
                "SELECT category FROM files WHERE path = ?", (str(self.movie.resolve()),)
            ).fetchone()
        finally:
            connection.close()

    def test_a_scanned_file_is_catalogued_under_its_folder(self) -> None:
        """The scan records the row even though no encode job ever runs."""
        from unittest.mock import patch

        from shrinkray.engine import watch as watch_mod

        with patch(
            "shrinkray.engine.watch.process_movie",
            side_effect=lambda m, r, c, db_file=None: (self._full_data(m, r), None),
        ):
            ok = watch_mod._scan_into_db(self.movie, self.root, self.db_file)
        self.assertTrue(ok)
        row = self._recorded()
        self.assertIsNotNone(row, "the watchdog's scan must leave a catalog row")
        self.assertEqual(row["category"], "Title")

    def test_a_failed_recording_does_not_fail_the_scan(self) -> None:
        """The #507/#517 rule: a failed write costs the shortcut, never the
        measurement - the scan still reports success (the encode path's own
        recording, service.ensure_file_row, writes its minimal row then)."""
        import sqlite3
        from unittest.mock import patch

        from shrinkray.engine import watch as watch_mod

        with (
            patch(
                "shrinkray.engine.watch.process_movie",
                side_effect=lambda m, r, c, db_file=None: (self._full_data(m, r), None),
            ),
            patch("shrinkray.db.connect", side_effect=sqlite3.DatabaseError("not a database")),
        ):
            ok = watch_mod._scan_into_db(self.movie, self.root, self.db_file)
        self.assertTrue(ok, "a recording failure must not fail the scan")
        self.assertIsNone(self._recorded())


class TestWatcherSwapIdentity(unittest.TestCase):
    """Issue #585: the watcher's own swap entry declines a source that changed
    during the encode, exactly as the service path does."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"ORIGINAL" * 100)
        self.output = self.root / "enc.mkv"
        self.output.write_bytes(b"NEW" * 10)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_source_changed_during_the_encode_declines_the_swap(self) -> None:
        from shrinkray.engine import watch as watch_mod

        before = self.source.stat()
        self.source.write_bytes(b"CHANGED-DURING-THE-ENCODE" * 10)
        with patch.object(watch_mod, "log_error") as fake_log:
            replaced = watch_mod._swap(
                None,
                self.source,
                self.output,
                None,
                input_size=before.st_size,
                input_mtime=before.st_mtime,
            )
        self.assertFalse(replaced)
        self.assertEqual(self.source.read_bytes(), b"CHANGED-DURING-THE-ENCODE" * 10)
        logged = "".join(str(call.args[0]) for call in fake_log.call_args_list)
        self.assertIn("the source changed during the encode", logged)

    def test_a_declined_swap_corrects_the_encode_row(self) -> None:
        """Issue #585 with the #232 contract: the watcher's declined swap
        discards the encode, so its row must stop pointing at the staging
        path."""
        from shrinkray import db
        from shrinkray.engine import watch as watch_mod

        db_file = self.root / "metadata.db"
        connection = db.connect(db_file)
        file_id = db.upsert_file(
            connection,
            db.MovieRecord(
                path=self.source,
                mtime=self.source.stat().st_mtime,
                size=self.source.stat().st_size,
                name="movie",
                category="Cat",
            ),
        )
        encode_id = db.add_encode(
            connection,
            file_id,
            "VMAF>95",
            "svt_av1",
            self.source.stat().st_size,
            self.output.stat().st_size,
            96.0,
            chosen_crf=30,
            achieved_vmaf=95.2,
            output_file=str(self.output),
        )
        connection.close()
        before = self.source.stat()
        self.source.write_bytes(b"CHANGED-DURING-THE-ENCODE" * 10)
        replaced = watch_mod._swap(
            db_file,
            self.source,
            self.output,
            encode_id,
            input_size=before.st_size,
            input_mtime=before.st_mtime,
        )
        self.assertFalse(replaced)
        self.assertEqual(self.source.read_bytes(), b"CHANGED-DURING-THE-ENCODE" * 10)
        connection = db.connect(db_file)
        try:
            row = connection.execute(
                "SELECT output_file FROM encodes WHERE id = ?", (encode_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()


class TestWatchInPlace(unittest.TestCase):
    """Issue #127: the watcher can encode in place.

    Mode B's watchdog is supposed to encode the library in place, but it required
    two directories and refused to run when they overlapped - the in-place case was
    the one it rejected. The overlap guard is right for what it protects, so the fix
    adds an in-place form instead of removing it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.movie = self.inbox / "one.mkv"
        self.movie.write_bytes(b"x" * 100)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> "object":
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            return runner.invoke(app, list(argv))
        finally:
            os.chdir(previous)

    def test_the_in_place_form_does_not_need_an_output_directory(self) -> None:
        """The criterion: `watch` with no output dir is accepted (issue #127;
        the watch-mode CLI verb was removed with issue #202, the engine call
        it covered is what these tests exercise directly)."""
        from shrinkray.engine.watch import watch

        with (
            mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(self.tmp.name) / "watch.lock"),
            mock.patch.object(watch_mod, "_wait_or_stop", lambda *a, **k: None),
            mock.patch.object(watch_mod, "decide", lambda *a, **k: None),
        ):
            watch([self.inbox], None, once=True)

    def test_the_two_directory_form_still_passes_both(self) -> None:
        """The regression guard: naming an output dir keeps the behaviour
        (issue #127)."""
        from shrinkray.engine.watch import watch

        out = Path(self.tmp.name) / "encoded"
        with (
            mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(self.tmp.name) / "watch.lock"),
            mock.patch.object(watch_mod, "_wait_or_stop", lambda *a, **k: None),
            mock.patch.object(watch_mod, "decide", lambda *a, **k: None),
        ):
            watch([self.inbox], out, once=True)

    def test_the_overlap_guard_still_refuses_two_overlapping_dirs(self) -> None:
        """The guard is right for what it protects: keep it for that form."""
        from shrinkray.engine.watch import watch

        with (
            mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(self.tmp.name) / "watch.lock"),
            self.assertRaises(ValueError) as raised,
        ):
            watch([self.inbox], self.inbox, once=True)
        self.assertIn("separate directories", str(raised.exception))

    def test_the_in_place_form_does_not_trip_the_overlap_guard(self) -> None:
        """In place there is no second tree, so the guard must not fire."""
        from shrinkray.engine.watch import watch

        with (
            patch("shrinkray.engine.watch.encode_with_quality") as fake_encode,
            mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(self.tmp.name) / "watch.lock"),
        ):
            fake_encode.return_value = type(
                "R",
                (),
                {
                    "success": False,
                    "skipped_reason": "below min size",
                    "error": "skipped",
                    "chosen_crf": None,
                    "achieved_vmaf": None,
                    "saved_percent": 0.0,
                    "output": self.movie,
                },
            )()
            watch([self.inbox], None, once=True, poll_interval=0.01)

    def test_a_retained_original_is_never_a_candidate(self) -> None:
        """The retained original must not be re-encoded as a new movie.

        The extension filter is what protects this: the retained name carries the
        suffix, so it never matches a movie extension. The explicit suffix check
        in find_new_movies is belt-and-braces for a misconfigured extension list,
        which is why the second half asserts it against a pattern that WOULD match.
        """
        from shrinkray.engine.watch import find_new_movies
        from shrinkray.replace import RETAINED_SUFFIX, retained_path_for

        retained = retained_path_for(self.movie)
        retained.write_bytes(b"o" * 100)
        found = find_new_movies(self.inbox, [".mkv"], set())
        self.assertIn(self.movie, found)
        self.assertNotIn(retained, found, "the retained original was picked up")
        # a misconfigured extension list must still not pick it up
        loose = find_new_movies(self.inbox, [".mkv", RETAINED_SUFFIX], set())
        self.assertNotIn(retained, loose, "a loose extension list picked it up")

    def test_a_dangling_entry_is_skipped_not_fatal(self) -> None:
        """Issue #427's discovery-class escape, on the RESIDENT shape
        (submit set): a file the directory listing reports that vanishes
        before its identity stat (a rename/delete race) raised out of
        find_new_movies and killed the watcher thread. The guard skips it;
        the loop continues and the surviving movie is queued. The REAL
        find_new_movies runs - the guard under test is its per-candidate
        identity stat; the dangling entry is a real dangling symlink.
        """
        from shrinkray.engine import watch as watch_mod

        inbox = self.inbox
        good = inbox / "good.mkv"
        good.write_bytes(b"x" * 100)
        old = time.time() - 7200
        os.utime(good, (old, old))
        dangling = inbox / "vanished.mkv"
        os.symlink(inbox / "gone-target.mkv", dangling)

        queued: list[str] = []

        with (
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
            mock.patch.object(watch_mod, "_scan_into_db", return_value=True) as scan_spy,
            mock.patch("shrinkray.engine.watch.encode_with_quality"),
        ):
            _isolated_lock(stack := ExitStack(), self.tmp.name)
            with stack:
                watch_mod.watch(
                    [inbox],
                    None,
                    once=True,
                    db_file=Path(self.tmp.name) / "test.db",
                    submit=lambda p: queued.append(p.name) or True,
                )
        # the dangling entry did not kill the loop: the good movie got
        # scanned and queued; the dangling one was skipped
        self.assertEqual(queued, ["good.mkv"], "the loop died on the dangling entry")
        self.assertEqual(scan_spy.call_count, 1)

    def test_a_scan_filesystem_error_keeps_the_file_eligible(self) -> None:
        """Issue #427's scan-class escape, on the RESIDENT shape: the scan's
        own filesystem reads (scan.py's getsize/stat) raise on a file that
        vanished or a mount that dropped; the escape killed the watcher
        thread. The guard returns False - the file stays eligible and the
        loop continues; the queued movie proves the loop is alive.
        """
        from shrinkray.engine import watch as watch_mod

        inbox = self.inbox
        scanfail = inbox / "scanfail.mkv"
        scanfail.write_bytes(b"x" * 100)
        good = inbox / "good.mkv"
        good.write_bytes(b"x" * 100)
        old = time.time() - 7200
        for f in (scanfail, good, self.movie):
            os.utime(f, (old, old))

        # the guard UNDER TEST is _scan_into_db's OSError handling: patch its
        # INNER call (the scan's process_movie) - the failing file raises
        # OSError (the guard's catch); the good file returns a scan result
        # (a 100-byte junk file cannot run the real ffprobe scan)
        queued: list[str] = []

        def failing_scan(movie, root, config, db_file=None):
            if movie.name == "scanfail.mkv":
                raise OSError("Input/output error")
            return _watch_scan_data(movie, root), None

        with (
            mock.patch("shrinkray.engine.watch.process_movie", side_effect=failing_scan),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack := ExitStack(), self.tmp.name)
            with stack:
                watch_mod.watch(
                    [inbox],
                    None,
                    once=True,
                    db_file=Path(self.tmp.name) / "test.db",
                    submit=lambda p: queued.append(p.name) or True,
                )
        # the scan-failing file's guard returned False (stayed eligible, no
        # queue) and the OTHER movies were still queued: the loop is alive
        self.assertNotIn("scanfail.mkv", queued, "the failed scan's file was queued")
        self.assertIn("good.mkv", queued, "the loop died on the scan's OSError")

    def test_a_filesystem_error_in_the_swap_ends_not_the_watcher(self) -> None:
        """Issue #427: _swap's filesystem work (stage, the db writes, the
        rename) raises OSError - a full disk, a permission change, a mount
        dropping - and the exception escaped watch()'s handlers (they caught
        only EncodeBusyError/Cancelled/RuntimeError), killing the watcher
        thread while /api/status reported it stopped - the library silently
        stopped being watched until a restart. The #260 rule covers the
        filesystem too: the failure is logged with the file's name and the
        loop continues to the next movie.
        """
        from shrinkray.engine import watch as watch_mod

        inbox = self.inbox
        # the in-place form: the swap runs (the two-directory form never
        # reaches _swap - the first draft of this test passed vacuously there)
        swap_fails = inbox / "swapfail.mkv"
        swap_fails.write_bytes(b"x" * 100)
        old = time.time() - 7200
        for f in (self.movie, swap_fails):
            os.utime(f, (old, old))  # stable
        processed: list[str] = []

        def fake_quality(m, o, **kwargs):
            processed.append(m.name)
            # the staged output must EXIST with content smaller than the
            # source: the swap's size gate passes and the stage patch fires
            o.write_bytes(b"e" * 50)
            return engine_plan.QualityResult(
                input=m,
                output=o,
                success=True,
                target_vmaf=96.0,
                chosen_crf=22,
                achieved_vmaf=96.0,
                probes=[],
                input_size=100,
                output_size=50,
                error=None,
                input_bit_depth=8,
            )

        def failing_stage(*args, **kwargs):
            raise OSError("No space left on device")

        stack = ExitStack()
        with (
            mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
            mock.patch("shrinkray.replace.stage", side_effect=failing_stage),
            # the test's own runtime: the seven sibling tests patch the
            # stability interval to 0.0 (issue #478); so does this one
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([inbox], None, once=True)
        # BOTH processed in place: the swap's OSError did not end the loop
        self.assertEqual(
            sorted(processed),
            ["one.mkv", "swapfail.mkv"],
            "the loop died on the swap's OSError; the good one was never processed",
        )

    def test_a_staging_copy_is_never_a_candidate(self) -> None:
        """A staging copy must not be encoded while the swap is in flight."""
        from shrinkray.engine.watch import find_new_movies
        from shrinkray.replace import STAGING_SUFFIX, staging_path_for

        staged = staging_path_for(self.movie)
        staged.write_bytes(b"s" * 100)
        found = find_new_movies(self.inbox, [".mkv"], set())
        self.assertNotIn(staged, found, "the staging copy was picked up")
        loose = find_new_movies(self.inbox, [".mkv", STAGING_SUFFIX], set())
        self.assertNotIn(staged, loose, "a loose extension list picked it up")


class TestWatchLibraryTree(unittest.TestCase):
    """Issue #133: the watcher covers the library TREE, not one inbox.

    Mode B's watchdog is specified over the mounted library: a new or changed file
    anywhere in the tree must be picked up. An inbox only ever caught what someone
    dropped into one folder.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        self.out = Path(self.tmp.name) / "out"
        self.root.mkdir()
        self.out.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _movie(self, sub: str, name: str = "movie.mkv") -> Path:
        folder = self.root / sub
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / name
        movie.write_bytes(b"movie")
        old = time.time() - 7200
        os.utime(movie, (old, old))
        return movie

    def _encode_once(self, seen: list, roots: list[Path] | None = None) -> None:
        def fake_quality(m, o, **kwargs):
            seen.append(Path(m))
            return engine_plan.QualityResult(input=m, output=o, success=True)

        stack = ExitStack()
        with (
            mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
            mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            mock.patch("shrinkray.engine.watch.probe_duration_seconds", return_value=600.0),
            mock.patch("shrinkray.engine.watch.find_handbrake", return_value="hb"),
            # a busy CI runner's disk can dip below the pass guard's floor
            # mid-suite; the tests must not depend on the runner's free
            # space (the #303 CI face of the #291 family)
            mock.patch("shrinkray.engine.watch.disk_free_bytes", return_value=1 << 40),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch(roots or [self.root], self.out, once=True)

    def test_a_new_file_in_a_subdirectory_is_picked_up(self) -> None:
        """The criterion: a new file anywhere under the root is found."""
        movie = self._movie("Action/Matrix")
        seen: list = []
        self._encode_once(seen)
        self.assertIn(movie, seen, "a new file in a subdirectory was missed")

    def test_a_changed_file_is_re_decided(self) -> None:
        """A changed file (same path, new mtime/size) is a new question."""
        movie = self._movie("Action/Matrix")
        seen: list = []
        self._encode_once(seen)
        self.assertEqual(seen, [movie])

        movie.write_bytes(b"movie changed")
        old = time.time() - 7200
        os.utime(movie, (old, old))
        seen.clear()
        self._encode_once(seen)
        self.assertEqual(seen, [movie], "a changed file was never re-decided")

    def test_an_unchanged_file_is_not_re_decided(self) -> None:
        """The regression guard: an untouched file must not be re-encoded.

        One continuous watch run, two passes: `known` lives for the whole run,
        so a fresh `watch()` call would reset it and prove nothing.
        """
        movie = self._movie("Action/Matrix")
        stop = threading.Event()
        seen: list = []

        def fake_quality(m, o, **kwargs):
            seen.append(Path(m))
            return engine_plan.QualityResult(input=m, output=o, success=True)

        def fake_sleep(interval, stop_event, **kwargs):
            # the first pass encoded the movie: stop before the second pass
            if stop_event is not None:
                stop_event.set()

        stack = ExitStack()
        with (
            mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_quality),
            mock.patch("shrinkray.engine.watch._wait_or_stop", side_effect=fake_sleep),
            mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
            mock.patch("shrinkray.engine.watch.probe_duration_seconds", return_value=600.0),
            mock.patch("shrinkray.engine.watch.find_handbrake", return_value="hb"),
        ):
            _isolated_lock(stack, self.tmp.name)
            with stack:
                watch_mod.watch([self.root], self.out, once=False, stop_event=stop)
        self.assertEqual(seen, [movie], "an unchanged file was re-encoded")

    def test_a_still_writing_file_is_not_decided_yet(self) -> None:
        """A file still being written must wait until it is stable."""
        movie = self.root / "Action" / "Matrix" / "movie.mkv"
        movie.parent.mkdir(parents=True)
        movie.write_bytes(b"movie")
        seen: list = []
        self._encode_once(seen)
        self.assertEqual(seen, [], "a still-writing file was decided too early")

    def test_every_root_is_walked(self) -> None:
        """Repeatable --root: a second root is watched too."""
        second = Path(self.tmp.name) / "second"
        (second / "Deep").mkdir(parents=True)
        movie = second / "Deep" / "other.mkv"
        movie.write_bytes(b"movie")
        old = time.time() - 7200
        os.utime(movie, (old, old))

        seen: list = []
        self._encode_once(seen, roots=[self.root, second])
        self.assertIn(movie, seen, "the second root was not walked")


class TestWatcherEncodeWindow(unittest.TestCase):
    """Issue #434: the watcher defers outside the encode window — at the
    pass level AND per file (a mid-pass crossing); a deferred file is never
    entered into `known`, so it is reconsidered inside the window."""

    def test_the_window_gates_per_pass_and_per_file(self) -> None:
        """One watch() run, two passes: pass 1 outside the window defers
        (nothing encoded); pass 2 inside it encodes the first candidate but
        the per-file check defers the second (the mid-pass crossing). The
        first file being encoded EXACTLY ONCE pins the known-persistence
        claim (a pass-1 encode would have put it in `known` and pass 2 would
        have skipped it)."""
        import tempfile
        from contextlib import ExitStack

        from shrinkray.engine import watch as watch_mod
        from shrinkray.engine.plan import QualityResult
        from shrinkray.engine.watch import WatchSettings

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "inbox"
            root.mkdir()
            out = tmp_path / "out"
            seen: list[str] = []
            for name in ("one.mkv", "two.mkv"):
                movie = root / name
                movie.write_bytes(b"movie")
                old = time.time() - 7200
                os.utime(movie, (old, old))

            settings_obj = WatchSettings(
                max_threads=None,
                target_vmaf=95.0,
                min_source_height=0,
                min_source_width=0,
                min_source_size_gb=0.0,
                candidate_codecs=("h264", "hevc", "vp9", "av1"),
                crop_sample_count=10,
                encode_window_start="09:00",
                encode_window_end="17:00",
            )
            window_calls: list[bool] = []

            def window(start: str, end: str, now=None) -> bool:
                window_calls.append(True)
                # 1: pass 1's pass-level check (closed); 2: pass 2's pass-level
                # check (open); 3: candidate 1's per-file check (open);
                # 4: candidate 2's per-file check (closed - the crossing);
                # 5: pass 3's pass-level check (open); 6: the deferred
                # candidate's per-file check (open - it is reconsidered)
                return [False, True, True, False, True, True][len(window_calls) - 1]

            def fake_encode(m, o, **kw):
                seen.append(Path(m).name)
                return QualityResult(
                    input=m, output=o, success=False, skipped_reason="no size gain"
                )

            passes = [0]

            def fake_sleep(interval, stop_event, **kwargs):
                passes[0] += 1
                if passes[0] >= 3 and stop_event is not None:
                    stop_event.set()

            stop = threading.Event()
            stack = ExitStack()
            with (
                mock.patch("shrinkray.engine.watch.encode_window_open", side_effect=window),
                mock.patch("shrinkray.engine.watch.encode_with_quality", side_effect=fake_encode),
                mock.patch("shrinkray.engine.watch._wait_or_stop", side_effect=fake_sleep),
                mock.patch("shrinkray.engine.watch.STABLE_CHECK_INTERVAL", 0.0),
                mock.patch("shrinkray.engine.watch.probe_duration_seconds", return_value=600.0),
                mock.patch("shrinkray.engine.watch.find_handbrake", return_value="hb"),
            ):
                _isolated_lock(stack, tmp)
                with stack:
                    watch_mod.watch(
                        [root], out, once=False, stop_event=stop, settings=lambda: settings_obj
                    )
            # the candidates' order is the readdir's, not mine. Pass 1
            # encoded nothing (the pass-level deferral); pass 2 encoded the
            # first candidate and deferred the second (the per-file check);
            # pass 3 encoded the deferred one (it was never in `known` - the
            # starvation pin: had the per-file deferral entered it into
            # `known`, pass 3 would have skipped it and seen would hold one
            # name) and skipped the first (already in `known`).
            self.assertEqual(
                sorted(seen), ["one.mkv", "two.mkv"], f"the window gate misfired: {window_calls}"
            )
            self.assertEqual(len(window_calls), 6, "a check did not run as expected")
