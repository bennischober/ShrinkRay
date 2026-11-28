"""The scan asks the completeness witness before its own ffprobe (issue #738).

The scan is the FIRST probe every path runs: a file a copy is still writing
was ffprobed torn on every pass - the exact cost #437's witness exists to
prevent. An unsettled file is catalogued from the stat() alone; its row
carries no duration, so `db.is_unchanged` treats it as changed and the next
pass re-probes it - the nulls can never freeze behind a mtime+size match.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db
from shrinkray.config import Conf
from shrinkray.engine.scan import scan_movies


def _probe_data(name: str) -> dict:
    """A real-ish get_data shape for the settled file's probe."""
    return {
        "codec": "h264",
        "other_codecs": [],
        "bitrate": 12345,
        "duration": "00:00:10",
        "duration_raw": 10.0,
        "fps": 25,
        "aspect_ratio": "16x9",
        "dimensions": {"width": 1920, "height": 1080},
        "audio_languages": "",
        "hdr": 0,
        "bit_depth": 8,
    }


class TestScanAsksTheWitness(unittest.TestCase):
    """#738: an unsettled file is catalogued stat-only, never ffprobed, and
    re-probed on the next pass once it settles."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        self.connection = db.connect(Path(self.tmp.name) / "metadata.db")
        self.addCleanup(self.connection.close)

    def test_an_unsettled_file_is_catalogued_without_a_probe(self) -> None:
        """The young fixture never reaches the probe seam; the catalogue row
        is stat-only (no duration) and the file stays in the scan's result."""
        young = self.root / "Movies" / "young.mkv"
        young.parent.mkdir()
        young.write_bytes(b"x" * 64)
        with (
            patch("shrinkray.engine.scan.get_data") as probe,
            # the young verdict by the frozen clock, not wall-clock luck: a
            # loaded runner may sit between the fixture write and the stat()
            patch("shrinkray.engine.watch.time.time", return_value=young.stat().st_mtime + 5.0),
        ):
            by_category, files, errors = scan_movies(
                self.root,
                Conf(),
                workers=1,
                connection=self.connection,
                files=[young],
                walked=True,
            )
        probe.assert_not_called()
        self.assertEqual(errors, [])
        self.assertEqual([str(f) for f in files], [str(young)])
        result = by_category["Movies"][0]
        self.assertIsNone(result["duration"])
        self.assertIsNone(result["codec"])
        self.assertIsNone(result["crop"])
        self.assertIsNotNone(result["size"])
        row = db.get_file_by_path(self.connection, young)
        self.assertIsNotNone(row, "the stat-only row was not written")
        self.assertIsNone(row["duration_raw"])
        self.assertEqual(row["mtime"], young.stat().st_mtime)
        self.assertEqual(row["size"], young.stat().st_size)

    def test_the_stat_only_row_is_reprobed_once_settled(self) -> None:
        """The freeze pin: without the metadata rule in is_unchanged, the
        second scan would take the null row from the cache and the file
        would never be probed again. Settled, the re-probe fills the row."""
        young = self.root / "Movies" / "young.mkv"
        young.parent.mkdir()
        young.write_bytes(b"x" * 64)
        with (
            patch(
                "shrinkray.engine.scan.get_data",
                side_effect=lambda p, c: _probe_data(p.name),
            ) as probe,
            patch("shrinkray.engine.watch.time.time", return_value=young.stat().st_mtime + 5.0),
        ):
            scan_movies(
                self.root,
                Conf(),
                workers=1,
                connection=self.connection,
                files=[young],
                walked=True,
            )
        probe.assert_not_called()
        row = db.get_file_by_path(self.connection, young)
        self.assertIsNone(row["duration_raw"])
        os.utime(young, (0, 0))  # settle it
        with patch(
            "shrinkray.engine.scan.get_data",
            side_effect=lambda p, c: _probe_data(p.name),
        ) as probe:
            scan_movies(
                self.root,
                Conf(),
                workers=1,
                connection=self.connection,
                files=[young],
                walked=True,
            )
        probe.assert_called_once()
        row = db.get_file_by_path(self.connection, young)
        self.assertEqual(row["duration_raw"], 10.0)
        self.assertEqual(row["codec"], "h264")

    def test_a_settled_file_probes_normally(self) -> None:
        """The witness never holds back a settled file: the probe runs on the
        first pass."""
        settled = self.root / "Movies" / "old.mkv"
        settled.parent.mkdir()
        settled.write_bytes(b"x" * 64)
        os.utime(settled, (0, 0))
        with patch(
            "shrinkray.engine.scan.get_data",
            side_effect=lambda p, c: _probe_data(p.name),
        ) as probe:
            scan_movies(
                self.root,
                Conf(),
                workers=1,
                connection=self.connection,
                files=[settled],
                walked=True,
            )
        probe.assert_called_once()
        row = db.get_file_by_path(self.connection, settled)
        self.assertEqual(row["duration_raw"], 10.0)


if __name__ == "__main__":
    unittest.main()
