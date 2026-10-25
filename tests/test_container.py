"""Tests for the container image's constraints (issue #34).

The image is a claim only if the things it must never bake in are excluded and
the CPU share it is given is the one it plans with. Both are checkable here
without building the image, which is what keeps them from rotting.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shrinkray.tools import process as resources

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import healthcheck  # noqa: E402 - tools/ is not a package; the path is set above


class TestCpuCount(unittest.TestCase):
    """os.cpu_count() ignores a container's quota; the helper must not."""

    def test_a_container_quota_caps_the_worker_plan(self) -> None:
        """--cpus=2 on a 24-core host must plan 2 workers, not 24.

        Measured, not assumed: os.cpu_count() AND os.process_cpu_count() both
        report the host's 24 inside a 2-CPU container, so the quota has to come
        from the cgroup files.
        """
        with (
            mock.patch.object(
                resources,
                "_cgroup_cpu_quota",
                return_value=2,
            ),
        ):
            self.assertEqual(resources.cpu_count(), 2)

    def test_the_quota_is_read_from_cpu_max(self) -> None:
        """cgroup v2 spells it '<quota> <period>'; 200000/100000 is 2 CPUs."""
        with mock.patch.object(Path, "read_text", return_value="200000 100000"):
            self.assertEqual(resources._cgroup_cpu_quota(), 2)

    def test_an_unlimited_quota_is_none(self) -> None:
        with mock.patch.object(Path, "read_text", return_value="max 100000"):
            self.assertEqual(resources._cgroup_cpu_quota(), None)

    def test_a_sub_cpu_quota_never_yields_zero(self) -> None:
        """--cpus=0.5 permits no whole CPU, and 0 workers would be worse."""
        with mock.patch.object(resources, "_cgroup_cpu_quota", return_value=0):
            self.assertEqual(resources.cpu_count(), 1)

    def test_an_unreadable_quota_falls_back_to_the_machine(self) -> None:
        with (
            mock.patch.object(resources, "_cgroup_cpu_quota", return_value=None),
            mock.patch.object(os, "cpu_count", return_value=16),
        ):
            self.assertEqual(resources.cpu_count(), 16)

    def test_the_count_is_never_zero(self) -> None:
        """A zero would mean zero workers: a division by it or an empty pool."""
        with (
            mock.patch.object(resources, "_cgroup_cpu_quota", return_value=None),
            mock.patch.object(os, "cpu_count", return_value=0),
        ):
            self.assertEqual(resources.cpu_count(), 1)

    def test_the_engine_uses_the_helper_not_os_cpu_count(self) -> None:
        """Every planner must ask the helper, or a quota is silently ignored."""
        for name in ("engine/pipeline.py", "engine/watch.py", "engine/plan.py", "engine/scan.py"):
            source = (REPO / "src" / "shrinkray" / name).read_text()
            self.assertNotIn("os.cpu_count()", source, f"{name} bypasses resources.cpu_count()")


class TestDockerignore(unittest.TestCase):
    """The build context must never carry a secret or user state."""

    def _patterns(self) -> list[str]:
        text = (REPO / ".dockerignore").read_text()
        return [line.strip() for line in text.splitlines() if line.strip() and line[0] != "#"]

    def test_the_context_excludes_credentials(self) -> None:
        patterns = self._patterns()
        self.assertIn(".opencode/", patterns)
        self.assertIn("*.key", patterns)
        self.assertIn(".env", patterns)

    def test_the_context_excludes_the_database(self) -> None:
        patterns = self._patterns()
        self.assertIn("*.db", patterns)

    def test_the_context_excludes_the_environment_and_media(self) -> None:
        patterns = self._patterns()
        for expected in (".venv/", "node_modules/", "test_media/"):
            self.assertIn(expected, patterns)

    def test_no_secret_is_reachable_in_the_context(self) -> None:
        """The key the repo documents lives under a directory that is excluded."""
        patterns = self._patterns()
        excluded = {p.rstrip("/") for p in patterns if p.endswith("/")}
        self.assertIn(".opencode", excluded)


