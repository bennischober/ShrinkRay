"""The config on /data and the reload at the next file (issue #247).

The container's config must survive restarts and image updates, so the first
command creates /data/config.json from the image's defaults and never overwrites
it. A long-running process applies a saved change at its next file, and an edit
that does not parse or validate never replaces a working config.
"""

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import shrinkray.server.app as app_module
from shrinkray import config as config_module
from shrinkray.config import (
    CONFIG_ENV_VAR,
    CONFIG_SEED_ENV_VAR,
    load_config,
    refresh_config,
    reset_config,
    seed_config_file,
    seed_config_from_env,
)
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import watch as watch_mod
from shrinkray.server.app import AppState
from shrinkray.server.app import app as fast_app

REPO = Path(__file__).resolve().parent.parent


def _write_config(path: Path, data: dict, mtime_ns: int) -> None:
    """Writes a config with an explicit mtime, so a change never depends on
    the filesystem's timestamp granularity."""
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


class TestSeedConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.seed = self.root / "seed.json"
        self.seed.write_text('{"app": {"auto_backlog": true}}', encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_missing_config_is_created_from_the_seed(self) -> None:
        target = self.root / "data" / "config.json"  # the folder does not exist yet
        self.assertTrue(seed_config_file(target, self.seed))
        self.assertEqual(target.read_bytes(), self.seed.read_bytes())

    def test_an_existing_config_is_never_overwritten(self) -> None:
        """The operator's file wins: a new image's defaults must not replace it."""
        target = self.root / "config.json"
        target.write_text('{"handbrake": {"max_threads": 4}}', encoding="utf-8")
        self.assertFalse(seed_config_file(target, self.seed))
        self.assertEqual(target.read_text(encoding="utf-8"), '{"handbrake": {"max_threads": 4}}')

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_copy_takes_the_seed_permissions_not_the_temp_files(self) -> None:
        """mkstemp creates 0600; a config the operator cannot read from their
        share would be a support ticket."""
        os.chmod(self.seed, 0o644)
        target = self.root / "config.json"
        seed_config_file(target, self.seed)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_no_temp_file_is_left_behind(self) -> None:
        target = self.root / "config.json"
        seed_config_file(target, self.seed)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json", "seed.json"])

    def test_a_target_created_concurrently_is_kept(self) -> None:
        """A second process that wins the race keeps its file; the link fails
        rather than replacing it, and the temp copy is removed."""
        target = self.root / "config.json"
        real_link = os.link

        def racing_link(src: str, dst: Path) -> None:
            Path(dst).write_text('{"winner": true}', encoding="utf-8")
            real_link(src, dst)

        with mock.patch.object(config_module.os, "link", side_effect=racing_link):
            self.assertFalse(seed_config_file(target, self.seed))
        self.assertEqual(target.read_text(encoding="utf-8"), '{"winner": true}')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json", "seed.json"])

    def test_the_environment_drives_the_seeding(self) -> None:
        target = self.root / "config.json"
        with mock.patch.dict(
            os.environ, {CONFIG_ENV_VAR: str(target), CONFIG_SEED_ENV_VAR: str(self.seed)}
        ):
            seed_config_from_env()
        self.assertTrue(target.is_file())

    def test_without_the_environment_nothing_happens(self) -> None:
        """Outside the container neither variable is set: no file appears."""
        with mock.patch.dict(os.environ, {}, clear=True):
            seed_config_from_env()
        self.assertEqual([p.name for p in self.root.iterdir()], ["seed.json"])

    def test_an_unreadable_seed_is_reported_not_raised(self) -> None:
        target = self.root / "config.json"
        env = {CONFIG_ENV_VAR: str(target), CONFIG_SEED_ENV_VAR: str(self.root / "missing.json")}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(config_module, "log_error") as log_error,
        ):
            seed_config_from_env()
        self.assertFalse(target.exists())
        log_error.assert_called_once()
        self.assertIn("missing.json", log_error.call_args.args[0])

    def test_every_cli_command_seeds_first(self) -> None:
        """The CLI's callback runs before the command, so the container's first
        command (server or one-shot) creates the operator's config."""
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        target = self.root / "config.json"
        env = {CONFIG_ENV_VAR: str(target), CONFIG_SEED_ENV_VAR: str(self.seed)}
        with mock.patch.dict(os.environ, env):
            # `recover` without --dir fails fast after the callback ran
            result = CliRunner().invoke(app, ["retained", "recover"])
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(target.is_file())


class TestRefreshConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.mtime = time.time_ns() - 10**12
        _write_config(self.path, {"handbrake": {"max_threads": 2}}, self.mtime)
        reset_config()
        self.config = load_config(config_file=self.path)

    def tearDown(self) -> None:
        reset_config()
        self.tmp.cleanup()

    def _change(self, data: dict | str) -> None:
        self.mtime += 10**9
        if isinstance(data, str):
            self.path.write_text(data, encoding="utf-8")
            os.utime(self.path, ns=(self.mtime, self.mtime))
        else:
            _write_config(self.path, data, self.mtime)

    def test_an_unchanged_file_is_not_read_again(self) -> None:
        with mock.patch.object(config_module.json, "loads") as loads:
            self.assertIs(refresh_config(), self.config)
        loads.assert_not_called()

    def test_a_saved_change_is_applied(self) -> None:
        self._change({"handbrake": {"max_threads": 6}})
        self.assertEqual(refresh_config().handbrake_max_threads, 6)
        self.assertIsNone(config_module.config_error)

    def test_an_invalid_edit_keeps_the_previous_settings(self) -> None:
        """A half-typed or out-of-schema edit must not stop or reconfigure a
        running app: the last good config stays, and the reason is visible."""
        for bad in ("{not json", {"eligibility": {"min_source_height": 720}}):
            with self.subTest(bad=bad):
                self._change(bad)
                self.assertIs(refresh_config(), self.config)
                self.assertIn("not applied", config_module.config_error or "")
                self.assertIn(str(self.path), config_module.config_error or "")

    def test_a_bad_file_is_reported_once_not_per_file(self) -> None:
        self._change("{not json")
        with mock.patch.object(config_module, "log_error") as log_error:
            refresh_config()
            refresh_config()
            refresh_config()
        log_error.assert_called_once()

    def test_fixing_the_file_clears_the_error(self) -> None:
        self._change("{not json")
        refresh_config()
        self._change({"handbrake": {"max_threads": 3}})
        self.assertEqual(refresh_config().handbrake_max_threads, 3)
        self.assertIsNone(config_module.config_error)

    def test_a_deleted_file_is_an_error_not_a_reset_to_defaults(self) -> None:
        self.path.unlink()
        self.assertIs(refresh_config(), self.config)
        self.assertIn("no longer exists", config_module.config_error or "")

    def test_an_explicit_file_wins_over_an_already_cached_config(self) -> None:
        """--config is an instruction, not a cache hint (issue #328).

        load_config returned the cached config before consulting config_file, so a
        process that had loaded any config silently ignored the explicit file.
        The repo's own tracked ./config.json is what made this visible: a test
        running from the repo root read it instead of the file it passed.
        """
        other = Path(self.tmp.name) / "other.json"
        _write_config(other, {"handbrake": {"max_threads": 9}}, self.mtime)
        # self.config is cached from self.path; the explicit file must still win
        self.assertEqual(load_config(config_file=other).handbrake_max_threads, 9)
        # and the cache now refers to the explicit file, so a no-arg load keeps it
        self.assertEqual(load_config().handbrake_max_threads, 9)

    def test_a_rejected_explicit_file_leaves_the_cache_identity_intact(self) -> None:
        """A bad explicit file must not become the file refresh_config watches.

        Recording the path before Conf(data) validated left the cache pointing at a
        file whose settings are not in effect: refresh_config would then watch the
        rejected file, and a retry would silently return the stale settings.
        """
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"eligibility": {"min_source_height": 720}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(config_file=bad)
        # the cache still refers to the good file, and still yields its settings
        self.assertEqual(load_config().handbrake_max_threads, 2)


