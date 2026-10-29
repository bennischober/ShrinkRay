"""Tests for the run-identity guard (issue #436).

The engine used to derive "which bytes is this run about" ad hoc: a file
replaced while a run was in flight could leave the database holding
decisions about bytes that no longer exist. The identity is captured once
at run start (size + mtime) and rechecked loudly before a decision is
committed or an encode starts. Every test here is deterministic (mocked
seams, a same-size rewrite at a call-shaped seam) - none races a
stat:commit window.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shrinkray import db
from shrinkray.engine.pipeline import encode_with_quality
from shrinkray.engine.plan import QualityResult, plan_quality


def _movie(root: Path, content: bytes) -> Path:
    movie = root / "movie.mkv"
    movie.write_bytes(content)
    return movie


def _file_row(movie: Path) -> "db.MovieRecord":
    stat = movie.stat()
    return db.MovieRecord(
        path=movie,
        mtime=stat.st_mtime,
        size=stat.st_size,
        name=movie.name,
        category="movies",
        crop=0,
        hdr=0,
        other_codecs="",
        audio_languages="",
    )


class TestRunIdentityCapture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = _movie(self.root, b"x" * 16)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_identity_captured_once_matches_the_file(self) -> None:
        from shrinkray.engine.identity import capture_run_identity

        identity = capture_run_identity(self.movie)
        stat = self.movie.stat()
        self.assertEqual(identity.size, stat.st_size)
        self.assertEqual(identity.mtime, stat.st_mtime)

    def test_ensure_unchanged_raises_naming_the_stage(self) -> None:
        from shrinkray.engine.identity import RunIdentity, ensure_unchanged

        with self.assertRaisesRegex(RuntimeError, "the full encode") as ctx:
            ensure_unchanged(self.movie, RunIdentity(size=1, mtime=1.0), "the full encode")
        self.assertIn("movie.mkv", str(ctx.exception))

    def test_ensure_unchanged_passes_when_nothing_changed(self) -> None:
        from shrinkray.engine.identity import RunIdentity, ensure_unchanged

        stat = self.movie.stat()
        ensure_unchanged(self.movie, RunIdentity(size=stat.st_size, mtime=stat.st_mtime), "plan")


class TestProbeScratchAttribution(unittest.TestCase):
    """Issue #435: the probe scratch directories carry the source's name.

    One grammar, three mkdtemp sites (plan's probe workdir, the pipeline's
    probe workdir, the verification measurement's workdir); the name is
    decoration - the engine never reads it back.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = _movie(self.root, b"x" * 16)
        self.sample = self.root / "sample.mkv"
        self.sample.write_bytes(b"y" * 8)
        self.db_file = self.root / "metadata.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_prefix_carries_stem_and_identity(self) -> None:
        from shrinkray.engine.identity import probe_scratch_prefix

        prefix = probe_scratch_prefix(self.movie)
        self.assertTrue(prefix.startswith("shrinkray-probe-movie-"))
        digest = prefix.split("-")[-2]
        self.assertEqual(len(digest), 8)
        int(digest, 16)  # parses as hex
        # same movie, same digest; different movie, different digest
        other = self.root / "other.mkv"
        other.write_bytes(b"x" * 16)
        self.assertEqual(probe_scratch_prefix(self.movie), prefix)
        self.assertNotEqual(probe_scratch_prefix(other), prefix)
        # a stem containing hyphens keeps its name readable: the digest
        # separator survives
        dashed = self.root / "my-movie-2024.mkv"
        dashed.write_bytes(b"x" * 16)
        self.assertTrue(probe_scratch_prefix(dashed).startswith("shrinkray-probe-my-movie-2024-"))

    def test_prefix_caps_the_stem_utf8_safely(self) -> None:
        from shrinkray.engine.identity import PROBE_SCRATCH_STEM_BYTES, probe_scratch_prefix

        # a 40-char CJK stem: each char is 3 bytes, so the byte cap cuts
        # mid-name but never mid-character
        movie = self.root / ("\u65b0" * 40 + ".mkv")
        movie.write_bytes(b"x" * 16)
        prefix = probe_scratch_prefix(movie)
        # strip the fixed-width tail ("-<8 hex>-") and the grammar prefix
        stem_part = prefix[len("shrinkray-probe-") :][:-10]
        self.assertLessEqual(len(stem_part.encode("utf-8")), PROBE_SCRATCH_STEM_BYTES)
        stem_part.encode("utf-8")  # a cut that broke a character would fail here
        # the whole name stays far under the filesystem's 255-byte limit
        self.assertLess(len(prefix) + 8, 255)

    def test_all_three_scratch_sites_use_the_grammar(self) -> None:
        """Every scratch mkdtemp site derives its prefix from the one helper
        (no second implementation of the grammar)."""
        prefixes: list[str] = []
        self._plan_with_patched_scratch(prefixes)
        self._encode_with_patched_scratch(prefixes)
        # three sites created three scratch dirs (an empty list would pass
        # the loop above vacuously)
        self.assertEqual(len(prefixes), 3)
        for prefix in prefixes:
            self.assertTrue(
                prefix.startswith("shrinkray-probe-movie-"),
                f"anonymous scratch prefix survived: {prefix!r}",
            )

    def test_a_non_utf8_filename_hashes_instead_of_raising(self) -> None:
        """POSIX filenames arrive surrogate-escaped (os.fsdecode of the raw
        bytes); the digest input is the filename's on-disk bytes
        (os.fsencode), so such a source gets a stable, attributable prefix
        instead of a UnicodeEncodeError on the plan path (review finding 1).
        """
        from shrinkray.engine.identity import probe_scratch_prefix

        movie = self.root / os.fsdecode(b"\xff\xfe-movie.mkv")
        movie.write_bytes(b"x" * 16)
        first = probe_scratch_prefix(movie)
        second = probe_scratch_prefix(movie)
        self.assertEqual(first, second, "the digest must be stable per filename")
        self.assertTrue(first.startswith("shrinkray-probe-"))

    def _plan_with_patched_scratch(self, prefixes: list[str]) -> None:
        """Runs plan_quality with every external seam mocked and the scratch
        mkdtemp captured. (The plan path's patches stay in their own helper:
        Python 3.10 compiles each context manager of a with-statement as one
        nested block with a limit of 20 - a combined with would exceed it.)"""
        real_mkdtemp = tempfile.mkdtemp

        def fake_mkdtemp(prefix: str = "", **kwargs):
            prefixes.append(prefix)
            return real_mkdtemp(prefix=prefix, **kwargs)

        with (
            patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.plan.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.plan.extract_samples", return_value=[]),
            patch("shrinkray.engine.plan.estimate_size_factor", return_value=0.5),
            patch(
                "shrinkray.engine.plan.find_optimal_crf_and_skip",
                return_value=(20, 96.5, [], False),
            ),
            patch("shrinkray.engine.plan.tempfile.mkdtemp", side_effect=fake_mkdtemp),
        ):
            plan_quality(self.movie, db_file=None)

    def _encode_with_patched_scratch(self, prefixes: list[str]) -> None:
        """Runs encode_with_quality (verify=True, so the verification workdir
        is created too) with every external seam mocked and the scratch
        mkdtemp captured."""
        real_mkdtemp = tempfile.mkdtemp

        def fake_encode(input_file, output_file, command, **kwargs):
            output_file.write_bytes(b"out" * 4)  # smaller than the 16-byte source
            return SimpleNamespace(success=True, cancelled=False, error=None)

        def fake_mkdtemp(prefix: str = "", **kwargs):
            prefixes.append(prefix)
            return real_mkdtemp(prefix=prefix, **kwargs)

        with (
            patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
            patch(
                "shrinkray.engine.pipeline.durations_match", return_value=True
            ),  # the stub output is not a real video; the 1:1 check must not gate it
            patch("shrinkray.engine.pipeline.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.pipeline.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.pipeline.source_codec", return_value="h264"),
            patch("shrinkray.engine.pipeline.source_bit_depth", return_value=None),
            patch("shrinkray.engine.pipeline.extract_samples", return_value=[]),
            patch(
                "shrinkray.engine.pipeline.find_optimal_crf_and_skip",
                return_value=(20, 96.5, [], False),
            ),
            patch("shrinkray.engine.pipeline.tempfile.mkdtemp", side_effect=fake_mkdtemp),
            # the verification measurement is a real binary seam; this test's
            # subject is the scratch grammar, not VMAF (review finding 3)
            patch("shrinkray.engine.pipeline.measure_vmaf", return_value=94.5),
            patch("shrinkray.engine.pipeline._encode_with_command", side_effect=fake_encode),
        ):
            out = self.root / "out" / "movie.mkv"
            out.parent.mkdir()
            # the mocked encode writes a SMALLER output: the product's size
            # gate ("a re-encode happens only when the file gets smaller")
            # returns before the verification block otherwise, and the third
            # scratch site (the verify workdir) is never created
            encode_with_quality(self.movie, out, db_file=None, verify=True)

    def test_scratch_name_is_decoration_for_the_probe_cache(self) -> None:
        """The cache is keyed only from its supplied identity inputs: two
        probe runs of the same movie whose scratch directory carries a
        DIFFERENT name still share one cache row - the name is never an
        identity input.
        """
        import inspect

        from shrinkray.engine import search as engine_search

        # the compile-time pin: probe_cache_key has no workdir/name input
        params = inspect.signature(engine_search.probe_cache_key).parameters
        self.assertNotIn("workdir", params)
        self.assertNotIn("prefix", params)

        # the runtime pin: probe_crf into two differently named workdirs over
        # one database - the second run reuses the first run's measurement
        encodes: list = []

        def fake_run(command, cancel=None):
            encodes.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", return_value=95.5),
            tempfile.TemporaryDirectory() as scratch,
        ):
            first = Path(scratch) / "shrinkray-probe-movie-aaaaaaaa-1"
            second = Path(scratch) / "shrinkray-probe-movie-bbbbbbbb-1"
            first.mkdir()
            second.mkdir()
            common = {
                "samples": [(self.sample, 10.0)],
                "crf": 20,
                "handbrake": "hb",
                "movie": self.movie,
                "sample_seconds": 10.0,
                "encoder": "svt_av1",
                "probe_encoder_preset": "medium",
                "db_file": self.db_file,
            }
            engine_search.probe_crf(workdir=first, **common)
            engine_search.probe_crf(workdir=second, **common)
        self.assertEqual(len(encodes), 1, "the differently named scratch must not re-measure")


