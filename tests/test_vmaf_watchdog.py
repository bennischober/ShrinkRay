"""The stall watchdog versus the measurement children (issue #431).

libvmaf is silent by design until the whole file is scored: a full-precision
measurement of a large movie produces no output for the entire run, so the
stall watchdog (DEFAULT_STALL_MINUTES = 10.0, "a healthy child reports
progress at least once per N minutes") kills a healthy measurement as hung.
The shipped encode path is partially exposed: the verify pass sub-samples
1/10 above 2 GiB (large outputs measure in minutes), but any output at or
below 2 GiB is measured at full precision - for a long movie exactly the
silent-past-10-minutes pass the issue reproduces. Other exposed callers
are full-precision by design: scripts/vmaf_parity.py,
scripts/verify_fullmovie.py, any operator script on measure_vmaf.

The fix (issue #431): the vmaf measurement is a silent child BY DESIGN, so
the measurement path opts out of the stall watchdog (watchdog=False on the
runner); the cancel event remains the kill path, and the HandBrake watchdog
protection is unchanged (its progress lines still feed the watchdog).
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


@unittest.skipIf(os.name == "nt", "POSIX shell stubs")
class TestVmafMeasurementOutlivesTheWatchdog(unittest.TestCase):
    """A silent-but-finishing libvmaf child must not be killed as hung."""

    def _fake_ffmpeg(self, tmp: Path, silent_seconds: float) -> Path:
        """A stub ffmpeg: silent for silent_seconds, then writes a valid
        vmaf.json (the pooled-metrics shape parse_vmaf_log reads) and exits 0.
        It accepts any arguments (measure_vmaf builds the real command)."""
        stub = tmp / "ffmpeg_stub.sh"
        stub.write_text(
            "#!/bin/sh\n"
            f"sleep {silent_seconds}\n"
            'printf \'%s\' \'{"pooled_metrics": {"vmaf": {"mean": 93.5}}}\' > vmaf.json\n'
            "exit 0\n"
        )
        stub.chmod(0o755)
        return stub

    def _measure_with_stub(self, tmp: Path, stub: Path, watchdog_seconds: float):
        """Runs measure_vmaf against the stub with the watchdog at
        watchdog_seconds; returns (score, error)."""
        from shrinkray.tools import process as process_mod
        from shrinkray.tools import vmaf as vmaf_mod

        old_stall = process_mod._stall_minutes
        try:
            process_mod.configure_stall(watchdog_seconds / 60.0)
            with (
                mock.patch.object(vmaf_mod, "FFMPEG_BINARY", str(stub)),
                mock.patch.object(
                    vmaf_mod, "detect_vmaf_style", return_value=vmaf_mod.VMAF_STYLE_MODERN
                ),
            ):
                workdir = tmp / "work"
                workdir.mkdir()
                try:
                    score = vmaf_mod.measure_vmaf(
                        tmp / "d.mkv",
                        tmp / "r.mkv",
                        workdir,
                    )
                    return score, None
                except RuntimeError as e:
                    return None, str(e)
        finally:
            process_mod._stall_minutes = old_stall

    def test_an_unspawnable_binary_raises_the_measure_shape(self):
        """Issue #458: a spawn failure at the VMAF measurement's seam raises
        the step's documented RuntimeError (the binary named), not a raw
        OSError traceback."""

        from shrinkray.tools import process as process_mod
        from shrinkray.tools import vmaf as vmaf_mod

        old_stall = process_mod._stall_minutes
        try:
            process_mod.configure_stall(5.0 / 60.0)
            with (
                mock.patch.object(
                    vmaf_mod,
                    "_run",
                    side_effect=FileNotFoundError(2, "No such file or directory", "ffmpeg"),
                ),
                mock.patch.object(
                    vmaf_mod, "detect_vmaf_style", return_value=vmaf_mod.VMAF_STYLE_MODERN
                ),
                # the recipe's probe is NOT this test's subject (it would run
                # a real ffprobe on a nonexistent path): stub it with a real
                # model name so the model copy works and the _run stub is what
                # raises
                mock.patch.object(
                    vmaf_mod,
                    "_recipe_for_file",
                    return_value=vmaf_mod.vmaf_recipe(crop=None, hdr=False),
                ),
                tempfile.TemporaryDirectory() as t,
            ):
                tmp = Path(t)
                workdir = tmp / "work"
                workdir.mkdir()
                with self.assertRaises(RuntimeError) as ctx:
                    vmaf_mod.measure_vmaf(tmp / "d.mkv", tmp / "r.mkv", workdir)
        finally:
            process_mod._stall_minutes = old_stall
        self.assertIn("VMAF measurement cannot run 'ffmpeg'", str(ctx.exception))

    def test_silent_measurement_completes_under_a_short_watchdog(self):
        """The issue's acceptance criterion: a full-precision measure_vmaf on a
        >watchdog-window-silent file completes instead of being killed."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "d.mkv").write_bytes(b"d")
            (tmp / "r.mkv").write_bytes(b"r")
            stub = self._fake_ffmpeg(tmp, silent_seconds=3.0)
            score, err = self._measure_with_stub(tmp, stub, watchdog_seconds=1.0)
            self.assertIsNone(err, f"measurement was killed: {err}")
            self.assertAlmostEqual(score, 93.5)

    def test_cancel_still_terminates_a_silent_measurement(self):
        """No protection regression: the cancel event still kills the vmaf
        child (the watchdog's removal must not remove the kill path)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "d.mkv").write_bytes(b"d")
            (tmp / "r.mkv").write_bytes(b"r")
            stub = tmp / "ffmpeg_stub.sh"
            # silent for 60s: only the cancel event can end this
            stub.write_text("#!/bin/sh\nsleep 60\nexit 0\n")
            stub.chmod(0o755)
            from shrinkray.tools import process as process_mod
            from shrinkray.tools import vmaf as vmaf_mod

            cancel = threading.Event()
            result_holder: list = []

            old_stall = process_mod._stall_minutes

            def runner():
                try:
                    process_mod.configure_stall(1.0 / 60.0)
                    with (
                        mock.patch.object(vmaf_mod, "FFMPEG_BINARY", str(stub)),
                        mock.patch.object(
                            vmaf_mod, "detect_vmaf_style", return_value=vmaf_mod.VMAF_STYLE_MODERN
                        ),
                    ):
                        workdir = tmp / "work"
                        workdir.mkdir()
                        vmaf_mod.measure_vmaf(
                            tmp / "d.mkv",
                            tmp / "r.mkv",
                            workdir,
                            cancel=cancel,
                        )
                    result_holder.append("completed")
                except process_mod.Cancelled as e:
                    result_holder.append(e)
                except Exception as e:
                    result_holder.append(("WRONG-TYPE", e))
                finally:
                    process_mod._stall_minutes = old_stall

            thread = threading.Thread(target=runner)
            thread.start()
            time.sleep(2.0)  # the watchdog window passes: the child must survive it
            self.assertEqual(result_holder, [], "measurement ended before cancel")
            cancel.set()
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive(), "cancel did not terminate the child")
            self.assertIsInstance(
                result_holder[0], process_mod.Cancelled, "cancel must raise Cancelled"
            )


if __name__ == "__main__":
    unittest.main()
