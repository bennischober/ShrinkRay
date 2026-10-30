"""The _run call-site guards (issue #458): on a host where a binary
disappears after startup (or a spawn fails with EMFILE/ENOMEM), the engine's
`_run` callers outside `engine/crop.py` raised the raw `OSError` as a
traceback. Each site now raises its step's documented failure shape
(`RuntimeError` naming the binary and the cause), so the entry point reports
it per file and leaves the file alone. The runner itself stays unguarded BY
DECISION (#401's record): a runner-level sentinel would collide with the
`-1`/`-2` signal-death contract and let the CRF search read "binary missing"
as a failed probe encode - a wrong decision.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from shrinkray.engine.samples import extract_samples
from shrinkray.tools import vmaf


class _FakeFFProbe:
    """The duration ffprobe answer extract_samples needs before its _run calls."""

    @staticmethod
    def run(*args, **kwargs):
        proc = type("Proc", (), {})()
        proc.returncode = 0
        proc.stdout = json.dumps({"format": {"duration": "120.0"}}).encode()
        return proc


class TestSampleExtractionRunGuards(unittest.TestCase):
    """engine/samples.py `extract_samples`: both `_run` sites (stream-copy and
    the lossless re-extract) raise the documented RuntimeError, not OSError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.movie = Path(self.tmp.name) / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stream_copy_site_raises_runtime_error_not_oserror(self) -> None:
        with (
            patch("shrinkray.engine.samples.subprocess.run", side_effect=_FakeFFProbe.run),
            patch(
                "shrinkray.engine.samples._run",
                side_effect=FileNotFoundError(2, "No such file or directory: 'ffmpeg'"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            extract_samples(
                self.movie,
                Path(self.tmp.name),
                count=1,
                seconds=1.0,
                cancel=threading.Event(),
            )
        message = str(ctx.exception)
        self.assertIn("sample extraction could not start", message)
        self.assertIn("No such file or directory", message)

    def test_lossless_reextract_site_raises_runtime_error_not_oserror(self) -> None:
        # The fallback site fires only when the first site "succeeds" with a
        # failed encode: _run returns (1, "") and the sample is missing.
        def run_side_effect(command, **kwargs):
            if "-c:v" in command and "copy" in command:
                return 1, ""
            raise FileNotFoundError(2, "No such file or directory: 'ffmpeg'")

        with (
            patch("shrinkray.engine.samples.subprocess.run", side_effect=_FakeFFProbe.run),
            patch(
                "shrinkray.engine.samples._run",
                side_effect=run_side_effect,
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            extract_samples(
                self.movie,
                Path(self.tmp.name),
                count=1,
                seconds=1.0,
                cancel=threading.Event(),
            )
        message = str(ctx.exception)
        self.assertIn("sample extraction could not start", message)
        self.assertIn("No such file or directory", message)


class TestProbeEncodeRunGuards(unittest.TestCase):
    """engine/search.py: the HandBrake probe encodes raise the documented
    RuntimeError, not OSError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sample = Path(self.tmp.name) / "sample_0.mkv"
        self.sample.write_bytes(b"x" * 8)
        self.movie = Path(self.tmp.name) / "one.mkv"
        self.movie.write_bytes(b"x" * 8)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_size_probe_site_raises_runtime_error_not_oserror(self) -> None:
        with (
            patch(
                "shrinkray.engine.search._run",
                side_effect=FileNotFoundError(2, "No such file or directory: 'HandBrakeCLI'"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            from shrinkray.engine.search import estimate_size_factor

            estimate_size_factor(
                [(self.sample, 0.0)],
                Path(self.tmp.name),
                "/usr/bin/HandBrakeCLI",
                30,
                self.movie,
                1.0,
                "svt_av1",
                8,
                cancel=threading.Event(),
                db_file=None,
                use_probe_cache=False,
            )
        message = str(ctx.exception)
        self.assertIn("size probe encode could not start", message)
        self.assertIn("No such file or directory", message)

    def test_probe_encode_site_raises_runtime_error_not_oserror(self) -> None:
        from shrinkray.engine.search import probe_crf

        with (
            patch(
                "shrinkray.engine.search._run",
                side_effect=FileNotFoundError(2, "No such file or directory: 'HandBrakeCLI'"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            probe_crf(
                [(self.sample, 0.0)],
                30,
                Path(self.tmp.name),
                "/usr/bin/HandBrakeCLI",
                self.movie,
                1.0,
                "svt_av1",
                8,
                db_file=None,
                use_probe_cache=False,
            )
        message = str(ctx.exception)
        self.assertIn("probe encode could not start", message)
        self.assertIn("No such file or directory", message)


class TestVmafMeasureRunGuard(unittest.TestCase):
    """tools/vmaf.py measure_vmaf: the measurement raises the documented
    RuntimeError, not OSError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_measure_vmaf_site_raises_runtime_error_not_oserror(self) -> None:
        from shrinkray.tools.vmaf import VmafRecipe, measure_vmaf

        workdir = Path(self.tmp.name)
        distorted = workdir / "probe.mkv"
        distorted.write_bytes(b"x" * 8)
        reference = workdir / "sample_0.mkv"
        reference.write_bytes(b"x" * 8)
        with (
            patch("shrinkray.tools.vmaf.detect_vmaf_style", return_value=vmaf.VMAF_STYLE_MODERN),
            patch(
                "shrinkray.tools.vmaf._run",
                side_effect=FileNotFoundError(2, "No such file or directory: 'ffmpeg'"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            measure_vmaf(
                distorted,
                reference,
                workdir,
                crop=None,
                hdr=False,
                subsample=1,
                recipe=VmafRecipe(
                    model="vmaf_v0.6.1.json", scale=None, pix_fmt="yuv420p", hdr=False
                ),
                db_file=None,
                cancel=threading.Event(),
            )
        message = str(ctx.exception)
        self.assertIn("vmaf measurement could not start", message)
        self.assertIn("No such file or directory", message)


if __name__ == "__main__":
    unittest.main()
