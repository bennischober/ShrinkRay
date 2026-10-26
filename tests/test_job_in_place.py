"""Tests for the encode job's in-place path (issue #192).

The backlog's encode payload carries no output_dir - the job crashed on the
unconditional `payload["output_dir"]` index on every container boot, and the
dashboard showed a failed encode job every start. A missing output_dir now
means the IN-PLACE form: staging inside the scanned root, swap after verify,
original retained - the same contract the CLI documents.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shrinkray import replace
from shrinkray.server import jobs


class TestEncodeJobInPlace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "library"
        self.root.mkdir()
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"x" * 16)
        from shrinkray import db

        self.connection = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def _job(self, **extra) -> "object":
        payload = {"paths": [str(self.root)], "quality": True, **extra}
        return jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)

    def test_a_backlog_payload_does_not_crash(self) -> None:
        """The exact payload auto_backlog enqueues executes without a KeyError.

        Reproduction of the issue: execute_encode indexed
        payload["output_dir"] unconditionally, so the boot backlog's encode
        job failed on every container start.
        """
        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
        ):

            def encode_and_write(movie, output, **kwargs):
                output.write_bytes(b"y" * 8)
                return SimpleNamespace(
                    input_size=16,
                    output_size=8,
                    chosen_crf=20,
                    achieved_vmaf=95.0,
                    error=None,
                    input_width=1920,
                    input_height=1080,
                    input_codec="h264",
                    input_bit_depth=None,
                    input_duration=600.0,
                    input_bitrate=None,
                    output_bitrate=None,
                    hdr=False,
                    encoder="svt_av1",
                    encoder_preset="3",
                    probe_encoder_preset="5",
                    probes=[],
                    encode_seconds=1.0,
                    probe_seconds=1.0,
                    verified_vmaf=None,
                    verified_subsample=None,
                    skipped_reason=None,
                    success=True,
                    output=self.root / "movie.mkv",
                    crf_source="probe",
                    plan_created_at=None,
                    target_vmaf=96.0,
                )

            fake_encode.side_effect = encode_and_write
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            result = jobs.execute_encode(self._job(), self.connection, threading.Event())
        self.assertEqual(result["outcomes"][0]["movie"], "movie.mkv")
        self.assertEqual(result["outcomes"][0]["state"], "encoded")

    def test_the_in_place_encode_swaps_into_the_library(self) -> None:
        """A verified output takes the source's path; the original is retained."""
        from shrinkray import replace

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
        ):

            def encode_and_write(movie, output, **kwargs):
                output.write_bytes(b"y" * 8)
                return SimpleNamespace(
                    input_size=16,
                    output_size=8,
                    chosen_crf=20,
                    achieved_vmaf=95.0,
                    error=None,
                    success=True,
                    skipped_reason=None,
                    probes=[],
                    probe_seconds=1.0,
                    encode_seconds=1.0,
                    target_vmaf=96.0,
                    hdr=False,
                    encoder="svt_av1",
                    encoder_preset="3",
                    probe_encoder_preset="5",
                    input_width=None,
                    input_height=None,
                    input_codec=None,
                    input_bit_depth=None,
                    input_duration=None,
                    input_bitrate=None,
                    output_bitrate=None,
                    verified_vmaf=None,
                    verified_subsample=None,
                    crf_source="probe",
                    plan_created_at=None,
                )

            fake_encode.side_effect = encode_and_write
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            result = jobs.execute_encode(self._job(), self.connection, threading.Event())
        # the library file now holds the (mocked) smaller encode, and the
        # original is retained beside it
        self.assertEqual(self.movie.read_bytes(), b"y" * 8)
        retained = replace.retained_path_for(self.movie)
        self.assertTrue(retained.is_file())
        self.assertEqual(result["outcomes"][0]["state"], "encoded")

    def test_a_declined_swap_reports_a_skip_not_a_crash(self) -> None:
        """A swap refused (retained original present) is an honest outcome."""
        from shrinkray import replace

        replace.retained_path_for(self.movie).write_bytes(b"OLDER")
        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
        ):

            def encode_and_write(movie, output, **kwargs):
                output.write_bytes(b"y" * 8)
                return SimpleNamespace(
                    input_size=16,
                    output_size=8,
                    chosen_crf=20,
                    achieved_vmaf=95.0,
                    error=None,
                    success=True,
                    skipped_reason=None,
                    probes=[],
                    probe_seconds=1.0,
                    encode_seconds=1.0,
                    target_vmaf=96.0,
                    hdr=False,
                    encoder="svt_av1",
                    encoder_preset="3",
                    probe_encoder_preset="5",
                    input_width=None,
                    input_height=None,
                    input_codec=None,
                    input_bit_depth=None,
                    input_duration=None,
                    input_bitrate=None,
                    output_bitrate=None,
                    verified_vmaf=None,
                    verified_subsample=None,
                    crf_source="probe",
                    plan_created_at=None,
                )

            fake_encode.side_effect = encode_and_write
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            result = jobs.execute_encode(self._job(), self.connection, threading.Event())
        self.assertEqual(result["outcomes"][0]["state"], "skipped")
        self.assertIn("declined", result["outcomes"][0]["error"])

    def test_the_staging_root_is_the_scanned_root_not_its_parent(self) -> None:
        """The boot backlog's payload names a DIRECTORY: staging happens inside it.

        Reproduction of issue #335: execute_encode derived the staging root
        from `first[0].parent`, which is `/` for the container's canonical
        `{"paths": ["/media"]}` payload — the staging TemporaryDirectory was
        created at `/` and the job failed with PermissionError on every boot.
        The watcher (watch.py) stages inside `roots[0]` itself; the job must
        agree. A file payload keeps the parent form.
        """
        from unittest.mock import patch

        recorded_roots: list[Path] = []
        real_td = tempfile.TemporaryDirectory

        def spying_td(*args, **kwargs):
            recorded_roots.append(Path(kwargs["dir"]))
            return real_td(*args, **kwargs)

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
            patch("shrinkray.server.jobs.tempfile.TemporaryDirectory", spying_td),
        ):
            fake_encode.return_value = SimpleNamespace(
                input_size=16,
                output_size=8,
                chosen_crf=20,
                achieved_vmaf=95.0,
                error=None,
                success=False,
                skipped_reason="resolution too low",
                probes=[],
                probe_seconds=1.0,
                encode_seconds=1.0,
                target_vmaf=96.0,
                hdr=False,
                encoder="svt_av1",
                encoder_preset="3",
                probe_encoder_preset="5",
                input_width=None,
                input_height=None,
                input_codec=None,
                input_bit_depth=None,
                input_duration=None,
                input_bitrate=None,
                output_bitrate=None,
                verified_vmaf=None,
                verified_subsample=None,
                crf_source="probe",
                plan_created_at=None,
            )
            fake_record.return_value = SimpleNamespace(
                encoded=False, skipped=True, error=None, encode_id=None
            )
            result = jobs.execute_encode(self._job(), self.connection, threading.Event())
        # the staging dir was created INSIDE the scanned root, never at `/`
        self.assertEqual(recorded_roots, [self.root])
        self.assertEqual(result["outcomes"][0]["state"], "skipped")

    def test_the_staging_root_for_a_file_payload_is_its_parent(self) -> None:
        """A single-file payload stages beside the file (same filesystem)."""
        from unittest.mock import patch

        recorded_roots: list[Path] = []
        real_td = tempfile.TemporaryDirectory

        def spying_td(*args, **kwargs):
            recorded_roots.append(Path(kwargs["dir"]))
            return real_td(*args, **kwargs)

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
            patch("shrinkray.server.jobs.tempfile.TemporaryDirectory", spying_td),
        ):
            fake_encode.return_value = SimpleNamespace(
                input_size=16,
                output_size=8,
                chosen_crf=20,
                achieved_vmaf=95.0,
                error=None,
                success=False,
                skipped_reason="resolution too low",
                probes=[],
                probe_seconds=1.0,
                encode_seconds=1.0,
                target_vmaf=96.0,
                hdr=False,
                encoder="svt_av1",
                encoder_preset="3",
                probe_encoder_preset="5",
                input_width=None,
                input_height=None,
                input_codec=None,
                input_bit_depth=None,
                input_duration=None,
                input_bitrate=None,
                output_bitrate=None,
                verified_vmaf=None,
                verified_subsample=None,
                crf_source="probe",
                plan_created_at=None,
            )
            fake_record.return_value = SimpleNamespace(
                encoded=False, skipped=True, error=None, encode_id=None
            )
            payload = {"paths": [str(self.movie)], "quality": True}
            job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)
            result = jobs.execute_encode(job, self.connection, threading.Event())
        self.assertEqual(recorded_roots, [self.root])
        self.assertEqual(result["outcomes"][0]["state"], "skipped")

    def test_an_unmeasured_payload_is_rejected_not_silently_fixed(self) -> None:
        """A payload without quality:true fails the job, never an unmeasured
        encode (issue #329, invariant 6): the fixed-preset path was deleted,
        so the branch must not exist to fall into. It raises, so the worker
        marks the job FAILED rather than DONE, and it does so before staging
        or scanning anything inside the media root.
        """
        from unittest.mock import patch

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
            patch.object(jobs, "resolve_movies") as fake_resolve,
        ):
            payload = {"paths": [str(self.movie)], "quality": False}
            job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)
            with self.assertRaisesRegex(ValueError, "quality"):
                jobs.execute_encode(job, self.connection, threading.Event())
        fake_encode.assert_not_called()
        fake_record.assert_not_called()
        fake_resolve.assert_not_called()
        staged = [
            p for p in self.movie.parent.iterdir() if p.name.startswith(jobs.STAGING_DIR_PREFIX)
        ]
        self.assertEqual(staged, [], "a rejected payload staged inside the media root")

    def test_the_worker_records_an_unmeasured_job_as_failed(self) -> None:
        """The operator sees the rejection: the job ends FAILED, not DONE."""
        queue = jobs.JobQueue(self.connection)
        job = queue.enqueue(
            jobs.Job(kind=jobs.JobKind.ENCODE, payload={"paths": [str(self.movie)]})
        )
        # direct execution: deterministic, no thread wait
        jobs.JobWorker(self.connection, queue)._execute(job)
        stored = queue.get(job.id)
        assert stored is not None
        self.assertEqual(stored.state, jobs.JobState.FAILED)
        self.assertIn("quality", stored.error or "")

    def test_the_job_worker_verifies_by_default(self) -> None:
        """Mode B verifies every encode (issue #235): the job worker is the
        resident mode's other arm - nobody watches the log in an unattended
        container, so the full-file VMAF is the only evidence the encode
        met the target that the operator ever gets. A payload can still opt
        out with verify=false explicitly.
        """
        from unittest.mock import patch

        captured: dict = {}

        def fake_quality(movie, output_file, **kwargs):
            captured.update(kwargs)
            # in place, the job swaps after recording: the swap stats the
            # staged output, so the fake must write it (the happy-path test
            # does the same)
            output_file.write_bytes(b"y" * 8)
            return SimpleNamespace(
                input_size=16,
                output_size=8,
                chosen_crf=20,
                achieved_vmaf=95.0,
                error=None,
                success=True,
                skipped_reason=None,
                probes=[],
                probe_seconds=1.0,
                encode_seconds=1.0,
                target_vmaf=96.0,
                hdr=False,
                encoder="svt_av1",
                encoder_preset="3",
                probe_encoder_preset="5",
                input_width=None,
                input_height=None,
                input_codec=None,
                input_bit_depth=None,
                input_duration=None,
                input_bitrate=None,
                output_bitrate=None,
                verified_vmaf=94.5,
                verified_subsample=None,
                crf_source="probe",
                plan_created_at=None,
            )

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_quality),
            patch("shrinkray.service.record_quality_results") as fake_record,
        ):
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            jobs.execute_encode(self._job(), self.connection, threading.Event())
        self.assertTrue(captured.get("verify"))
        # an explicit opt-out is honoured
        captured.clear()
        with (
            patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=fake_quality),
            patch("shrinkray.service.record_quality_results") as fake_record,
        ):
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            payload = {"paths": [str(self.movie)], "quality": True, "verify": False}
            job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)
            jobs.execute_encode(job, self.connection, threading.Event())
        self.assertFalse(captured.get("verify"))

    def test_an_output_dir_payload_keeps_the_two_directory_form(self) -> None:
        """Naming an output dir keeps the old behaviour (no swap)."""
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        with (
            patch("shrinkray.engine.pipeline.encode_with_quality") as fake_encode,
            patch("shrinkray.service.record_quality_results") as fake_record,
        ):
            fake_encode.return_value = SimpleNamespace(
                input_size=16,
                output_size=8,
                chosen_crf=20,
                achieved_vmaf=95.0,
                error=None,
                success=True,
                skipped_reason=None,
                probes=[],
                probe_seconds=1.0,
                encode_seconds=1.0,
                target_vmaf=96.0,
                hdr=False,
                encoder="svt_av1",
                encoder_preset="3",
                probe_encoder_preset="5",
                input_width=None,
                input_height=None,
                input_codec=None,
                input_bit_depth=None,
                input_duration=None,
                input_bitrate=None,
                output_bitrate=None,
                verified_vmaf=None,
                verified_subsample=None,
                crf_source="probe",
                plan_created_at=None,
            )
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            result = jobs.execute_encode(
                self._job(output_dir=str(out)), self.connection, threading.Event()
            )
        self.assertEqual(result["outcomes"][0]["state"], "encoded")
        # the source is untouched: no swap happened
        self.assertEqual(self.movie.read_bytes(), b"x" * 16)
        self.assertFalse(replace.retained_path_for(self.movie).exists())

    def test_the_in_place_slot_is_keyed_on_the_movies_own_directory(self) -> None:
        """In place the slot must be the MOVIE'S OWN DIRECTORY (issue #230):
        that is where the staging copy and the swap's two renames happen, so it
        is the only key every writer can agree on. A key on a library root is
        not an identity - two writers given nested roots take two different
        locks for one file (#379).

        The movie sits in a nested folder, so movie.parent differs from both the
        payload root and the staging directory's parent: only the movie's own
        folder can pass this test.
        """
        from shrinkray.engine.lock import _lock_path, guard_encodes

        nested = self.root / "Movies"
        nested.mkdir()
        movie = nested / "deep.mkv"
        movie.write_bytes(b"x" * 16)
        payload = {"paths": [str(self.root)], "quality": True}
        job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=_fake_quality),
            patch("shrinkray.service.record_quality_results") as fake_record,
            patch("shrinkray.service.swap_encode_into_place") as fake_swap,
        ):
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            fake_swap.return_value = SimpleNamespace(replaced=True, reason=None)
            # a CLI run holds the MOVIE's folder: the job must skip
            with guard_encodes(nested, holder="encode-cli"):
                result = jobs.execute_encode(job, self.connection, threading.Event())
            self.assertEqual(result["outcomes"][0]["state"], "skipped")
            self.assertIn("another encoder", result["outcomes"][0]["error"])
            # the converse pins the identity: a slot on the payload ROOT does
            # NOT stop the job, because the root is not what is contended
            with guard_encodes(self.root, holder="encode-cli-root"):
                result = jobs.execute_encode(job, self.connection, threading.Event())
            self.assertEqual(result["outcomes"][0]["state"], "encoded")
            self.assertFalse(_lock_path(nested).exists())

    def test_a_multi_path_payload_locks_each_movies_own_directory(self) -> None:
        """A payload naming TWO roots keys each movie on its own folder: a hold
        on root B must skip b.mkv and leave root A's movie alone."""
        from shrinkray.engine.lock import _lock_path, guard_encodes

        root_b = self.root / "second"
        root_b.mkdir()
        movie_b = root_b / "b.mkv"
        movie_b.write_bytes(b"x" * 16)
        payload = {"paths": [str(self.root), str(root_b)], "quality": True}
        job = jobs.Job(kind=jobs.JobKind.ENCODE, payload=payload)

        with (
            patch("shrinkray.engine.pipeline.encode_with_quality", side_effect=_fake_quality),
            patch("shrinkray.service.record_quality_results") as fake_record,
            patch("shrinkray.service.swap_encode_into_place") as fake_swap,
        ):
            fake_record.return_value = SimpleNamespace(
                encoded=True, skipped=False, error=None, encode_id=None
            )
            fake_swap.return_value = SimpleNamespace(replaced=True, reason=None)
            with guard_encodes(root_b, holder="encode-cli-b"):
                result = jobs.execute_encode(job, self.connection, threading.Event())
        by_movie = {o["movie"]: o for o in result["outcomes"]}
        self.assertEqual(by_movie["b.mkv"]["state"], "skipped")
        self.assertIn("another encoder", by_movie["b.mkv"]["error"])
        self.assertEqual(by_movie["movie.mkv"]["state"], "encoded")
        self.assertFalse(_lock_path(root_b).exists())


def _fake_quality(movie_in, output_file, **kwargs):
    output_file.write_bytes(b"y" * 8)
    return SimpleNamespace(
        input_size=16,
        output_size=8,
        chosen_crf=20,
        achieved_vmaf=95.0,
        error=None,
        success=True,
        skipped_reason=None,
        probes=[],
        probe_seconds=1.0,
        encode_seconds=1.0,
        target_vmaf=96.0,
        hdr=False,
        encoder="svt_av1",
        encoder_preset="3",
        probe_encoder_preset="5",
        input_width=None,
        input_height=None,
        input_codec=None,
        input_bit_depth=None,
        input_duration=None,
        input_bitrate=None,
        output_bitrate=None,
        verified_vmaf=None,
        verified_subsample=None,
        crf_source="probe",
        plan_created_at=None,
    )



if __name__ == "__main__":
    unittest.main()
