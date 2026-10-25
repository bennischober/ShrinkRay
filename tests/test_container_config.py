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


if __name__ == "__main__":
    unittest.main()
