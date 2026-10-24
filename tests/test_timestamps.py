"""Timestamp format tests (issue #167).

Every human-readable timestamp in the product shares one shape:
`YYYY-MM-DD HH:MM:SS`. The console log is local time, the report is UTC;
both must carry the date and the seconds so a captured log line answers
"which day, exactly, did this print" without external context.
"""

import re
import unittest

from shrinkray import report
from shrinkray.logger import CONSOLE_FORMAT

# the one shape every human-readable timestamp shares
SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestConsoleFormat(unittest.TestCase):
    def test_the_console_format_carries_date_and_seconds(self) -> None:
        """The console format constant carries the date + seconds (issue #167)."""
        self.assertIn("{time:YYYY-MM-DD HH:MM:SS}", CONSOLE_FORMAT)

    def test_a_rendered_line_matches_the_shape(self) -> None:
        """The format string is real loguru syntax, not just a constant."""
        from loguru import logger

        rendered: list[str] = []

        def sink(message) -> None:
            rendered.append(str(message))

        logger.remove()
        logger.add(sink, format=CONSOLE_FORMAT, level="INFO")
        logger.info("format probe")
        logger.remove()
        self.assertEqual(len(rendered), 1)
        # strip the color markup loguru may wrap the timestamp in
        plain = rendered[0]
        match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", plain)
        self.assertIsNotNone(match, f"no YYYY-MM-DD HH:MM:SS in {plain!r}")
        self.assertTrue(SHAPE.match(match.group(0)))


class TestReportFormat(unittest.TestCase):
    def test_the_report_generated_line_has_seconds(self) -> None:
        """The report's generated timestamp carries seconds (issue #167)."""
        # the format string used for the report's "generated" line
        import inspect

        source = inspect.getsource(report)
        self.assertIn('"%Y-%m-%d %H:%M:%S UTC"', source)


if __name__ == "__main__":
    unittest.main()
