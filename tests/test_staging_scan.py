"""Tests for staging paths never being scan candidates (issue #237).

The in-place working directory is created INSIDE the library, and the encode
it holds is named like the movie it will become: without the filter, a scan
(or a watcher pass) treats an in-flight or crashed run's output as a library
movie - a permanent phantom after a crash, and a wasted probe of a file that
is still being written.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray import replace
from shrinkray.engine import scan as engine_scan
from shrinkray.engine import watch as watch_mod


class TestStagingPathsAreNeverCandidates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "movie.mkv").write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_find_movies_skips_the_staging_directory(self) -> None:
        """A crashed run's staging directory is debris, not a library movie."""
        staging = self.root / ".shrinkray-x8k2"
        staging.mkdir()
        (staging / "movie.mkv").write_bytes(b"partial")
        found = engine_scan.find_movies(self.root, [".mkv"])
        self.assertEqual(found, [self.root / "movie.mkv"])

    def test_find_movies_skips_the_swap_siblings(self) -> None:
        """A retained original and a staging copy are never movies."""
        (self.root / "movie.mkv.shrinkray-original").write_bytes(b"orig")
        (self.root / "other.mkv.shrinkray-staging").write_bytes(b"staged")
        found = engine_scan.find_movies(self.root, [".mkv"])
        self.assertEqual(found, [self.root / "movie.mkv"])

    def test_find_new_movies_skips_the_staging_directory(self) -> None:
        """The watcher does not pick up an in-flight encode as a new arrival."""
        staging = self.root / ".shrinkray-x8k2"
        staging.mkdir()
        (staging / "movie.mkv").write_bytes(b"partial")
        candidates = watch_mod.find_new_movies(self.root, [".mkv"], set())
        self.assertEqual(candidates, [self.root / "movie.mkv"])

    def test_the_predicate_covers_every_depth(self) -> None:
        """A staging directory nested anywhere in the tree filters its content."""
        deep = self.root / "a" / "b" / ".shrinkray-x8k2" / "movie.mkv"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b"partial")
        found = engine_scan.find_movies(self.root, [".mkv"])
        self.assertEqual(found, [self.root / "movie.mkv"])
        self.assertEqual(
            watch_mod.find_new_movies(self.root, [".mkv"], set()), [self.root / "movie.mkv"]
        )

    def test_the_predicate_lives_beside_the_names_it_filters(self) -> None:
        """One helper owns the rule, so the two walkers cannot drift apart.

        The staging directory's name and the swap suffixes are `replace.py`'s
        contract; the filter is defined there and both scan paths import it.
        """
        self.assertTrue(
            replace.is_staging_path(self.root / ".shrinkray-x" / "movie.mkv", self.root)
        )
        self.assertTrue(
            replace.is_staging_path(self.root / "movie.mkv.shrinkray-original", self.root)
        )
        self.assertTrue(
            replace.is_staging_path(self.root / "movie.mkv.shrinkray-staging", self.root)
        )
        self.assertFalse(replace.is_staging_path(self.root / "movie.mkv", self.root))
        self.assertFalse(replace.is_staging_path(self.root / ".hidden" / "movie.mkv", self.root))
