"""Pause/resume and the manual encode's pause of the watcher (issue #250).

The resident app starts no new file while the operator paused it or while a
manual `shrinkray encode` runs beside it; a running encode always finishes. A
marker whose process is gone must never pause the app for good.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import shrinkray.server.app as app_module
from shrinkray import db
from shrinkray.engine import pause
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import watch as watch_mod
from shrinkray.server import jobs
from shrinkray.server.app import AppState
from shrinkray.server.app import app as fast_app
from shrinkray.tools.process import process_start_ticks

HAS_PROC = Path("/proc/self/stat").exists()


def _dead_pid() -> int:
    """A pid that existed and is gone now."""
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return int(child.stdout.strip())


def _skip(movie: Path, output: Path) -> engine_plan.QualityResult:
    return engine_plan.QualityResult(
        input=movie, output=output, success=False, skipped_reason="no size gain"
    )


class TestOperatorPause(unittest.TestCase):
    def test_pause_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertIsNone(pause.pause_reason(state))
            pause.set_operator_paused(state, True)
            reason = pause.pause_reason(state) or ""
            self.assertIn("paused by the operator", reason)
            self.assertIn(
                str(state / pause.PAUSE_FLAG), reason, "the reason must say how to resume"
            )
            pause.set_operator_paused(state, False)
            self.assertIsNone(pause.pause_reason(state))

    def test_no_data_directory_is_never_paused(self) -> None:
        self.assertIsNone(pause.pause_reason(None))


class TestManualEncodeMarker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.folder = self.state / pause.MANUAL_ENCODES

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _marker(self, pid: int, content: str) -> Path:
        self.folder.mkdir(exist_ok=True)
        marker = self.folder / str(pid)
        marker.write_text(content, encoding="ascii")
        return marker

    def test_the_marker_lives_exactly_as_long_as_the_run(self) -> None:
        with pause.manual_encode(self.state):
            self.assertEqual(pause.running_manual_encodes(self.state), [os.getpid()])
            self.assertIn("manual encode", pause.pause_reason(self.state) or "")
        self.assertEqual(pause.running_manual_encodes(self.state), [])
        self.assertIsNone(pause.pause_reason(self.state))

    def test_the_marker_is_removed_when_the_run_fails(self) -> None:
        with self.assertRaises(RuntimeError), pause.manual_encode(self.state):
            raise RuntimeError("the encode failed")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_dead_process_never_pauses_and_its_marker_is_cleaned(self) -> None:
        marker = self._marker(_dead_pid(), "")
        self.assertEqual(pause.running_manual_encodes(self.state), [])
        self.assertFalse(marker.exists())

    @unittest.skipUnless(HAS_PROC, "process start times come from /proc")
    def test_a_reused_pid_is_not_the_process_that_wrote_the_marker(self) -> None:
        """A container restart reuses pids: a live pid with another start time
        is someone else, not the manual encode that left the marker."""
        started = process_start_ticks(os.getpid())
        assert started is not None
        self._marker(os.getpid(), str(started + 1))
        self.assertEqual(pause.running_manual_encodes(self.state), [])

    def test_without_start_times_an_old_marker_is_stale(self) -> None:
        """Where /proc is missing the pid is all there is, so age bounds it."""
        marker = self._marker(os.getpid(), "")
        with mock.patch.object(pause, "process_start_ticks", return_value=None):
            self.assertEqual(pause.running_manual_encodes(self.state), [os.getpid()])
            old = time.time() - pause._MARKER_STALE_SECONDS - 60
            os.utime(marker, (old, old))
            self.assertEqual(pause.running_manual_encodes(self.state), [])

    def test_no_data_directory_writes_no_marker(self) -> None:
        with pause.manual_encode(None):
            pass
        self.assertFalse(self.folder.exists())

    def test_an_unwritable_folder_does_not_fail_the_encode(self) -> None:
        with (
            mock.patch.object(Path, "mkdir", side_effect=PermissionError("read-only")),
            mock.patch.object(pause, "log") as log,
            pause.manual_encode(self.state),
        ):
            ran = True
        self.assertTrue(ran)
        self.assertIn("may encode alongside", log.call_args.args[0])

    def test_the_cli_shares_the_containers_data_folder(self) -> None:
        with mock.patch.dict(os.environ, {pause.CONFIG_ENV_VAR: str(self.state / "config.json")}):
            self.assertEqual(pause.state_dir_for_cli(None), self.state.resolve())
            self.assertEqual(
                pause.state_dir_for_cli(self.state / "sub" / "metadata.db"),
                (self.state / "sub").resolve(),
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(pause.state_dir_for_cli(None))


class _ResumingEvent(threading.Event):
    """A cancel event whose wait() resumes the app: the pause ends without any
    real sleeping or timing."""

    def __init__(self, state: Path) -> None:
        super().__init__()
        self.state = state
        self.waits = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        pause.set_operator_paused(self.state, False)
        return self.is_set()


class TestWaitWhilePaused(unittest.TestCase):
    def test_a_job_waits_until_resumed_and_reports_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            pause.set_operator_paused(state, True)
            event = _ResumingEvent(state)
            reasons: list[str] = []
            pause.wait_while_paused(state, event, on_pause=reasons.append)
        self.assertEqual(event.waits, 1)
        self.assertEqual(len(reasons), 1)
        self.assertIn("paused by the operator", reasons[0])

    def test_a_cancel_ends_the_wait(self) -> None:
        """A cancelled job must not hang on a pause that never ends."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            pause.set_operator_paused(state, True)
            cancel = threading.Event()
            pause.wait_while_paused(state, cancel, on_pause=lambda reason: cancel.set())
            # it returned because of the cancel, while still paused
            self.assertTrue(cancel.is_set())
            self.assertTrue(pause.operator_paused(state))


