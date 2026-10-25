"""HTML report tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import json
import tempfile
import unittest
from pathlib import Path

from shrinkray import db


class TestReport(unittest.TestCase):
    def test_generate_report(self):

        from shrinkray.report import generate_report

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            connection = db.connect(db_path)
            file_id = connection.execute(
                "INSERT INTO files (path, mtime, size, name, category, duration, codec, width, height)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("D:/Movies/a.mkv", 1.0, 1000, "a", "Movies", "01:00:00", "h264", 1920, 1080),
            ).lastrowid
            connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
                " saved_percent, chosen_crf, achieved_vmaf)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, "VMAF>95.0", "x265", 1000, 400, 60.0, 20, 95.4),
            )
            connection.commit()
            connection.close()

            out = Path(tmp) / "report.html"
            generate_report(db_path, out)
            html = out.read_text(encoding="utf-8")
            self.assertIn("ShrinkRay Report", html)
            self.assertIn("a.mkv", html)
            self.assertIn("95.40", html)
            # the skipped-preset rendering
            self.assertIn("quality mode (VMAF&gt;95.0)", html)


class TestReportCategory(unittest.TestCase):
    def test_generate_report_category_breakdown(self):

        from shrinkray.report import generate_report

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            connection = db.connect(db_path)

            def add_file(name, category):
                return connection.execute(
                    "INSERT INTO files (path, mtime, size, name, category, duration, codec, width, height)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"D:/Movies/{name}",
                        1.0,
                        1000,
                        name,
                        category,
                        "01:00:00",
                        "h264",
                        1920,
                        1080,
                    ),
                ).lastrowid

            gb = 1024**3
            # category "Movies": one encode, one skip
            movies_encode = add_file("m1.mkv", "Movies")
            movies_skip = add_file("m2.mkv", "Movies")
            # category "Series": one skip, one never processed (no encode row)
            series_skip = add_file("s1.mkv", "Series")
            add_file("s2.mkv", "Series")

            connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
                " saved_percent, chosen_crf, achieved_vmaf)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (movies_encode, "VMAF>95.0", "x265", 3 * gb, int(1.5 * gb), 50.0, 20, 95.4),
            )
            connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
                " saved_percent, chosen_crf, achieved_vmaf)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (movies_skip, "skipped (too short)", "x265", gb, gb, 0.0, None, None),
            )
            connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
                " saved_percent, chosen_crf, achieved_vmaf)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (series_skip, "skipped (below minimum)", "x265", gb, gb, 0.0, None, None),
            )
            connection.commit()
            connection.close()

            out = Path(tmp) / "report.html"
            generate_report(db_path, out)
            html = out.read_text(encoding="utf-8")

            # the category breakdown sits between the summary cards and the movie table
            self.assertLess(html.index("Movies in database"), html.index("<th>Category</th>"))
            self.assertLess(html.index("<th>Category</th>"), html.index("<th>Movie</th>"))

            # "Movies": 2 movies, 1 encoded, 1 skipped
            self.assertIn(
                "<td>Movies</td><td class='num'>2</td><td class='num'>1</td><td class='num'>1</td>",
                html,
            )
            # "Series": 2 movies, 0 encoded, 1 skipped, and no encodes => '—' saved
            self.assertIn(
                "<td>Series</td><td class='num'>2</td><td class='num'>0</td><td class='num'>1</td>"
                "<td class='num'>1.00 GB</td><td class='num'>—</td>",
                html,
            )


class TestReportVerification(unittest.TestCase):
    """The Verified column added with the full-file verification (B5)."""

    def test_verified_vmaf_rendered_with_subsample_divisor(self):
        """A verified encode shows the verified VMAF and the 1/n divisor;
        an unverified encode shows the em-dash placeholder."""

        from shrinkray.report import generate_report

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            connection = db.connect(db_path)
            file_id = connection.execute(
                "INSERT INTO files (path, mtime, size, name, category, duration, codec, width, height)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("D:/Movies/v.mkv", 1.0, 1000, "v", "Movies", "01:00:00", "h264", 1920, 1080),
            ).lastrowid
            connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
                " saved_percent, chosen_crf, achieved_vmaf, verified_vmaf, verified_subsample)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, "VMAF>95.0", "x265", 1000, 400, 60.0, 20, 95.4, 95.1, 10),
            )
            connection.commit()
            connection.close()

            out = Path(tmp) / "report.html"
            generate_report(db_path, out)
            html = out.read_text(encoding="utf-8")
            self.assertIn("<th class='num'>Verified</th>", html)
            self.assertIn("95.10", html)
            self.assertIn("(1/10)", html)

    def test_unverified_encode_placeholder(self):
        """An encode without verification renders '—' in the Verified column
        (pre-v8 rows have NULL there)."""

        from shrinkray.report import generate_report

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            connection = db.connect(db_path)
            file_id = connection.execute(
                "INSERT INTO files (path, mtime, size, name, category, duration, codec, width, height)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("D:/Movies/u.mkv", 1.0, 1000, "u", "Movies", "01:00:00", "h264", 1920, 1080),
            ).lastrowid
            connection.execute(
                "INSERT INTO encodes (file_id, preset, encoder, input_size, output_size,"
                " saved_percent, chosen_crf, achieved_vmaf)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, "VMAF>95.0", "x265", 1000, 400, 60.0, 20, 95.4),
            )
            connection.commit()
            connection.close()

            out = Path(tmp) / "report.html"
            generate_report(db_path, out)
            html = out.read_text(encoding="utf-8")
            self.assertIn("<th class='num'>Verified</th>", html)
            # the placeholder directly follows the achieved-VMAF cell
            self.assertIn("<td class='num'>95.40</td><td class='num'>—</td>", html)


class TestReportCli(unittest.TestCase):
    """The report command's CLI surface."""

    def test_report_writes_html_and_exits_zero(self):
        """A healthy database produces the report file and a success note."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            connection = db.connect(db_path)
            connection.execute(
                "INSERT INTO files (path, mtime, size, name, category, duration, codec, width, height)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("D:/Movies/a.mkv", 1.0, 1000, "a", "Movies", "01:00:00", "h264", 1920, 1080),
            )
            connection.commit()
            connection.close()

            out = Path(tmp) / "out" / "report.html"
            runner = CliRunner()
            result = runner.invoke(app, ["report", "--db", str(db_path), "--output", str(out)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(out.is_file())
            html = out.read_text(encoding="utf-8")
            self.assertIn("ShrinkRay Report", html)
            self.assertIn("a.mkv", html)

    def test_report_missing_db_exits_one(self):
        """A missing database is a clean error with exit 1 (the message goes
        through the logger, which the CliRunner does not capture)."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            runner = CliRunner()
            result = runner.invoke(
                app,
                [
                    "report",
                    "--db",
                    str(Path(tmp) / "nope.db"),
                    "--output",
                    str(Path(tmp) / "r.html"),
                ],
            )
            self.assertEqual(result.exit_code, 1)
            self.assertFalse((Path(tmp) / "r.html").exists())


