"""CLI command tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import tempfile
import unittest
from pathlib import Path

from shrinkray import config as config_helper
from shrinkray import db
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