@contextmanager
def _watcher(tmp: Path, fake_quality):
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(watch_mod, "WATCH_LOCK_FILE", tmp / "watch.lock"))
        stack.enter_context(
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_quality)
        )
        stack.enter_context(mock.patch.object(watch_mod, "decide", return_value="encode"))
        stack.enter_context(mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0))
        # the pass guard's free-space reading: a CI disk dip must not eat a
        # pass (the #303 CI face of the #291 family)
        stack.enter_context(mock.patch.object(watch_mod, "disk_free_bytes", return_value=1 << 40))
        yield


class TestWatcherYields(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "in"
        self.out = self.root / "out"
        self.inbox.mkdir()
        self.out.mkdir()
        self.db_file = self.root / "metadata.db"
        old = time.time() - 7200
        for name in ("a.mkv", "b.mkv"):
            movie = self.inbox / name
            movie.write_bytes(b"movie")
            os.utime(movie, (old, old))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_paused_watcher_encodes_nothing_and_keeps_files_eligible(self) -> None:
        seen: list[str] = []

        def fake_quality(movie, output, **kwargs):
            seen.append(movie.name)
            return _skip(movie, output)

        pause.set_operator_paused(self.root, True)
        with _watcher(self.root, fake_quality):
            watch_mod.watch([self.inbox], self.out, once=True, db_file=self.db_file)
        self.assertEqual(seen, [])
        pause.set_operator_paused(self.root, False)
        with _watcher(self.root, fake_quality):
            watch_mod.watch([self.inbox], self.out, once=True, db_file=self.db_file)
        self.assertEqual(sorted(seen), ["a.mkv", "b.mkv"])

    def test_a_manual_encode_started_mid_pass_holds_the_next_file(self) -> None:
        """The running file finishes; the next one waits for the manual encode."""
        seen: list[str] = []
        held = ExitStack()

        def fake_quality(movie, output, **kwargs):
            seen.append(movie.name)
            # a console starts `shrinkray encode` while this file encodes
            held.enter_context(pause.manual_encode(self.root))
            return _skip(movie, output)

        with held, _watcher(self.root, fake_quality):
            watch_mod.watch([self.inbox], self.out, once=True, db_file=self.db_file)
        self.assertEqual(len(seen), 1)


class TestJobsHold(unittest.TestCase):
    def test_a_plan_job_holds_before_the_next_movie_and_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / "a.mkv"
            movie.write_bytes(b"x" * 16)
            connection = db.connect(root / "metadata.db")
            try:
                queue = jobs.JobQueue(connection)
                job = queue.enqueue(
                    jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]})
                )
                pause.set_operator_paused(root, True)
                cancel = threading.Event()
                events: list[dict] = []

                def progress(event: dict) -> None:
                    events.append(event)
                    if event.get("stage") == "paused":
                        cancel.set()  # the operator gives up waiting

                with (
                    mock.patch.object(jobs, "resolve_movies", return_value=[movie]),
                    mock.patch("shrinkray.engine.plan.plan_quality") as plan_quality,
                ):
                    result = jobs.execute_plan(job, connection, cancel, progress=progress)
            finally:
                connection.close()
        plan_quality.assert_not_called()
        self.assertEqual(result["plans"], [{"movie": "a.mkv", "state": "cancelled"}])
        paused = [e for e in events if e.get("stage") == "paused"]
        self.assertEqual(len(paused), 1)
        self.assertIn("paused by the operator", paused[0]["detail"])


class TestStatusAndCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(app_module.AUTH_TOKEN_ENV, None)

    def tearDown(self) -> None:
        app_module.state = None
        self.env.stop()
        self.tmp.cleanup()

    def test_the_status_reports_the_pause(self) -> None:
        app_module.state = AppState(self.root / "metadata.db")
        client = TestClient(fast_app)
        self.assertIsNone(client.get("/api/status").json()["paused"])
        pause.set_operator_paused(self.root, True)
        self.assertIn("paused by the operator", client.get("/api/status").json()["paused"])
        self.assertIn(
            "paused?: string | null",
            (Path(__file__).resolve().parent.parent / "ui/src/lib/api.ts").read_text(),
        )

    def test_a_cli_encode_marks_itself_for_its_whole_run(self) -> None:
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        movie = self.root / "film.mkv"
        movie.write_bytes(b"x" * 8)
        # the floor AND the interval constants are patched to 0: the real seam
        # runs its full shape instantly (this test's subject is the
        # manual-encode marker, not stability) - no test sleeps
        during: list[list[int]] = []

        def fake_encode(source, output, **kwargs):
            during.append(pause.running_manual_encodes(self.root))
            return _skip(source, output)

        with (
            mock.patch.object(watch_mod, "STABLE_MIN_AGE", 0.0),
            mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0),
            mock.patch("shrinkray.service.shutil.which", return_value="ffprobe"),
            mock.patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
            mock.patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
            mock.patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value="modern"),
            mock.patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_encode),
        ):
            result = CliRunner().invoke(
                app, ["encode", str(movie), "--db", str(self.root / "metadata.db")]
            )
        self.assertEqual(during, [[os.getpid()]], result.output)
        self.assertEqual(pause.running_manual_encodes(self.root), [])


if __name__ == "__main__":
    unittest.main()
