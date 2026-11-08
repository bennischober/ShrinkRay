"""plan asks the completeness witness before probing (issue #587).

`plan` probed and recorded whatever was on disk: run during a copy into the
library, it predicted from the torn file, and a stalled sparse download -
which keeps its identity - kept that verdict in the plan table and on the
dashboard. plan now asks the same witness `encode` asks (`check_stability`,
#479) per file, before any gate or sample. A young file or a stalled sparse
download is reported at once (plan is a prediction command, not a waiter),
with the witness's reason, and is neither probed nor recorded; the file stays
eligible and a later run decides it again.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shrinkray.__main__ import app
from shrinkray.engine.plan import QualityResult
from shrinkray.engine.watch import STABLE_MIN_AGE, check_stability

MIB = 1024 * 1024


@contextmanager
def _toolchain_present():
    """The plan preflight (#194) checks the toolchain before any work; these
    tests mock the engine itself, so the check must see a working one
    (CI runners have no ffprobe on PATH)."""
    with (
        patch("shrinkray.service.shutil.which", return_value="ffprobe"),
        patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
        patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
        patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value="modern"),
    ):
        yield


def _plan_result(movie: Path, *, success: bool = False, skipped: str | None = None) -> QualityResult:
    return QualityResult(
        input=movie,
        output=movie,
        success=success,
        skipped_reason=skipped,
        input_size=movie.stat().st_size,
        output_size=movie.stat().st_size,
    )


class TestPlanAsksTheWitness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # a file a copy is still writing: fresh mtime
        self.young = self.root / "young.mkv"
        self.young.write_bytes(b"x" * 64)
        # a stalled sparse download: full size from the start, almost nothing
        # allocated, and old enough that the age check alone cannot save it
        self.sparse = self.root / "sparse.mkv"
        self.sparse.write_bytes(b"x" * 4096)
        os.truncate(self.sparse, 4 * MIB)
        os.utime(self.sparse, (0, 0))
        # a settled file: old and fully allocated
        self.settled = self.root / "settled.mkv"
        self.settled.write_bytes(b"x" * 64)
        os.utime(self.settled, (0, 0))


    def _scan_cataloguing(self, *movies: Path):
        """A faked scan that writes each movie's file row exactly as a real
        library scan does, so record_plan has a row to key on (the same
        seam test_plan_no_table fakes: plans are keyed by the scanned
        library, and these tests pass files explicitly)."""

        def fake_scan(root_dir, config, **kwargs):
            from shrinkray import db

            connection = kwargs.get("connection")
            if connection is not None:
                for m in movies:
                    stat = m.stat()
                    db.upsert_file(
                        connection,
                        db.MovieRecord(
                            path=m,
                            mtime=stat.st_mtime,
                            size=stat.st_size,
                            name=m.stem,
                            category="movies",
                        ),
                    )
            return {}, list(movies), []

        return fake_scan


    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str):
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            return runner.invoke(app, list(argv))
        finally:
            os.chdir(previous)

    def test_the_fixtures_say_what_their_names_say(self) -> None:
        """The test's own guard: the witness must read the three files as
        young, sparse and settled, or the assertions below prove nothing."""
        young = check_stability(self.young)
        self.assertGreater(young.young_for, 0)
        sparse = check_stability(self.sparse)
        self.assertEqual(sparse.young_for, 0.0)
        self.assertIsNotNone(sparse.incomplete, "the sparse fixture lost its holes")
        self.assertTrue(check_stability(self.settled).settled)

    def test_a_young_file_is_not_probed_or_recorded(self) -> None:
        """The prediction command reports a copy in progress at once instead
        of predicting from the torn file - and stores no plan row for it."""
        probed: list[Path] = []
        db_file = self.root / "plans.db"

        def fake_plan(movie, **kwargs):
            probed.append(movie.name)
            return _plan_result(movie, skipped="too short")

        with (
            _toolchain_present(),
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
            patch("shrinkray.__main__.scan_movies", side_effect=self._scan_cataloguing(self.young)),
        ):
            result = self._run("plan", str(self.young), "--db", str(db_file))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(probed, [], "the young file was probed")
        self.assertIn("last write", result.output)
        self.assertIn(f"needs {STABLE_MIN_AGE:g} s", result.output)
        connection = sqlite3.connect(db_file)
        try:
            planned = connection.execute(
                "SELECT COUNT(*) FROM plans JOIN files ON plans.file_id = files.id"
                " WHERE files.path LIKE '%young.mkv'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(planned, 0, "a plan row was recorded for a young file")

    def test_a_sparse_download_is_not_probed_or_recorded(self) -> None:
        """A stalled download keeps its identity, so a plan recorded from it
        would be reused until the lease of the file changes: none is."""
        probed: list[Path] = []
        db_file = self.root / "plans.db"

        def fake_plan(movie, **kwargs):
            probed.append(movie.name)
            return _plan_result(movie, skipped="too short")

        with (
            _toolchain_present(),
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
            patch("shrinkray.__main__.scan_movies", side_effect=self._scan_cataloguing(self.sparse)),
        ):
            result = self._run("plan", str(self.sparse), "--db", str(db_file))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(probed, [], "the sparse file was probed")
        self.assertIn("appears incomplete", result.output)
        self.assertIn("allocated", result.output)
        connection = sqlite3.connect(db_file)
        try:
            planned = connection.execute(
                "SELECT COUNT(*) FROM plans JOIN files ON plans.file_id = files.id"
                " WHERE files.path LIKE '%sparse.mkv'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(planned, 0, "a plan row was recorded for a sparse file")

    def test_a_settled_file_is_planned_and_recorded_as_before(self) -> None:
        """The witness costs a settled file nothing: it is probed once and
        its verdict recorded, exactly as before the fix."""
        probed: list[Path] = []
        db_file = self.root / "plans.db"

        def fake_plan(movie, **kwargs):
            probed.append(movie.name)
            return _plan_result(movie, skipped="too short")

        with (
            _toolchain_present(),
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
            patch(
                "shrinkray.__main__.scan_movies",
                side_effect=self._scan_cataloguing(self.settled),
            ),
        ):
            result = self._run("plan", str(self.settled), "--db", str(db_file))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(probed, ["settled.mkv"])
        self.assertTrue(db_file.exists())
        connection = sqlite3.connect(db_file)
        try:
            planned = connection.execute(
                "SELECT COUNT(*) FROM plans JOIN files ON plans.file_id = files.id"
                " WHERE files.path LIKE '%settled.mkv'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(planned, 1, "the settled file's plan was not recorded")

    def test_a_mixed_library_plans_only_its_settled_files(self) -> None:
        """One folder, three files: the wait rows and the plan row must sit
        in the same run, and only the settled file reaches the engine."""
        probed: list[Path] = []
        db_file = self.root / "plans.db"

        def fake_plan(movie, **kwargs):
            probed.append(movie.name)
            return _plan_result(movie, skipped="too short")

        with (
            _toolchain_present(),
            patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
            patch(
                "shrinkray.__main__.scan_movies",
                side_effect=self._scan_cataloguing(self.young, self.sparse, self.settled),
            ),
        ):
            result = self._run("plan", str(self.root), "--db", str(db_file))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(probed, ["settled.mkv"])
        self.assertIn("young.mkv", result.output)
        self.assertIn("sparse.mkv", result.output)
        self.assertIn("wait", result.output)
        connection = sqlite3.connect(db_file)
        try:
            planned = connection.execute(
                "SELECT COUNT(*) FROM plans JOIN files ON plans.file_id = files.id"
                " WHERE files.path LIKE '%young.mkv' OR files.path LIKE '%sparse.mkv'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(planned, 0, "an unsettled file got a plan row")


if __name__ == "__main__":
    unittest.main()
