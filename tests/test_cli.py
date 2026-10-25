"""CLI command tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from shrinkray import config as config_helper
from shrinkray import db
from shrinkray.__main__ import app
from shrinkray.engine import scan


class TestMain(unittest.TestCase):
    def test_get_category(self):
        from shrinkray.__main__ import get_category

        root = Path("D:/Movies")
        # normal: root/Category/MovieFolder/file.mkv
        self.assertEqual(
            get_category(Path("D:/Movies/Action/Movie 2020/movie.mkv"), root), "Action"
        )
        # movie directly in the root
        self.assertEqual(get_category(Path("D:/Movies/movie.mkv"), root), "Uncategorized")
        # deeper nesting => still the first folder level
        self.assertEqual(get_category(Path("D:/Movies/Action/Sub/movie.mkv"), root), "Action")

    def test_get_name(self):
        from shrinkray.__main__ import get_name

        self.assertEqual(get_name(Path("D:/Movies/Action/Movie.mkv")), "Movie")
        # multi-file folder => folder name
        self.assertEqual(get_name(Path("D:/Movies/Action/movie_t0.mkv")), "Action")

    def test_find_movies_exclude(self):

        from shrinkray.__main__ import find_movies

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mkv").write_bytes(b"x")
            (root / "out").mkdir()
            (root / "out" / "b.mkv").write_bytes(b"x")

            movies = find_movies(root, [".mkv"], exclude=root / "out")
            names = [m.name for m in movies]
            self.assertIn("a.mkv", names)
            self.assertNotIn("b.mkv", names)
            # without exclusion both are found
            movies = find_movies(root, [".mkv"])
            self.assertEqual(len(movies), 2)

    def test_scan_movies_parallel_deterministic(self):

        from shrinkray.__main__ import scan_movies

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # create test files (no valid media => all error, but order is checked)
            (root / "Cat1").mkdir()
            (root / "Cat1" / "a1.mkv").write_bytes(b"x")
            (root / "Cat1" / "a2.mkv").write_bytes(b"x")
            (root / "Cat2").mkdir()
            (root / "Cat2" / "b1.mkv").write_bytes(b"x")

            config = config_helper.Conf()

            for workers in (1, 2, 8):
                _, _movies, errors = scan_movies(root, config, workers=workers)
                # all files error => error messages in deterministic file order
                self.assertEqual(len(errors), 3)
                self.assertIn("a1.mkv", errors[0])
                self.assertIn("a2.mkv", errors[1])
                self.assertIn("b1.mkv", errors[2])


class TestCompareCli(unittest.TestCase):
    """The compare command: provenance chain table (live-smoked only until
    now - these tests cover the db queries and the CLI exit codes)."""

    def setUp(self):

        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _chain(self, movie: Path):
        """Stores a two-generation encode chain for the movie (with codec
        snapshot data, as record_quality_results would write it)."""
        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(path=movie, mtime=100.0, size=1000, name="movie", category="Cat"),
        )
        gen1 = db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "x265",
            1000,
            400,
            60.0,
            chosen_crf=22,
            achieved_vmaf=95.4,
            source_codec="h264",
            source_file=str(movie),
            output_file=str(movie.parent / "enc" / movie.name),
            generation=1,
        )
        db.add_encode(
            self.connection,
            file_id,
            "VMAF>95",
            "x265",
            400,
            200,
            50.0,
            chosen_crf=24,
            achieved_vmaf=95.1,
            source_codec="hevc",
            source_file=str(movie.parent / "enc" / movie.name),
            output_file=str(movie.parent / "enc2" / movie.name),
            generation=2,
        )
        return file_id, gen1

    def test_compare_shows_chain(self):
        """A movie with a two-generation chain renders both rows."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            self._chain(movie)
            runner = CliRunner()
            result = runner.invoke(app, ["compare", str(movie), "--db", str(self.db_file)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("movie.mkv", result.output)

            # both generations appear with their CRFs
            self.assertIn("22", result.output)
            self.assertIn("24", result.output)

    def test_compare_no_encodes_exit_zero_with_hint(self):
        """A movie without encodes prints the hint and exits 0 (not an
        error - it is an expected state for a freshly scanned library)."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "plain.mkv"
            movie.write_bytes(b"x")
            runner = CliRunner()
            result = runner.invoke(app, ["compare", str(movie), "--db", str(self.db_file)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("No encodes recorded", result.output)

    def test_compare_missing_db_exits_error(self):
        """A missing database with an existing movie file: the command's
        own check fires with exit 1."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "x.mkv"
            movie.write_bytes(b"x")
            runner = CliRunner()
            result = runner.invoke(app, ["compare", str(movie), "--db", str(Path(tmp) / "nope.db")])
            # exit 1 is the contract (scripts can detect it); the message
            # goes through the logger, which the CliRunner does not capture
            self.assertEqual(result.exit_code, 1)


class TestDoctorCli(unittest.TestCase):
    """The doctor command's CLI surface (service logic tested in TestDb)."""

    def setUp(self):

        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def test_doctor_healthy_exit_zero(self):
        """A healthy library prints the all-clear and exits 0."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            db.upsert_file(
                self.connection,
                db.MovieRecord(path=movie, mtime=100.0, size=1, name="movie", category="C"),
            )
            runner = CliRunner()
            with patch("shrinkray.service.toolchain_problems", return_value=[]):
                result = runner.invoke(app, ["doctor", "--db", str(self.db_file)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("healthy", result.output)

    def test_doctor_finding_exits_one(self):
        """A dead path produces the findings table and exit 1 (scripts/
        cron can detect unhealth)."""

        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db.upsert_file(
                self.connection,
                db.MovieRecord(
                    path=root / "gone.mkv", mtime=100.0, size=1, name="gone", category="C"
                ),
            )
            runner = CliRunner()
            with patch("shrinkray.service.toolchain_problems", return_value=[]):
                result = runner.invoke(app, ["doctor", "--db", str(self.db_file)])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("dead path", result.output)
            self.assertIn("gone.mkv", result.output)


class TestReadmeInvocations(unittest.TestCase):
    """Every `shrinkray ...` invocation documented in the README must use only
    commands and options the real CLI defines (regression: the encode example
    showed --encoder, an option that does not exist - issue #32)."""

    README = Path(__file__).resolve().parent.parent / "README.md"

    def _invocations(self) -> list[list[str]]:
        """Extracts the argv lists of all shrinkray invocations in the README."""
        import shlex

        text = self.README.read_text(encoding="utf-8")
        # join backslash line continuations so a wrapped command is one line
        text = text.replace("\\\n", " ")
        invocations = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()  # strip inline comments
            if "shrinkray" not in line:
                continue
            tokens = shlex.split(line)
            if "shrinkray" not in tokens:
                continue
            argv = tokens[tokens.index("shrinkray") + 1 :]
            if argv:
                invocations.append(argv)
        return invocations

    def test_readme_options_exist(self):
        import typer

        from shrinkray.__main__ import app

        commands = typer.main.get_command(app).commands
        self.assertTrue(commands, "the CLI app exposes no commands?")
        invocations = self._invocations()
        self.assertGreaterEqual(len(invocations), 10, "the README stopped documenting invocations?")
        for argv in invocations:
            command_name = argv[0]
            self.assertIn(
                command_name,
                commands,
                f"README documents a command the CLI does not define: {command_name}",
            )
            known = {
                opt for param in commands[command_name].params for opt in getattr(param, "opts", [])
            }
            for token in argv[1:]:
                if not token.startswith("--"):
                    continue
                flag = token.split("=", 1)[0]
                self.assertIn(
                    flag,
                    known,
                    f"README documents an option the CLI does not define: "
                    f"{flag} (on '{command_name}')",
                )


if __name__ == "__main__":
    unittest.main()


class TestInputShapes(unittest.TestCase):
    """Issue #123: the Mode A contract is "a file, a folder, or a glob".

    All three shapes must resolve to the same movie list, because the one-shot
    mode exists for the single stubborn file and a quoted glob arrives
    unexpanded on Windows. Real tempfile directories throughout: a fictional
    path is absolute on Windows and relative on Linux, so resolve() differs.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movies = self.root / "movies"
        self.nested = self.movies / "Matrix"
        self.nested.mkdir(parents=True)
        self.one = self.nested / "one.mkv"
        self.one.write_bytes(b"x")
        self.two = self.movies / "two.mp4"
        self.two.write_bytes(b"y")
        (self.nested / "poster.jpg").write_bytes(b"z")
        self.extensions = [".mkv", ".mp4"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_single_file_is_one_movie(self) -> None:
        """The case the one-shot mode exists for: one stubborn file."""
        _root, found = scan.resolve_input_paths(str(self.one), self.extensions)
        self.assertEqual(found, [self.one.resolve()])

    def test_a_folder_walks_recursively(self) -> None:
        """A folder behaves as before: every movie under it."""
        _root, found = scan.resolve_input_paths(str(self.movies), self.extensions)
        self.assertEqual(found, sorted([self.one.resolve(), self.two.resolve()]))

    def test_a_glob_matches_every_file_it_names(self) -> None:
        """A quoted glob arrives unexpanded, so the CLI must expand it."""
        pattern = str(self.movies / "*.mp4")
        _root, found = scan.resolve_input_paths(pattern, self.extensions)
        self.assertEqual(found, [self.two.resolve()])

    def test_a_recursive_glob_reaches_nested_movies(self) -> None:
        pattern = str(self.movies / "**" / "*.mkv")
        _root, found = scan.resolve_input_paths(pattern, self.extensions)
        self.assertEqual(found, [self.one.resolve()])

    def test_a_glob_matching_nothing_is_an_error(self) -> None:
        """Silently doing nothing is the failure mode this refuses."""
        pattern = str(self.movies / "*.avi")
        with self.assertRaises(FileNotFoundError) as raised:
            scan.resolve_input_paths(pattern, self.extensions)
        self.assertIn("matched no movie file", str(raised.exception))

    def test_a_missing_path_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            scan.resolve_input_paths(str(self.root / "nope"), self.extensions)

    def test_a_glob_keeps_the_pattern_directory_as_category_root(self) -> None:
        """A glob over one folder groups like a directory scan would.

        The category is derived from the root, so the root must be the pattern's
        leading literal directory, not the filesystem root.
        """
        pattern = str(self.movies / "*.mp4")
        root, _found = scan.resolve_input_paths(pattern, self.extensions)
        self.assertEqual(root, self.movies.resolve())

    def test_the_output_directory_is_excluded_from_a_glob(self) -> None:
        """A glob must not re-encode the outputs a previous run produced."""
        out = self.root / "encoded"
        out.mkdir()
        (out / "done.mkv").write_bytes(b"o")
        pattern = str(self.root / "**" / "*.mkv")
        _root, found = scan.resolve_input_paths(pattern, self.extensions, exclude=out)
        self.assertIn(self.one.resolve(), found)
        self.assertNotIn((out / "done.mkv").resolve(), found)

    def test_a_non_movie_glob_match_is_ignored(self) -> None:
        """The extension decides, so a glob naming a poster keeps only movies."""
        pattern = str(self.nested / "*")
        _root, found = scan.resolve_input_paths(pattern, self.extensions)
        self.assertEqual(found, [self.one.resolve()])


class TestBracketInPath(unittest.TestCase):
    """Issue #200: an argument is decided by what it is, not what characters it holds.

    The old dispatch treated any argument containing `[` as a glob, so a real file
    or folder whose name carries a scene-release bracket ([EN+DE], [1080p]) was
    glob-expanded and reported as matching no movie. The filesystem is checked
    first; a pattern is only glob-expanded when nothing exists at that path. Real
    tempfile directories throughout, per the acceptance criteria.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.extensions = [".mkv", ".mp4"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_an_existing_file_with_brackets_in_its_name_scans_that_file(self) -> None:
        """Criterion 1: a bracketed file name is a file, not a glob."""
        movie = self.root / "Media03 (2011) - WEBDL-1080p h264 TrueHD[EN+DE] [DE+EN].mkv"
        movie.write_bytes(b"x")
        root, found = scan.resolve_input_paths(str(movie), self.extensions)
        self.assertEqual(found, [movie.resolve()])
        self.assertEqual(root, self.root.resolve())

    def test_an_existing_folder_with_brackets_in_its_name_scans_that_folder(self) -> None:
        """Criterion 2: a bracketed folder name is a directory, not a glob."""
        folder = self.root / "Series [x265] [1080p]"
        folder.mkdir()
        (folder / "one.mkv").write_bytes(b"x")
        root, found = scan.resolve_input_paths(str(folder), self.extensions)
        self.assertEqual(found, [(folder / "one.mkv").resolve()])
        self.assertEqual(root, folder.resolve())

    def test_a_glob_that_matches_nothing_still_errors(self) -> None:
        """Criterion 3: the glob no-match error is unchanged."""
        (self.root / "movies").mkdir()
        pattern = str(self.root / "movies" / "*.avi")
        with self.assertRaises(FileNotFoundError) as raised:
            scan.resolve_input_paths(pattern, self.extensions)
        self.assertIn("matched no movie file", str(raised.exception))

    def test_a_glob_that_matches_files_still_scans_them(self) -> None:
        """Criterion 4: a bracketed glob that matches real files still expands."""
        folder = self.root / "movies"
        folder.mkdir()
        (folder / "alpha.mp4").write_bytes(b"a")
        (folder / "beta.mp4").write_bytes(b"b")
        (folder / "gamma.mkv").write_bytes(b"c")
        # a character class in the glob, so `[` here is genuinely a pattern
        pattern = str(folder / "[ab]*.mp4")
        root, found = scan.resolve_input_paths(pattern, self.extensions)
        self.assertEqual(found, [(folder / "alpha.mp4").resolve(), (folder / "beta.mp4").resolve()])
        self.assertEqual(root, folder.resolve())

    def test_a_nonexistent_path_without_glob_chars_still_errors(self) -> None:
        """Criterion 5: the missing-path error is unchanged."""
        with self.assertRaises(FileNotFoundError) as raised:
            scan.resolve_input_paths(str(self.root / "nope"), self.extensions)
        self.assertIn("does not exist", str(raised.exception))


class TestStatelessModeA(unittest.TestCase):
    """Issue #125: a one-shot run must not create a database it never asked for.

    The CLI opened metadata.db unconditionally, so a stranger encoding one file got a
    database as a side effect plus plan-cache behaviour that made the run depend on
    prior state. The database is opt-in now: no --db, no database, and the scan is
    given connection=None (which the engine already supports).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.movie = self.root / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> int:
        """Runs the CLI in the temp dir and returns its exit code."""
        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            return runner.invoke(app, list(argv)).exit_code
        finally:
            os.chdir(previous)

    def test_a_stateless_scan_creates_no_database(self) -> None:
        """The criterion: encoding one file leaves no metadata.db behind."""
        with patch("shrinkray.__main__.scan_movies", return_value=({}, [], [])):
            self._run("scan", str(self.movie), "--no-table")
        self.assertFalse(
            (self.root / "metadata.db").exists(),
            "a stateless run created a database the operator never asked for",
        )

    def test_a_stateless_run_passes_no_connection_to_the_scan(self) -> None:
        """The connection must be None, not a lazily created one."""
        with patch("shrinkray.__main__.scan_movies", return_value=({}, [], [])) as fake_scan:
            self._run("scan", str(self.movie), "--no-table")
        self.assertIsNone(fake_scan.call_args.kwargs["connection"])

    def test_an_explicit_db_is_opened_and_used(self) -> None:
        """Passing --db restores today's caching behaviour."""
        db_file = self.root / "explicit.db"
        with patch("shrinkray.__main__.scan_movies", return_value=({}, [], [])) as fake_scan:
            self._run("scan", str(self.movie), "--no-table", "--db", str(db_file))
        self.assertIsNotNone(fake_scan.call_args.kwargs["connection"])
        self.assertTrue(db_file.exists(), "an explicit --db was not created")

    def test_a_stateless_plan_creates_no_database(self) -> None:
        with patch("shrinkray.engine.plan.plan_quality", return_value=_skipped_plan(self.movie)):
            self._run("plan", str(self.movie))
        self.assertFalse((self.root / "metadata.db").exists())

    def test_a_stateless_plan_passes_no_db_file_to_the_engine(self) -> None:
        """No database means no plan cache: the engine gets db_file=None."""
        with patch(
            "shrinkray.engine.plan.plan_quality", return_value=_skipped_plan(self.movie)
        ) as fake:
            self._run("plan", str(self.movie))
        self.assertIsNone(fake.call_args.kwargs["db_file"])


def _skipped_plan(movie: Path) -> "object":
    """Builds a skipped plan result for the mocked plan command."""
    from shrinkray.engine.plan import QualityResult

    return QualityResult(
        input=movie,
        output=movie,
        success=False,
        skipped_reason="below min size",
        error="skipped: below min size",
        input_size=movie.stat().st_size,
        output_size=movie.stat().st_size,
    )


class TestEncodeInPlace(unittest.TestCase):
    """Issue #124: the one-shot encode lands in the source's own path.

    The output directory used to be a required positional, so a one-shot run always
    produced a second copy in a second directory - the duplication the product
    excludes, and an output directory the operator never wanted to name. Omitting it
    now encodes in place: the verified output takes the source's path and the
    original is renamed aside and kept. Naming one keeps today's behaviour.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / "movies"
        self.library.mkdir()
        self.movie = self.library / "one.mkv"
        self.movie.write_bytes(b"x" * 100)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _encode(self, *argv: str) -> "object":
        """Runs encode with the engine faked to write a smaller output."""
        from shrinkray.engine.plan import QualityResult

        def fake_encode(movie, output, **kwargs):
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"y" * 50)
            return QualityResult(
                input=Path(movie),
                output=Path(output),
                success=True,
                chosen_crf=20,
                achieved_vmaf=96.0,
                input_size=100,
                output_size=50,
            )

        from typer.testing import CliRunner

        runner = CliRunner()
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            with patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_encode):
                return runner.invoke(app, list(argv))
        finally:
            os.chdir(previous)

    def test_an_omitted_output_dir_encodes_in_place(self) -> None:
        """The criterion: no second argument, the source's path holds the encode."""
        result = self._encode("encode", str(self.library), "--quality")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.movie.read_bytes(), b"y" * 50)

    def test_the_original_is_retained_beside_the_source(self) -> None:
        """The source is never deleted: it is renamed aside and kept."""
        from shrinkray.replace import retained_path_for

        result = self._encode("encode", str(self.library), "--quality")
        self.assertEqual(result.exit_code, 0, result.output)
        retained = retained_path_for(self.movie)
        self.assertTrue(retained.exists(), "the original was not retained")
        self.assertEqual(retained.read_bytes(), b"x" * 100)

    def test_no_second_copy_is_left_behind(self) -> None:
        """In place means one file, not a source plus an output elsewhere."""
        result = self._encode("encode", str(self.library), "--quality")
        self.assertEqual(result.exit_code, 0, result.output)
        left = sorted(p.name for p in self.library.iterdir() if p.is_file())
        self.assertEqual(len(left), 2, f"expected source+original only, got {left}")

    def test_the_working_directory_is_removed(self) -> None:
        """The in-place staging directory must not litter the library."""
        result = self._encode("encode", str(self.library), "--quality")
        self.assertEqual(result.exit_code, 0, result.output)
        leftovers = [p.name for p in self.library.iterdir() if p.name.startswith(".shrinkray-")]
        self.assertEqual(leftovers, [], "the staging directory was left behind")

    def test_an_explicit_output_dir_still_writes_there(self) -> None:
        """The regression guard: naming an output directory keeps today's behaviour."""
        out = self.root / "encoded"
        result = self._encode("encode", str(self.library), str(out), "--quality")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((out / "one.mkv").exists(), "the output was not written")
        self.assertEqual(self.movie.read_bytes(), b"x" * 100, "the source was touched")


class TestToolchainPreflight(unittest.TestCase):
    """Issue #132: an unusable toolchain is refused before any work starts.

    doctor reported "Library is healthy" on a host that cannot measure a single
    file, and the app served jobs it could only fail. The libvmaf probe existed
    but only lazily from the encode path, and nothing checked that HandBrakeCLI
    knows the svt_av1 encoder at all.
    """

    def test_a_missing_handbrake_is_reported_with_the_minimum_version(self) -> None:
        from shrinkray import service

        with patch("shrinkray.tools.handbrake.find_handbrake", return_value=None):
            problems = service.toolchain_problems()
        handbrake = [p for p in problems if "HandBrakeCLI" in p]
        self.assertEqual(len(handbrake), 1, problems)
        self.assertIn("1.6", handbrake[0], "the minimum version must be named")

    def test_a_handbrake_without_svt_av1_is_reported(self) -> None:
        from shrinkray import service

        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
            patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=False),
            patch("shrinkray.tools.handbrake.handbrake_version", return_value="1.4.2"),
        ):
            problems = service.toolchain_problems()
        handbrake = [p for p in problems if "svt_av1" in p]
        self.assertEqual(len(handbrake), 1, problems)
        self.assertIn("1.4.2", handbrake[0], "the actual version must be named")
        self.assertIn("1.6", handbrake[0], "the minimum version must be named")

    def test_an_unmeasurable_vmaf_is_reported(self) -> None:
        from shrinkray import service

        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
            patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
            patch(
                "shrinkray.tools.vmaf.detect_vmaf_style",
                side_effect=RuntimeError("ffmpeg has no libvmaf filter!"),
            ),
        ):
            problems = service.toolchain_problems()
        vmaf = [p for p in problems if "VMAF" in p]
        self.assertEqual(len(vmaf), 1, problems)
        self.assertIn("no libvmaf filter", vmaf[0])

    def test_a_complete_toolchain_reports_nothing(self) -> None:
        from shrinkray import service

        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value="hb"),
            patch("shrinkray.tools.handbrake.supports_svt_av1", return_value=True),
            patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value="modern"),
        ):
            self.assertEqual(service.toolchain_problems(), [])

    def test_the_app_refuses_to_start_on_a_broken_toolchain(self) -> None:
        from shrinkray.server import app as app_module

        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value=None),
            patch(
                "shrinkray.tools.vmaf.detect_vmaf_style",
                side_effect=RuntimeError("ffmpeg has no libvmaf filter!"),
            ),
            patch.object(app_module, "allow_roots"),
            patch.object(app_module, "AppState"),
            self.assertRaises(ValueError) as raised,
        ):
            app_module.run_app(Path("unused.db"))
        message = str(raised.exception)
        self.assertIn("unusable toolchain", message)
        self.assertIn("HandBrakeCLI", message)
        self.assertIn("libvmaf", message)

    def test_the_doctor_refuses_a_broken_toolchain(self) -> None:
        """doctor must fail loudly instead of reporting a healthy library."""
        from shrinkray import service

        with (
            patch("shrinkray.tools.handbrake.find_handbrake", return_value=None),
            patch(
                "shrinkray.tools.vmaf.detect_vmaf_style",
                side_effect=RuntimeError("ffmpeg has no libvmaf filter!"),
            ),
        ):
            problems = service.toolchain_problems()
        self.assertEqual(len(problems), 2, problems)


