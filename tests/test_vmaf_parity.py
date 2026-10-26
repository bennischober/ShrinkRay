"""Opt-in smoke test: ShrinkRay's VMAF measurement vs ab-av1's (issue #306).

The yardstick for the "VMAF measurement matches ab-av1" milestone: for the same
(reference, distorted) pair, measure_vmaf(distorted, reference) versus
`ab-av1 vmaf`. The comparison is the MEASUREMENT, not the search - ab-av1
encodes with ffmpeg's libsvtav1, ShrinkRay with HandBrakeCLI, so chosen CRFs
would differ for reasons unrelated to VMAF.

Tolerance (the issue's proposal): |ShrinkRay - ab-av1| <= 0.1 VMAF per pair.
The HDR pair is REPORTED but not held to the tolerance: ShrinkRay tone-maps
both inputs to SDR before scoring (tools/vmaf.py, documented deviation #310),
ab-av1 scores the PQ signal directly.

Skipped by default everywhere: SHRINKRAY_SMOKE=1 AND ffmpeg (libvmaf, zscale)
AND ab-av1 on PATH. ab-av1 is a development/test tool only - nothing under
src/ invokes it, and it is not a dependency.

The ab-av1 version used for the baseline is pinned in the issue: v0.11.7
(recipe read at main ed5f5461f0aa). Its fps override is DISABLED here
(--vmaf-fps 0) so both sides score the source's own timestamps; since #308
ShrinkRay rebases both inputs to PTS 0 too, so the two agree either way.

Its scaling is left at ab-av1's default auto, and since #309 ShrinkRay applies
the same auto rule (the 4K model above 2560x1440, the 1080p model otherwise,
each with ab-av1's upscale thresholds). For the 1080p and 2160p pairs neither
side scales, and the model choice is what the 2160p pair now proves.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from shrinkray.tools.vmaf import measure_vmaf

AB_AV1 = shutil.which("ab-av1")
FFMPEG_OK = shutil.which("ffmpeg") is not None
SMOKE_ENV = os.environ.get("SHRINKRAY_SMOKE")
TOLERANCE = 0.1  # |ShrinkRay - ab-av1| per pair (issue #306's proposal)


class _AbAv1Mixin(unittest.TestCase):
    """The shared ab-av1 invocation (both parity classes need it)."""

    def _ab_av1_vmaf(self, reference: Path, distorted: Path, vfilter: str | None = None) -> float:
        cmd = [
            AB_AV1 or "ab-av1",
            "vmaf",
            "--reference",
            str(reference),
            "--distorted",
            str(distorted),
            # ab-av1's DEFAULT_VMAF_FPS is 25: without this the two inputs are re-timed
            # to 25 fps and the SDR deltas stop being 0.0000 for a reason unrelated
            # to the measurement. 0 disables the override.
            "--vmaf-fps",
            "0",  # score the source's own timestamps (ShrinkRay does)
        ]
        if vfilter:
            cmd += ["--reference-vfilter", vfilter]
        print("ABAV1 CMD: " + " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            self.fail(f"ab-av1 vmaf failed: {proc.stderr[-400:]}")
        return float(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(SMOKE_ENV, "smoke: opt-in (SHRINKRAY_SMOKE=1)")
@unittest.skipUnless(FFMPEG_OK and AB_AV1, "real ffmpeg + ab-av1 required on PATH")
class TestVmafParityAgainstAbAv1(_AbAv1Mixin):
    """The measurement yardstick: measure_vmaf vs ab-av1 vmaf, same pairs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workdir = self.root / "vmafwork"
        self.workdir.mkdir()
        self.report: list[tuple[str, float, float]] = []

    def tearDown(self) -> None:
        if self.report:
            print("\n\nVMAF parity report (ShrinkRay vs ab-av1 v0.11.7):")
            for name, ours, theirs in self.report:
                print(f"  {name}: {ours:.4f} vs {theirs:.4f} (delta {abs(ours - theirs):.4f})")
            ff = shutil.which("ffmpeg")
            out = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=30)
            print(f"  ffmpeg: {out.stdout.splitlines()[0]}")
        self.tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _encode(self, source: Path, dest: Path, crf: str, pix_fmt: str = "yuv420p") -> None:
        """Re-encodes a source with libx264 at the given crf (the distorter)."""
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                crf,
                "-pix_fmt",
                pix_fmt,
                "-c:a",
                "copy",
                str(dest),
            ],
            check=True,
            timeout=600,
        )

    def _clip(self, name: str, size: str = "1920x1080", extra_v: list[str] | None = None) -> Path:
        """A synthetic clip: testsrc2 + sine, h264, 20 s (no committed media)."""
        dest = self.root / name
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=25:duration=20",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=20",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            *(extra_v or []),
            "-c:a",
            "aac",
            str(dest),
        ]
        subprocess.run(cmd, check=True, timeout=600)
        return dest

    def _check(
        self,
        name: str,
        reference: Path,
        distorted: Path,
        crop: tuple | None = None,
        hdr: bool = False,
        hold: bool = True,
        why: str = "",
    ) -> None:
        """Runs both measurements, records the report line, holds the tolerance.

        hold=False reports the pair without asserting (a documented deviation
        or a known bug with its own issue, named in `why`).
        """
        ours = measure_vmaf(distorted, reference, self.workdir, crop=crop, hdr=hdr)
        theirs = self._ab_av1_vmaf(
            reference,
            distorted,
            vfilter=(
                # x = left, y = top (the crop tuple is top,bottom,left,right)
                f"crop=iw-{crop[2] + crop[3]}:ih-{crop[0] + crop[1]}:{crop[2]}:{crop[0]}"
                if crop
                else None
            ),
        )
        self.report.append((name + (f" [{why}]" if why else ""), ours, theirs))
        if hold:
            self.assertLessEqual(
                abs(ours - theirs),
                TOLERANCE,
                f"{name}: ShrinkRay {ours:.4f} vs ab-av1 {theirs:.4f} "
                f"(delta {abs(ours - theirs):.4f} > {TOLERANCE})",
            )

    # -- the pairs (issue #306) ---------------------------------------------

    def test_sdr_8bit_1080p_no_bars(self) -> None:
        ref = self._clip("ref8.mkv")
        dis = self.root / "dis8.mkv"
        self._encode(ref, dis, crf="28")
        self._check("SDR 8-bit 1080p", ref, dis)

    def test_sdr_with_black_bars_crop(self) -> None:
        # the reference carries baked bars (1920x816 letterboxed into
        # 1920x1080); the distorted is the CROPPED content re-encoded - the
        # geometry ShrinkRay's encoder produces. measure_vmaf crops the
        # REFERENCE by (132,132,0,0) to match; ab-av1 gets the same crop as
        # its --reference-vfilter.
        ref = self._clip("refbars.mkv", extra_v=["-vf", "pad=1920:1080:0:132:black"])
        cropped = self.root / "cropped.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(ref),
                "-vf",
                "crop=1920:816:0:132",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(cropped),
            ],
            check=True,
            timeout=600,
        )
        dis = self.root / "disbars.mkv"
        self._encode(cropped, dis, crf="28")
        self._check("SDR bars (crop 132/132)", ref, dis, crop=(132, 132, 0, 0))

    def test_sdr_10bit(self) -> None:
        ref = self._clip("ref10.mkv", extra_v=["-pix_fmt", "yuv420p10le"])
        dis = self.root / "dis10.mkv"
        self._encode(ref, dis, crf="28", pix_fmt="yuv420p10le")
        self._check("SDR 10-bit", ref, dis)

    def test_2160p(self) -> None:
        # ab-av1 switches to the 4k model (vmaf_4k_v0.6.1) above 2560x1440;
        # since #309 ShrinkRay picks the same model from the cropped
        # dimensions, so this pair is HELD. At 3840x2160 neither side's
        # auto-scale scales anything (both are >= their 4K threshold).
        ref = self._clip("ref4k.mkv", size="3840x2160")
        dis = self.root / "dis4k.mkv"
        self._encode(ref, dis, crf="28")
        self._check("2160p (ab-av1's 4k model)", ref, dis)

    def test_hdr10_reported_not_held(self) -> None:
        # a PQ/bt2020 clip; ShrinkRay tone-maps both inputs, ab-av1 scores PQ
        # a PQ/bt2020 clip (libx265: libx264 rejects the bt2020 colorprim
        # values); ShrinkRay tone-maps both inputs, ab-av1 scores PQ
        ref = self.root / "refhdr.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1920x1080:rate=25:duration=20",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=20",
                "-c:v",
                "libx265",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p10le",
                "-x265-params",
                "hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                "-c:a",
                "aac",
                str(ref),
            ],
            check=True,
            timeout=600,
        )
        dis = self.root / "dishdr.mkv"
        self._encode(ref, dis, crf="28", pix_fmt="yuv420p10le")
        # the tone-map deviation is #310's documented behaviour, REPORTED
        self._check(
            "HDR10 tone-mapped (reported, not held)",
            ref,
            dis,
            hdr=True,
            hold=False,
            why="tone-map deviation, #310",
        )
        # Issue #312's HDR criterion: with tone-mapping OFF both sides score the
        # PQ signal directly, which is exactly ab-av1's comparison. HELD - it
        # proves the rest of the pipeline (model, scale, pixel format, sync)
        # matches on an HDR pair, so the tone-map is the only remaining gap.
        self._check("HDR10 PQ-direct (tone-map off)", ref, dis, hdr=False)


