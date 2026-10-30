"""Timestamp format tests (issue #167).

Every human-readable timestamp in the product shares one shape:
`YYYY-MM-DD HH:MM:SS`. The console log is local time; both must carry the
date and the seconds so a captured log line answers "which day, exactly, did
this print" without external context. (The report half was removed with
issue #202: the report module is gone.)
"""

import re
import unittest

from shrinkray.logger import CONSOLE_FORMAT

# the one shape every human-readable timestamp shares
SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


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
        self.assertIsNotNone(match, f"no YYYY-MM-DD HH:mm:ss in {plain!r}")
        self.assertTrue(SHAPE.match(match.group(0)))
        # the rendered SECONDS field is bounded 00-59 (centiseconds would
        # cycle to 99): render twice a second apart and the last field must
        # never exceed 59 - the minute field must MATCH the wall clock's
        # minute at render time
        import time as time_mod
        from datetime import datetime

        logger.remove()
        probe_lines: list[str] = []

        def probe_sink(message) -> None:
            probe_lines.append(str(message))

        logger.add(probe_sink, format=CONSOLE_FORMAT, level="INFO")
        before = datetime.now()
        time_mod.sleep(0.02)
        logger.info("seconds probe")
        time_mod.sleep(0.02)
        after = datetime.now()
        logger.remove()
        rendered_line = probe_lines[0]
        m = re.search(r"\d{4}-\d{2}-\d{2} (\d{2}):(\d{2}):(\d{2})", rendered_line)
        self.assertIsNotNone(m, rendered_line)
        hh, mm, ss = (int(g) for g in m.groups())
        self.assertLessEqual(ss, 59, f"centiseconds leaked into seconds: {rendered_line!r}")
        self.assertLessEqual(mm, 59)
        # the rendered minute matches the wall clock's minute (the month
        # would not): the render happened between before and after
        self.assertTrue(
            (
                (before.minute, before.hour, before.day) <= (mm, hh, before.day)
                and (mm, hh, after.day) <= (after.minute, after.hour, after.day)
            )
            or mm in (before.minute, after.minute),
            f"the rendered minute {mm} is not the wall clock's minute "
            f"({before.minute}..{after.minute}): {rendered_line!r}",
        )


if __name__ == "__main__":
    unittest.main()
