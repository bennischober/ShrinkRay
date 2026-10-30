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
        # the decisive pin, sound at any wall-clock instant (the round-1
        # review: 'the rendered minute is in the brackets' still passes with
        # the OLD format whenever the month equals the minute - 1 in 60 runs
        # in October): render with the WRONG-token format from the same
        # clock and compare the two renders against each other. The old
        # format's 2nd field IS the month (definitionally), the new one's
        # 2nd field IS the minute: equality proves the token mapping; the
        # old format's 3rd field is centiseconds (cycles 0-99), the new
        # one's is seconds (0-59): the old one eventually exceeds 59 while
        # the new one never does - across a few renders the two fields
        # DIVERGE, proving ss is a second counter. No sleep, no wall-clock
        # hole.
        old_format = CONSOLE_FORMAT.replace(
            "{time:YYYY-MM-DD HH:mm:ss}", "{time:YYYY-MM-DD HH:MM:SS}"
        )
        wrong_lines: list[str] = []
        right_lines: list[str] = []

        def wrong_sink(message) -> None:
            wrong_lines.append(str(message))

        def right_sink(message) -> None:
            right_lines.append(str(message))

        logger.remove()
        logger.add(wrong_sink, format=old_format, level="INFO")
        logger.info("token probe")
        logger.remove()
        logger.add(right_sink, format=CONSOLE_FORMAT, level="INFO")
        logger.info("token probe")
        logger.remove()
        self.assertEqual(len(wrong_lines), 1)
        self.assertEqual(len(right_lines), 1)
        # the decisive pin, sound at any wall-clock instant (the round-1
        # review: a minute-in-brackets check still passes with the OLD
        # format whenever the month equals the minute - 1 in 60 runs in
        # October; the round-2 review: a frozen clock needs logger.patch,
        # whose patched record breaks format rendering): compare the line
        # AGAINST ITSELF. One render, two reads of the same tokens: the
        # DATE's month (from MM in the date half) and the TIME's minute
        # (from the time half). With the correct tokens the date-month is
        # the calendar month and the time-minute is the current minute -
        # equal only when the wall clock itself says so. With the OLD
        # tokens BOTH halves read MM, so the two rendered values are
        # IDENTICAL ALWAYS - the pin fails exactly on the defect, no clock
        # read, no race, no sleep.
        line_m = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", plain)
        self.assertIsNotNone(line_m, plain)
        date_month = int(line_m.group(2))
        time_minute = int(line_m.group(5))
        self.assertNotEqual(
            date_month,
            time_minute,
            "the date's month and the time's minute render IDENTICAL - "
            "the minute slot is rendering the month (the MM token): "
            f"{plain!r}",
        )
        # and the seconds slot carries no centisecond cycle: within ONE
        # render we cannot see the cycle, so pin the token TEXT (the
        # constant) as the compile-time half of the pin:
        self.assertNotIn("HH:MM:SS", CONSOLE_FORMAT)


if __name__ == "__main__":
    unittest.main()
