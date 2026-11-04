import contextlib
import sys
from collections.abc import Iterator
from contextvars import ContextVar

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


# the [i/N] file counter (issue #471): the per-file transcript lines name the
# file they are about; the counter says where the run is. The scope is set by
# whatever loops over a payload (the CLI's encode loop, the job executors) and
# read by log()/log_warning/log_error at emit time, so engine code that does
# not know its position still labels its lines. Outside a scope no prefix is
# added (job-level and startup lines carry none).
_file_counter: ContextVar[tuple[int, int] | None] = ContextVar(
    "shrinkray_file_counter", default=None
)


@contextlib.contextmanager
def file_scope(index: int, total: int) -> Iterator[None]:
    """Labels every transcript line emitted inside with ``[index/total]``.

    Args:
        index (int): This file's 1-based position in the payload.
        total (int): The payload's resolved file count.
    """
    token = _file_counter.set((index, total))
    try:
        yield
    finally:
        _file_counter.reset(token)


def _counter_prefix() -> str:
    counter = _file_counter.get()
    return f"[{counter[0]}/{counter[1]}] " if counter else ""


def log(message: str) -> None:
    logger.info(_counter_prefix() + message)


def log_warning(message: str) -> None:
    logger.warning(_counter_prefix() + message)


def log_error(message: str) -> None:
    logger.error(_counter_prefix() + message)