class TestPlanPathGuard(unittest.TestCase):
    """The issue's plan-path reproduction: a QualityResult whose probes
    derive from bytes other than the file on disk must not commit.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = _movie(self.root, b"ORIGINAL" * 100)
        self.connection = db.connect(self.root / "metadata.db")
        db.upsert_file(self.connection, _file_row(self.movie))

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_a_same_size_replacement_between_probes_and_record_stops_the_commit(self) -> None:
        """The file is rewritten (same size, different mtime) at the seam
        between the mocked search and the recording call: the engine's
        record-time recheck raises (loud abort), no row lands.
        """
        calls: list[dict] = []

        def fake_search(*args, **kwargs):
            # the replacement happens AFTER the search (the call-shaped seam),
            # BEFORE the engine's recheck: a size-only guard would bless this
            self.movie.write_bytes(b"REPLACED" * 100)
            calls.append({"searched": True})
            return 20, 96.5, [], False

        with (
            patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.plan.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.plan.extract_samples", return_value=[]),
            patch("shrinkray.engine.plan.estimate_size_factor", return_value=0.5),
            patch("shrinkray.engine.plan.find_optimal_crf_and_skip", side_effect=fake_search),
            self.assertRaises(RuntimeError),
        ):
            plan_quality(self.movie, db_file=None)
        self.assertEqual(calls, [{"searched": True}])
        self.assertIsNone(
            db.get_valid_plan(
                self.connection,
                self.movie,
                encoder="svt_av1",
                encoder_preset="5",
                target_vmaf=96.0,
                crf_range=(14, 45),
                sample_count=6,
                sample_seconds=10.0,
                crop_sample_count=10,
                vmaf_statistic="mean",
                vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            )
        )

    def test_an_unchanged_file_records_and_the_row_carries_the_run_mtime(self) -> None:
        """The happy path is untouched: the row commits, and its mtime is
        the run-start capture (not a commit-time re-read).
        """
        from shrinkray.service import record_plan

        mtimes: list[float] = []

        def fake_search(*args, **kwargs):
            mtimes.append(self.movie.stat().st_mtime)
            return 20, 96.5, [], False

        with (
            patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.plan.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.plan.extract_samples", return_value=[]),
            patch("shrinkray.engine.plan.estimate_size_factor", return_value=0.5),
            patch("shrinkray.engine.plan.find_optimal_crf_and_skip", side_effect=fake_search),
        ):
            result = plan_quality(self.movie, db_file=None)
        self.assertEqual(result.input_mtime, mtimes[0])
        record_plan(self.connection, self.movie, result, _config())
        row = db.get_valid_plan(
            self.connection,
            self.movie,
            encoder="svt_av1",
            encoder_preset="5",
            target_vmaf=96.0,
            crf_range=(14, 45),
            sample_count=6,
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
        )
        self.assertIsNotNone(row)


class TestEncodeStartRecheck(unittest.TestCase):
    """The encode-start recheck: a CRF chosen from bytes-A is never encoded
    against bytes-B (decision integrity, the issue's criterion 3).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = _movie(self.root, b"ORIGINAL" * 100)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_replacement_between_search_and_encode_aborts_loudly(self) -> None:
        """The file is rewritten same-size at the call-shaped seam between
        the (mocked) probe pipeline and the encode-start recheck: the recheck
        raises before any HandBrake child runs.
        """

        def fake_probe(*args, **kwargs):
            # the replacement happens AFTER the search decided (inside the
            # probe step) and BEFORE the engine's recheck
            self.movie.write_bytes(b"REPLACED" * 100)
            return 20, 96.5, [], None

        def exploding_encode(*args, **kwargs):
            raise AssertionError("the encode must not start on replaced bytes")

        with (
            patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.pipeline.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.pipeline._probe_movie", side_effect=fake_probe),
            patch("shrinkray.engine.pipeline._encode_with_command", side_effect=exploding_encode),
            self.assertRaisesRegex(RuntimeError, "changed while the run"),
        ):
            encode_with_quality(
                self.movie,
                self.root / "out" / "movie.mkv",
                db_file=None,
            )

    def test_an_unchanged_file_encodes(self) -> None:
        """The happy path is untouched: no abort, the encode runs."""
        out = self.root / "out" / "movie.mkv"
        out.parent.mkdir()
        out.write_bytes(b"NEW" * 50)
        with (
            patch("shrinkray.engine.pipeline.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.pipeline.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.pipeline._probe_movie", return_value=(20, 96.5, [], None)),
            patch(
                "shrinkray.engine.pipeline._encode_with_command",
                return_value=SimpleNamespace(success=True, cancelled=False, error=None),
            ),
        ):
            result = encode_with_quality(
                self.movie,
                out,
                db_file=None,
            )
        self.assertTrue(result.success)


class TestEngineSkipPathGuard(unittest.TestCase):
    """The reviewer's finding 1: the guards must hold for the shape the
    ENGINE emits - a skip produced by plan_quality/encode_with_quality
    carries the run-start mtime, so a same-size replacement before the
    record call aborts instead of committing a poisoned fingerprint.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = _movie(self.root, b"ORIGINAL" * 100)
        self.connection = db.connect(self.root / "metadata.db")
        db.upsert_file(self.connection, _file_row(self.movie))

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _plan_with_engine_skip(self):
        """Runs plan_quality with the mocked search returning skip=True and
        the same-size rewrite inside it (the call-shaped seam)."""

        def fake_search(*args, **kwargs):
            self.movie.write_bytes(b"REPLACED" * 100)
            return 20, 96.5, [], True

        with (
            patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.plan.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.plan.extract_samples", return_value=[]),
            patch("shrinkray.engine.plan.estimate_size_factor", return_value=0.5),
            patch("shrinkray.engine.plan.find_optimal_crf_and_skip", side_effect=fake_search),
            self.assertRaises(RuntimeError),
        ):
            plan_quality(self.movie, db_file=None)

    def test_an_engine_skip_result_aborts_on_a_same_size_replacement(self) -> None:
        """The engine's own recheck fires first (the rewrite happens inside
        the search); no plan row may exist afterwards.
        """
        self._plan_with_engine_skip()
        self.assertIsNone(
            db.get_valid_plan(
                self.connection,
                self.movie,
                encoder="svt_av1",
                encoder_preset="5",
                target_vmaf=96.0,
                crf_range=(14, 45),
                sample_count=6,
                sample_seconds=10.0,
                crop_sample_count=10,
                vmaf_statistic="mean",
                vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            )
        )

    def test_the_engine_skip_result_carries_the_run_mtime_for_the_record_guard(
        self,
    ) -> None:
        """The guard's second half: even when the engine's recheck is outside
        the window (a replacement AFTER plan_quality returned), the skip
        result carries input_mtime, so record_plan's own gate fires.
        """
        from shrinkray.service import record_plan

        def fake_search(*args, **kwargs):
            return 20, 96.5, [], True

        with (
            patch("shrinkray.engine.plan.find_handbrake", return_value="hb"),
            patch("shrinkray.engine.plan.detect_crop_summary_cached", return_value=None),
            patch("shrinkray.engine.plan.detect_crop_rect", return_value=None),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.plan.extract_samples", return_value=[]),
            patch("shrinkray.engine.plan.estimate_size_factor", return_value=0.5),
            patch("shrinkray.engine.plan.find_optimal_crf_and_skip", side_effect=fake_search),
        ):
            result = plan_quality(self.movie, db_file=None)
        self.assertIsNotNone(result.input_mtime)
        # the replacement happens AFTER the engine returned (a caller-side
        # window the engine's recheck cannot cover)
        self.movie.write_bytes(b"REPLACED" * 100)
        with self.assertRaises(RuntimeError):
            record_plan(self.connection, self.movie, result, _config())

    def test_a_gate_skip_result_stops_the_record_when_the_file_grew(self) -> None:
        """The reviewer's finding 2: a gate-skip result (below min size) now
        carries the run facts, so a replacement that grew the file between
        the gate and the record cannot commit the new identity with the old
        verdict.
        """
        from shrinkray.service import record_plan

        with (
            patch("shrinkray.engine.plan.probe_duration_seconds", return_value=3600.0),
            patch("shrinkray.engine.plan.source_codec", return_value="h264"),
            patch("shrinkray.engine.plan.source_bit_depth", return_value=None),
        ):
            result = plan_quality(self.movie, db_file=None, min_source_size_gb=0.001)
        self.assertEqual(result.skipped_reason, "below min size")
        self.assertIsNotNone(result.input_mtime)
        # the file grew after the gate decided (the replacement window)
        self.movie.write_bytes(b"X" * 100_000)
        with self.assertRaises(RuntimeError):
            record_plan(self.connection, self.movie, result, _config())

    def test_a_record_gate_error_is_one_error_row_never_an_aborted_plan(self) -> None:
        """The round-2 review's finding 5: the record call sits inside the
        per-movie error handling on both entry points - one changed file is
        one error row/entry, the plan run continues to the next movie.
        """
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie_a = root / "a.mkv"
            movie_b = root / "b.mkv"
            movie_a.write_bytes(b"x" * 16)
            movie_b.write_bytes(b"x" * 16)
            # the record path needs a database: without --db the connection is
            # None and the record call (the code under test) never runs
            db_file = root / "metadata.db"
            conn = db.connect(db_file)
            stat = movie_b.stat()
            db.upsert_file(
                conn,
                db.MovieRecord(
                    path=movie_b,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    name=movie_b.name,
                    category="movies",
                    crop=0,
                    hdr=0,
                    other_codecs="",
                    audio_languages="",
                ),
            )
            conn.close()
            calls: list[Path] = []

            def fake_plan(movie_in, *args, **kwargs):
                calls.append(movie_in)
                if len(calls) == 2:
                    # the second movie's record will raise (its file changed
                    # after the run decided it)
                    movie_b.write_bytes(b"y" * 32)
                return QualityResult(
                    input=movie_in,
                    output=movie_in,
                    success=True,
                    target_vmaf=96.0,
                    chosen_crf=20,
                    achieved_vmaf=96.5,
                    probes=[],
                    input_size=16,
                    output_size=8,
                    input_mtime=movie_in.stat().st_mtime,
                    crf_range=(14, 45),
                    sample_count=3,
                    sample_seconds=10.0,
                )

            with (
                patch("shrinkray.engine.plan.plan_quality", side_effect=fake_plan),
                patch("shrinkray.service.all_problems", return_value=[]),
            ):
                result = CliRunner().invoke(app, ["plan", str(root), "--db", str(db_file)])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(calls), 2, "the second movie must still be planned")
        # both halves of the pin: the error row renders (the run did not
        # abort before the table printed) and the run continued
        self.assertIn("b.mkv", result.output)
        self.assertIn("error", result.output)


def _config():
    from shrinkray.config import load_config

    return load_config()


class TestJobPathGuard(unittest.TestCase):
    """Acceptance criterion 5's job path: an identity abort in a queue job
    ends FAILED with the message, and the failure bundle is written.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.connection = db.connect(self.root / "metadata.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_an_identity_abort_fails_the_encode_job_with_telemetry(self) -> None:
        from shrinkray.server import jobs

        movie = _movie(self.root, b"ORIGINAL" * 100)
        queue = jobs.JobQueue(self.connection)
        job = queue.enqueue(
            jobs.Job(
                kind=jobs.JobKind.ENCODE,
                payload={"paths": [str(movie)], "quality": True},
            )
        )

        def abort(*args, **kwargs):
            raise RuntimeError(
                f"'{movie.name}' changed while the run was measuring it - "
                "the encode would record a decision about bytes it never measured"
            )

        # the encode job has no per-movie catch for a RuntimeError (only the
        # worker's outer handler): the identity abort reaches queue.fail and
        # the telemetry bundle, which IS the honest report for a decision
        # that cannot be measured
        with (
            patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=abort) as engine,
            patch("shrinkray.telemetry.save_failure") as telemetry,
        ):
            jobs.JobWorker(self.connection, queue)._execute(job)
        engine.assert_called_once()
        stored = queue.get(job.id)
        assert stored is not None
        self.assertEqual(stored.state, jobs.JobState.FAILED)
        self.assertIn("changed while the run was measuring it", stored.error or "")
        telemetry.assert_called_once()


