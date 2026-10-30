"""Quality/CRF-search tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import json
import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray import db, service
from shrinkray.engine import crop as engine_crop
from shrinkray.engine import encoders as engine_encoders
from shrinkray.engine import pipeline as engine_pipeline
from shrinkray.engine import plan as engine_plan
from shrinkray.engine import samples as engine_samples
from shrinkray.engine import search as engine_search
from shrinkray.engine.plan import SOURCE_CODECS
from shrinkray.tools import ffprobe as tools_ffprobe
from shrinkray.tools import handbrake as tools_handbrake
from shrinkray.tools import vmaf as tools_vmaf


class TestQuality(unittest.TestCase):
    def test_telemetry_saves_failure_bundle(self):
        """A failure bundle keeps stderr, command, context and artifacts."""
        from shrinkray.telemetry import save_failure

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.db"
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            (workdir / "vmaf.json").write_text('{"pooled_metrics": {}}', encoding="utf-8")

            bundle = save_failure(
                db_file=db_file,
                kind="vmaf-measure",
                command=["ffmpeg", "-i", "x.mkv"],
                stderr="",
                workdir=workdir,
                artifacts=["vmaf.json"],
                context={"hdr": True, "returncode": 1},
            )
            self.assertIsNotNone(bundle)
            # empty stderr is still recorded as an empty file (evidence of
            # the emptiness — yesterday's bug was exactly this blindness)
            self.assertTrue((bundle / "stderr.txt").exists())
            self.assertEqual(
                (bundle / "command.txt").read_text(encoding="utf-8"), "ffmpeg\n-i\nx.mkv\n"
            )
            self.assertTrue((bundle / "traceback.txt").exists())
            context = (bundle / "context.json").read_text(encoding="utf-8")
            self.assertIn('"hdr": true', context)
            # the partial artifact was copied (not moved)
            self.assertTrue((bundle / "artifact-vmaf.json").exists())
            self.assertTrue((workdir / "vmaf.json").exists())

    def test_telemetry_never_raises(self):
        """Telemetry must not break the error path it reports on."""
        from shrinkray.telemetry import save_failure

        with tempfile.TemporaryDirectory() as tmp:
            # a file where the logs dir should be => mkdir fails => None
            blocker = Path(tmp) / "logs"
            blocker.write_text("not a directory", encoding="utf-8")
            bundle = save_failure(
                db_file=Path(tmp) / "test.db",
                kind="encode",
            )
            self.assertIsNone(bundle)

    def test_the_decided_vmaf_default_is_96(self):
        """Issue #103: VMAF 96 is the decided default, and nothing repeats it.

        The defect was a literal 95.0 copied into every CLI option, the API model
        and the job payload, so raising the constant left the surfaces behind. They
        all derive from DEFAULT_TARGET_VMAF now; this pins the decision itself.
        """
        self.assertEqual(engine_search.DEFAULT_TARGET_VMAF, 96.0)

    def test_the_hdr_default_is_the_sdr_default_minus_the_offset(self):
        """The HDR target is calibrated DOWN from the SDR one by a fixed offset.

        It used to be a second literal (94.5), which is how the two drifted apart
        when only one was changed (issue #103). It is a difference now.
        """
        self.assertEqual(
            engine_encoders.DEFAULT_HDR_TARGET_VMAF,
            engine_search.DEFAULT_TARGET_VMAF - engine_encoders.HDR_VMAF_OFFSET,
        )
        self.assertLess(engine_encoders.DEFAULT_HDR_TARGET_VMAF, engine_search.DEFAULT_TARGET_VMAF)

    def test_no_surface_repeats_the_default_literal(self):
        """Every surface must DERIVE the default, not restate it as a literal.

        A literal 95.0 in a CLI option or the API model is invisible to a change
        of the constant - that is exactly how the docs said 96 while the code
        shipped 95 (issue #103).
        """
        root = Path(__file__).resolve().parent.parent / "src" / "shrinkray"
        offenders: list[str] = []
        for name in ("__main__.py", "server/app.py", "server/jobs.py"):
            for lineno, line in enumerate((root / name).read_text().splitlines(), 1):
                if "95.0" not in line:
                    continue
                if line.lstrip().startswith("#"):
                    continue
                offenders.append(f"{name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "these repeat the VMAF default as a literal")

    def test_sample_offsets(self):
        # 3 samples spread over 10%-90% of a 100s movie
        offsets = engine_samples.sample_offsets(100.0, 3)
        self.assertEqual(offsets, [10.0, 50.0, 90.0])
        # single sample => middle-ish position
        self.assertEqual(engine_samples.sample_offsets(100.0, 1), [10.0])
        # degenerate inputs
        self.assertEqual(engine_samples.sample_offsets(0.0, 3), [])
        self.assertEqual(engine_samples.sample_offsets(100.0, 0), [])

    def test_bisect_crf(self):
        # monotonic: low crf passes, high crf fails => largest passing crf wins
        scores = {c: 99.0 - (c - 14) * 0.5 for c in range(14, 32)}
        calls: list[int] = []

        def score(c: int) -> float:
            calls.append(c)
            return scores[c]

        chosen = engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0)
        self.assertEqual(chosen, 22)  # 99 - (c-14)*0.5 >= 95 up to c=22
        # every CRF measured at most once
        self.assertEqual(len(calls), len(set(calls)))
        # even the lowest crf fails => fall back to the lowest
        self.assertEqual(engine_search.bisect_crf(lambda c: 80.0, 14, 32, target_vmaf=95.0), 14)
        # everything passes => the largest measured pass (hi is exclusive)
        self.assertEqual(engine_search.bisect_crf(lambda c: 99.0, 14, 32, target_vmaf=95.0), 31)

    def _interpolated_search(self, curve, lo: int, hi: int, target: float) -> tuple[int, int]:
        """Runs bisect_crf over a synthetic curve; returns (chosen, probes)."""
        calls: list[int] = []
        cache: dict[int, float] = {}

        def score(c: int) -> float:
            calls.append(c)
            if c not in cache:
                cache[c] = curve(c)
            return cache[c]

        chosen = engine_search.bisect_crf(score, lo, hi, target_vmaf=target)
        assert len(calls) == len(set(calls))  # each CRF measured once
        return chosen, len(calls)

    def _plain_bisect(self, curve, lo: int, hi: int, target: float) -> tuple[int, int]:
        calls: list[int] = []

        def check(c: int) -> bool:
            calls.append(c)
            return curve(c) >= target

        while hi - lo > 1:
            mid = (lo + hi) // 2
            if check(mid):
                lo = mid
            else:
                hi = mid
        assert len(calls) == len(set(calls))
        return lo, len(calls)

    def test_interpolation_matches_bisect_on_curve_family(self):
        """Issue #23's acceptance property: on stated monotonic curve
        families (linear, concave, convex, step), the interpolated search
        returns the same CRF as the plain bisect, and never needs more
        probes than that bisect's worst case."""
        lo, hi, target = 14, 32, 95.0
        curves = {
            # linear fall from VMAF 99 at CRF 14 to 80 at CRF 32
            "linear": lambda c: 99.0 - (99.0 - 80.0) * (c - lo) / (hi - lo - 1),
            # concave: steep drop first, then flattens
            "concave": lambda c: 99.0 - 19.0 * ((c - lo) / (hi - lo - 1)) ** 0.5,
            # convex: flat first, then steep — the regula-falsi trap
            "convex": lambda c: 99.0 - 19.0 * ((c - lo) / (hi - lo - 1)) ** 2.0,
            # step: perfect until a cliff, then worthless
            "step": lambda c: 97.0 if c <= 21 else 60.0,
        }
        for name, curve in curves.items():
            with self.subTest(curve=name):
                chosen_interp, probes_interp = self._interpolated_search(curve, lo, hi, target)
                chosen_plain, probes_plain = self._plain_bisect(curve, lo, hi, target)
                self.assertEqual(chosen_interp, chosen_plain, f"CRF mismatch on {name}")
                self.assertLessEqual(
                    probes_interp,
                    probes_plain + 1,
                    f"{name}: interpolation used more probes ({probes_interp}) "
                    f"than bisect ({probes_plain})",
                )

    def test_bisect_crf_does_not_probe_the_low_end_upfront(self):
        """Issue #313: the range's lowest CRF is probed only when the
        search actually heads there - the unconditional CRF-14 round spent
        the search's most expensive probe on nearly every file."""
        calls: list[int] = []

        def score(c: int) -> float:
            calls.append(c)
            return 99.0 - (c - 14) * 0.5

        chosen = engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0)
        self.assertEqual(chosen, 22)
        self.assertNotIn(14, calls, "the low end was probed without the search heading there")

    def test_bisect_crf_unreachable_costs_three_rounds(self):
        """The unreachable case follows ab-av1's round structure: the
        midpoint, the 20% point of the range, then lo - whose probe is the
        SkipEncode evidence. The PR's first shape walked down by midpoints
        and spent five rounds here (review of #339)."""
        calls: list[int] = []

        def score(c: int) -> float:
            calls.append(c)
            return 80.0

        chosen = engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0)
        self.assertEqual(chosen, 14)
        self.assertEqual(calls, [23, 18, 14])

    def test_bisect_crf_round_two_moves_to_the_eighty_percent_point(self):
        """A passing midpoint moves round two to the 80% point of the range
        (ab-av1's cut), not to the range's end."""
        calls: list[int] = []

        def score(c: int) -> float:
            calls.append(c)
            return 99.0

        chosen = engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0)
        # midpoint 23, then round(0.4*23 + 0.6*31) = 28, then the top (31)
        self.assertEqual(calls, [23, 28, 31])
        self.assertEqual(chosen, 31)

    def test_bisect_crf_early_stop_tolerance_doubles_per_round(self):
        """Issue #313's early stop uses ab-av1's tolerance, 0.1 * 2**(round-1),
        and fires only on a bracket at most 2 CRF wide (issue #395). Here round
        3 lands on a 2-wide bracket (21 passing, 23 failing) at +0.35: inside
        the doubled 0.4, where a linear 0.3 would probe 22 as well."""
        calls: list[int] = []
        # 23 fails; 18 passes; interpolation between them lands on 21
        scores = {23: 94.333, 18: 96.0, 21: 95.35, 22: 80.0}

        def score(c: int) -> float:
            calls.append(c)
            return scores[c]

        self.assertEqual(engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0), 21)
        self.assertEqual(calls, [23, 18, 21])

        # the same bracket at +0.45 is outside round 3's 0.4 => 22 is probed
        calls.clear()
        scores[21] = 95.45
        self.assertEqual(engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0), 21)
        self.assertEqual(calls, [23, 18, 21, 22])

    def test_bisect_crf_early_stop_waits_for_a_narrow_bracket(self):
        """Issue #395 (the 22:21 review of #339): a pass inside the tolerance
        must not stop the search while the failing bound is far above it. On
        this plateau-then-knee curve the midpoint (29) fails and the 20% point
        (20) passes at +0.19, inside round 2's 0.2; stopping there returned 20
        while CRF 28 still passes - 8 CRF given away, and a file that shrinks
        at 28 but grows at 20 turned into a 'would not shrink' skip."""

        def score(c: int) -> float:
            return 94.367 - 0.0232 * (c - 14) if c <= 30 else 80.0

        self.assertEqual(engine_search.bisect_crf(score, 14, 45, target_vmaf=94.034), 28)

    def test_bisect_crf_early_stop_gives_away_at_most_one_crf(self):
        """Issue #395: the early stop fires only on a bracket at most 2 wide,
        so the result is the largest passing CRF or the one below it - never
        further (the merged #339 shape lost up to 8 on a plateau). Two curve
        families over the default range: plateau-then-knee curves with the
        target just under the plateau at every knee, and the smooth
        100 - k*e^(crf/s) curves docs/encode-decision.md simulates."""
        lo, hi = 14, 45
        curves: list[tuple[dict[int, float], float]] = []
        for knee in range(lo + 1, hi - 1):
            for slope in (0.0, 0.0232, 0.1):
                for margin in (0.01, 0.15, 0.35):
                    curve = {
                        c: 96.0 - slope * (c - lo) if c <= knee else 80.0 - (c - knee)
                        for c in range(lo, hi)
                    }
                    curves.append((curve, curve[knee] - margin))
        for s in (8.0, 10.0, 12.0, 14.0, 16.0):
            for cross in range(16, 45, 2):
                for target in (94.0, 95.5, 97.0):
                    k = (100.0 - target) / math.exp(cross / s)
                    curves.append(({c: 100.0 - k * math.exp(c / s) for c in range(lo, hi)}, target))
        for curve, target in curves:
            chosen = engine_search.bisect_crf(curve.__getitem__, lo, hi, target)
            best = max(c for c, v in curve.items() if v >= target)
            with self.subTest(target=target, best=best):
                self.assertGreaterEqual(curve[chosen], target)
                self.assertGreaterEqual(chosen, best - 1)

    def test_bisect_crf_rejects_an_empty_range(self):
        """bisect_crf's contract is a CRF in [lo, hi); an empty range used to
        probe outside it (lo=hi=20 probed 20 and 19, returned 19)."""
        with self.assertRaises(ValueError):
            engine_search.bisect_crf(lambda c: 99.0, 20, 20, target_vmaf=95.0)

    def test_bisect_crf_early_stop_never_misses_the_target(self):
        """The early stop never returns a CRF whose score misses the target:
        the tolerance is measured ABOVE the target on a PASSING probe."""
        calls: list[int] = []

        def score(c: int) -> float:
            calls.append(c)
            return 96.4 if c <= 25 else 60.0

        chosen = engine_search.bisect_crf(score, 14, 32, target_vmaf=95.0)
        self.assertGreaterEqual(score(chosen), 95.0)

    def test_build_vmaf_command(self):
        for style in (tools_vmaf.VMAF_STYLE_LEGACY, tools_vmaf.VMAF_STYLE_MODERN):
            command = tools_vmaf.build_vmaf_command(Path("enc.mkv"), Path("ref.mkv"), style=style)
            joined = " ".join(command)
            # distorted stream must be the first input (libvmaf main)
            self.assertEqual(command[command.index("-i") + 1], "enc.mkv")
            self.assertEqual(command[command.index("-i", command.index("-i") + 1) + 1], "ref.mkv")
            # model by bare filename => no windows drive colon in the filtergraph
            self.assertNotIn("\\", joined)
            if style == tools_vmaf.VMAF_STYLE_MODERN:
                self.assertIn("model=path=vmaf_v0.6.1.json", joined)
            else:
                self.assertIn("model_path=vmaf_v0.6.1.json", joined)

    def test_build_vmaf_command_hdr_crop_crops_the_reference(self):
        """Issue #307: an HDR source with bars crops the reference before
        the tone-map. The hdr branch used to ignore the crop, so the
        HandBrake-cropped distorted input met an uncropped reference."""
        command = tools_vmaf.build_vmaf_command(
            Path("d.mkv"),
            Path("r.mkv"),
            8,
            style=tools_vmaf.VMAF_STYLE_MODERN,
            crop=(140, 140, 0, 0),
            hdr=True,
        )
        joined = " ".join(command)
        # cropped to the distorted geometry first, then tone-mapped
        self.assertIn("[1:v]crop=iw-0:ih-280:0:140,zscale=t=linear", joined)
        self.assertIn("[0:v]zscale=t=linear", joined)
        # exactly one explicit pixel format per input, AFTER the tone-map: the
        # tone-mapped float intermediate must not be converted back to 8-bit
        self.assertEqual(joined.count("format=yuv420p10le"), 2)
        self.assertNotIn("format=yuv420p,", joined)

    def test_build_vmaf_command_hdr_without_crop_is_unchanged(self):
        """The plain HDR path (no bars) keeps its tone-map-only graph."""
        command = tools_vmaf.build_vmaf_command(
            Path("d.mkv"), Path("r.mkv"), 8, style=tools_vmaf.VMAF_STYLE_MODERN, hdr=True
        )
        joined = " ".join(command)
        self.assertNotIn("crop=", joined)
        self.assertIn("[0:v]zscale=t=linear", joined)
        self.assertIn("[1:v]zscale=t=linear", joined)

    def test_detect_vmaf_style_without_ffmpeg(self):
        # a missing ffmpeg must raise a clear error (CI has no ffmpeg)
        with self.assertRaises(RuntimeError):
            tools_vmaf.detect_vmaf_style(binary="definitely-not-installed-ffmpeg")

    def test_parse_vmaf_log(self):
        log_text = json.dumps({"pooled_metrics": {"vmaf": {"mean": 94.002412, "min": 46.9}}})
        self.assertAlmostEqual(tools_vmaf.parse_vmaf_log(log_text), 94.002412, places=5)
        with self.assertRaises(ValueError):
            tools_vmaf.parse_vmaf_log(json.dumps({"pooled_metrics": {}}))
        with self.assertRaises(ValueError):
            tools_vmaf.parse_vmaf_log("{}")

    def test_build_encode_command(self):
        command = tools_handbrake.build_encode_command(
            "hb.exe", Path("in.mkv"), Path("out.mkv"), 23, "x265", "medium"
        )
        self.assertIn("--quality", command)
        self.assertEqual(command[command.index("--quality") + 1], "23")
        self.assertIn("--encoder-preset", command)
        self.assertEqual(command[command.index("--encoder-preset") + 1], "medium")

    def test_min_encode_seconds_guard(self):
        self.assertEqual(engine_plan.MIN_ENCODE_SECONDS, 90.0)
        # the guard lives in the encode path => importable constant

    def test_parse_crop_line(self):
        text = "[Parsed_cropdetect] crop=1920:800:0:140\n[Parsed_cropdetect] crop=1920:796:0:142\n"
        self.assertEqual(engine_crop.parse_crop_line(text), (1920, 796, 0, 142))
        self.assertIsNone(engine_crop.parse_crop_line("nothing here"))

    def test_intersect_crop_rects(self):
        frame = (1920, 1080)
        # identical rects => those bars
        rects = [(1920, 800, 0, 140), (1920, 800, 0, 140)]
        bars = engine_crop.intersect_crop_rects(rects, frame)
        self.assertEqual(bars, (140, 140, 0, 0))
        # cropdetect reports the PICTURE rectangle, so the crop may only remove
        # bars present in ALL samples => keep the union of the picture areas
        # (here y in [90, 990): only the 90 px bar common to both is removed)
        smaller = [(1920, 800, 0, 140), (1920, 900, 0, 90)]
        self.assertEqual(engine_crop.intersect_crop_rects(smaller, frame), (90, 90, 0, 0))
        # a sample with no bars at all vetoes the crop entirely
        sides = [(1920, 1080, 0, 0), (1800, 1080, 60, 0)]
        self.assertIsNone(engine_crop.intersect_crop_rects(sides, frame))
        # noise-sized bars (below 2% of the dimension) => None
        tiny = [(1920, 1060, 0, 10), (1920, 1060, 0, 10)]
        self.assertIsNone(engine_crop.intersect_crop_rects(tiny, frame))
        # empty input
        self.assertIsNone(engine_crop.intersect_crop_rects([], frame))

    def test_intersect_crop_rects_bar_free_sample_vetoes(self):
        """A sample with no bars must prevent cropping, not be overruled."""
        frame = (1920, 1080)
        # one sample is the full frame => no bar is present in every sample
        self.assertIsNone(
            engine_crop.intersect_crop_rects([(1920, 800, 0, 140), (1920, 1080, 0, 0)], frame)
        )
        # a sample with smaller bars still restricts the crop: the 140 px bar is
        # only a bar in one sample, so only the common 90 px may be removed
        self.assertEqual(
            engine_crop.intersect_crop_rects([(1920, 800, 0, 140), (1920, 900, 0, 90)], frame),
            (90, 90, 0, 0),
        )

    def test_build_encode_command_crop(self):
        command = tools_handbrake.build_encode_command(
            "hb.exe", Path("in.mkv"), Path("out.mkv"), 20, "x265", "medium", crop=(140, 140, 0, 0)
        )
        self.assertEqual(command[command.index("--crop") + 1], "140:140:0:0")
        # no crop => explicit 0:0:0:0 (auto-crop would break VMAF geometry)
        plain = tools_handbrake.build_encode_command(
            "hb.exe", Path("in.mkv"), Path("out.mkv"), 20, "x265", "medium"
        )
        self.assertEqual(plain[plain.index("--crop") + 1], "0:0:0:0")

    def test_build_encode_command_threads(self):
        """The quality pipeline passes the cap via --encopts pin=N."""
        command = tools_handbrake.build_encode_command(
            "hb",
            Path("in.mkv"),
            Path("out.mkv"),
            20,
            "svt_av1",
            "medium",
            encoder_threads=6,
        )
        self.assertIn("--encopts", command)
        self.assertIn("pin=6", command)
        plain = tools_handbrake.build_encode_command(
            "hb", Path("in.mkv"), Path("out.mkv"), 20, "svt_av1", "medium"
        )
        self.assertNotIn("--encopts", plain)


