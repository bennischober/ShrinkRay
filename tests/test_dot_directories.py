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
        sibling and a STAGING_SUFFIX directory stay out of the walk. The
        honest reach (the reviewer's finding 7): the extension glob never
        yields a `*.mkv.shrinkray-original` name and every staging
        DIRECTORY starts with `.shrinkray-`, which the dot rule already
        excludes - so inside find_movies this predicate is unreachable for
        every candidate the glob can produce; its load-bearing pin lives in
        tests/test_staging_scan.py (the retained/staging swap tests), and
        this test documents the redundancy instead of pretending to pin it.
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

    def test_the_cli_folder_shape_excludes_the_hidden_subtrees(self) -> None:
        """Finding 1 of round 2 (measured by the reviewer): the CLI folder
        shape (plan/encode <folder>) passes the walk's result with
        walked=True - the exclusion set must be computed for THAT shape
        too, or a catalogued hidden row is bumped and hard-deleted while
        its file is still on disk (the regression the round-1 hoist
        introduced with its files-is-None guard). The pin drives the CLI
        shape through the real resolve_input_paths.
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
            from shrinkray.engine.scan import resolve_input_paths

            _root, files, walked = resolve_input_paths(str(self.root), [".mkv"])
            self.assertTrue(walked)
            for _ in range(db.MISSING_GRACE + 1):
                engine_scan.scan_movies(
                    self.root,
                    _make_scan_config(),
                    connection=conn,
                    workers=1,
                    files=files,
                    walked=walked,
                )
            h_row = conn.execute("SELECT missing_scans FROM files WHERE path = ?", (h,)).fetchone()
            self.assertIsNotNone(h_row, "the hidden row survives the CLI folder shape")
            # h was seeded with missing_scans=1; the exclusion means the
            # reconcile has NO OPINION about it - the flag stays EXACTLY as
            # seeded (not recovered to 0: recovery would be an opinion)
            self.assertEqual(h_row["missing_scans"], 1)
            self.assertIsNotNone(
                conn.execute("SELECT missing_since FROM files WHERE path = ?", (h,)).fetchone()[
                    "missing_since"
                ]
            )
            a_row = conn.execute("SELECT missing_scans FROM files WHERE path = ?", (a,)).fetchone()
            self.assertEqual(a_row["missing_scans"], 0)
        finally:
            conn.close()

    def test_the_empty_walk_keeps_the_hidden_rows(self) -> None:
        """The empty-walk shape (round-1 finding 1; round-2 finding 2's
        corrected fixture): a root whose ONLY content is hidden (every
        visible file gone - moved library, detached mount) while hidden
        rows remain. The fixture holds NO dot-FILE at the root, so the
        walk returns NOTHING and the early return's reconcile is the one
        under test - the reviewer measured that the original fixture kept
        a dot-FILE at the root, the scan stayed non-empty, and the early
        return never ran (the pin could not fail under mutation M2).
        """
        conn = db.connect(self.root / "t.db")
        try:
            a = str(self.root / "movie.mkv")
            h = str(self.root / ".previews" / "h.mkv")
            for p, name in ((a, "movie.mkv"), (h, "h.mkv")):
                conn.execute(
                    "INSERT INTO files (path, mtime, size, name, category) "
                    "VALUES (?, 0, 16, ?, 'movies')",
                    (p, name),
                )
            conn.commit()
            (self.root / "movie.mkv").unlink()
            (self.root / ".hidden-file.mkv").unlink()
            for _ in range(db.MISSING_GRACE + 1):
                engine_scan.scan_movies(self.root, _make_scan_config(), connection=conn, workers=1)
            h_row = conn.execute("SELECT missing_scans FROM files WHERE path = ?", (h,)).fetchone()
            self.assertIsNotNone(h_row, "the hidden row survives the empty-walk grace")
            # h's row was never seeded as missing: the exclusion means the
            # reconcile never touched it, so it stays 0 (untouched, not
            # recovered - a recovery would be an opinion)
            self.assertEqual(h_row["missing_scans"], 0)
        finally:
            conn.close()

    def test_the_empty_walk_still_warns_on_mass_flagging(self) -> None:
        """Finding 4 (round-2 finding 3's discriminating fixture): rows
        inside excluded subtrees do not inflate the mass heuristic's
        denominator - the fixture is 10 visible + 50 HIDDEN rows, wide
        enough that the mutated code (in_scope counting the excluded rows,
        denominator 60, threshold 12) silences the warning while the head
        code (denominator 10, threshold 5) fires. The reviewer measured
        the original 10+1 fixture staying mass=True under the mutation,
        so the pin could not fail.
        """
        conn = db.connect(self.root / "t.db")
        try:
            for i in range(10):
                conn.execute(
                    "INSERT INTO files (path, mtime, size, name, category) "
                    "VALUES (?, 0, 16, ?, 'movies')",
                    (str(self.root / f"v{i}.mkv"), f"v{i}.mkv"),
                )
            for i in range(50):
                conn.execute(
                    "INSERT INTO files (path, mtime, size, name, category) "
                    "VALUES (?, 0, 16, ?, 'movies')",
                    (str(self.root / ".previews" / f"h{i}.mkv"), f"h{i}.mkv"),
                )
            conn.commit()
            result = db.prune_missing(
                conn, set(), scope=str(self.root), except_paths={str(self.root / ".previews")}
            )
            self.assertEqual(result.flagged, 10)
            self.assertTrue(result.mass)
        finally:
            conn.close()

    def test_watch_mode_never_lists_hidden_media(self) -> None:
        """Finding 3: the two listings share one membership rule - the
        watcher's own rglob applies the same segment rule, so hidden media
        is never encoded in place by the resident watch loop.
        """
        from shrinkray.engine.watch import find_new_movies

        found = find_new_movies(self.root, [".mkv"], set())
        names = sorted(p.relative_to(self.root).as_posix() for p in found)
        self.assertEqual(names, [".hidden-file.mkv", "movie.mkv"])

    def test_the_db_seam_excludes_subtrees_on_both_separators(self) -> None:
        """Finding 2: the exclusion comparison is separator-agnostic -
        Windows-shaped stored paths (backslash-separated) are excluded by
        the same call, and a sibling directory (.previews2) is not caught
        by the prefix.
        """
        conn = db.connect(self.root / "t.db")
        try:
            win_row = "C:\\Movies\\.previews\\h.mkv"
            win_sibling = "C:\\Movies\\.previews2\\x.mkv"
            posix_row = str(self.root / ".previews" / "h.mkv")
            for p in (win_row, win_sibling, posix_row):
                conn.execute(
                    "INSERT INTO files (path, mtime, size, name, category, missing_scans) "
                    "VALUES (?, 0, 16, 'x.mkv', 'movies', 0)",
                    (p,),
                )
            conn.commit()
            # the WINDOWS-shaped rows are reconciled through the same
            # comparison WITHOUT Path.resolve() mangling them on this POSIX
            # host: _is_below is exercised directly (the seam's unit), and
            # the POSIX row's exclusion is exercised through the function
            result = db.prune_missing(
                conn,
                set(),
                scope=str(self.root),
                except_paths={str(self.root / ".previews")},
            )
            # the posix hidden row is out of opinion; the posix SIBLING
            # (.hidden-file.mkv's subtree does not exist here) — assert the
            # posix seam behaviour
            self.assertEqual(result.flagged, 0)
            p = conn.execute(
                "SELECT missing_scans FROM files WHERE path = ?", (posix_row,)
            ).fetchone()
            self.assertEqual(p["missing_scans"], 0, "the POSIX hidden row untouched")
            # the WINDOWS shapes are the unit's business (resolve() would
            # mangle them on this POSIX host): _is_below decides both
            from shrinkray.db import _is_below

            self.assertTrue(_is_below(win_row, "C:\\Movies\\.previews"))
            self.assertFalse(_is_below(win_sibling, "C:\\Movies\\.previews"))
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
