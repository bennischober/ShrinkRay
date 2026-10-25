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
        },
    )()


class TestInPlaceSlotKeying(unittest.TestCase):
    """Two in-place writers of one library must not both hold the slot."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_in_place_writers_of_one_library_conflict(self) -> None:
        """A second in-place writer takes the slot on the SAME library root.

        The old shape locked each writer's own staging directory: two `mkdtemp`
        paths never collide, so both writers believed they were exclusive and
        both encoded the same movie. Distinct staging paths are not the
        contended resource - the library directory is (issue #230).
        """
        with guard_encodes(self.root, holder="one"), self.assertRaises(EncodeBusyError):
            guard_encodes(self.root, holder="two").__enter__()
        self.assertFalse(_lock_path(self.root).exists())

    def test_the_cli_parks_the_slot_on_the_library_root_in_place(self) -> None:
        """`_record_encodes` keys the slot on the library root when in place.

        Asserted at the seam: the guard receives the library root, never the
        staging directory, when the run replaces sources in place.
        """
        from shrinkray import __main__ as cli

        captured: dict = {}

        def fake_guard(directory, holder="encode"):
            captured["dir"] = directory
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
                library_root=self.root,
            )
        self.assertEqual(captured["dir"], self.root)
        self.assertEqual(captured["holder"], "encode-cli")

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
                library_root=None,
            )
        self.assertEqual(captured["dir"], out)

    def test_the_watcher_parks_the_slot_on_the_movies_directory_in_place(self) -> None:
        """The watcher's in-place slot is keyed on the movie's own directory.

        The swaps rename inside it, and one slot at a time keeps the pass
        nesting-free across multiple roots.
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
        self.assertEqual(captured["dirs"], [self.root])

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
