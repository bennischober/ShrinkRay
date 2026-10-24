"""Reproduces the logs/ telemetry leak: encode_with_quality(verify=True)
with no db_file hits the real measure_vmaf failure path, whose save_failure
fallback writes into the repo root (db_file or Path("metadata.db")).

Run with: uv run pytest tests/test_telemetry_leak.py -q --no-cov
The fixed source makes this test pass WITHOUT any logs/ appearing in the
checkout (the fallback never runs because the failure is not reached, or
the caller always passes a db_file).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shrinkray.engine.pipeline import encode_with_quality


class TestVerifyTelemetryLeak(unittest.TestCase):
    def test_verify_with_no_db_file_never_writes_logs_into_the_repo(self) -> None:
        """A verify run without a db_file that fails its measurement must not
        create a logs/ bundle next to the (nonexistent) metadata.db — i.e.
        in the caller's working directory."""
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src = root / "in.mkv"
            out_dir = root / "enc"
            out_dir.mkdir()
            out = out_dir / "in.mkv"
            src.write_bytes(b"x" * 8)
            stub = root / "hb_stub.cmd" if os.name == "nt" else root / "hb_stub.sh"
            if os.name == "nt":
                stub.write_bytes(b"@echo off\r\necho x > %~4\r\nexit 0\r\n")
            else:
                stub.write_bytes(b'#!/bin/sh\necho x > "$4"\nexit 0\n')
                stub.chmod(0o755)
            before = set(Path.cwd().glob("logs/*"))
            with (
                mock.patch(
                    "shrinkray.engine.pipeline._probe_movie", return_value=(20, 95.2, [], None)
                ),
                mock.patch("shrinkray.engine.pipeline.find_handbrake", return_value=str(stub)),
                mock.patch("shrinkray.engine.pipeline.durations_match", return_value=True),
                mock.patch(
                    "shrinkray.engine.pipeline.measure_vmaf",
                    side_effect=RuntimeError("ffmpeg exploded"),
                ),
                mock.patch("shrinkray.engine.pipeline.disk_free_bytes", return_value=None),
            ):
                result = encode_with_quality(src, out, progress=None, verify=True)
            self.assertTrue(result.success)
            self.assertIsNone(result.verified_vmaf)
            after = set(Path.cwd().glob("logs/*"))
            self.assertEqual(
                after - before,
                set(),
                "a verify run without db_file created a telemetry bundle in the"
                " working directory — save_failure's fallback must never point"
                " at the repo root",
            )


if __name__ == "__main__":
    unittest.main()