@unittest.skipUnless(SMOKE_ENV, "smoke: opt-in (SHRINKRAY_SMOKE=1)")
@unittest.skipUnless(
    FFMPEG_OK and AB_AV1 and shutil.which("HandBrakeCLI"),
    "real ffmpeg + ab-av1 + HandBrakeCLI required on PATH",
)
class TestVmafParityProbeSample(_AbAv1Mixin):
    """The open-GOP probe-sample pair (needs HandBrakeCLI for the encode).

    The sample is exactly what engine/samples.extract_samples produces (a
    stream-copied window), encoded by HandBrake at the probe preset the way
    probe_crf does - the one pair that exercises the real probe pipeline.

    Baseline (2026-10-25, HandBrake 1.11.2 in the repo's smoke image):
    probe-sample parity 56.5253 vs 56.5253 (delta 0.0000), 1 passed in 33.4s -
    the pair held on its first real run, so this class is a guard now, not a
    placeholder.
    """

    def test_probe_sample_parity(self) -> None:
        import tempfile

        from shrinkray.engine.samples import extract_samples

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name).resolve()
        workdir = root / "vmafwork"
        workdir.mkdir()
        try:
            # a 60 s 1080p source with a fixed keyframe cadence
            source = root / "src.mkv"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=1920x1080:rate=25:duration=60",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=60",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "18",
                    "-g",
                    "25",
                    "-keyint_min",
                    "25",
                    "-sc_threshold",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(source),
                ],
                check=True,
                timeout=600,
            )
            windows = extract_samples(source, root, count=2, seconds=10.0)
            self.assertEqual(len(windows), 2)
            sample, _offset = windows[0]
            # HandBrake encodes the sample at the probe preset (probe_crf's shape)
            encoded = root / "sample_enc.mkv"
            subprocess.run(
                [
                    "HandBrakeCLI",
                    "-i",
                    str(sample),
                    "-o",
                    str(encoded),
                    "-e",
                    "svt_av1",
                    "--encoder-preset",
                    "5",
                    "--quality",
                    "28",
                    "-f",
                    "av_mkv",
                    "--crop",
                    "0:0:0:0",
                    "--all-audio",
                    "--aencoder",
                    "copy",
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            ours = measure_vmaf(encoded, sample, workdir)
            theirs = self._ab_av1_vmaf(sample, encoded)
            print(
                f"\nprobe-sample parity: {ours:.4f} vs {theirs:.4f} "
                f"(delta {abs(ours - theirs):.4f})"
            )
            self.assertLessEqual(abs(ours - theirs), TOLERANCE)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
