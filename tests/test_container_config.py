"""The container's shipped config parses and covers the common containers
(issue #204, hygiene rework 2026-10-25): the image's config.json must let a
`.mp4` library scan out of the box, not just `.mkv`. The config is a tracked
file (container-config.json) COPYed into the image, and the Dockerfile must
actually COPY it - a file that exists but never ships fails the test too.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "container-config.json"
DOCKERFILE = ROOT / "Dockerfile"


class TestContainerConfig(unittest.TestCase):
    def test_the_shipped_config_sets_the_common_extensions(self) -> None:
        """The tracked config.json parses, enables auto_backlog, and its
        file.extensions include .mkv AND .mp4 (the issue's report: a
        container that only saw .mkv)."""
        self.assertTrue(CONFIG_FILE.is_file(), "container-config.json is missing")
        baked = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(baked["app"]["auto_backlog"])
        extensions = baked["file"]["extensions"]
        self.assertIn(".mkv", extensions)
        self.assertIn(".mp4", extensions)
        self.assertIn(".avi", extensions)
        self.assertIn(".mov", extensions)

    def test_the_dockerfile_copies_the_tracked_config(self) -> None:
        """The image must actually ship the file: a COPY line for it must
        exist (the old printf baked the JSON into a RUN layer)."""
        content = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("COPY container-config.json /app/config.json", content)
        self.assertNotIn("RUN printf", content, "a printf-baked config survived in the Dockerfile")

    def test_the_entrypoint_passes_no_config_flag(self) -> None:
        """Issue #236 part 3: the entrypoint's explicit --config overrode the
        operator's SHRINKRAY_CONFIG mount (an explicit file wins the
        resolution order), so the mounted config was silently dead. The
        entrypoint passes none; the env var decides."""
        content = DOCKERFILE.read_text(encoding="utf-8")
        entrypoint = next(line for line in content.splitlines() if line.startswith("ENTRYPOINT"))
        self.assertNotIn("--config", entrypoint, "the entrypoint pins a config path again")

    def test_the_two_argument_sets_resolve_differently_and_correctly(self) -> None:
        """The issue's required test: resolve_config_path under the compose
        argument set (no --config, SHRINKRAY_CONFIG set) resolves the MOUNT;
        under the bare docker-run set (no env) it resolves the BAKED file."""
        import os
        from unittest.mock import patch

        from shrinkray.config import CONFIG_ENV_VAR, resolve_config_path

        with patch.dict(os.environ, {CONFIG_ENV_VAR: "/data/config.json"}):
            self.assertEqual(resolve_config_path(config_file=None), Path("/data/config.json"))
        with patch.dict(os.environ, {}, clear=True):
            # the bare run: no env, no db_dir -> ./config.json, and the
            # container's WORKDIR is /app (the baked file's home)
            self.assertEqual(resolve_config_path(config_file=None), Path("config.json"))


if __name__ == "__main__":
    unittest.main()
