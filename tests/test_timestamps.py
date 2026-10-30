"""Timestamp format tests (issue #167).

Every human-readable timestamp in the product shares one shape. The console
log is local time and carries the date and the seconds, so a captured log
line answers "which day, exactly, did this print" without external context.
In loguru's tokens the shape is `YYYY-MM-DD HH:mm:ss` (mm is the minute, ss
the second - MM is the MONTH and SS the CENTISECOND, issue #429's defect).
(The report half was removed with issue #202: the report module is gone.)

The token pin renders through loguru's own token engine
(`loguru._datetime._compile_format`, the same compiled formatter a sink's
spec is rendered with) against a frozen instant - a wall-clock read cannot
make the pin flaky, and no wall-clock hole can make it pass on the wrong
tokens (the review rounds 1-3 of issue #429's PR each found one).
"""

import re
import unittest
from datetime import datetime, timezone

from loguru import logger
from loguru._datetime import _compile_format

from shrinkray.logger import CONSOLE_FORMAT

# the one shape every human-readable timestamp shares
SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# the frozen instant the token pin renders: month 1, minute 4, second 5 -
# three different values, so no token confusion can reproduce the literal
FROZEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class TestConsoleFormat(unittest.TestCase):
    def test_the_console_format_carries_date_and_seconds(self) -> None:
        """The console format constant carries the date + seconds, in
        loguru's case-sensitive tokens (issue #167, #429: MM is the MONTH
        and SS is CENTISECONDS - minutes are mm, seconds are ss, so the old
        format printed hour:month:centiseconds)."""
        self.assertIn("{time:YYYY-MM-DD HH:mm:ss}", CONSOLE_FORMAT)
        # the wrong tokens never return
        self.assertNotIn("HH:MM:SS", CONSOLE_FORMAT)

    def test_a_rendered_line_matches_the_shape(self) -> None:
        """The format string is real loguru syntax, not just a constant."""
        rendered: list[str] = []

        def sink(message) -> None:
            rendered.append(str(message))

        logger.remove()
        logger.add(sink, format=CONSOLE_FORMAT, level="INFO")
        logger.info("format probe")
        logger.remove()
        self.assertEqual(len(rendered), 1)
        match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rendered[0])
        self.assertIsNotNone(match, f"no timestamp in {rendered[0]!r}")
        self.assertTrue(SHAPE.match(match.group(0)))

    def test_the_token_engine_renders_the_frozen_fields(self) -> None:
        """The decisive pin, deterministic: loguru's token engine renders the
        frozen instant as 2026-01-02 03:04:05 under the product's spec - the
        month, the minute and the second each land in their own slot.
        """
        render = _compile_format("YYYY-MM-DD HH:mm:ss")
        self.assertEqual(render(FROZEN), "2026-01-02 03:04:05")

    def test_the_old_tokens_fail_the_frozen_pin(self) -> None:
        """The pin discriminates: under the old tokens the same engine
        renders the same frozen instant as 2026-01-02 03:01:00 (the month 1
        in the minute slot, centisecond 0 in the second slot) - the defect
        issue #429 reports, reproducible on demand.
        """
        render = _compile_format("YYYY-MM-DD HH:MM:SS")
        self.assertEqual(render(FROZEN), "2026-01-02 03:01:00")
        self.assertNotEqual(render(FROZEN), "2026-01-02 03:04:05")


if __name__ == "__main__":
    unittest.main()
