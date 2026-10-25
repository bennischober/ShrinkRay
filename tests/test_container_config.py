"""The container's baked-in config parses and covers the common containers
(issue #204): the image's config.json must let a `.mp4` library scan out of
the box, not just `.mkv`.
"""

import json
import re
import unittest
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


class TestContainerConfig(unittest.TestCase):
    def test_the_baked_config_sets_the_common_extensions(self) -> None:
        """The image's `printf`ed config.json parses, enables auto_backlog,
        and its file.extensions include .mkv AND .mp4 (the issue's report:
        a container that only saw .mkv)."""
        content = DOCKERFILE.read_text(encoding="utf-8")
        match = re.search(r"RUN printf '(.*)' > /app/config\.json", content)
        self.assertIsNotNone(match, "no config.json printf in the Dockerfile")
        baked = json.loads(match.group(1).replace("\\n", "\n").replace('\\"', '"'))
        self.assertTrue(baked["app"]["auto_backlog"])
        extensions = baked["file"]["extensions"]
        self.assertIn(".mkv", extensions)
        self.assertIn(".mp4", extensions)
        self.assertIn(".avi", extensions)
        self.assertIn(".mov", extensions)


if __name__ == "__main__":
    unittest.main()
