"""The probe-window precedence (issue #526).

One rule for every entry point: an explicit flag, then quality.sample_count when
set, then quality.sample_every (density, the product's default). The count's
schema default is unset, so it no longer shadows density; a configured count
still overrides it.
"""

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shrinkray import db
from shrinkray.config import DEFAULT_SAMPLE_SECONDS, Conf
from shrinkray.engine.samples import DEFAULT_SAMPLE_EVERY
from shrinkray.engine.search import DEFAULT_CRF_RANGE, sample_count_for
from shrinkray.server import jobs


class TestTheResolver(unittest.TestCase):
    def test_the_default_is_density_with_an_unset_count(self) -> None:
        conf = Conf()
        self.assertEqual(conf.sample_every, DEFAULT_SAMPLE_EVERY)
        self.assertIsNone(conf.sample_count)
        self.assertEqual(conf.probe_settings(), (DEFAULT_SAMPLE_EVERY, None, 4.0))

    def test_a_configured_count_overrides_density(self) -> None:
        conf = Conf({"quality": {"sample_count": 5}})
        self.assertEqual(conf.probe_settings(), (240.0, 5, 4.0))

    def test_a_flag_wins_over_the_config(self) -> None:
        conf = Conf({"quality": {"sample_count": 5, "sample_every": 300}})
        every, count, seconds = conf.probe_settings(900.0, 7, 20.0)
        self.assertEqual((every, count, seconds), (900.0, 7, 20.0))

    def test_an_every_flag_overrides_a_configured_count(self) -> None:
        # an explicit flag of EITHER kind wins: naming the density must not be
        # overridden by a configured count (review round 1, finding 2)
        conf = Conf({"quality": {"sample_count": 5}})
        self.assertEqual(conf.probe_settings(sample_every=300.0), (300.0, None, 4.0))

    def test_a_count_flag_fills_only_the_count(self) -> None:
        conf = Conf()
        self.assertEqual(conf.probe_settings(sample_count=7), (240.0, 7, 4.0))

    def test_a_zero_every_flag_falls_back_to_the_count(self) -> None:
        """`--sample-every 0` disables density and falls back to the count, as
        its help states (issue #526 round 3): with a configured count that is
        the count, not the engine default."""
        conf = Conf({"quality": {"sample_count": 5}})
        self.assertEqual(conf.probe_settings(sample_every=0.0), (0.0, 5, 4.0))
        # without a configured count the engine default applies
        self.assertEqual(Conf().probe_settings(sample_every=0.0), (0.0, None, 4.0))

    def test_the_count_follows_density_for_a_two_hour_film(self) -> None:
        # the issue's own table: 7200 s at the 240 s density is 30 windows,
        # and a configured count still pins 5
        every, count, _ = Conf().probe_settings()
        self.assertEqual(sample_count_for(7200.0, every, count), 30)
        every, count, _ = Conf({"quality": {"sample_count": 5}}).probe_settings()
        self.assertEqual(sample_count_for(7200.0, every, count), 5)


