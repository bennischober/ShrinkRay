"""Tests for the encode slot's lock target (issue #230).

The slot must be keyed on the CONTENDED directory: in place that is the
library directory the swaps rename inside, and a slot keyed on the per-run
staging directory excluded no one - two encoders of one file raced on the
deterministic swap staging name (`replace.staging_path_for`), and the loser
could rename a half-written copy into a library path.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine import watch as engine_watch
from shrinkray.engine.lock import EncodeBusyError, _lock_path, guard_encodes


def _conf_stub() -> object:
    return type(
        "Conf",
        (),
        {
            "sample_every": 600.0,
            "sample_count": 3,
            "sample_seconds": 10.0,
            "handbrake_encoder": "svt_av1",
            "handbrake_probe_encoder_preset": "5",
            "handbrake_encoder_preset": "3",
            "handbrake_max_threads": 0,
            "crop_sample_count": 10,
            "min_source_height": 1080,
            "min_source_width": 1920,
            "min_source_size_gb": 0.0,
            "candidate_codecs": ("h264", "hevc", "vp9", "av1"),
            "handbrake_audio_languages": ["deu", "eng", "und", "jpn"],
            "handbrake_subtitle_languages": ["eng", "deu", "und"],
        },
    )()


class TestInPlaceSlotKeying(unittest.TestCase):
    """Two in-place writers of one library must not both hold the slot."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"x" * 8)
        # the CLI tests here run the REAL _record_encodes loop on this fresh
        # fixture: its #437 stability wait would hold every test for the age
        # floor (~60 s each). Their subject is the slot keying, not stability,
        # so the floor and the interval are patched to 0 - the real seam still
        # runs, instantly.
        self.stability_patch = (
            mock.patch.object(engine_watch, "STABLE_MIN_AGE", 0.0),
            mock.patch.object(engine_watch, "STABLE_CHECK_INTERVAL", 0.0),
        )
        for p in self.stability_patch:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.stability_patch])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_writers_of_one_movie_directory_conflict(self) -> None:
        """Two encoders of the SAME movie directory exclude each other, across
        threads, which is how the watcher and the job collide in one process.

        The old shape locked each writer's own staging directory: two `mkdtemp`
        paths never collide, so both writers believed they were exclusive and
        both encoded the same movie. The contended resource is the movie's own
        folder - where the staging copy and the swap's renames happen (#230, #379).

        A nested-call test proved only that the context manager re-enters; this one
        holds the slot on a second thread, as the resident app does (round-3
        finding 12).
        """
        import threading

        held = threading.Event()
        release = threading.Event()

        def first_writer() -> None:
            with guard_encodes(self.source.parent, holder="one"):
                held.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=first_writer)
        thread.start()
        try:
            self.assertTrue(held.wait(timeout=5), "the first writer never took the slot")
            with self.assertRaises(EncodeBusyError):
                guard_encodes(self.source.parent, holder="two").__enter__()
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(_lock_path(self.source.parent).exists())

    def test_the_cli_parks_the_slot_only_on_the_movies_directory_in_place(self) -> None:
        """An in-place run (`in_place=True`) keys the slot on the MOVIE's folder ONLY.

        That is where the staging copy and the swap's two renames happen, so it is
        the one key the job and the watcher can agree on (#379). The directory the
        run writes into is its own private temp dir, which nothing else ever locks:
        guarding it would exclude no one, so it is not taken (round-6 M1).
        """
        from shrinkray import __main__ as cli

        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured.setdefault("dirs", []).append(directory)
            captured["holder"] = holder
            return _null_guard(directory)

        with (
            mock.patch.object(cli, "guard_encodes", side_effect=fake_guard),
            mock.patch.object(engine_pipeline, "encode_with_quality") as fake_encode,
        ):
            fake_encode.return_value = engine_pipeline.QualityResult(
                input=self.source, output=self.source, success=False
            )
            cli._record_encodes(
                None,
                [self.source],
                self.root / ".shrinkray-stage",
                _conf_stub(),
                dry_run=False,
                overwrite=False,
                quality=True,
                target_vmaf=96.0,
                verify=False,
                threads=None,
                db_file=None,
                replace=True,
                in_place=True,
            )
        self.assertEqual(captured["dirs"], [self.source.parent])
        self.assertEqual(captured["holder"], "encode-cli")

    def test_the_cli_guards_the_output_dir_too_when_replacing(self) -> None:
        """`--output-dir X --replace` writes into X and swaps into the movie's
        folder, so both are contended and both are guarded (PR #409's review
        finding 4: main guarded X here, and keying only on movie.parent lost it)."""
        from shrinkray import __main__ as cli

        out = self.root / "out"
        out.mkdir()
        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured.setdefault("dirs", []).append(directory)
            return _null_guard(directory)

        with (
            mock.patch.object(cli, "guard_encodes", side_effect=fake_guard),
            mock.patch.object(engine_pipeline, "encode_with_quality") as fake_encode,
        ):
            fake_encode.return_value = engine_pipeline.QualityResult(
                input=self.source, output=out / "movie.mkv", success=False
            )
            cli._record_encodes(
                None,
                [self.source],
                out,
                _conf_stub(),
                dry_run=False,
                overwrite=False,
                quality=True,
                target_vmaf=96.0,
                verify=False,
                threads=None,
                db_file=None,
                replace=True,
            )
        self.assertEqual(captured["dirs"], sorted([out, self.source.parent]))

    def test_a_second_spelling_of_one_directory_is_taken_once(self) -> None:
        """`--output-dir X --replace` guards X and the movie's folder; when they
        are the SAME directory spelled differently (a relative path, a symlink), the
        set must dedupe them AFTER resolving, or the run takes one lock twice and
        refuses itself (PR #409's review finding 1)."""
        from shrinkray import __main__ as cli

        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured.setdefault("dirs", []).append(directory)
            return _null_guard(directory)

        # a second spelling of the movie's own folder
        alias = self.root / "." / ".." / self.root.name
        with (
            mock.patch.object(cli, "guard_encodes", side_effect=fake_guard),
            mock.patch.object(engine_pipeline, "encode_with_quality") as fake_encode,
        ):
            fake_encode.return_value = engine_pipeline.QualityResult(
                input=self.source, output=self.source, success=False
            )
            cli._record_encodes(
                None,
                [self.source],
                alias,
                _conf_stub(),
                dry_run=False,
                overwrite=False,
                quality=True,
                target_vmaf=96.0,
                verify=False,
                threads=None,
                db_file=None,
                replace=True,
            )
        self.assertEqual(len(captured["dirs"]), 1, f"took the slot twice: {captured['dirs']}")
        self.assertEqual(captured["dirs"][0], self.source.parent)

    def test_the_cli_parks_the_slot_on_the_output_dir_when_given(self) -> None:
        """A two-directory run keeps the output directory as the slot's target."""
        from shrinkray import __main__ as cli

        out = self.root / "out"
        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured["dir"] = directory
            return _null_guard(directory)

        with (
            mock.patch.object(cli, "guard_encodes", side_effect=fake_guard),
            mock.patch.object(engine_pipeline, "encode_with_quality") as fake_encode,
        ):
            fake_encode.return_value = engine_pipeline.QualityResult(
                input=self.source, output=out / "movie.mkv", success=False
            )
            cli._record_encodes(
                None,
                [self.source],
                out,
                _conf_stub(),
                dry_run=False,
                overwrite=False,
                quality=True,
                target_vmaf=96.0,
                verify=False,
                threads=None,
                db_file=None,
                replace=False,
            )
        self.assertEqual(captured["dir"], out)

    def test_the_watcher_parks_the_slot_on_the_movies_directory_in_place(self) -> None:
        """The watcher's in-place slot is keyed on the movie's own directory.

        The movie sits in a NESTED folder, so its directory differs from the
        watched root: a key on the root (the #389 shape) fails this test. The
        swaps rename inside the movie's folder, so that is what must be guarded.
        """
        from shrinkray.engine import watch as watch_mod

        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured.setdefault("dirs", []).append(directory)
            return _null_guard(directory)

        def fake_encode(movie, output, **kwargs):
            return engine_pipeline.QualityResult(
                input=movie,
                output=output,
                success=False,
                skipped_reason="no size gain",
            )

        nested = self.root / "Title (2020)"
        nested.mkdir()
        (nested / "movie.mkv").write_bytes(b"x" * 8)
        self.source.unlink()
        with (
            mock.patch.object(watch_mod, "WATCH_LOCK_FILE", self.root / "watch.lock"),
            mock.patch.object(watch_mod, "_wait_or_stop", lambda *a, **k: None),
            mock.patch.object(watch_mod, "guard_encodes", side_effect=fake_guard),
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "decide", return_value="encode"),
            mock.patch.object(watch_mod, "is_stable", return_value=True),
            mock.patch.object(watch_mod, "disk_free_bytes", return_value=None),
        ):
            watch_mod.watch([self.root], None, once=True, poll_interval=0.0)
        self.assertEqual(captured["dirs"], [nested])

    def test_the_watcher_parks_the_slot_on_the_output_dir_when_given(self) -> None:
        """A two-directory watcher run keeps the output dir as the slot target.

        The output directory must live OUTSIDE the watched tree (the overlap
        guard refuses an inbox that contains it).
        """
        from shrinkray.engine import watch as watch_mod

        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured.setdefault("dirs", []).append(directory)
            return _null_guard(directory)

        def fake_encode(movie, output, **kwargs):
            return engine_pipeline.QualityResult(
                input=movie,
                output=output,
                success=False,
                skipped_reason="no size gain",
            )

        inbox = self.root / "in"
        out = self.root / "out"
        inbox.mkdir()
        (inbox / "movie.mkv").write_bytes(b"x" * 8)
        out.mkdir()
        with (
            mock.patch.object(watch_mod, "WATCH_LOCK_FILE", self.root / "watch.lock"),
            mock.patch.object(watch_mod, "_wait_or_stop", lambda *a, **k: None),
            mock.patch.object(watch_mod, "guard_encodes", side_effect=fake_guard),
            mock.patch.object(watch_mod, "encode_with_quality", side_effect=fake_encode),
            mock.patch.object(watch_mod, "decide", return_value="encode"),
            mock.patch.object(watch_mod, "is_stable", return_value=True),
            mock.patch.object(watch_mod, "disk_free_bytes", return_value=None),
        ):
            watch_mod.watch([inbox], out, once=True, poll_interval=0.0)
        self.assertEqual(captured["dirs"], [out])


def _null_guard(directory, holder="encode"):
    import contextlib

    @contextlib.contextmanager
    def ctx():
        yield Path(directory) / ".shrinkray_encode.lock"

    return ctx()