class TestPlanReuse(unittest.TestCase):
    """The encode-path "quicksave": a valid plan replaces probing; any
    fingerprint mismatch fails safe to the normal probe pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _movie(self, name: str = "movie.mkv") -> Path:
        movie = Path(self.tmp.name) / name
        movie.write_bytes(b"x" * 8)
        service.ensure_file_row(self.connection, movie)
        self.connection.commit()
        return movie

    def _store_plan(self, movie: Path, **overrides) -> None:
        """Stores a plan for the movie via the service write path."""
        from shrinkray.engine.search import SampleProbe

        hdr_value = overrides.get("hdr", False)
        result = engine_plan.QualityResult(
            input=movie,
            output=movie,
            success=overrides.pop("success", True),
            target_vmaf=overrides.pop("target_vmaf", engine_search.DEFAULT_TARGET_VMAF),
            chosen_crf=overrides.pop("chosen_crf", 21),
            achieved_vmaf=overrides.pop("achieved_vmaf", 95.47),
            probes=overrides.pop("probes", [SampleProbe(crf=21, vmaf=95.47)]),
            input_size=movie.stat().st_size,
            output_size=movie.stat().st_size // 2,
            skipped_reason=overrides.pop("skipped_reason", None),
            encoder_preset="3",
            probe_encoder_preset="5",
            crop=None,
            crf_range=overrides.pop("crf_range", engine_search.DEFAULT_CRF_RANGE),
            sample_count=engine_search.sample_count_for(600.0, engine_plan.DEFAULT_SAMPLE_EVERY),
            sample_seconds=10.0,
            hdr=hdr_value,
            encoder=overrides.pop("encoder", "svt_av1"),
            # the fingerprint's crop-independent recipe part, derived from the
            # same hdr flag the result carries (the tests' source snapshot has
            # no dimensions, so the model and scale cannot enter it)
            vmaf_recipe=overrides.pop(
                "vmaf_recipe", tools_vmaf.vmaf_recipe(hdr=hdr_value).fingerprint()
            ),
        )
        config = type("Conf", (), {"handbrake_encoder": "svt_av1"})()
        record_plan = overrides.pop("record_plan", service.record_plan)
        record_plan(self.connection, movie, result, config)

    def _encode(self, movie: Path, **kwargs):
        """Runs encode_with_quality with every external seam mocked."""
        from shrinkray.engine.pipeline import EncodeResult

        output = Path(self.tmp.name) / "out.mkv"

        def fake_encode_command(input_file, output_file, command, **kwargs):
            output_file.write_bytes(b"y" * (input_file.stat().st_size // 2))
            return EncodeResult(
                input=input_file,
                output=output_file,
                success=True,
                input_size=input_file.stat().st_size,
                output_size=output_file.stat().st_size,
            )

        with (
            patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
            patch("shrinkray.engine.pipeline.check_disk_space", return_value=(True, "")),
            patch.object(
                engine_pipeline,
                "_source_snapshot",
                return_value={
                    "width": None,
                    "height": None,
                    "codec": None,
                    "duration": None,
                    "bitrate": None,
                    "bit_depth": None,
                },
            ),
            patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
            patch.object(
                engine_pipeline, "_encode_with_command", side_effect=fake_encode_command
            ) as fake_encode,
        ):
            result = engine_pipeline.encode_with_quality(
                movie, output, db_file=self.db_file, **kwargs
            )
        return result, fake_encode

    def test_valid_plan_skips_probing_and_encodes_planned_crf(self):
        movie = self._movie()
        self._store_plan(movie)
        with (
            patch.object(engine_pipeline, "_probe_movie") as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            result, fake_encode = self._encode(movie)
        fake_probe.assert_not_called()  # zero probe encodes
        fake_encode.assert_called_once()
        self.assertTrue(result.success)
        self.assertEqual(result.chosen_crf, 21)
        self.assertEqual(result.achieved_vmaf, 95.47)
        self.assertEqual(result.crf_source, "plan")
        self.assertIsNotNone(result.plan_created_at)
        # the final command encodes at the plan's CRF
        command = fake_encode.call_args.args[2]
        self.assertEqual(command[command.index("--quality") + 1], "21")
        # the caller (job worker / CLI) persists the result; the encodes row
        # carries the provenance
        config = type("Conf", (), {"handbrake_encoder": "svt_av1"})()
        service.record_quality_results(self.connection, movie, result, config)
        row = self.connection.execute(
            "SELECT crf_source, plan_created_at FROM encodes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["crf_source"], "plan")
        self.assertEqual(row["plan_created_at"], result.plan_created_at)

    def test_probe_and_final_presets_differ_in_one_run(self):
        """Issue #22's core acceptance: in ONE run the probe encodes receive
        the probe preset and the final encode receives the final preset —
        measurement and output are two separate settings."""
        from shrinkray.engine.pipeline import EncodeResult

        movie = self._movie()
        built: list[tuple[str, str]] = []  # (preset, phase)

        def fake_build(binary, input_file, output_file, crf, encoder, preset, **kwargs):
            built.append((preset, "final"))
            return ["hb", "--quality", str(crf), "--encoder-preset", preset]

        with (
            patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
            patch("shrinkray.engine.pipeline.check_disk_space", return_value=(True, "")),
            patch.object(
                engine_pipeline,
                "_source_snapshot",
                return_value={
                    "width": None,
                    "height": None,
                    "codec": None,
                    "duration": None,
                    "bitrate": None,
                    "bit_depth": None,
                },
            ),
            patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
            patch.object(engine_pipeline, "_probe_movie", return_value=(21, 95.5, [], None)),
            patch.object(engine_pipeline, "build_encode_command", side_effect=fake_build),
            patch.object(
                engine_pipeline,
                "_encode_with_command",
                side_effect=lambda i, o, c, **kw: (
                    o.write_bytes(b"y" * (i.stat().st_size // 2)),
                    EncodeResult(
                        input=i,
                        output=o,
                        success=True,
                        input_size=i.stat().st_size,
                        output_size=o.stat().st_size,
                    ),
                )[1],
            ),
        ):
            result = engine_pipeline.encode_with_quality(
                movie,
                Path(self.tmp.name) / "out.mkv",
                db_file=self.db_file,
                probe_encoder_preset="5",
                encoder_preset="6",
            )
        self.assertEqual(built, [("6", "final")])  # final built ONCE at '6'
        # both presets land on the result (provenance: which number produced
        # which measurement)
        self.assertEqual(result.probe_encoder_preset, "5")
        self.assertEqual(result.encoder_preset, "6")

    def test_fingerprint_mismatch_runs_probe_path(self):
        movie = self._movie()
        self._store_plan(movie, chosen_crf=21)
        with (
            patch.object(
                engine_pipeline, "_probe_movie", return_value=(20, 95.1, [], None)
            ) as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            # the plan fingerprint keys on the PROBE preset (issue #22): the
            # stored plan was probed at 'medium'; probing at 'veryslow' is a
            # mismatch => the plan is ignored and everything is measured
            result, _ = self._encode(movie, probe_encoder_preset="9")
        fake_probe.assert_called_once()
        self.assertEqual(result.chosen_crf, 20)
        self.assertEqual(result.crf_source, "probe")

    def test_final_preset_change_reuses_plan(self):
        """Issue #22's cache decision: a stored plan is a probe result —
        changing the FINAL preset does not invalidate it (the CRF decision
        depends on the probe preset only)."""
        movie = self._movie()
        self._store_plan(movie, chosen_crf=21)
        with (
            patch.object(engine_pipeline, "_probe_movie") as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            result, _ = self._encode(movie, encoder_preset="9")
        fake_probe.assert_not_called()
        self.assertEqual(result.crf_source, "plan")

    def test_changed_file_invalidates_plan(self):
        movie = self._movie()
        self._store_plan(movie)
        movie.write_bytes(b"x" * 64)  # size change => fingerprint mismatch
        with (
            patch.object(
                engine_pipeline, "_probe_movie", return_value=(20, 95.1, [], None)
            ) as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            result, _ = self._encode(movie)
        fake_probe.assert_called_once()
        self.assertEqual(result.crf_source, "probe")

    def test_plan_skip_decision_short_circuits_encode(self):
        movie = self._movie()
        self._store_plan(movie, success=False, skipped_reason="no size gain")
        with (
            patch.object(engine_pipeline, "_probe_movie") as fake_probe,
            patch.object(engine_pipeline, "_encode_with_command") as fake_encode,
        ):
            result, _ = self._encode(movie)
        fake_probe.assert_not_called()
        fake_encode.assert_not_called()  # nothing encoded at all
        self.assertFalse(result.success)
        self.assertEqual(result.skipped_reason, "no size gain")
        self.assertEqual(result.crf_source, "plan")

    def test_force_reprobe_bypasses_valid_plan(self):
        movie = self._movie()
        self._store_plan(movie)
        with (
            patch.object(
                engine_pipeline, "_probe_movie", return_value=(20, 95.1, [], None)
            ) as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            result, _ = self._encode(movie, force_reprobe=True)
        fake_probe.assert_called_once()
        self.assertEqual(result.chosen_crf, 20)
        self.assertEqual(result.crf_source, "probe")

    def test_no_plan_row_runs_probe_path(self):
        movie = self._movie()
        with (
            patch.object(
                engine_pipeline, "_probe_movie", return_value=(20, 95.1, [], None)
            ) as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            result, _ = self._encode(movie)
        fake_probe.assert_called_once()
        self.assertEqual(result.chosen_crf, 20)
        self.assertEqual(result.crf_source, "probe")

    def test_hdr_plan_reuses_after_engine_switches_to_10bit(self):
        """An HDR plan is stored under the EFFECTIVE encoder (x265_10bit)
        and a following encode of the same HDR file reuses it — regression:
        plan results never carried hdr/encoder, so HDR plans were stored as
        'x265' and the encode-side lookup (which matches on the overridden
        encoder) never found them; every HDR encode re-probed from scratch.
        See issues #26 and #24."""
        movie = self._movie()
        # plan_quality's write path: HDR override applied on the result
        # (effective encoder x265_10bit, HDR target/range), stored via
        # record_plan
        self._store_plan(
            movie,
            target_vmaf=engine_encoders.DEFAULT_HDR_TARGET_VMAF,
            chosen_crf=18,
            achieved_vmaf=94.8,
            crf_range=engine_search.DEFAULT_HDR_CRF_RANGE,
            encoder="svt_av1_10bit",
            hdr=True,
        )
        row = self.connection.execute("SELECT encoder FROM plans").fetchone()
        self.assertEqual(row["encoder"], "svt_av1_10bit")
        # the encode side: same HDR file => the engine switches to 10-bit
        # and MUST find the plan under it
        with (
            patch.object(engine_plan, "is_hdr_file", return_value=True),
            patch.object(engine_pipeline, "_probe_movie") as fake_probe,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0"),
        ):
            result, _ = self._encode(movie)
        fake_probe.assert_not_called()  # the plan replaces probing
        self.assertEqual(result.crf_source, "plan")
        self.assertEqual(result.chosen_crf, 18)
        self.assertEqual(result.encoder, "svt_av1_10bit")

    def _encode_growing(self, movie: Path, factor: float = 1.5):
        """Runs _encode with a fake final encode that writes a LARGER file.

        The engine's size gate is the thing under test, so the fake must
        produce a grown artifact exactly as a real encode can (the probe
        estimate is a prediction; the full encode is the ground truth).
        """
        from shrinkray.engine.pipeline import EncodeResult

        output = Path(self.tmp.name) / "out.mkv"

        def fake_encode_command(input_file, output_file, command, **kwargs):
            output_file.write_bytes(b"y" * int(input_file.stat().st_size * factor))
            return EncodeResult(
                input=input_file,
                output=output_file,
                success=True,
                input_size=input_file.stat().st_size,
                output_size=output_file.stat().st_size,
            )

        with (
            patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
            patch("shrinkray.engine.pipeline.check_disk_space", return_value=(True, "")),
            patch.object(
                engine_pipeline,
                "_source_snapshot",
                return_value={
                    "width": None,
                    "height": None,
                    "codec": None,
                    "duration": None,
                    "bitrate": None,
                    "bit_depth": None,
                },
            ),
            patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
            patch.object(engine_pipeline, "_encode_with_command", side_effect=fake_encode_command),
        ):
            result = engine_pipeline.encode_with_quality(movie, output, db_file=self.db_file)
        return result, output

    def test_a_grown_encode_is_a_failure_not_a_success(self):
        """Issue #130: the product rule is that a re-encode happens only when
        the file gets smaller. A grown output must be reported as a skip, not as
        a success with a negative saved_percent."""
        movie = self._movie()
        self._store_plan(movie)
        result, _output = self._encode_growing(movie, factor=1.5)
        self.assertFalse(result.success)
        self.assertEqual(result.skipped_reason, "output not smaller")
        self.assertIsNone(result.verified_vmaf)

    def test_a_grown_encode_names_both_sizes(self):
        """The reason must carry the numbers, so the operator can see how far
        off the estimate was without re-running anything."""
        movie = self._movie()
        self._store_plan(movie)
        result, _ = self._encode_growing(movie, factor=2.0)
        self.assertIn("did not shrink", result.error)
        self.assertIn("bytes", result.error)

    def test_a_grown_output_is_discarded(self):
        """The grown artifact must be removed, or it sits next to the source as
        a second copy of the film - the duplication the product excludes."""
        movie = self._movie()
        self._store_plan(movie)
        _result, output = self._encode_growing(movie, factor=1.5)
        self.assertFalse(output.exists(), "a grown output must be deleted")
        self.assertTrue(movie.exists(), "the source must be untouched")

    def test_an_identical_size_is_also_a_failure(self):
        """Equal size is not a shrink: the operator gains nothing and pays a
        generation loss, so it counts as grown."""
        movie = self._movie()
        self._store_plan(movie)
        result, output = self._encode_growing(movie, factor=1.0)
        self.assertFalse(result.success)
        self.assertEqual(result.skipped_reason, "output not smaller")
        self.assertFalse(output.exists())

    def test_a_smaller_encode_still_succeeds(self):
        """The regression guard: the new gate must not reject the normal case."""
        movie = self._movie()
        self._store_plan(movie)
        result, output = self._encode_growing(movie, factor=0.5)
        self.assertTrue(result.success)
        self.assertIsNone(result.skipped_reason)
        self.assertGreater(result.saved_percent, 0)
        self.assertTrue(output.exists())


