"""plan --no-table: the scripted read-only command without rich rendering
(issue #201). scan already has the flag; plan is the other scripted,
parsed command, so it gets the same one.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app
from shrinkray.engine.plan import QualityResult


def _encode_plan(movie: Path) -> QualityResult:
    return QualityResult(
        input=movie,
        output=movie,
        success=True,
        chosen_crf=22,
        achieved_vmaf=95.2,
        input_size=movie.stat().st_size,
        output_size=movie.stat().st_size // 2,
    )


class TestPlanNoTable(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movie = self.root / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> str:
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            result = runner.invoke(app, list(argv))
            return result.output
        finally:
            os.chdir(previous)

    def test_plan_no_table_prints_no_table_but_the_summary(self) -> None:
        with patch(
            "shrinkray.engine.plan.plan_quality", side_effect=lambda m, **k: _encode_plan(m)
        ):
            output = self._run("plan", str(self.movie), "--no-table")
        self.assertNotIn("┃", output, "--no-table still rendered the rich table")
        self.assertNotIn("─", output, "--no-table still rendered box-drawing characters")
        self.assertIn("estimated total", output, "the summary line vanished with the table")

    def test_plan_without_the_flag_prints_the_table(self) -> None:
        with patch(
            "shrinkray.engine.plan.plan_quality", side_effect=lambda m, **k: _encode_plan(m)
        ):
            output = self._run("plan", str(self.movie))
        self.assertIn("Encode plan", output, "the default lost its table")

    def test_plan_no_table_still_persists_the_decisions(self) -> None:
        """The table is rendering, not the record: --db keeps working.

        record_plan keys on the scanned file row, so the faked scan step
        writes the row exactly as the real scan does.
        """
        db_file = self.root / "plans.db"

        def fake_scan(root_dir, config, **kwargs):
            from shrinkray import db

            connection = kwargs.get("connection")
            if connection is not None:
                record = db.MovieRecord(
                    path=self.movie,
                    mtime=self.movie.stat().st_mtime,
                    size=self.movie.stat().st_size,
                    name="one",
                    category="one",
                )
                db.upsert_file(connection, record)
            return {}, [self.movie], []

        with (
            patch("shrinkray.engine.plan.plan_quality", side_effect=lambda m, **k: _encode_plan(m)),
            patch("shrinkray.__main__.scan_movies", side_effect=fake_scan),
        ):
            self._run("plan", str(self.movie), "--no-table", "--db", str(db_file))
        self.assertTrue(db_file.exists(), "the plan cache was not written")
        from shrinkray import db

        connection = db.connect(db_file)
        try:
            row = connection.execute("SELECT COUNT(*) FROM plans").fetchone()
        finally:
            connection.close()
        self.assertGreaterEqual(row[0], 1, "no plan decision was recorded")


if __name__ == "__main__":
    unittest.main()
