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

    def test_the_job_slot_is_keyed_on_the_movie_directory(self) -> None:
        """The job's in-place encode slot must exclude the CLI/watcher
        (issue #336): the contended resource in place is the movie's own
        directory — the per-run staging directory is a fresh path no other
        encoder ever locks, so the old key guarded nothing. A CLI encode
        holding the movie's directory must make the job's encode skip, not
        encode beside it. The movie sits in its own folder below the payload
        root, as in a one-folder-per-title library, so a key on the root (or
        on the staging directory's parent) cannot pass for the movie's.
        """
        from shrinkray.engine.lock import acquire_encode_slot, release_encode_slot

        title = self.root / "Title (2020)"
        title.mkdir()
        self.movie.rename(title / self.movie.name)
        lock = acquire_encode_slot(title, holder="encode-cli")
        try:
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
                result = jobs.execute_encode(self._job(), self.connection, threading.Event())
        finally:
            release_encode_slot(lock)
        # the job did NOT encode beside the CLI holder: it skipped with the
        # busy error instead
        self.assertEqual(result["outcomes"][0]["state"], "skipped")
        fake_encode.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