class TestStartupBacklog(unittest.TestCase):
    """Issue #126: a fresh container starts the backlog with no client.

    The entrypoint started the resident app and nothing else, so a container pointed
    at a library did nothing until someone opened the dashboard and clicked Scan,
    Plan, Encode - which also makes the observe-only dashboard impossible.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _state(self) -> "object":
        from shrinkray.server import app as app_module

        with (
            mock.patch.object(app_module, "ProgressBus"),
            mock.patch.object(app_module, "JobQueue"),
            mock.patch.object(app_module, "JobWorker"),
        ):
            return app_module.AppState(Path(self.tmp.name) / "m.db")

    def test_the_backlog_is_off_by_default(self) -> None:
        """A developer's dashboard must not start encoding the library."""
        from shrinkray.server import app as app_module

        state = self._state()
        self.assertEqual(app_module.auto_backlog(state, [self.root], enabled=False), [])
        self.assertFalse(state.queue.enqueue.called)

    def test_the_config_default_is_off(self) -> None:
        """The non-container default is off, per the criterion."""
        from shrinkray.config import Conf

        self.assertFalse(Conf().auto_backlog)

    def test_the_backlog_runs_scan_then_plan_then_encode(self) -> None:
        """The criterion: the order is scan -> plan -> encode."""
        from shrinkray.server import app as app_module

        state = self._state()
        kinds = app_module.auto_backlog(state, [self.root], enabled=True)
        self.assertEqual(kinds, ["scan", "plan", "encode"])

    def test_every_root_reaches_the_scan_payload(self) -> None:
        """Every allowlisted root is scanned, not just the first."""
        from shrinkray.server import app as app_module

        second = Path(self.tmp.name) / "second"
        second.mkdir()
        state = self._state()
        app_module.auto_backlog(state, [self.root, second], enabled=True)
        scan = state.queue.enqueue.call_args_list[0].args[0]
        self.assertEqual(scan.payload["paths"], [str(self.root), str(second)])

    def test_the_plan_and_encode_are_quality_jobs(self) -> None:
        """A fixed-preset encode would be a second, unmeasured path."""
        from shrinkray.server import app as app_module

        state = self._state()
        app_module.auto_backlog(state, [self.root], enabled=True)
        calls = state.queue.enqueue.call_args_list
        self.assertTrue(calls[1].args[0].payload["quality"])
        self.assertTrue(calls[2].args[0].payload["quality"])

    def test_no_roots_enqueues_nothing(self) -> None:
        """An empty allowlist must not enqueue a job that scans nothing."""
        from shrinkray.server import app as app_module

        state = self._state()
        self.assertEqual(app_module.auto_backlog(state, [], enabled=True), [])
        self.assertFalse(state.queue.enqueue.called)