class TestWatcherAppliesSettingsPerFile(unittest.TestCase):
    def test_settings_are_asked_for_before_every_file(self) -> None:
        """A changed config reaches the watcher at the next file: the provider
        is asked once per candidate and its values reach the engine."""
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            old = time.time() - 7200
            for name in ("a.mkv", "b.mkv"):
                movie = inbox / name
                movie.write_bytes(b"movie")
                os.utime(movie, (old, old))

            # one call at the start of the pass, then one per file
            floors = iter([1080, 1080, 2160])
            seen: dict[str, int] = {}

            def provider() -> watch_mod.WatchSettings:
                return watch_mod.WatchSettings(
                    max_threads=None,
                    target_vmaf=94.0,
                    min_source_height=next(floors),
                    min_source_width=1920,
                    min_source_size_gb=0.0,
                    candidate_codecs=engine_plan.SOURCE_CODECS,
                    crop_sample_count=10,
                )

            targets: set[float] = set()

            def fake_quality(movie, output, **kwargs):
                seen[movie.name] = kwargs["min_source_height"]
                targets.add(kwargs["target_vmaf"])
                return engine_plan.QualityResult(
                    input=movie, output=output, success=False, skipped_reason="no size gain"
                )

            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "watch.lock")
                )
                stack.enter_context(
                    mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_quality)
                )
                stack.enter_context(mock.patch.object(watch_mod, "decide", return_value="encode"))
                stack.enter_context(mock.patch.object(watch_mod, "STABLE_CHECK_INTERVAL", 0.0))
                watch_mod.watch([inbox], out, once=True, settings=provider)

        self.assertEqual(sorted(seen.values()), [1080, 2160])
        self.assertEqual(targets, {94.0}, "the configured VMAF must reach the engine")

    def test_an_idle_pass_still_asks_for_settings(self) -> None:
        """With no new file, a broken config edit must still be noticed (and
        reported) within one poll interval, not when the next file arrives."""
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "in"
            out = Path(tmp) / "out"
            inbox.mkdir()
            out.mkdir()
            provider = mock.Mock(
                return_value=watch_mod.WatchSettings(
                    max_threads=None,
                    target_vmaf=96.0,
                    min_source_height=1080,
                    min_source_width=1920,
                    min_source_size_gb=0.0,
                    candidate_codecs=engine_plan.SOURCE_CODECS,
                    crop_sample_count=10,
                )
            )
            with mock.patch.object(watch_mod, "WATCH_LOCK_FILE", Path(tmp) / "watch.lock"):
                watch_mod.watch([inbox], out, once=True, settings=provider)
        provider.assert_called_once()


class TestJobsApplySettingsPerMovie(unittest.TestCase):
    def test_a_plan_job_refreshes_the_config_before_every_movie(self) -> None:
        """A config saved while a backlog job runs applies from its next movie."""
        import threading

        from shrinkray import db
        from shrinkray.config import Conf
        from shrinkray.server import jobs

        with tempfile.TemporaryDirectory() as tmp:
            movies = [Path(tmp) / "a.mkv", Path(tmp) / "b.mkv"]
            for movie in movies:
                movie.write_bytes(b"x" * 16)
            connection = db.connect(Path(tmp) / "test.db")
            try:
                queue = jobs.JobQueue(connection)
                job = queue.enqueue(
                    jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(m) for m in movies]})
                )
                configs = iter(
                    [
                        Conf({"eligibility": {"min_source_height": 1080}}),
                        Conf({"eligibility": {"min_source_height": 2160}}),
                    ]
                )
                seen: list[int] = []

                def fake_plan(movie, **kwargs):
                    seen.append(kwargs["min_source_height"])
                    return engine_plan.QualityResult(
                        input=movie,
                        output=movie,
                        success=False,
                        skipped_reason="resolution too low",
                    )

                with (
                    mock.patch.object(jobs, "resolve_movies", return_value=movies),
                    mock.patch(
                        "shrinkray.config.refresh_config", side_effect=lambda: next(configs)
                    ),
                    mock.patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
                    mock.patch("shrinkray.service.record_plan"),
                ):
                    jobs.execute_plan(job, connection, threading.Event())
            finally:
                connection.close()
        self.assertEqual(seen, [1080, 2160])


class TestStatusShowsConfigError(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(app_module.AUTH_TOKEN_ENV, None)
        app_module.state = AppState(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        app_module.state = None
        reset_config()
        self.env.stop()
        self.tmp.cleanup()

    def test_the_status_carries_the_error_and_its_absence(self) -> None:
        client = TestClient(fast_app)
        reset_config()
        self.assertIsNone(client.get("/api/status").json()["config_error"])
        with mock.patch.object(
            config_module, "config_error", "'/data/config.json' was not applied"
        ):
            self.assertEqual(
                client.get("/api/status").json()["config_error"],
                "'/data/config.json' was not applied",
            )

    def test_the_dashboard_types_the_field(self) -> None:
        """AGENTS.md: a FastAPI field missing from ui/src/lib/api.ts is
        invisible to the dashboard."""
        self.assertIn("config_error?: string | null", (REPO / "ui/src/lib/api.ts").read_text())


class TestImageKeepsItsConfigOnTheVolume(unittest.TestCase):
    def test_the_image_points_the_config_at_data_and_seeds_it(self) -> None:
        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        runtime = dockerfile.split("FROM runtime AS dev")[0]
        self.assertIn(f"{CONFIG_ENV_VAR}=/data/config.json", runtime)
        self.assertIn(f"{CONFIG_SEED_ENV_VAR}=/app/config.json", runtime)
        self.assertIn("COPY container-config.json /app/config.json", runtime)


if __name__ == "__main__":
    unittest.main()