class TestDockerfile(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (REPO / "Dockerfile").read_text()

    def test_both_targets_exist(self) -> None:
        self.assertIn("FROM runtime AS dev", self.text)
        self.assertIn("AS runtime", self.text)
        self.assertIn("AS toolchain", self.text)

    def test_the_toolchain_is_pinned_by_digest(self) -> None:
        """A distro package has no libvmaf; an unpinned build mixes scores."""
        self.assertIn("sha256sum -c -", self.text)
        self.assertIn("FFMPEG_SHA256", self.text)
        self.assertIn("HANDBRAKE_SHA256", self.text)

    def test_the_build_fails_when_libvmaf_is_missing(self) -> None:
        self.assertIn("filter=libvmaf", self.text)

    def test_the_image_runs_as_non_root(self) -> None:
        self.assertIn("USER shrinkray", self.text)

    def test_the_entrypoint_passes_a_root(self) -> None:
        """Without --root, every library path is a 403 on Linux."""
        self.assertIn("--root", self.text)
        self.assertIn("/media", self.text)

    def test_the_healthcheck_uses_the_status_route(self) -> None:
        self.assertIn("tools/healthcheck.py", self.text)
        self.assertIn("/api/status", (REPO / "tools" / "healthcheck.py").read_text())

    def test_the_cli_is_the_entrypoint_and_serve_the_default_command(self) -> None:
        """The image runs either mode (issue #242): with no arguments the
        resident server starts, any arguments are one CLI command. A `serve`
        baked into ENTRYPOINT appended `encode ...` to the server's argv."""
        runtime = self.text.split("FROM runtime AS dev")[0]
        entrypoints = [ln for ln in runtime.splitlines() if ln.startswith("ENTRYPOINT ")]
        commands = [ln for ln in runtime.splitlines() if ln.startswith("CMD ")]
        self.assertEqual(len(entrypoints), 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(json.loads(entrypoints[0].removeprefix("ENTRYPOINT ")), ["shrinkray"])
        command = json.loads(commands[0].removeprefix("CMD "))
        self.assertEqual(command[0], "serve")
        self.assertIn("--root", command)
        self.assertIn("/data/metadata.db", command)

    def test_the_scratch_dir_is_a_volume(self) -> None:
        """Probe scratch is sized to the film; the container layer is not."""
        self.assertIn("/tmp/shrinkray", self.text)  # noqa: S108 - asserting the Dockerfile text, not opening it


class TestHealthcheck(unittest.TestCase):
    """tools/healthcheck.py (issue #242): only the server has a status to
    check. A one-shot CLI container must not turn `unhealthy` for the hours an
    encode takes, and must never need a token it was not given."""

    def _cmdline(self, tmp: str, *argv: str) -> Path:
        path = Path(tmp) / "cmdline"
        path.write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        return path

    def test_the_server_is_recognised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for command in ("serve", "app"):
                with self.subTest(command=command):
                    cmdline = self._cmdline(
                        tmp, "/app/.venv/bin/python", "/app/.venv/bin/shrinkray", command, "--db"
                    )
                    self.assertTrue(healthcheck.runs_server(cmdline))

    def test_a_one_shot_command_is_healthy_without_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = self._cmdline(
                tmp, "/app/.venv/bin/python", "/app/.venv/bin/shrinkray", "encode", "/media/x.mkv"
            )
            with mock.patch.object(healthcheck.urllib.request, "urlopen") as urlopen:
                self.assertEqual(healthcheck.main(cmdline), 0)
            urlopen.assert_not_called()

    def test_a_path_segment_named_app_is_not_the_server(self) -> None:
        """`/app/...` paths are whole argv elements, never the `app` alias."""
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = self._cmdline(tmp, "/app/.venv/bin/shrinkray", "plan", "/media/app")
            self.assertFalse(healthcheck.runs_server(cmdline))


if __name__ == "__main__":
    unittest.main()
