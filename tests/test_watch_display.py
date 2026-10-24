"""Tests for the watch mode's live display (issue #178).

The display is a renderer over the engine's progress events: the callback
updates a shared state dict, the Live panel renders it, and --no-live (or a
non-TTY) skips the panel entirely. No real TTY is needed — the tests assert
the state machine and the wiring, not the pixels.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray.__main__ import _watch_display, _watch_state


class TestWatchDisplay(unittest.TestCase):
    def setUp(self) -> None:
        _watch_state.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        _watch_state.clear()
        self._tmp.cleanup()

    def test_encode_events_update_the_status(self) -> None:
        """An encode event names the movie and its percent (issue #178)."""
        _live, callback = _watch_display([self.root], None, 60)
        callback({"stage": "encode", "percent": 42.0, "movie": "film.mkv"})
        self.assertEqual(_watch_state["status"], "encoding 'film.mkv' at 42%")
        self.assertEqual(_watch_state["percent"], 42.0)

    def test_probe_events_set_the_detail_and_clear_the_percent(self) -> None:
        """A probe event shows the detail; the percent resets (no stale bar)."""
        _live, callback = _watch_display([self.root], None, 60)
        callback({"stage": "encode", "percent": 42.0, "movie": "film.mkv"})
        callback({"stage": "probe", "detail": "sample 2/10"})
        self.assertEqual(_watch_state["status"], "probing: sample 2/10")
        self.assertIsNone(_watch_state["percent"])

    def test_the_render_callable_reads_the_state(self) -> None:
        """The render callable passed to Live reads the shared state dict —
        the same dict the callback writes, which is the whole wiring."""
        _live, callback = _watch_display([self.root], None, 60)
        callback({"stage": "encode", "percent": 7.0, "movie": "x.mkv"})
        self.assertEqual(_watch_state["status"], "encoding 'x.mkv' at 7%")
        self.assertEqual(_watch_state["percent"], 7.0)


if __name__ == "__main__":
    unittest.main()