class TestProbeCache(unittest.TestCase):
    def test_probe_command_carries_probe_preset(self):
        """The probe encode's --encoder-preset is the PROBE preset (a
        measurement setting), not the final encode's (issue #22)."""
        _score, encodes = self._probe(probe_encoder_preset="9")
        self.assertEqual(len(encodes), 1)
        command = encodes[0]
        self.assertEqual(command[command.index("--encoder-preset") + 1], "9")

    """The probe-encode cache (issue #24): per-sample measurements are
    content-addressed, so a re-run with the same inputs reuses them and a
    changed target VMAF reuses the probes it already measured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name) / "work"
        self.workdir.mkdir()
        self.db_file = Path(self.tmp.name) / "test.db"
        self.connection = db.connect(self.db_file)
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x" * 8)
        self.sample = Path(self.tmp.name) / "sample.mkv"
        self.sample.write_bytes(b"y" * 8)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _probe(self, **kwargs):
        """One probe_crf call with the encode/VMAF seams mocked."""
        encodes: list = []

        def fake_run(command, cancel=None):
            # the probe encode writes its output file (the --output argument)
            encodes.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        defaults = {
            "samples": [(self.sample, 10.0)],
            "crf": 20,
            "workdir": self.workdir,
            "handbrake": "hb",
            "movie": self.movie,
            "sample_seconds": 10.0,
            "encoder": "svt_av1",
            "probe_encoder_preset": "medium",
            "db_file": self.db_file,
        }
        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", return_value=95.5),
        ):
            score = engine_search.probe_crf(**{**defaults, **kwargs})
        return score, encodes

    def test_an_unspawnable_binary_raises_the_probe_shape(self):
        """Issue #458: a spawn failure at the probe-encode seam raises the
        step's documented RuntimeError (the binary named), not a raw OSError
        traceback - the CRF search must never read 'binary missing' as a
        failed probe encode (a wrong decision)."""

        def broken_run(command, cancel=None):
            raise FileNotFoundError(2, "No such file or directory", command[0])

        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=broken_run),
            self.assertRaises(RuntimeError) as ctx,
        ):
            engine_search.probe_crf(
                [(self.sample, 10.0)],
                20,
                self.workdir,
                "hb",
                self.movie,
                10.0,
                "svt_av1",
                "medium",
                db_file=None,
            )
        self.assertIn("probe encode cannot run 'hb'", str(ctx.exception))

    def test_a_size_probe_spawn_failure_raises_the_probe_shape(self):
        """Issue #458: the size probe's seam converts a spawn failure to its
        RuntimeError shape (the binary named)."""

        def broken_run(command, cancel=None):
            raise FileNotFoundError(2, "No such file or directory", command[0])

        with (
            # the cache key's tool metadata is a real subprocess seam (the
            # sibling probe pin stubs it too); this test's subject is the
            # encode seam
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=broken_run),
            self.assertRaises(RuntimeError) as ctx,
        ):
            engine_search.estimate_size_factor(
                [(self.sample, 10.0)],
                self.workdir,
                "hb",
                20,
                self.movie,
                10.0,
                "svt_av1",
                "medium",
            )
        self.assertIn("size probe encode cannot run 'hb'", str(ctx.exception))

    def test_probe_crf_returns_the_mean_of_samples(self):
        """probe_crf aggregates with the MEAN, not the minimum.

        A minimum is monotone non-increasing in the sample count: every window
        added can only lower it or leave it unchanged, which makes it
        structurally hostile to denser sampling (issue #14). The mean buys
        robustness against one pathological window.
        """
        scores = iter([96.0, 96.0, 90.0])

        def fake_run(command, cancel=None):
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        samples = [(Path(f"s{i}.mkv"), float(i)) for i in range(3)]
        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", side_effect=lambda *a, **k: next(scores)),
        ):
            score = engine_search.probe_crf(
                samples,
                20,
                self.workdir,
                "hb",
                self.movie,
                10.0,
                "svt_av1",
                "medium",
                db_file=None,
                use_probe_cache=False,
            )
        # the mean is 94.0, not the minimum (90.0)
        self.assertAlmostEqual(score, 94.0)

    def test_outlier_no_longer_makes_the_target_unreachable(self):
        """One bad window no longer poisons the decision.

        Under min, a single low window at the floor CRF made the search
        report 'target unreachable' for a film that is encodable.
        """
        low = [96.0, 96.0, 60.0]
        scores = iter(low)

        def fake_run(command, cancel=None):
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        samples = [(Path(f"s{i}.mkv"), float(i)) for i in range(3)]
        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", side_effect=lambda *a, **k: next(scores)),
        ):
            score = engine_search.probe_crf(
                samples,
                20,
                self.workdir,
                "hb",
                self.movie,
                10.0,
                "svt_av1",
                "medium",
                db_file=None,
                use_probe_cache=False,
            )
        self.assertAlmostEqual(score, (96.0 + 96.0 + 60.0) / 3)

    def test_uniformly_failing_samples_still_skip(self):
        """A film that misses the target everywhere still skips.

        The mean must not mask a film whose every window fails.
        """
        scores = iter([80.0, 81.0, 82.0])

        def fake_run(command, cancel=None):
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        samples = [(Path(f"s{i}.mkv"), float(i)) for i in range(3)]
        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", side_effect=lambda *a, **k: next(scores)),
        ):
            score = engine_search.probe_crf(
                samples,
                20,
                self.workdir,
                "hb",
                self.movie,
                10.0,
                "svt_av1",
                "medium",
                db_file=None,
                use_probe_cache=False,
            )
        self.assertLess(score, 95.0)

    def test_stale_min_plan_is_not_reused(self):
        """A plan recorded under the min statistic is not reused as a mean one.

        The statistic is part of the plan fingerprint (issue #15): a plan made
        under min was a different decision.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.db"
            connection = db.connect(db_file)
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            stat = movie.stat()
            file_id = db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    name="movie",
                    category="Cat",
                ),
            )
            common = {
                "encoder": "svt_av1",
                "encoder_preset": "5",
                "target_vmaf": 95.0,
                "crf_range": (14, 32),
                "sample_count": 3,
                "sample_seconds": 10.0,
            }
            db.save_plan(
                connection,
                file_id,
                chosen_crf=20,
                achieved_vmaf=95.0,
                crop=None,
                probes=[],
                skipped_reason=None,
                estimated_size=None,
                input_mtime=stat.st_mtime,
                input_size=stat.st_size,
                vmaf_statistic="min",
                crf_low=14,
                crf_high=32,
                sample_count=3,
                sample_seconds=10.0,
                target_vmaf=95.0,
                encoder="svt_av1",
                encoder_preset="5",
            )
            self.assertIsNone(db.get_valid_plan(connection, movie, **common, vmaf_statistic="mean"))
            self.assertIsNotNone(
                db.get_valid_plan(connection, movie, **common, vmaf_statistic="min")
            )
            connection.close()

    def test_cache_hit_skips_encode_and_measurement(self):
        score, encodes = self._probe()
        self.assertEqual(score, 95.5)
        self.assertEqual(len(encodes), 1)
        # identical inputs again: no encode, no VMAF measurement, same score
        score2, encodes2 = self._probe()
        self.assertEqual(score2, 95.5)
        self.assertEqual(len(encodes2), 0)

    def test_cache_hit_yields_the_uncached_result(self):
        """A cached run must produce the same per-CRF score as an uncached
        one (the acceptance criterion: same chosen CRF)."""
        with tempfile.TemporaryDirectory() as tmp2:
            uncached_db = Path(tmp2) / "other.db"
            db.connect(uncached_db).close()
            score_cached, _ = self._probe()
            score_uncached, encodes = self._probe(db_file=uncached_db)
            self.assertEqual(score_cached, score_uncached)
            self.assertEqual(len(encodes), 1)  # the uncached run encoded once

    def test_changed_target_reuses_measured_probes(self):
        """Changing ONLY the target VMAF keeps every probe-cache component
        identical, so the probes already measured are reused (they are the
        expensive part; the target is a decision threshold, not an input)."""
        score, encodes_first = self._probe(crf=20)
        self.assertEqual(len(encodes_first), 1)
        # a second CRF round at a different CRF: encodes (new key), then a
        # re-run of the SAME crf hits while the other stays cached
        _score2, encodes_new_crf = self._probe(crf=24)
        self.assertEqual(len(encodes_new_crf), 1)
        score3, encodes_again = self._probe(crf=20)
        self.assertEqual(len(encodes_again), 0)
        self.assertEqual(score3, score)

    def test_cache_invalidated_by_source_change(self):
        self._probe()
        # the source changed (mtime+size): the cached score is stale
        self.movie.write_bytes(b"x" * 64)

        stat = self.movie.stat()
        os.utime(self.movie, (stat.st_mtime + 5, stat.st_mtime + 5))
        _score, encodes = self._probe()
        self.assertEqual(len(encodes), 1)  # re-encoded, not reused

    def test_cache_key_changes_with_encoder_and_preset(self):
        self._probe()
        _score, encodes = self._probe(encoder="svt_av1_10bit")
        self.assertEqual(len(encodes), 1)  # different encoder => new key
        _score2, encodes2 = self._probe(probe_encoder_preset="6")
        self.assertEqual(len(encodes2), 1)  # different preset => new key

    def test_force_reprobe_bypasses_cache(self):
        self._probe()
        _score, encodes = self._probe(use_probe_cache=False)
        self.assertEqual(len(encodes), 1)  # measured fresh despite the cache

    def test_no_db_file_probes_uncached(self):
        """Without a database (CLI one-shot), probing still works and
        re-encodes (nothing to cache into)."""
        score, encodes = self._probe(db_file=None)
        self.assertEqual(score, 95.5)
        self.assertEqual(len(encodes), 1)
        score2, encodes2 = self._probe(db_file=None)
        self.assertEqual(score2, 95.5)
        self.assertEqual(len(encodes2), 1)

    def test_the_chosen_crf_size_uses_all_windows(self):
        """Issue #314: the chosen-CRF size decision reads the encoded sizes
        the VMAF probes already recorded for EVERY window - not one
        re-encoded window. A fixture where window 0 shrinks but the total
        grows must SKIP. The reverse direction (every window shrinks =>
        encode) lives in test_the_chosen_crf_size_encodes_when_every_window_shrinks.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            # three sample windows: the first small (its encode shrinks),
            # the others large (their encodes grow)
            samples = []
            sizes = [100, 1000, 1000]
            for i, size in enumerate(sizes):
                s = root / f"s{i}.mkv"
                s.write_bytes(b"m" * size)
                samples.append((s, 10.0 * i))

            # seed the cache: at the chosen CRF, window 0 encoded to 40
            # bytes (0.4x), windows 1-2 to 1200 each (1.2x)
            encoded = [40, 1200, 1200]
            for i, (_sample, offset) in enumerate(samples):
                key = engine_search.probe_cache_key(
                    movie,
                    offset,
                    10.0,
                    22,
                    "svt_av1",
                    "5",
                    None,
                    False,
                    None,
                    ("hb", "1.6"),
                    tools_vmaf.vmaf_recipe().identifier(),
                )
                db.save_probe_score(connection, key, 95.0, encoded[i])

            with (
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
                # a cache miss here must fail as a DIAGNOSTIC (the fallback
                # was reached), not as FileNotFoundError from a real encode
                patch.object(
                    engine_search,
                    "estimate_size_factor",
                    side_effect=AssertionError("the fallback ran: the cache was incomplete"),
                ),
            ):
                factor = engine_search.check_chosen_crf_shrinks(
                    samples,
                    workdir,
                    "hb",
                    22,
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    db_file=db_file,
                )
            # the total: 2440 encoded / 2100 sample = 1.1619 (GROWS) -
            # window 0 alone would have said 0.4 (shrinks)
            self.assertGreater(factor, 1.0)
            self.assertAlmostEqual(factor, 2440 / 2100, places=4)
            connection.close()

    def test_a_recipe_change_misses_both_caches(self):
        """Issue #308's acceptance: the measurement recipe is part of the probe
        cache key AND of the plan fingerprint, so changing how a score is measured
        re-measures instead of reusing the old number.

        A stored probe measured with the 1080p model must not answer a 4K
        measurement of the same window: the model, the scale, the pixel format and
        the tone-map each move the score, and none of them used to be in the key.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")

            hd = tools_vmaf.vmaf_recipe(width=1920, height=1080, bit_depth=8)
            uhd = tools_vmaf.vmaf_recipe(width=3840, height=2160, bit_depth=10)
            key_args = (movie, 10.0, 10.0, 22, "svt_av1", "5", None, False, None, ("hb", "1.6"))
            hd_key = engine_search.probe_cache_key(*key_args, hd.identifier())
            uhd_key = engine_search.probe_cache_key(*key_args, uhd.identifier())
            self.assertNotEqual(hd_key, uhd_key)

            # a probe stored under the 1080p recipe is invisible to the 4K one
            db.save_probe_score(connection, hd_key, 95.0, 40)
            self.assertIsNotNone(db.get_probe_entry(connection, hd_key))
            self.assertIsNone(db.get_probe_entry(connection, uhd_key))
            connection.close()

    def test_a_gate_skip_stays_reusable_without_a_recipe(self):
        """Issue #308's review: a GATE skip (too short, resolution, size, codec)
        never runs a VMAF measurement, so its plan row carries no recipe. The
        lookup must still reuse it - requiring a recipe there would re-derive every
        gate skip on every run, a cache regression the measured-plan rule must not
        cause. A MEASURED plan still rejects a different recipe.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x" * 16)
            connection = db.connect(root / "meta.db")
            file_id = db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie,
                    mtime=movie.stat().st_mtime,
                    size=movie.stat().st_size,
                    name=movie.name,
                    category="Cat",
                ),
            )
            connection.commit()
            save = {
                "encoder": "svt_av1",
                "encoder_preset": "5",
                "target_vmaf": 96.0,
                "crf_low": 14,
                "crf_high": 45,
                "sample_count": 3,
                "sample_seconds": 10.0,
            }
            lookup = {
                "encoder": "svt_av1",
                "encoder_preset": "5",
                "target_vmaf": 96.0,
                "crf_range": (14, 45),
                "sample_count": 3,
                "sample_seconds": 10.0,
            }
            # a gate skip: achieved_vmaf None, no recipe
            db.save_plan(
                connection,
                file_id,
                None,
                None,
                None,
                [],
                "too short",
                None,
                movie.stat().st_mtime,
                movie.stat().st_size,
                **save,
            )
            hit = db.get_valid_plan(
                connection,
                movie,
                vmaf_recipe=tools_vmaf.vmaf_recipe().fingerprint(),
                **lookup,
            )
            self.assertIsNotNone(hit, "a gate skip must stay reusable")
            self.assertEqual(hit["skipped_reason"], "too short")

            # a MEASURED plan still keys on the recipe
            db.save_plan(
                connection,
                file_id,
                22,
                95.5,
                None,
                [],
                None,
                100,
                movie.stat().st_mtime,
                movie.stat().st_size,
                vmaf_recipe=tools_vmaf.vmaf_recipe(bit_depth=8).fingerprint(),
                **save,
            )
            self.assertIsNotNone(
                db.get_valid_plan(
                    connection,
                    movie,
                    vmaf_recipe=tools_vmaf.vmaf_recipe(bit_depth=8).fingerprint(),
                    **lookup,
                )
            )
            self.assertIsNone(
                db.get_valid_plan(
                    connection,
                    movie,
                    vmaf_recipe=tools_vmaf.vmaf_recipe(bit_depth=10).fingerprint(),
                    **lookup,
                )
            )
            connection.close()

    def test_a_recipe_change_invalidates_the_plan_fingerprint(self):
        """The same rule for the plan: a stored plan measured with one recipe is
        never reused under another (issue #308). The recipe part the fingerprint
        carries is the crop-independent half (the plan lookup runs before bar
        detection), so the pixel format and the tone-map must each invalidate it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x" * 16)
            connection = db.connect(root / "meta.db")
            file_id = db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie,
                    mtime=movie.stat().st_mtime,
                    size=movie.stat().st_size,
                    name=movie.name,
                    category="Cat",
                ),
            )
            connection.commit()
            common = {
                "encoder": "svt_av1",
                "encoder_preset": "5",
                "target_vmaf": 96.0,
                "crf_low": 14,
                "crf_high": 45,
                "sample_count": 3,
                "sample_seconds": 10.0,
            }
            eight_bit = tools_vmaf.vmaf_recipe(bit_depth=8).fingerprint()
            ten_bit = tools_vmaf.vmaf_recipe(bit_depth=10).fingerprint()
            self.assertNotEqual(eight_bit, ten_bit)
            db.save_plan(
                connection,
                file_id,
                22,
                95.5,
                None,
                [],
                None,
                100,
                movie.stat().st_mtime,
                movie.stat().st_size,
                vmaf_recipe=eight_bit,
                **common,
            )
            lookup = {
                "crf_range": (14, 45),
                **{k: v for k, v in common.items() if k != "crf_low" and k != "crf_high"},
            }
            self.assertIsNotNone(
                db.get_valid_plan(connection, movie, vmaf_recipe=eight_bit, **lookup)
            )
            self.assertIsNone(db.get_valid_plan(connection, movie, vmaf_recipe=ten_bit, **lookup))
            # an empty recipe (a plan stored before the field existed) never matches
            self.assertIsNone(db.get_valid_plan(connection, movie, **lookup))
            connection.close()

    def test_the_chosen_crf_size_keys_on_hdr(self):
        """The cache key carries the HDR flag (issue #314's reviewer round 3):
        a cache seeded under hdr=True must be READ under hdr=True - flipping
        the flag on either side misses every entry and falls back. Mutating
        the reader's hdr to False fails this test (the fallback fires); the
        all-windows test above cannot catch it because its fixture is SDR.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            samples = []
            for i in range(2):
                s = root / f"s{i}.mkv"
                s.write_bytes(b"m" * 100)
                samples.append((s, 10.0 * i))

            for _sample, offset in samples:
                key = engine_search.probe_cache_key(
                    movie,
                    offset,
                    10.0,
                    22,
                    "svt_av1",
                    "5",
                    None,
                    True,
                    None,
                    ("hb", "1.6"),
                    tools_vmaf.vmaf_recipe(hdr=True).identifier(),
                )
                db.save_probe_score(connection, key, 95.0, 40)

            with (
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
                patch.object(
                    engine_search,
                    "estimate_size_factor",
                    side_effect=AssertionError("the fallback ran: the cache was incomplete"),
                ),
            ):
                factor = engine_search.check_chosen_crf_shrinks(
                    samples,
                    workdir,
                    "hb",
                    22,
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    hdr=True,
                    db_file=db_file,
                )
            # the HDR-keyed rows were found: the factor came from the cache
            self.assertAlmostEqual(factor, 80 / 200, places=4)
            connection.close()

    def test_the_chosen_crf_size_encodes_when_every_window_shrinks(self):
        """The reverse direction (issue #314's acceptance): a cache whose
        every window shrank yields a factor BELOW 1.0 - the file encodes.
        A production change that never returns a shrinking factor (e.g. the
        totals summed on the wrong side) fails here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            samples = []
            for i in range(3):
                s = root / f"s{i}.mkv"
                s.write_bytes(b"m" * 1000)
                samples.append((s, 10.0 * i))

            # every window encoded to 400 bytes (0.4x)
            for _sample, offset in samples:
                key = engine_search.probe_cache_key(
                    movie,
                    offset,
                    10.0,
                    22,
                    "svt_av1",
                    "5",
                    None,
                    False,
                    None,
                    ("hb", "1.6"),
                    tools_vmaf.vmaf_recipe().identifier(),
                )
                db.save_probe_score(connection, key, 95.0, 400)

            with (
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
                patch.object(
                    engine_search,
                    "estimate_size_factor",
                    side_effect=AssertionError("the fallback ran: the cache was incomplete"),
                ),
            ):
                factor = engine_search.check_chosen_crf_shrinks(
                    samples,
                    workdir,
                    "hb",
                    22,
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    db_file=db_file,
                )
            self.assertLess(factor, 1.0)
            self.assertAlmostEqual(factor, 1200 / 3000, places=4)
            connection.close()

    def test_probe_crf_records_the_encoded_size(self):
        """The producer side of #314: when probe_crf encodes a window it
        stores that window's encoded byte count beside the score. Deleting
        the encoded.stat().st_size argument must fail this test - otherwise
        the all-window decision reads a cache that nothing fills.
        """

        def fake_run(command, cancel=None):
            # the command's output path follows the --output flag: create it
            # with known bytes so the recorded size is exact
            out = Path(command[command.index("--output") + 1])
            out.write_bytes(b"e" * 777)
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            connection.close()
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            sample = root / "s0.mkv"
            sample.write_bytes(b"m" * 100)

            with (
                patch.object(engine_search, "_run", side_effect=fake_run),
                patch.object(engine_search, "measure_vmaf", return_value=95.0),
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
            ):
                score = engine_search.probe_crf(
                    [(sample, 0.0)],
                    22,
                    workdir,
                    "hb",
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    db_file=db_file,
                )
            self.assertEqual(score, 95.0)
            # the producer wrote the size: the row reads (95.0, 777)
            connection = db.connect(db_file)
            key = engine_search.probe_cache_key(
                movie,
                0.0,
                10.0,
                22,
                "svt_av1",
                "5",
                None,
                False,
                None,
                ("hb", "1.6"),
                tools_vmaf.vmaf_recipe().identifier(),
            )
            self.assertEqual(db.get_probe_entry(connection, key), (95.0, 777))
            connection.close()

    def test_the_chosen_crf_size_falls_back_on_a_null_size(self):
        """A cache entry whose size predates #314 (NULL) makes the all-window
        estimate incomplete: the decision falls back to the single-window
        encode - a miss degrades to the old behaviour, never to a guess.

        Every window HAS a row here; the only defect is the missing size.
        That isolates the NULL half of the guard: a mutation that treats a
        NULL size as usable puts None into the sum (TypeError) or, if the
        guard is skipped entirely, returns a factor computed from fewer
        windows - either way this test fails, because the fallback must be
        the outcome.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            samples = []
            for i in range(3):
                s = root / f"s{i}.mkv"
                s.write_bytes(b"m" * 100)
                samples.append((s, 10.0 * i))

            # every window has a row; window 0's size is NULL (the legacy shape)
            for i, offset in enumerate(10.0 * n for n in range(3)):
                key = engine_search.probe_cache_key(
                    movie,
                    offset,
                    10.0,
                    22,
                    "svt_av1",
                    "5",
                    None,
                    False,
                    None,
                    ("hb", "1.6"),
                    tools_vmaf.vmaf_recipe().identifier(),
                )
                if i == 0:
                    db.save_probe_score(connection, key, 95.0)  # NULL size
                else:
                    db.save_probe_score(connection, key, 95.0, 40)

            logged: list[str] = []

            def fake_log(message: str) -> None:
                logged.append(message)

            real_get = db.get_probe_entry
            reads: list = []

            def spy(conn, key):
                r = real_get(conn, key)
                reads.append((key, r))
                return r

            with (
                patch.object(engine_search, "estimate_size_factor", return_value=0.5) as est,
                patch.object(engine_search, "log", side_effect=fake_log),
                patch.object(db, "get_probe_entry", side_effect=spy),
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
            ):
                factor = engine_search.check_chosen_crf_shrinks(
                    samples,
                    workdir,
                    "hb",
                    22,
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    db_file=db_file,
                )
                # the fallback path ran (the old single-window estimate)
                est.assert_called_once()
                self.assertEqual(factor, 0.5)
                # the decision came from the FALLBACK: the function logs
                # only its completeness claim, so ANY log on the fallback
                # path is the reviewer's V2 lie (a partial factor announced
                # as the all-window one) - caught structurally, whatever the
                # message says. Every guard weakening ends either here, in
                # the factor mismatch above, or in a TypeError from the sum.
                self.assertEqual(
                    len(logged), 0, f"the fallback path logged a completeness claim: {logged}"
                )
                # the fixture's NULL entry was actually READ: without this a
                # key mismatch silently turns the test into the missing-
                # window case (the reads-spy's one non-tautological job)
                self.assertTrue(
                    any(entry is not None and entry[1] is None for _, entry in reads),
                    f"the seeded NULL entry was never read: {reads}",
                )
            connection.close()

    def test_the_chosen_crf_size_falls_back_on_a_missing_window(self):
        """A window with NO cache row makes the all-window estimate
        incomplete the same way (issue #314): the fallback runs. This test
        isolates the missing-entry half of the guard - no NULL sizes exist
        here, so a mutation that drops the missing-entry half hits
        entry[1] on None (TypeError) or returns a partial sum as the
        all-window estimate; either way the fallback was owed and this
        test fails without it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            samples = []
            for i in range(3):
                s = root / f"s{i}.mkv"
                s.write_bytes(b"m" * 100)
                samples.append((s, 10.0 * i))

            # window 0 has a full row; windows 1-2 have NO row at all
            key0 = engine_search.probe_cache_key(
                movie,
                0.0,
                10.0,
                22,
                "svt_av1",
                "5",
                None,
                False,
                None,
                ("hb", "1.6"),
                tools_vmaf.vmaf_recipe().identifier(),
            )
            db.save_probe_score(connection, key0, 95.0, 40)

            logged: list[str] = []

            def fake_log(message: str) -> None:
                logged.append(message)

            real_get = db.get_probe_entry
            reads: list = []

            def spy(conn, key):
                r = real_get(conn, key)
                reads.append((key, r))
                return r

            with (
                patch.object(engine_search, "estimate_size_factor", return_value=0.5) as est,
                patch.object(engine_search, "log", side_effect=fake_log),
                patch.object(db, "get_probe_entry", side_effect=spy),
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
            ):
                factor = engine_search.check_chosen_crf_shrinks(
                    samples,
                    workdir,
                    "hb",
                    22,
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    db_file=db_file,
                )
                est.assert_called_once()
                self.assertEqual(factor, 0.5)
                # the same structural log pin: ANY log on the fallback path
                # is a completeness lie, whatever it says
                self.assertEqual(
                    len(logged), 0, f"the fallback path logged a completeness claim: {logged}"
                )
                # the fixture's MISSING window was actually read (a None
                # read): the exercise-shape witness, not a tautology
                self.assertTrue(
                    any(entry is None for _, entry in reads), f"no missing read seen: {reads}"
                )
            connection.close()

    def test_size_factor_is_cached(self):
        factor, _ = self._size_factor()
        self.assertEqual(factor, 0.5)
        # second call: no encode
        factor2, encodes2 = self._size_factor()
        self.assertEqual(factor2, 0.5)
        self.assertEqual(len(encodes2), 0)

    def _size_factor(self, **kwargs):
        encodes: list = []

        def fake_run(command, cancel=None):
            encodes.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        defaults = {
            "samples": [(self.sample, 10.0)],
            "workdir": self.workdir,
            "handbrake": "hb",
            "crf": 32,
            "movie": self.movie,
            "sample_seconds": 10.0,
            "encoder": "x265",
            "probe_encoder_preset": "medium",
            "db_file": self.db_file,
        }
        with (
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
        ):
            factor = engine_search.estimate_size_factor(**{**defaults, **kwargs})
        return factor, encodes

    def _recording_connect(self):
        """Patches db.connect to record every probe-cache connection."""
        opened: list[sqlite3.Connection] = []
        real_connect = db.connect

        def recording_connect(db_path):
            connection = real_connect(db_path)
            opened.append(connection)
            return connection

        return opened, patch("shrinkray.db.connect", side_effect=recording_connect)

    def assert_closed(self, connections):
        self.assertTrue(connections, "the probe opened no cache connection to check")
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError, msg="the connection was left open"):
                connection.execute("SELECT 1")

    def test_a_failed_vmaf_measurement_closes_the_cache_connection(self):
        """Issue #480: the probe holds its cache connection across the encode
        and the measurement; a measurement that raised left it to the garbage
        collector instead of closing it."""

        def fake_run(command, cancel=None):
            Path(command[command.index("--output") + 1]).write_bytes(b"z" * 4)
            return 0, ""

        opened, recording = self._recording_connect()
        with (
            recording,
            patch.object(engine_search, "_tool_versions", return_value="hb 1.0;libvmaf=v3"),
            patch.object(engine_search, "_run", side_effect=fake_run),
            patch.object(engine_search, "measure_vmaf", side_effect=RuntimeError("libvmaf failed")),
            self.assertRaises(RuntimeError),
        ):
            engine_search.probe_crf(
                [(self.sample, 10.0)],
                20,
                self.workdir,
                "hb",
                self.movie,
                10.0,
                "svt_av1",
                "medium",
                db_file=self.db_file,
            )
        self.assert_closed(opened)

    def test_a_failed_probe_cache_write_closes_the_connection(self):
        """A cache write that raises (a full disk) closes the connection, in
        both the VMAF probe and the size probe."""
        full = sqlite3.OperationalError("database or disk is full")
        for name, call in (
            ("probe_crf", self._probe),
            ("estimate_size_factor", self._size_factor),
        ):
            with self.subTest(name):
                opened, recording = self._recording_connect()
                with (
                    recording,
                    patch("shrinkray.db.save_probe_score", side_effect=full),
                    self.assertRaises(sqlite3.OperationalError),
                ):
                    call()
                self.assert_closed(opened)

    def test_size_and_vmaf_caches_do_not_collide(self):
        """The same (sample, CRF) probed as size ratio and as VMAF score are
        different measurements: the kind is part of the key."""
        self._probe(crf=32)
        factor, encodes = self._size_factor()
        self.assertEqual(len(encodes), 1)  # not served from the vmaf entry
        self.assertEqual(factor, 0.5)


class TestChosenCrfSizeGate(unittest.TestCase):
    """Issue #129: the search must check size at the CRF it chose, not only at
    the top of the range.

    The top-of-range probe (crf_range[1], 45) is a *bound*: it answers "can any
    CRF shrink this file?". The chosen CRF sits far below it, so the file there
    is much bigger and can still grow. These tests drive the two calls to
    different answers, which a flat mock cannot do.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name) / "work"
        self.workdir.mkdir()
        self.movie = Path(self.tmp.name) / "movie.mkv"
        self.movie.write_bytes(b"x" * 100)
        self.sample = self.workdir / "sample.mkv"
        self.sample.write_bytes(b"y" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_search(self, factors: dict[int, float]):
        """Runs find_optimal_crf_and_skip with estimate_size_factor keyed by CRF.

        A dict (rather than a constant) is the point: it lets the top-of-range
        probe and the chosen-CRF probe disagree, which is the case under test.
        """
        seen: list[int] = []

        def fake_estimate(*args, **kwargs):
            crf = args[3]
            seen.append(crf)
            return factors[crf]

        with (
            patch.object(engine_search, "estimate_size_factor", side_effect=fake_estimate),
            patch.object(engine_search, "find_optimal_crf", return_value=(22, 95.5, [])),
        ):
            result = engine_search.find_optimal_crf_and_skip(
                self.movie,
                self.workdir,
                "hb",
                samples=[(self.sample, 10.0)],
            )
        return result, seen

    def test_a_growth_at_the_chosen_crf_is_skipped(self):
        """The top of the range shrinks, the chosen CRF does not: skip.

        This is the case the old code missed entirely - it would have encoded the
        file at CRF 22 and made it bigger.
        """
        result, seen = self._run_search({45: 0.5, 22: 1.2})
        _crf, _vmaf, _probes, skip = result
        self.assertTrue(skip, "a grown chosen CRF must be skipped")
        self.assertIn(45, seen, "the bound probe must still run")
        self.assertIn(22, seen, "the chosen CRF must be checked")

    def test_a_shrink_at_both_points_is_encoded(self):
        """The regression guard: the new gate must not reject a normal file."""
        result, seen = self._run_search({45: 0.4, 22: 0.7})
        _crf, _vmaf, _probes, skip = result
        self.assertFalse(skip, "a file that shrinks at the chosen CRF must encode")
        self.assertIn(22, seen)

    def test_the_early_exit_still_skips_the_search(self):
        """When the bound itself cannot shrink, the search never runs.

        The bound is the whole point of the top-of-range probe: if CRF 45 cannot
        shrink the file, nothing in the range can, so the bisect is wasted work.
        """
        with (
            patch.object(engine_search, "estimate_size_factor", return_value=1.3),
            patch.object(engine_search, "find_optimal_crf") as fake_search,
        ):
            result = engine_search.find_optimal_crf_and_skip(
                self.movie,
                self.workdir,
                "hb",
                samples=[(self.sample, 10.0)],
            )
        fake_search.assert_not_called()
        self.assertTrue(result[3], "the early exit must report a skip")

    def test_a_bypassed_cache_never_reads_the_sizes(self):
        """The use_probe_cache contract for the size decision (issue #391):
        False means bypass ENTIRELY - the chosen-CRF decision must not read
        the cached sizes even when every entry is present, because the flag's
        caller (--replan) wants independence from everything stored. The
        round-5 review's mutation deleted the use_probe_cache check and the
        whole suite stayed green; this test is the pin it lacked.

        The cache is fully seeded (every window has a row with a size): the
        ONLY reason to fall back is the bypass itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db_file = root / "meta.db"
            connection = db.connect(db_file)
            workdir = root / "w"
            workdir.mkdir()
            movie = root / "movie.mkv"
            movie.write_bytes(b"x")
            samples = []
            for i in range(2):
                s = root / f"s{i}.mkv"
                s.write_bytes(b"m" * 100)
                samples.append((s, 10.0 * i))

            for _sample, offset in samples:
                key = engine_search.probe_cache_key(
                    movie,
                    offset,
                    10.0,
                    22,
                    "svt_av1",
                    "5",
                    None,
                    False,
                    None,
                    ("hb", "1.6"),
                    tools_vmaf.vmaf_recipe().identifier(),
                )
                db.save_probe_score(connection, key, 95.0, 40)

            real_get = db.get_probe_entry
            reads: list = []

            def spy(conn, key):
                r = real_get(conn, key)
                reads.append(r)
                return r

            with (
                patch.object(engine_search, "_tool_versions", return_value=("hb", "1.6")),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5) as est,
                patch.object(db, "get_probe_entry", side_effect=spy),
            ):
                factor = engine_search.check_chosen_crf_shrinks(
                    samples,
                    workdir,
                    "hb",
                    22,
                    movie,
                    10.0,
                    "svt_av1",
                    "5",
                    db_file=db_file,
                    use_probe_cache=False,
                )
            # the bypass: the fallback ran and the cache was never read
            est.assert_called_once()
            self.assertEqual(factor, 0.5)
            self.assertEqual(reads, [], "a bypassed cache was read for the size decision")
            connection.close()

    def test_the_chosen_check_is_a_separate_measurement(self):
        """The search's own chosen-CRF check encodes one window separately.

        Since #314 the size DECISION at the chosen CRF reads the VMAF probes'
        recorded byte counts when every window's cache entry carries a size
        (check_chosen_crf_shrinks). The path this test exercises is the
        NO-DATABASE one (_run_search passes no db_file): with nothing cached,
        the decision falls back to a separate single-window encode - two
        encodes at two CRFs, the bound and the chosen. Asserting both were
        measured keeps the fallback honest.
        """
        _result, seen = self._run_search({45: 0.5, 22: 1.2})
        self.assertEqual(len(seen), 2, "one bound probe + one chosen-CRF probe")
        self.assertEqual(seen.count(45), 1)
        self.assertEqual(seen.count(22), 1)

    def test_the_plumbing_reaches_the_chosen_check(self):
        """find_optimal_crf_and_skip forwards hdr and db_file to the chosen-CRF
        check (review round 4, finding 4): the decision's cache key must carry
        the HDR flag and the database the caller was given - mutating either
        at the call site left the whole suite green before this test.
        """
        captured: dict = {}

        def fake_check(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return 0.5

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "meta.db"
            with (
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "find_optimal_crf", return_value=(22, 95.5, [])),
                patch.object(engine_search, "check_chosen_crf_shrinks", side_effect=fake_check),
            ):
                engine_search.find_optimal_crf_and_skip(
                    self.movie,
                    self.workdir,
                    "hb",
                    samples=[(self.sample, 10.0)],
                    hdr=True,
                    db_file=db_file,
                )
        self.assertEqual(
            captured["kwargs"].get("hdr"), True, "the HDR flag did not reach the chosen check"
        )
        self.assertEqual(
            captured["kwargs"].get("db_file"),
            db_file,
            "the database did not reach the chosen check",
        )

        # the False half (review round 5, MEDIUM 1): a call site that
        # hard-codes hdr=True passes the capture above; a False call must
        # arrive as False
        captured.clear()
        with (
            patch.object(engine_search, "estimate_size_factor", return_value=0.5),
            patch.object(engine_search, "find_optimal_crf", return_value=(22, 95.5, [])),
            patch.object(engine_search, "check_chosen_crf_shrinks", side_effect=fake_check),
        ):
            engine_search.find_optimal_crf_and_skip(
                self.movie,
                self.workdir,
                "hb",
                samples=[(self.sample, 10.0)],
                hdr=False,
                db_file=db_file,
            )
        self.assertEqual(
            captured["kwargs"].get("hdr"), False, "the HDR flag did not reach the chosen check"
        )


class TestSubprocessTimeouts(unittest.TestCase):
    """The short-lived ffprobe/ffmpeg calls turn a TimeoutExpired into their
    documented failure path instead of hanging the thread forever (issue
    #62: a stale NFS/SMB mount blocks the kernel read with no timeout)."""

    def test_detect_vmaf_style_timeout_raises_documented_error(self):
        import subprocess

        # the style is detected once and cached module-wide (in tools.vmaf
        # since the phase-2 move); other tests in the suite may have
        # populated the cache, so reset it around this test (and restore it
        # - the real value stays valid)
        import shrinkray.tools.vmaf as vmaf_tools

        saved = vmaf_tools._vmaf_style
        vmaf_tools._vmaf_style = None
        try:
            with (
                patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                patch(
                    "shrinkray.tools.vmaf.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60),
                ),
                self.assertRaises(RuntimeError) as ctx,
            ):
                tools_vmaf.detect_vmaf_style()
            self.assertIn("60s", str(ctx.exception))
            # a failed detection must not be cached: the next call probes again
            self.assertIsNone(vmaf_tools._vmaf_style)
        finally:
            vmaf_tools._vmaf_style = saved

    def test_detect_crop_rect_timeout_returns_none(self):
        import subprocess

        movie = Path(tempfile.mkdtemp()) / "no-such-file.mkv"
        try:
            with patch(
                "shrinkray.engine.crop.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
            ) as mocked:
                self.assertIsNone(engine_crop.detect_crop_rect(movie, None, count=3))
            # exactly the ffprobe frame-size call was made (not the
            # cropdetect _run); the duration probe inside uses -show_format
            self.assertIn("-show_format", mocked.call_args.args[0])
        finally:
            movie.parent.rmdir()

    def test_sample_count_for_derives_from_duration(self):
        """The probe count follows the runtime at a fixed density.

        A count-based knob makes coverage inverse to runtime: the longer the
        movie, the less of it gets probed. Deriving the count from
        sample_every keeps the probed fraction at sample_seconds /
        sample_every regardless of length (issue #14).
        """
        # a 20-minute short: one window per 10 minutes => 2
        self.assertEqual(engine_search.sample_count_for(1200.0, 600.0), 2)
        # a 2-hour film => 12
        self.assertEqual(engine_search.sample_count_for(7200.0, 600.0), 12)
        # a 4-hour film => 24 (coverage stays the same fraction)
        self.assertEqual(engine_search.sample_count_for(14400.0, 600.0), 24)

    def test_sample_count_for_short_input_yields_one(self):
        """An input shorter than the density still yields one window."""
        self.assertEqual(engine_search.sample_count_for(300.0, 600.0), 1)
        self.assertEqual(engine_search.sample_count_for(1.0, 600.0), 1)

    def test_sample_count_for_explicit_count_overrides(self):
        """An explicit sample_count wins over the density knob.

        A latency-sensitive caller (watch mode) must still be able to pin a
        fixed probe budget.
        """
        self.assertEqual(engine_search.sample_count_for(7200.0, 600.0, sample_count=3), 3)

    def test_sample_count_for_disabled_density_uses_default(self):
        """A disabled density falls back to the count-based default."""
        self.assertEqual(
            engine_search.sample_count_for(7200.0, 0.0), engine_plan.DEFAULT_SAMPLE_COUNT
        )
        self.assertEqual(
            engine_search.sample_count_for(None, 600.0), engine_plan.DEFAULT_SAMPLE_COUNT
        )

    def test_plan_records_derived_sample_count(self):
        """The plan records the DERIVED count, not the raw default.

        The plan fingerprint keys on the sample count, so a plan recorded with
        three windows must not be reused by a run whose density derives
        twelve (issue #14).
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=7200.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 2160}
                ),
                patch.object(engine_plan, "source_codec", return_value="h264"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(
                    engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]
                ) as fake_extract,
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src, sample_every=600.0)
        self.assertTrue(result.success)
        self.assertEqual(result.sample_count, 12)
        # the extraction was asked for the derived count
        self.assertEqual(fake_extract.call_args.args[2], 12)

    def test_detect_crop_rect_defaults_to_ten_samples(self):
        """The crop detector samples ten windows by default.

        With the bars common to every sample the crop is monotone
        non-increasing in the sample count, so more samples only ever shrink
        it. That is the safe direction for an irreversible crop, and it makes
        an aspect-ratio switch much less likely to be missed (issue #17).
        """
        commands: list[list[str]] = []

        def fake_run(command, cancel=None):
            commands.append(command)
            return 0, "crop=1920:800:0:140"

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            with (
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=600.0),
                patch(
                    "shrinkray.engine.crop.subprocess.run",
                    return_value=type(
                        "P",
                        (),
                        {
                            "returncode": 0,
                            "stdout": b'{"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}',
                        },
                    )(),
                ),
                patch("shrinkray.engine.crop._run", side_effect=fake_run),
            ):
                engine_crop.detect_crop_rect(movie, Path(tmp))
        self.assertEqual(len(commands), 10)

    def test_crop_samples_are_configurable(self):
        """The crop sample count reaches the detector from the caller.

        A plan made with three crop samples must not be reused by a run that
        samples ten, so the count cannot be a hidden default.
        """
        commands: list[list[str]] = []

        def fake_run(command, cancel=None):
            commands.append(command)
            return 0, "crop=1920:800:0:140"

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            with (
                patch("shrinkray.tools.ffprobe.probe_duration_seconds", return_value=600.0),
                patch(
                    "shrinkray.engine.crop.subprocess.run",
                    return_value=type(
                        "P",
                        (),
                        {
                            "returncode": 0,
                            "stdout": b'{"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}',
                        },
                    )(),
                ),
                patch("shrinkray.engine.crop._run", side_effect=fake_run),
            ):
                engine_crop.detect_crop_rect(movie, Path(tmp), count=4)
        self.assertEqual(len(commands), 4)

    def test_plan_fingerprint_records_crop_sample_count(self):
        """A plan made with a different crop sample count is not reused.

        The crop depends on the sample count, so the count belongs to the
        decision fingerprint (issue #17).
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.db"
            connection = db.connect(db_file)
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            stat = movie.stat()
            file_id = db.upsert_file(
                connection,
                db.MovieRecord(
                    path=movie,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    name="movie",
                    category="Cat",
                ),
            )
            db.save_plan(
                connection,
                file_id,
                chosen_crf=20,
                achieved_vmaf=95.0,
                crop=None,
                probes=[],
                skipped_reason=None,
                estimated_size=None,
                input_mtime=stat.st_mtime,
                input_size=stat.st_size,
                encoder="svt_av1",
                encoder_preset="5",
                target_vmaf=95.0,
                crf_low=14,
                crf_high=32,
                sample_count=3,
                sample_seconds=10.0,
                crop_sample_count=3,
            )
            common = {
                "encoder": "svt_av1",
                "encoder_preset": "5",
                "target_vmaf": 95.0,
                "crf_range": (14, 32),
                "sample_count": 3,
                "sample_seconds": 10.0,
            }
            self.assertIsNotNone(
                db.get_valid_plan(connection, movie, **common, crop_sample_count=3)
            )
            self.assertIsNone(db.get_valid_plan(connection, movie, **common, crop_sample_count=10))
            connection.close()

    def test_codec_gate_rejects_older_codec(self):
        """A codec outside the whitelist is never re-encoded, before probing.

        PRODUCT.md: a source in a codec older than H.264 is not a candidate.
        The gate must decide from the codec alone and reach neither sample
        extraction nor HandBrake, and the message must name the actual rule
        (the candidate list) - the old text said "below the h264 minimum",
        which was false for every codec newer than H.264 that the list still
        excluded (issue #233).
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
                ),
                patch.object(engine_plan, "source_codec", return_value="mpeg2video"),
                patch.object(engine_plan, "extract_samples") as fake_extract,
                patch.object(engine_plan, "find_handbrake") as fake_hb,
            ):
                result = engine_plan.plan_quality(src)
        self.assertFalse(result.success)
        self.assertEqual(result.skipped_reason, "source codec not a candidate")
        self.assertIn("mpeg2video", result.error or "")
        self.assertIn("candidate codec list", result.error or "")
        fake_extract.assert_not_called()
        fake_hb.assert_not_called()

    def test_codec_gate_admits_vp9(self):
        """VP9 is a candidate codec (issue #233).

        VP9 is newer than H.264 in every sense that matters here; it was
        excluded only because the gate predates the whitelist decision.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
                ),
                patch.object(engine_plan, "source_codec", return_value="vp9"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success)
        self.assertIsNone(result.skipped_reason)

    def test_the_default_config_never_empties_the_whitelist(self):
        """Every entry point passes the config's candidate_codecs to the gate,
        and the config holds only the operator's EXTRA codecs (empty by
        default). The gate must still accept every whitelisted codec, in plan
        and encode alike: #265 let the empty default replace the whitelist,
        and every source was skipped (issue #271)."""
        from shrinkray.config import Conf

        for extras in (tuple(Conf({}).candidate_codecs), ("mpeg4",)):
            for codec in SOURCE_CODECS:
                with self.subTest(extras=extras, codec=codec), tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "movie.mkv"
                    src.write_bytes(b"x" * 8)
                    with (
                        patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                        patch.object(
                            engine_plan,
                            "_source_dimensions",
                            return_value={"width": 1920, "height": 1080},
                        ),
                        patch.object(engine_plan, "source_codec", return_value=codec),
                        patch.object(engine_plan, "find_handbrake", return_value="hb"),
                        patch.object(
                            engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]
                        ),
                        patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                        patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                        patch.object(
                            engine_search,
                            "probe_crf",
                            return_value=engine_search.DEFAULT_TARGET_VMAF,
                        ),
                    ):
                        planned = engine_plan.plan_quality(src, candidate_codecs=extras)
                    # encode: the disk preflight right after the codec gate
                    # answers instead, so reaching it proves the gate passed
                    with (
                        patch.object(
                            engine_pipeline,
                            "_source_snapshot",
                            return_value={"width": 1920, "height": 1080},
                        ),
                        patch.object(engine_plan, "is_hdr_file", return_value=False),
                        patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
                        patch.object(engine_pipeline, "source_codec", return_value=codec),
                        patch.object(
                            engine_pipeline,
                            "check_disk_space",
                            return_value=(False, "past the codec gate"),
                        ),
                    ):
                        encoded = engine_pipeline.encode_with_quality(
                            src, Path(tmp) / "out.mkv", candidate_codecs=extras
                        )
                self.assertNotEqual(planned.skipped_reason, "source codec not a candidate")
                self.assertEqual(encoded.error, "past the codec gate")

    def test_codec_gate_config_extends_the_whitelist(self):
        """A config codec joins the product whitelist; neither replaces the other.

        The whitelist is a product rule, so config can only extend it - and
        the operator's list must reach the gate, which is the plan/encode
        parity this issue's first defect (issue #233).
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
                ),
                patch.object(engine_plan, "source_codec", return_value="mpeg4"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                extended = engine_plan.plan_quality(src, candidate_codecs=(*SOURCE_CODECS, "mpeg4"))
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
                ),
                patch.object(engine_plan, "source_codec", return_value="mpeg4"),
                patch.object(engine_plan, "extract_samples") as fake_extract,
            ):
                rejected = engine_plan.plan_quality(src)
        self.assertTrue(extended.success)
        self.assertIsNone(extended.skipped_reason)
        self.assertEqual(rejected.skipped_reason, "source codec not a candidate")
        fake_extract.assert_not_called()

    def test_codec_gate_admits_h264_on_size_alone(self):
        """An H.264 source is a candidate on size alone (PRODUCT.md)."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 1080}
                ),
                patch.object(engine_plan, "source_codec", return_value="h264"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success)
        self.assertIsNone(result.skipped_reason)

    def test_hevc_without_bars_is_probed_and_encoded(self):
        """A modern codec is probed on size alone, exactly like H.264 (issue #128).

        HEVC is *more* efficient than H.264, so it needs a higher size bar to
        clear - not a different rule. A 40 GB HEVC remux is precisely the file
        this tool exists for, and the old gate never probed it at all unless the
        operator remembered --remove-bars.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 2160}
                ),
                patch.object(engine_plan, "source_codec", return_value="hevc"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_crop, "detect_crop_rect", return_value=None),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success, result.error)
        self.assertIsNone(result.skipped_reason)
        # no bars is a normal outcome, not a skip: the encode proceeds uncropped
        self.assertIsNone(result.crop)

    def test_hevc_with_bars_gets_the_crop_on_probe_and_encode(self):
        """Bars are removed as part of every re-encode, with no flag (issue #128).

        The crop must reach the size estimate - otherwise the prediction describes
        an uncropped output and the size gate compares the wrong number.
        """
        bars = (20, 20, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 2160}
                ),
                patch.object(engine_plan, "source_codec", return_value="hevc"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "detect_crop_rect", return_value=bars) as fake_crop,
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5) as fake_size,
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.crop, bars)
        # the size estimate was made against the CROPPED output
        self.assertEqual(fake_size.call_args.kwargs["crop"], bars)
        # detected once by the pipeline, never pre-detected by a gate
        self.assertEqual(fake_crop.call_count, 1)

    def test_hevc_with_no_size_gain_is_skipped_by_size_not_codec(self):
        """A modern codec that cannot shrink is skipped for the size reason.

        The verdict must come from the size gate, not from a codec rule: 'already
        efficient' no longer exists as a reason (issue #128).
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 2160}
                ),
                patch.object(engine_plan, "source_codec", return_value="hevc"),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_crop, "detect_crop_rect", return_value=None),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=1.3),
                patch.object(engine_search, "estimate_size_factor", return_value=1.3),
            ):
                result = engine_plan.plan_quality(src)
        self.assertFalse(result.success)
        self.assertNotEqual(result.skipped_reason, "already efficient")
        self.assertEqual(result.skipped_reason, "no size gain")
        self.assertIn("would not shrink", result.error or "")

    def test_min_source_size_gate_skips_before_probing(self):
        """A source below the size floor is skipped before any probe runs.

        Probe cost is roughly constant per file while the payoff scales with
        size, so the smallest files have the worst cost/benefit ratio. The gate
        must decide from one stat and reach neither extraction nor HandBrake.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)  # a few bytes: far below 5 GB
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 2160}
                ),
                patch.object(engine_plan, "extract_samples") as fake_extract,
                patch.object(engine_plan, "find_handbrake") as fake_hb,
            ):
                result = engine_plan.plan_quality(src, min_source_size_gb=5.0)
        self.assertFalse(result.success)
        self.assertEqual(result.skipped_reason, "below min size")
        self.assertIn("5.00 GB", result.error or "")
        fake_extract.assert_not_called()
        fake_hb.assert_not_called()

    def test_min_source_size_gate_is_off_by_default(self):
        """A small file is a normal candidate when no floor is configured.

        The gate must be opt-in: a file that would previously be probed must
        still be probed when the operator has not asked for a size floor.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 2160}
                ),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success)
        self.assertIsNone(result.skipped_reason)

    def test_resolution_gate_skips_below_the_1080p_class(self):
        """A source below BOTH class floors is skipped before any probe runs.

        The gate exists to avoid spending the size probe and the CRF search on
        a source whose pixel count bounds the saving; it must decide from the
        resolution alone and reach neither extraction nor HandBrake.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1280, "height": 720}
                ),
                patch.object(engine_plan, "extract_samples") as fake_extract,
                patch.object(engine_plan, "find_handbrake") as fake_hb,
            ):
                result = engine_plan.plan_quality(src)
        self.assertFalse(result.success)
        self.assertEqual(result.skipped_reason, "resolution too low")
        self.assertIn("1280x720", result.error or "")
        self.assertIn("1920x1080", result.error or "")
        fake_extract.assert_not_called()
        fake_hb.assert_not_called()

    def test_resolution_gate_admits_a_scope_film_by_width(self):
        """A 1920x800 scope film is a 1080p-class master (issue #233).

        A height-only floor skipped exactly the widescreen sources this
        product exists for: either dimension at class size qualifies.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1920, "height": 800}
                ),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                planned = engine_plan.plan_quality(src)
            with (
                patch.object(
                    engine_pipeline,
                    "_source_snapshot",
                    return_value={
                        "width": 1920,
                        "height": 800,
                        "codec": "h264",
                        "duration": 600.0,
                        "bitrate": None,
                        "bit_depth": None,
                    },
                ),
                patch.object(engine_pipeline, "is_hdr_file", return_value=False),
                patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
                patch.object(engine_pipeline, "find_handbrake", return_value="hb"),
                patch.object(engine_pipeline, "check_disk_space", return_value=(True, "")),
                patch.object(
                    engine_pipeline,
                    "_encode_with_command",
                    side_effect=lambda _in, out, _cmd, **_kw: (
                        out.write_bytes(b"y" * 4),
                        engine_pipeline.EncodeResult(
                            input=_in,
                            output=out,
                            success=True,
                            input_size=_in.stat().st_size,
                            output_size=out.stat().st_size,
                        ),
                    )[1],
                ),
                patch.object(
                    engine_pipeline,
                    "_probe_movie",
                    return_value=(20, engine_search.DEFAULT_TARGET_VMAF, [], None),
                ) as fake_probe,
            ):
                encoded = engine_pipeline.encode_with_quality(src, Path(tmp) / "out.mkv")
        self.assertTrue(planned.success)
        self.assertIsNone(planned.skipped_reason)
        self.assertTrue(encoded.success)
        self.assertIsNone(encoded.skipped_reason)
        # the encode got past every gate on a 1920x800 source: the class
        # predicate admitted a scope film end to end
        fake_probe.assert_called_once()

    def test_resolution_gate_admits_anamorphic_by_height(self):
        """A 1440x1080 anamorphic source is 1080p class by height."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1440, "height": 1080}
                ),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success)
        self.assertIsNone(result.skipped_reason)

    def test_resolution_gate_partial_dimensions_skip_on_the_known_arm(self):
        """A read width below class with an unreadable height is still a skip.

        "Unknown is not a skip" protects an unreadable probe, not a known
        small one: when one dimension was read and neither reaches the class
        floor, the file is below the class and the probe cost buys nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1280, "height": None}
                ),
                patch.object(engine_plan, "extract_samples") as fake_extract,
            ):
                result = engine_plan.plan_quality(src)
        self.assertEqual(result.skipped_reason, "resolution too low")
        self.assertIn("width 1280px", result.error or "")
        fake_extract.assert_not_called()

    def test_resolution_gate_defaults_to_the_1080p_class(self):
        """A caller that passes no floor gets the product's 1080p class.

        The engine default used to be 0 (gate off), so a caller that forgot
        the argument silently disabled a product rule - which is how the
        `plan` command came to probe 720p sources that `encode` skips
        (issue #246).
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 1280, "height": 720}
                ),
                patch.object(engine_plan, "extract_samples") as fake_extract,
                patch.object(engine_plan, "find_handbrake") as fake_hb,
            ):
                result = engine_plan.plan_quality(src)
        self.assertEqual(result.skipped_reason, "resolution too low")
        fake_extract.assert_not_called()
        fake_hb.assert_not_called()

    def test_a_raised_floor_skips_1080p_in_plan_and_encode_alike(self):
        """A 4K-only floor skips a 1080p source in BOTH plan and encode.

        `plan` predicts and `encode` decides; a raised floor that only one of
        them honours would predict encodes that never happen (issue #246).
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": 3840, "height": 2160}
                ),
                patch.object(engine_plan, "extract_samples") as fake_extract,
            ):
                planned = engine_plan.plan_quality(
                    src, min_source_height=4320, min_source_width=7680
                )
            with (
                patch.object(
                    engine_pipeline,
                    "_source_snapshot",
                    return_value={"width": 3840, "height": 2160},
                ),
                patch.object(engine_pipeline, "is_hdr_file", return_value=False),
                patch.object(tools_ffprobe, "probe_duration_seconds", return_value=600.0),
                patch.object(engine_pipeline, "_probe_movie") as fake_probe,
            ):
                encoded = engine_pipeline.encode_with_quality(
                    src, Path(tmp) / "out.mkv", min_source_height=4320, min_source_width=7680
                )
        self.assertEqual(planned.skipped_reason, "resolution too low")
        self.assertEqual(encoded.skipped_reason, "resolution too low")
        self.assertIn("4320", encoded.error or "")
        fake_extract.assert_not_called()
        fake_probe.assert_not_called()

    def test_resolution_gate_unknown_dimensions_are_not_skipped(self):
        """An unreadable resolution must not silently exclude a file.

        A gate that treats "unknown" as "too low" would drop titles the
        operator expected to be considered; the honest answer is to probe.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "movie.mkv"
            src.write_bytes(b"x" * 8)
            with (
                patch.object(engine_plan, "probe_duration_seconds", return_value=600.0),
                patch.object(
                    engine_plan, "_source_dimensions", return_value={"width": None, "height": None}
                ),
                patch.object(engine_plan, "find_handbrake", return_value="hb"),
                patch.object(engine_plan, "extract_samples", return_value=[(Path("s0.mkv"), 10.0)]),
                patch.object(engine_plan, "estimate_size_factor", return_value=0.5),
                patch.object(engine_search, "estimate_size_factor", return_value=0.5),
                patch.object(
                    engine_search, "probe_crf", return_value=engine_search.DEFAULT_TARGET_VMAF
                ),
            ):
                result = engine_plan.plan_quality(src)
        self.assertTrue(result.success)
        self.assertIsNone(result.skipped_reason)

    def test_extract_samples_stream_copies_the_source(self):
        """Samples are stream-copied, never re-encoded.

        The samples are the VMAF reference and the probe encode's input, so a
        lossy extraction biases every measurement: the probe source becomes
        easier than the real one and the chosen CRF lands too high. A copy also
        keeps the source's bit depth and HDR side data, which an x264 pass drops.
        """
        commands: list[list[str]] = []

        def fake_run(command, cancel=None):
            commands.append(command)
            Path(command[-1]).write_bytes(b"x" * 8)
            return 0, ""

        with (
            patch(
                "shrinkray.engine.samples.subprocess.run",
                return_value=type(
                    "P", (), {"returncode": 0, "stdout": b'{"format": {"duration": 100.0}}'}
                )(),
            ),
            patch("shrinkray.engine.samples._run", side_effect=fake_run),
            tempfile.TemporaryDirectory() as tmp,
        ):
            engine_samples.extract_samples(Path("movie.mkv"), Path(tmp), 2, 10.0)
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertIn("-c:v", command)
            self.assertEqual(command[command.index("-c:v") + 1], "copy")
            for lossy in ("libx264", "libx265", "-crf", "-preset"):
                self.assertNotIn(lossy, command)

    def test_extract_samples_falls_back_when_copy_fails(self):
        """A container that cannot hold the source codec re-extracts losslessly.

        The fallback must stay pristine (ffv1), never a lossy re-encode, and
        must say so in the log because it costs a decode and encode.
        """
        commands: list[list[str]] = []

        def fake_run(command, cancel=None):
            commands.append(command)
            if command[command.index("-c:v") + 1] == "copy":
                return 1, "codec not supported in container"
            Path(command[-1]).write_bytes(b"x" * 8)
            return 0, ""

        with (
            patch(
                "shrinkray.engine.samples.subprocess.run",
                return_value=type(
                    "P", (), {"returncode": 0, "stdout": b'{"format": {"duration": 100.0}}'}
                )(),
            ),
            patch("shrinkray.engine.samples._run", side_effect=fake_run),
            patch("shrinkray.engine.samples.log_warning") as warned,
            tempfile.TemporaryDirectory() as tmp,
        ):
            samples = engine_samples.extract_samples(Path("movie.mkv"), Path(tmp), 1, 10.0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(commands[1][commands[1].index("-c:v") + 1], "ffv1")
        self.assertTrue(warned.called)

    def test_extract_samples_timeout_raises_documented_error(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "movie.mkv"
            movie.write_bytes(b"x")
            with (
                patch("shrinkray.engine.pipeline.probe_duration_seconds", return_value=600.0),
                patch(
                    "shrinkray.engine.samples.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
                ),
                self.assertRaises(RuntimeError) as ctx,
            ):
                engine_samples.extract_samples(movie, Path(tmp) / "work", count=3, seconds=10.0)
        self.assertIn("timed out after 60s", str(ctx.exception))
        self.assertIn("movie.mkv", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
