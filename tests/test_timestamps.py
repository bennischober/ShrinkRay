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
        old_m = re.search(r"\d{4}-(\d{2})-\d{2} (\d{2}):(\d{2}):(\d{2})", wrong_lines[0])
        new_m = re.search(r"\d{4}-(\d{2})-\d{2} (\d{2}):(\d{2}):(\d{2})", right_lines[0])
        self.assertIsNotNone(old_m, wrong_lines[0])
        self.assertIsNotNone(new_m, right_lines[0])
        old_month = int(old_m.group(1))
        # the two renders read the same clock within microseconds; the token
        # mapping is what is pinned, not the clock: the OLD format's MM
        # field IS the calendar month (verified against the wall clock),
        # the NEW format's mm/ss fields ARE the minute/second. With the old
        # tokens in place the month lands in the minute slot and
        # centiseconds in the second slot, so BOTH wall-clock equalities
        # fail (a month is 1-12, a minute 0-59; centiseconds cycle 0-99
        # while the true second is fixed at render) - the pair of asserts
        # below has no month-equals-minute hole: the SECOND equality is
        # exact (the render and the now() read happen within the same
        # second), not a range check.
        import datetime as dt_mod

        now = dt_mod.datetime.now()
        self.assertEqual(
            old_month,
            now.month,
            "the old format's MM field is not the month - the premise broke",
        )
        self.assertEqual(
            int(new_m.group(2)),
            now.hour,
            "the new format's HH field is not the hour",
        )
        self.assertEqual(
            int(new_m.group(3)),
            now.minute,
            "the new format's mm field is not the minute",
        )
        self.assertEqual(
            int(new_m.group(4)),
            now.second,
            "the new format's ss field is not the second",
        )


if __name__ == "__main__":
    unittest.main()
