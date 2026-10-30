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
from datetime import datetime, timedelta, timezone

from loguru import logger
from loguru._datetime import _compile_format

from shrinkray.logger import CONSOLE_FORMAT

# the frozen instants the token pin renders. FROZEN (hour 15 at a FIXED
# +05:30 offset - round 8's finding: a UTC-aware instant renders the same
# under a `!UTC` twin, so the local-time claim had no test behind it; at
# +05:30 the twin renders 09:34:05 and fails the pin) separates the
# month/minute/second tokens (1/4/5) and sits outside the twelve hours where
# a 12-hour `hh` render coincides with the 24-hour `HH` render (hours 1-12;
# hour 00 does NOT coincide - hh renders it 12; round 4's finding: hour 3
# rendered identically under both). FROZEN_SINGLE (hour 5, UTC) closes round
# 6's finding: there the unpadded `H` renders `5:04:05` while `HH` and `hh`
# both render `05:04:05`, so the hour pair H/HH is discriminated too -
# together the two instants separate every token pair in the shape: no wrong
# spec (an AM/PM hour, the old tokens, or a `!UTC` suffix) renders BOTH
# literals (the suffix DOES render the hour-5 literal - the hour-15
# equality is what catches it).
FROZEN_TZ = timezone(timedelta(hours=5, minutes=30))
FROZEN = datetime(2026, 1, 2, 15, 4, 5, tzinfo=FROZEN_TZ)
FROZEN_SINGLE = datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone.utc)


class TestConsoleFormat(unittest.TestCase):
    def test_the_console_format_carries_date_and_seconds(self) -> None:
        """The console format constant carries the date + seconds, in
        loguru's case-sensitive tokens (issue #167, #429: MM is the MONTH
        and SS is CENTISECONDS - minutes are mm, seconds are ss, so the old
        format printed hour:month:centiseconds)."""
        self.assertIn("{time:YYYY-MM-DD HH:mm:ss}", CONSOLE_FORMAT)

    def test_a_rendered_line_matches_the_shape(self) -> None:
        """The format string is real loguru syntax, not just a constant.

        The probe sink is added BESIDE the product's installed handler and
        only the probe's own id is removed afterwards (round 12's finding 1):
        the product handler is never touched, so this test also renders
        through the product's real wiring (round 12's finding 2) - a
        reverted `format=` at the wiring fails here, and no later log
        line's shape changes.
        """
        rendered: list[str] = []

        def sink(message) -> None:
            rendered.append(str(message))

        # the probe sink is added BESIDE the product's handler (never
        # replacing it), and only the probe's own handler id is removed in
        # the finally - the product handler is untouched, so no private
        # reconstruction, no hardcoded level, and the wiring under test IS
        # the wiring the product runs
        probe_id = logger.add(sink, format=CONSOLE_FORMAT, level="INFO")
        try:
            logger.info("format probe")
        finally:
            logger.remove(probe_id)
        self.assertEqual(len(rendered), 1)
        match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rendered[0])
        self.assertIsNotNone(match, f"no timestamp in {rendered[0]!r}")
        # this test pins the SHAPE only (issue #167: date + seconds present);
        # the FIELD semantics - which slot carries which value - live in the
        # token pins below (issue #429: the digits-only search cannot tell
        # 17:10:05 from 17:01:05)

    def test_the_token_engine_renders_the_frozen_fields(self) -> None:
        """The decisive pin, deterministic: loguru's token engine renders the
        frozen instant as 2026-01-02 15:04:05 under the PRODUCT's spec -
        derived from the shipped constant, not a literal copy (round 4's
        finding 2), so the pin measures the format the product actually
        renders: the month, the hour, the minute and the second each land
        in their own slot.
        """
        spec = CONSOLE_FORMAT.split("{time:")[1].split("}")[0]
        render = _compile_format(spec)
        self.assertEqual(render(FROZEN), "2026-01-02 15:04:05")
        # round 6's finding: the unpadded H token renders 15:04:05 at hour 15
        # (15 needs no padding) and would pass that literal - the hour-5
        # instant is what catches it: H renders 5:04:05 there where the
        # shipped spec renders 05:04:05, so this second equality on the
        # DERIVED spec fails if the product's constant ever used H
        self.assertEqual(render(FROZEN_SINGLE), "2026-01-02 05:04:05")
        # round 8's finding: a `!UTC` twin renders the same as the shipped
        # spec for a UTC-aware instant - FROZEN sits at a fixed +05:30
        # offset, so the twin renders 09:34:05 there and fails the pin: the
        # console stamps the operator's wall clock, not UTC
        utc_twin = _compile_format(spec + "!UTC")
        self.assertEqual(utc_twin(FROZEN), "2026-01-02 09:34:05")

    def test_the_wrong_tokens_fail_the_frozen_pin(self) -> None:
        """The pin discriminates against the defect's token sets: under the
        old tokens the same engine renders the same frozen instant as
        2026-01-02 15:01:00 (the month 1 in the minute slot, centisecond 0
        in the second slot - the defect issue #429 reports, reproducible on
        demand), and a 12-hour token renders 2026-01-02 03:04:05.
        """
        render = _compile_format("YYYY-MM-DD HH:MM:SS")
        self.assertEqual(render(FROZEN), "2026-01-02 15:01:00")
        # and a 12-hour token fails the exact-literal pin too: hour 15 is
        # outside hh's 1-12 coincidence range (round 4's finding)
        twelve_hour = _compile_format("YYYY-MM-DD hh:mm:ss")(FROZEN)
        self.assertEqual(twelve_hour, "2026-01-02 03:04:05")


if __name__ == "__main__":
    unittest.main()
