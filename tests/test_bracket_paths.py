"""Bracket handling in scan's path resolution (issue #200): '[' is legal in a
filename (the target library is full of [EN+DE], [1080p], [x265]) but the old
resolver treated any '[' as a glob character, so an existing file with a
bracket in its name was reported as matching no movie file.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray.engine.scan import resolve_input_paths


class TestBracketPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _movie(self, name: str) -> Path:
        movie = self.root / name
        movie.write_bytes(b"x" * 8)
        return movie

    def test_an_existing_file_with_brackets_scans_exactly_that_file(self) -> None:
        movie = self._movie("with [BRACKETS] name.mkv")
        root, movies = resolve_input_paths(str(movie), [".mkv"])
        self.assertEqual(movies, [movie.resolve()])
        self.assertEqual(root, movie.parent.resolve())

    def test_an_existing_folder_with_brackets_scans_that_folder(self) -> None:
        folder = self.root / "[2024] Films"
        folder.mkdir()
        movie = folder / "a.mkv"
        movie.write_bytes(b"x" * 8)
        root, movies = resolve_input_paths(str(folder), [".mkv"])
        self.assertIn(movie.resolve(), movies)
        self.assertEqual(root, folder.resolve())

    def test_a_quoted_glob_that_matches_nothing_still_errors(self) -> None:
        self._movie("plain.mkv")
        with self.assertRaises(FileNotFoundError) as ctx:
            resolve_input_paths(str(self.root / "*.xyz"), [".mkv"])
        self.assertIn("matched no movie file", str(ctx.exception))

    def test_a_quoted_glob_that_matches_files_still_scans_those(self) -> None:
        self._movie("a.mkv")
        self._movie("b.mkv")
        root, movies = resolve_input_paths(str(self.root / "*.mkv"), [".mkv"])
        self.assertEqual(len(movies), 2)
        self.assertEqual(root, self.root.resolve())

    def test_a_nonexistent_path_without_glob_chars_errors_with_does_not_exist(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            resolve_input_paths(str(self.root / "missing.mkv"), [".mkv"])
        self.assertIn("does not exist", str(ctx.exception))

    def test_a_nonexistent_bracketed_path_errors_with_does_not_exist(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            resolve_input_paths(str(self.root / "[2024] missing.mkv"), [".mkv"])
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