class TestTheResidentJobUsesTheSameRule(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.connection = db.connect(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.connection.close)
        self.queue = jobs.JobQueue(self.connection)

    def _plan_kwargs(self, config: Conf) -> dict:
        movie = Path(self.tmp.name) / "Movie_t00.mkv"
        movie.write_bytes(b"x" * 16)
        db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        fake_result = SimpleNamespace(
            input=movie,
            output=movie,
            success=True,
            target_vmaf=95.0,
            min_saving_percent=15,
            chosen_crf=21,
            achieved_vmaf=95.47,
            probes=[],
            input_size=16,
            output_size=8,
            saved_percent=50.0,
            error=None,
            skipped_reason=None,
            encoder_preset="3",
            probe_encoder_preset="5",
            crop=None,
            crf_range=(14, 32),
            sample_count=3,
            sample_seconds=DEFAULT_SAMPLE_SECONDS,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            hdr=False,
            encoder="svt_av1",
        )
        job = self.queue.enqueue(jobs.Job(kind=jobs.JobKind.PLAN, payload={"paths": [str(movie)]}))
        with (
            mock.patch("shrinkray.engine.plan.plan_quality", return_value=fake_result) as fake_plan,
            mock.patch("shrinkray.server.jobs.cap_threads", return_value=1),
            mock.patch("shrinkray.server.jobs.apply_reloaded_config"),
            mock.patch("shrinkray.config.load_config", return_value=config),
            mock.patch("shrinkray.config.refresh_config", return_value=config),
        ):
            jobs.execute_plan(job, self.connection, threading.Event())
        return fake_plan.call_args.kwargs

    def test_the_default_job_probes_density_not_a_fixed_three(self) -> None:
        kwargs = self._plan_kwargs(Conf())
        self.assertEqual(kwargs.get("sample_every"), 240.0)
        self.assertIsNone(kwargs.get("sample_count"))
        self.assertEqual(kwargs.get("sample_seconds"), 4.0)

    def test_the_decided_defaults_pin(self) -> None:
        """The maintainer's 2026-11-14 decision (issue #651): a 4 s window
        every 240 s - a 2 h film gives 30 windows of 4 s, the same 1.67%
        footprint as before, spread 2.5x more evenly."""
        self.assertEqual(DEFAULT_SAMPLE_EVERY, 240.0)
        self.assertEqual(DEFAULT_SAMPLE_SECONDS, 4.0)
        conf = Conf()
        self.assertEqual(conf.probe_settings(), (240.0, None, 4.0))
        self.assertEqual(sample_count_for(7200.0, DEFAULT_SAMPLE_EVERY), 30)

    def test_a_plan_stored_under_the_old_window_misses(self) -> None:
        """The plan fingerprint carries sample_seconds (issue #651): a plan
        measured under the old 10 s windows must not be reused by a run at
        the new 4 s default - the lookup misses and the file re-probes."""
        movie = Path(self.tmp.name) / "Old Window_t00.mkv"
        movie.write_bytes(b"x" * 16)
        file_id = db.upsert_file(
            self.connection,
            db.MovieRecord(
                path=movie,
                mtime=movie.stat().st_mtime,
                size=movie.stat().st_size,
                name=movie.name,
                category="Cat",
            ),
        )
        self.connection.commit()
        db.save_plan(
            self.connection,
            file_id,
            chosen_crf=22,
            achieved_vmaf=96.1,
            crop=None,
            probes=[],
            skipped_reason=None,
            estimated_size=100,
            input_mtime=movie.stat().st_mtime,
            input_size=movie.stat().st_size,
            encoder="svt_av1",
            encoder_preset="5",
            target_vmaf=96.0,
            crf_low=14,
            crf_high=70,
            sample_count=12,
            # the OLD window length: what a pre-#651 plan row carries
            sample_seconds=10.0,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            min_saving_percent=15,
        )
        fingerprint = {
            "encoder": "svt_av1",
            "encoder_preset": "5",
            "target_vmaf": 96.0,
            "crf_range": DEFAULT_CRF_RANGE,
            "crop_sample_count": 10,
            "vmaf_statistic": "mean",
            "vmaf_recipe": "vmaf=2;fmt=yuv420p;hdr=no",
            "min_saving_percent": 15,
        }
        # the row matches a lookup under its own fingerprint
        self.assertIsNotNone(
            db.get_valid_plan(
                self.connection, movie, sample_count=12, sample_seconds=10.0, **fingerprint
            )
        )
        # a run at the new default resolves a different fingerprint: miss
        self.assertIsNone(
            db.get_valid_plan(
                self.connection,
                movie,
                sample_count=30,
                sample_seconds=DEFAULT_SAMPLE_SECONDS,
                **fingerprint,
            )
        )
        # the window-length arm on its own: a row measured at the new 4 s
        # length must not match a lookup at the old 10 s length (the count
        # alone must not decide the miss)
        db.save_plan(
            self.connection,
            file_id,
            chosen_crf=22,
            achieved_vmaf=96.1,
            crop=None,
            probes=[],
            skipped_reason=None,
            estimated_size=100,
            input_mtime=movie.stat().st_mtime,
            input_size=movie.stat().st_size,
            encoder="svt_av1",
            encoder_preset="5",
            target_vmaf=96.0,
            crf_low=14,
            crf_high=70,
            sample_count=30,
            sample_seconds=DEFAULT_SAMPLE_SECONDS,
            crop_sample_count=10,
            vmaf_statistic="mean",
            vmaf_recipe="vmaf=2;fmt=yuv420p;hdr=no",
            min_saving_percent=15,
        )
        self.assertIsNone(
            db.get_valid_plan(
                self.connection,
                movie,
                sample_count=30,
                sample_seconds=10.0,
                **fingerprint,
            )
        )
        self.assertIsNotNone(
            db.get_valid_plan(
                self.connection,
                movie,
                sample_count=30,
                sample_seconds=DEFAULT_SAMPLE_SECONDS,
                **fingerprint,
            )
        )

    def test_a_configured_count_reaches_the_job(self) -> None:
        kwargs = self._plan_kwargs(Conf({"quality": {"sample_count": 5}}))
        self.assertEqual(kwargs.get("sample_count"), 5)


if __name__ == "__main__":
    unittest.main()
