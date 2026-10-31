import sys

from loguru import logger

# the console line format: date + time to the second, local time (issue #167 —
# the resident mode runs for days, a time-only stamp loses the day the moment
# the log leaves the terminal; issue #429 — loguru's tokens are case-sensitive:
# MM is the MONTH and SS is CENTISECONDS, so the old format printed
# hour:month:centiseconds; minutes are mm, seconds are ss). The DB stores its
# own timestamps in UTC; this line is the operator's wall clock.
CONSOLE_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"

# loguru default handler is already colorful => set our own format
logger.remove()
logger.add(
    sys.stderr,
    format=CONSOLE_FORMAT,
    level="INFO",
)


def log(message: str) -> None:
    logger.info(message)


def log_warning(message: str) -> None:
    logger.warning(message)


def log_error(message: str) -> None:
    logger.error(message)