if __name__ == "__main__":
    unittest.main()


class TestBarMeasurementReconciliation(unittest.TestCase):
    """Issue #131: the scan's flag and the encode's crop are two measurements.

    The decision (docs/black-bar-spike.md) keeps them separate: the flag is a cheap
    pre-filter, the rectangle is the only value that reaches an encode. They are not
    supposed to agree, so a file flagged "has bars" that yields no crop must be
    EXPLAINED in the report rather than look like a bug.
    """

    def test_a_flagged_file_with_no_crop_is_explained(self) -> None:
        from shrinkray.report import _bars_note

        note = _bars_note(1, None)
        self.assertIn("bars flagged by scan", note)
        self.assertIn("pre-filter", note)

    def test_a_flagged_file_with_a_crop_needs_no_note(self) -> None:
        from shrinkray.report import _bars_note

        self.assertEqual(_bars_note(1, "20,20,0,0"), "")

    def test_an_unflagged_file_needs_no_note(self) -> None:
        from shrinkray.report import _bars_note

        self.assertEqual(_bars_note(0, None), "")

    def test_a_file_with_no_scan_data_needs_no_note(self) -> None:
        from shrinkray.report import _bars_note

        self.assertEqual(_bars_note(None, None), "")

    def test_a_cached_timeline_summary_replaces_the_prefilter_wording(self) -> None:
        """Issue #181: the sustained-ratio table explains a flag-without-crop."""
        from shrinkray.report import _bars_note

        ratios = json.dumps([[[1920, 800, 0, 140], 0.6], [[1920, 1080, 0, 0], 0.4]])
        note = _bars_note(1, None, ratios)
        self.assertIn("variable picture size", note)
        self.assertIn("1920x800 (60% of the time)", note)
        self.assertIn("1920x1080 (40% of the time)", note)

    def test_a_garbled_ratio_table_falls_back_to_the_prefilter_wording(self) -> None:
        from shrinkray.report import _bars_note

        note = _bars_note(1, None, "not json")
        self.assertIn("pre-filter", note)
        # a table with no entries also falls back
        self.assertIn("pre-filter", _bars_note(1, None, "[]"))
