"""`shrinkray setup`, the terminal UI (issue #252), driven headless.

Textual's pilot runs the real app without a terminal; the suite has no async
test plugin, so each test drives it through asyncio.run from a sync method.
"""

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual.pilot import Pilot
from textual.widgets import Input, Static, Switch

from shrinkray.engine.pause import operator_paused
from shrinkray.tui import SetupApp


def run(app: SetupApp, steps: Callable[[Pilot[None]], Awaitable[None]]) -> None:
    """Runs `steps` against the app in a headless terminal big enough to
    show every field."""

    async def main() -> None:
        async with app.run_test(size=(140, 60)) as pilot:
            await steps(pilot)

    asyncio.run(main())


class TestSetupApp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.config = self.data / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "handbrake": {"max_threads": 4},
                    "language": "de",  # a key the UI does not show
                    "codec": {
                        "hdr": {"color_space": ["bt2020nc"], "color_transfer": ["smpte2084"]}
                    },
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.config, 0o640)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _saved(self) -> dict[str, Any]:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def _set(self, app: SetupApp, widget_id: str, value: str) -> None:
        app.query_one(f"#{widget_id}", Input).value = value

    def test_it_shows_the_file_and_the_defaults(self) -> None:
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self.assertEqual(app.query_one("#f-handbrake-max_threads", Input).value, "4")
            self.assertEqual(app.query_one("#f-quality-target_vmaf", Input).value, "96.0")
            self.assertEqual(app.query_one("#f-eligibility-min_source_height", Input).value, "1080")

        run(app, steps)

    def test_the_language_filters_are_editable(self) -> None:
        """Issue #337: the TUI is the operator's only control surface, but the
        language filters (which decide which audio/subtitle tracks survive an
        encode — issues #330/#297) had no editor. They render and save like
        the other list fields; empty means keep every track.
        """
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            # the defaults render (the config's lists, comma-separated)
            self.assertEqual(
                app.query_one("#f-handbrake-audio_languages", Input).value, "deu, eng, und, jpn"
            )
            # edit + save
            self._set(app, "f-handbrake-audio_languages", "deu, eng")
            self._set(app, "f-handbrake-subtitle_languages", "")
            await pilot.press("ctrl+s")

        run(app, steps)
        saved = self._saved()
        self.assertEqual(saved["handbrake"]["audio_languages"], ["deu", "eng"])
        # empty list = keep every track (the documented opt-out), and the
        # key IS written ("empty" must not mean "leave the old filter")
        self.assertEqual(saved["handbrake"]["subtitle_languages"], [])

    def test_a_saved_change_keeps_every_other_key(self) -> None:
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self._set(app, "f-handbrake-max_threads", "6")
            self._set(app, "f-eligibility-candidate_codecs", "mpeg4, vc1")
            await pilot.press("ctrl+s")
            self.assertIn("Saved", str(app.query_one("#message", Static).render()))

        run(app, steps)
        saved = self._saved()
        self.assertEqual(saved["handbrake"]["max_threads"], 6)
        self.assertEqual(saved["eligibility"]["candidate_codecs"], ["mpeg4", "vc1"])
        self.assertEqual(saved["language"], "de")
        self.assertEqual(saved["codec"]["hdr"]["color_space"], ["bt2020nc"])
        # untouched defaults are not written into the file
        self.assertNotIn("quality", saved)
        if os.name != "nt":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["config.json"])

    def test_a_value_the_app_would_refuse_is_never_written(self) -> None:
        """The same schema as a loaded file: the 1080p floor is raise-only."""
        before = self.config.read_text(encoding="utf-8")
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self._set(app, "f-eligibility-min_source_height", "720")
            await pilot.press("ctrl+s")
            message = str(app.query_one("#message", Static).render())
            self.assertIn("Not saved", message)
            self.assertIn("1080p floor", message)

        run(app, steps)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_text_that_is_not_a_number_names_the_field(self) -> None:
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self._set(app, "f-handbrake-max_threads", "four")
            await pilot.press("ctrl+s")
            self.assertIn("Thread cap per encode", str(app.query_one("#message", Static).render()))

        run(app, steps)
        self.assertEqual(self._saved()["handbrake"]["max_threads"], 4)

    def test_a_change_that_re_measures_the_library_says_so_first(self) -> None:
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            warning = app.query_one("#warning", Static)
            self._set(app, "f-handbrake-max_threads", "8")
            await pilot.pause()
            self.assertEqual(str(warning.render()), "", "a thread cap re-measures nothing")
            self._set(app, "f-quality-target_vmaf", "95")
            await pilot.pause()
            self.assertIn("re-measures the whole library", str(warning.render()))
            self.assertIn("Required VMAF", str(warning.render()))

        run(app, steps)

    def test_discard_restores_the_file_values(self) -> None:
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self._set(app, "f-handbrake-max_threads", "12")
            app.query_one("#f-app-auto_backlog", Switch).value = True
            await pilot.click("#discard")
            self.assertEqual(app.query_one("#f-handbrake-max_threads", Input).value, "4")
            self.assertFalse(app.query_one("#f-app-auto_backlog", Switch).value)

        run(app, steps)

    def test_the_pause_switch_drives_the_flag(self) -> None:
        app = SetupApp(self.config, self.data)

        async def steps(pilot: Pilot[None]) -> None:
            await pilot.click("#pause")
            self.assertTrue(operator_paused(self.data))
            self.assertIn("Paused", str(app.query_one("#pause-status", Static).render()))
            # a second click within the button's press animation is ignored
            # by Textual, so the resume goes through the same action directly
            app.action_toggle_pause()
            self.assertFalse(operator_paused(self.data))
            self.assertIn("Running", str(app.query_one("#pause-status", Static).render()))

        run(app, steps)

    def test_without_a_data_folder_there_is_no_pause_switch(self) -> None:
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self.assertEqual(len(app.query("#pause")), 0)

        run(app, steps)

    def test_a_missing_config_file_is_created_on_save(self) -> None:
        self.config.unlink()
        app = SetupApp(self.config, None)

        async def steps(pilot: Pilot[None]) -> None:
            self._set(app, "f-handbrake-max_threads", "2")
            await pilot.press("ctrl+s")

        run(app, steps)
        self.assertEqual(self._saved(), {"handbrake": {"max_threads": 2}})


class TestSetupCommand(unittest.TestCase):
    def test_it_refuses_to_run_without_a_terminal(self) -> None:
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        result = CliRunner().invoke(app, ["setup"])
        self.assertEqual(result.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
