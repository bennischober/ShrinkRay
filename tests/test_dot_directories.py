"""Tests for the dot-directory membership rule (issue #439).

A dot-directory (any path SEGMENT starting with a dot) inside the scan root
is never library content: find_movies excludes its candidates when reached
implicitly by walking the root, while an explicit user ask (Mode A: a named
path or a glob through resolve_input_paths) gets exactly what was named -
ShrinkRay never second-guesses an explicit argument. A dot-FILE (its NAME
starts with a dot) is still a library file: the rule is segments, not names.
The staging filter (is_staging_path, #237/#286) keeps both jobs unchanged.

The prune side (the maintainer's correction on the issue): the implicit
scan's reconcile carries an explicit EXCLUSION set - the dot-directories
discovery skipped - and rows inside those subtrees keep their missing/
recovery fields untouched (no silent deletion through the grace period).

The explicit file/glob reconciliation stays AS IT IS today (the split the
maintainer settled): the pin test records today's sibling-pruning behaviour
so the follow-up fix has a failing test to flip.
"""

import tempfile
import unittest
from pathlib import Path

from shrinkray import db
from shrinkray.engine import scan as engine_scan


def _make_scan_config():
    """A scan config that finds .mkv files (the test_cli harness's shape)."""
    from tests import test_cli as test_cli_mod

    return test_cli_mod.config_helper.Conf()


class TestDotDirectoriesAreNotLibraryContent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "movie.mkv").write_bytes(b"x" * 16)
        (self.root / ".hidden-file.mkv").write_bytes(b"x" * 16)
        for d in (".previews", ".backups/nested", ".shrinkray-staging-abc", ".передачи"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / ".previews" / "h.mkv").write_bytes(b"x" * 16)
        (self.root / ".backups" / "nested" / "b.mkv").write_bytes(b"x" * 16)
        (self.root / ".shrinkray-staging-abc" / "s.mkv").write_bytes(b"x" * 16)
        (self.root / ".передачи" / "u.mkv").write_bytes(b"x" * 16)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_find_movies_excludes_dot_directory_segments(self) -> None:
        """The walk skips every candidate under a dot-DIRECTORY segment -
        .previews, .backups, unicode dot-dirs and ShrinkRay's own staging
        names alike - while the dot-FILE and the real movie stay library
        content (the rule is segments, not names).
        """
        found = engine_scan.find_movies(self.root, [".mkv"])
        names = sorted(p.relative_to(self.root).as_posix() for p in found)
        self.assertEqual(names, [".hidden-file.mkv", "movie.mkv"])

    def test_mode_a_explicit_paths_stay(self) -> None:
        """Mode A: an explicit user ask is never second-guessed - a named
        path inside a hidden dir and a glob that happens to match one
        resolve exactly as given (resolve_input_paths, walked=False).
        """
        from shrinkray.engine.scan import resolve_input_paths

        named = self.root / ".previews" / "h.mkv"
        _root, files, walked = resolve_input_paths(str(named), [".mkv"])
        self.assertFalse(walked)
        self.assertEqual([p.resolve() for p in files], [named.resolve()])
        _root, files, walked = resolve_input_paths(str(self.root / ".previews" / "*.mkv"), [".mkv"])
        self.assertFalse(walked)
        self.assertEqual([p.resolve() for p in files], [named.resolve()])

    def test_staging_siblings_still_excluded(self) -> None:
        """The staging filter keeps both jobs unchanged: a RETAINED_SUFFIX
        sibling and a STAGING_SUFFIX directory stay out of the walk (and
        the dot-dir rule adds nothing to them - they are excluded even
        where no dot-dir segment is involved).
        """
        (self.root / "movie.mkv.shrinkray-original").write_bytes(b"x" * 16)
        (self.root / "other.mkv.shrinkray-staging").write_bytes(b"x" * 16)
        found = engine_scan.find_movies(self.root, [".mkv"])
        names = sorted(p.relative_to(self.root).as_posix() for p in found)
        self.assertEqual(names, [".hidden-file.mkv", "movie.mkv"])

    def test_the_implicit_reconcile_excludes_the_hidden_subtrees(self) -> None:
        """The prune plumbing (the maintainer's correction): the implicit
        scan's reconcile carries the dot-directories discovery skipped -
        rows inside those subtrees keep their missing/recovery fields
        untouched (no silent flag/deletion through the grace period).
        """
        conn = db.connect(self.root / "t.db")
        try:
            a = str(self.root / "movie.mkv")
            h = str(self.root / ".previews" / "h.mkv")
            for p, name in ((a, "movie.mkv"), (h, "h.mkv")):
                conn.execute(
                    "INSERT INTO files (path, mtime, size, name, category, missing_scans, "
                    "missing_since) VALUES (?, 0, 16, ?, 'movies', 1, datetime('now'))",
                    (p, name),
                )
            conn.commit()
            engine_scan.scan_movies(self.root, _make_scan_config(), connection=conn, workers=1)
            # a's flag is recovered (the scan saw it); h's row is UNTOUCHED
            # (its subtree is excluded from the reconcile)
            a_row = conn.execute("SELECT missing_scans FROM files WHERE path = ?", (a,)).fetchone()
            h_row = conn.execute(
                "SELECT missing_scans, missing_since FROM files WHERE path = ?", (h,)
            ).fetchone()
            self.assertEqual(a_row["missing_scans"], 0)
            self.assertEqual(h_row["missing_scans"], 1)
            self.assertIsNotNone(h_row["missing_since"])
        finally:
            conn.close()

    def test_explicit_scan_pinned_to_todays_sibling_pruning(self) -> None:
        """The maintainer's pin, updated to the post-#500 truth: an explicit
        file/glob scan reconciles EXACTLY its enumerated files (the #453
        `only` contract, merged while this issue was open) - a sibling
        under the same root is never flagged or deleted, its row is out of
        the scan's opinion entirely. The maintainer's reproduction
        ("plan --db a.mkv prunes its siblings after 3 runs") predates the
        #500 merge and is verified to FAIL on pure main today (the sibling
        survives); the pin records the CURRENT behaviour so any change to
        it - a regression, or the follow-up work the maintainer mentioned -
        fails here.
        """
        conn = db.connect(self.root / "t.db")
        try:
            a = self.root / "movie.mkv"
            b = self.root / "sibling.mkv"
            b.write_bytes(b"x" * 16)
            for p in (a, b):
                conn.execute(
                    "INSERT INTO files (path, mtime, size, name, category) "
                    "VALUES (?, 0, 16, ?, 'movies')",
                    (str(p), p.name),
                )
            conn.commit()
            for _ in range(db.MISSING_GRACE):
                engine_scan.scan_movies(
                    self.root,
                    _make_scan_config(),
                    connection=conn,
                    workers=1,
                    files=[a],
                    walked=False,
                )
            row = conn.execute("SELECT COUNT(*) FROM files WHERE path = ?", (str(b),)).fetchone()[0]
            self.assertEqual(
                row,
                1,
                "post-#500: the unenumerated sibling is out of an explicit scan's opinion",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
