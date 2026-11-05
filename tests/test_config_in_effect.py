"""The config read path: one reader (issue #541).

Each test names one acceptance criterion from the maintainer's 2026-11-01
decision comment. Run against main they must fail for the reason the issue
describes; after the fix they must pass.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from shrinkray import config as config_module
from shrinkray.config import reset_config
from shrinkray.server import app as app_module
from shrinkray.server.app import AppState
from shrinkray.server.app import app as fast_app


class _ConfigCase(unittest.TestCase):
    """A temp db directory holds config.json, so the route resolves it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(app_module.AUTH_TOKEN_ENV, None)
        os.environ.pop("SHRINKRAY_CONFIG", None)
        reset_config()
        self.addCleanup(reset_config)
        app_module.state = AppState(Path(self.tmp.name) / "test.db")
        self.addCleanup(self._teardown_state)

    def _teardown_state(self) -> None:
        if app_module.state is not None:
            app_module.state.shutdown()
        app_module.state = None

    def _load(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")
        config_module.load_config(config_file=self.path)


class TestTheRouteServesWhatTheLoaderApplied(_ConfigCase):
    """GET /api/config answers the loader's in-effect Conf, never its own
    parse of the file (the maintainer's decision, 2026-11-01)."""

    def test_a_syntax_error_answers_the_last_good_values_and_the_reason(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        self.path.write_text("{ not json", encoding="utf-8")
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200, "a broken file is not a 500 any more")
        self.assertEqual(response.json()["quality"]["target_vmaf"], 92)
        self.assertIn("not applied", response.json()["config_error"])

    def test_bad_bytes_answer_the_last_good_values_not_an_opaque_500(self) -> None:
        self._load('{"quality": {"target_vmaf": 93}}')
        self.path.write_bytes(b'{"language": "d' + b"\xc3")
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 93)
        self.assertIn("not applied", response.json()["config_error"])

    def test_a_validation_rejected_value_is_not_served_as_in_effect(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        self.path.write_text('{"quality": {"target_vmaf": "high"}}', encoding="utf-8")
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 92)
        self.assertIn("not applied", response.json()["config_error"])

    def test_a_deleted_file_keeps_the_last_good_settings(self) -> None:
        self._load('{"quality": {"target_vmaf": 91}}')
        self.path.unlink()
        config_module.refresh_config()

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 91)
        self.assertIn("not applied", response.json()["config_error"])

    def test_an_unreadable_directory_answers_last_good_and_the_reason(self) -> None:
        """Review finding 1 of #541: a directory without execute permission
        makes `path.stat()` raise PermissionError - which the old code let
        escape `refresh_config` (its stat ran outside the judging try), 500ing
        both routes and, on some interpreters, silently falling back to the
        code defaults. The judged answer: 200, last-good, the reason."""
        if os.name == "nt":
            self.skipTest("POSIX permission bits")
        self._load('{"quality": {"target_vmaf": 94}}')
        self.path.parent.chmod(0o000)
        self.addCleanup(self.path.parent.chmod, 0o755)

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 94)
        self.assertIn("not applied", response.json()["config_error"])

    def test_every_conf_attribute_round_trips_through_the_route(self) -> None:
        """Round-4's observation, made a pin: three review rounds each found
        one omitted Conf attribute (target_vmaf, the app section, the
        handbrake language filters) because nothing compared the loader's
        settings against the route's dict end to end. This test loads every
        section with distinctive values and asserts the route serves them
        all - a new Conf attribute with no pass-through fails here."""

        self._load(
            "{"
            '"file": {"extensions": [".mkv", ".mp4"], "output": {"separator": ";"}},'
            '"ffmpeg": {"black_bars": {"MIN_WIDTH": 25, "MIN_HEIGHT": 26}, "crop_sample_count": 11},'
            '"handbrake": {"preset": "P 5", "encoder": "svt_av1", "probe_encoder_preset": "8", "encoder_preset": "7",'
            '  "max_threads": 4, "stall_minutes": 12.5,'
            '  "audio_languages": ["ger"], "subtitle_languages": ["eng"]},'
            '"quality": {"target_vmaf": 93.5, "sample_every": 700, "sample_count": 4, "sample_seconds": 12},'
            '"resources": {"idle_priority": false},'
            '"app": {"auto_backlog": true, "encode_window_start": "01:00", "encode_window_end": "02:00"},'
            '"eligibility": {"min_source_height": 2160, "min_source_size_gb": 2,'
            '  "candidate_codecs": ["h264", "hevc"]},'
            '"language": "de"'
            "}"
        )
        conf = config_module.refresh_config()
        body = TestClient(fast_app).get("/api/config").json()

        self.assertEqual(body["file"]["extensions"], conf.extensions)
        self.assertEqual(body["file"]["output"]["separator"], conf.separator)
        self.assertEqual(body["ffmpeg"]["black_bars"]["MIN_WIDTH"], conf.black_bar_min_width)
        self.assertEqual(body["ffmpeg"]["black_bars"]["MIN_HEIGHT"], conf.black_bar_min_height)
        self.assertEqual(body["ffmpeg"]["crop_sample_count"], conf.crop_sample_count)
        self.assertEqual(body["handbrake"]["preset"], conf.handbrake_preset)
        self.assertEqual(body["handbrake"]["encoder"], conf.handbrake_encoder)
        self.assertEqual(
            body["handbrake"]["probe_encoder_preset"], conf.handbrake_probe_encoder_preset
        )
        self.assertEqual(body["handbrake"]["encoder_preset"], conf.handbrake_encoder_preset)
        self.assertEqual(body["handbrake"]["max_threads"], conf.handbrake_max_threads)
        self.assertEqual(body["handbrake"]["stall_minutes"], conf.handbrake_stall_minutes)
        self.assertEqual(body["handbrake"]["audio_languages"], conf.handbrake_audio_languages)
        self.assertEqual(body["handbrake"]["subtitle_languages"], conf.handbrake_subtitle_languages)
        self.assertEqual(body["quality"]["target_vmaf"], conf.target_vmaf)
        self.assertEqual(body["quality"]["sample_every"], conf.sample_every)
        self.assertEqual(body["quality"]["sample_count"], conf.sample_count)
        self.assertEqual(body["quality"]["sample_seconds"], conf.sample_seconds)
        self.assertEqual(body["resources"]["idle_priority"], conf.idle_priority)
        self.assertEqual(body["app"]["auto_backlog"], conf.auto_backlog)
        self.assertEqual(body["app"]["encode_window_start"], conf.encode_window_start)
        self.assertEqual(body["app"]["encode_window_end"], conf.encode_window_end)
        self.assertEqual(body["eligibility"]["min_source_height"], conf.min_source_height)
        self.assertEqual(body["eligibility"]["min_source_width"], conf.min_source_width)
        self.assertEqual(body["eligibility"]["min_source_size_gb"], conf.min_source_size_gb)
        self.assertEqual(body["eligibility"]["candidate_codecs"], conf.candidate_codecs)
        self.assertEqual(body["language"], conf.language)

    def test_the_handbrake_language_filters_ride_the_route(self) -> None:
        """Round-3 finding 1: _conf_to_dict omitted the handbrake audio/subtitle
        language filters, so the route served [] while the loader held the
        configured filters - a regression against the pre-one-reader answer,
        which mirrored the file."""
        self._load(
            '{"handbrake": {"audio_languages": ["ger", "deu"], "subtitle_languages": ["eng"]}}'
        )

        body = TestClient(fast_app).get("/api/config").json()
        self.assertEqual(body["handbrake"]["audio_languages"], ["ger", "deu"])
        self.assertEqual(body["handbrake"]["subtitle_languages"], ["eng"])

    def test_the_app_section_is_served_not_the_schema_defaults(self) -> None:
        """Round-2 finding 1: the merge brought the app section (#434's encode
        window, auto_backlog) and _conf_to_dict did not pass it, so the route
        served schema defaults for fields the loader holds real values for."""
        self._load(
            '{"app": {"auto_backlog": true, "encode_window_start": "01:00", '
            '"encode_window_end": "02:00"}}'
        )

        body = TestClient(fast_app).get("/api/config").json()
        self.assertEqual(body["app"]["auto_backlog"], True)
        self.assertEqual(body["app"]["encode_window_start"], "01:00")
        self.assertEqual(body["app"]["encode_window_end"], "02:00")

    def test_the_dashboard_poll_judges_a_broken_edit(self) -> None:
        """Round-3 finding: nothing pinned that /api/snapshot - the poll the
        dashboard actually makes - triggers refresh_config(); removing the call
        left every test green. The pin: a broken edit surfaces in the NEXT
        snapshot's status block with no watcher pass in between."""
        self._load('{"quality": {"target_vmaf": 92}}')
        client = TestClient(fast_app)
        self.assertIsNone(client.get("/api/snapshot").json()["status"]["config_error"])

        self.path.write_text("{ broken", encoding="utf-8")
        body = client.get("/api/snapshot").json()
        self.assertIsNotNone(
            body["status"]["config_error"], "snapshot must refresh the config itself"
        )
        self.assertIn("not applied", body["status"]["config_error"])

    def test_a_valid_edit_shows_the_new_values_after_the_refresh(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        # a DIFFERENT LENGTH so the file signature changes (same-size writes
        # inside one mtime tick are the loader's own business, not this test's)
        self.path.write_text('{"quality": {"target_vmaf": 95.5}}', encoding="utf-8")
        config_module.refresh_config()

        body = TestClient(fast_app).get("/api/config").json()
        self.assertEqual(body["quality"]["target_vmaf"], 95.5)
        self.assertIsNone(body["config_error"])

    def test_the_route_serves_a_config_it_could_not_have_read(self) -> None:
        """The one-reader rule, pinned at the filesystem: the loader has the
        settings in memory, then the file becomes unreadable. A route that
        parses the file answers 500 or the defaults; the loader's answer is 200
        with the in-effect value."""
        if os.name == "nt":
            self.skipTest("POSIX permission bits")
        self._load('{"quality": {"target_vmaf": 94}}')
        self.path.chmod(0o000)
        self.addCleanup(self.path.chmod, 0o644)

        response = TestClient(fast_app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["target_vmaf"], 94)


class TestTheStatusDetectsABrokenEdit(_ConfigCase):
    """/api/status triggers refresh_config(), so a broken edit is reported
    within one poll (seconds), not one watcher pass (up to 4 min)."""

    def test_a_broken_edit_surfaces_on_the_next_status_call(self) -> None:
        self._load('{"quality": {"target_vmaf": 92}}')
        client = TestClient(fast_app)
        self.assertIsNone(client.get("/api/status").json()["config_error"])

        self.path.write_text("{ broken", encoding="utf-8")
        body = client.get("/api/status").json()
        self.assertIsNotNone(body["config_error"], "status must refresh the config itself")
        self.assertIn("not applied", body["config_error"])


if __name__ == "__main__":
    unittest.main()