class TestCliExitGuard(unittest.TestCase):
    """Acceptance criterion 5's CLI path: the abort exits nonzero."""

    def test_a_plan_abort_exits_nonzero(self) -> None:
        from typer.testing import CliRunner

        from shrinkray.__main__ import app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = _movie(root, b"x" * 16)
            with (
                patch(
                    "shrinkray.engine.plan.plan_quality",
                    side_effect=RuntimeError("'movie.mkv' changed while the run was measuring it"),
                ),
                patch("shrinkray.service.all_problems", return_value=[]),
            ):
                result = CliRunner().invoke(app, ["plan", str(movie)])
        self.assertNotEqual(result.exit_code, 0)


class TestEncodePathSkipGuard(unittest.TestCase):
    """The issue's skip-path reproduction: a verdict derived from bytes-A
    must not commit under bytes-B's identity (record_quality_results).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.movie = _movie(self.root, b"ORIGINAL" * 100)
        self.connection = db.connect(self.root / "metadata.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_a_same_size_replacement_between_decision_and_record_raises(self) -> None:
        """The result was decided with input_mtime at T1; the file is rewritten
        same-size before the record call: loud abort, no skip row.
        """
        from shrinkray.service import record_quality_results

        result = QualityResult(
            input=self.movie,
            output=self.movie,
            success=False,
            skipped_reason="no size gain",
            error="skipped: re-encode would not shrink the file",
            input_size=self.movie.stat().st_size,
            input_mtime=self.movie.stat().st_mtime,
        )
        # the call-shaped seam: the replacement happens between the engine's
        # decision and the caller's record call
        self.movie.write_bytes(b"REPLACED" * 100)
        with self.assertRaises(RuntimeError):
            record_quality_results(self.connection, self.movie, result, _config())
        # a skip is an encode row with a 'skipped (...)' preset label (add_skip):
        # none may exist for the poisoned verdict
        row = self.connection.execute(
            """
            SELECT e.* FROM encodes e JOIN files f ON f.id = e.file_id
            WHERE f.path = ? AND e.encoder LIKE 'skipped%'
            """,
            (str(self.movie),),
        ).fetchone()
        self.assertIsNone(row, "the poisoned skip verdict must not commit")

    def test_an_unchanged_file_still_records_the_skip(self) -> None:
        """The happy path: same bytes, the skip row lands as before."""
        from shrinkray.service import record_quality_results

        stat = self.movie.stat()
        result = QualityResult(
            input=self.movie,
            output=self.movie,
            success=False,
            skipped_reason="no size gain",
            error="skipped: re-encode would not shrink the file",
            input_size=stat.st_size,
            input_mtime=stat.st_mtime,
        )
        outcome = record_quality_results(self.connection, self.movie, result, _config())
        self.assertTrue(outcome.skipped)

    def test_a_result_without_run_facts_still_records(self) -> None:
        """A result built outside the engine's capture (input_mtime None,
        input_size 0) keeps today's behavior - the guard needs facts to
        compare and must not invent them.
        """
        from shrinkray.service import record_quality_results

        result = QualityResult(
            input=self.movie,
            output=self.movie,
            success=False,
            skipped_reason="no size gain",
            error="skipped: re-encode would not shrink the file",
        )
        outcome = record_quality_results(self.connection, self.movie, result, _config())
        self.assertTrue(outcome.skipped)


if __name__ == "__main__":
    unittest.main()
