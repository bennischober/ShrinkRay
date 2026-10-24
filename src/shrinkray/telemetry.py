"""Failure telemetry: preserves the evidence a crash would otherwise lose.

A failed encode or VMAF measurement used to surface as a one-line error
in the job row (or worse, an empty message like 'libvmaf failed: ') while
the stderr that explains it evaporated with the subprocess. This module
writes a small, self-contained evidence bundle into logs/ next to the
database: the full stderr, the Python traceback, the failing command and
whatever partial artifacts (e.g. a truncated vmaf.json) still exist.

Bundles live in logs/<timestamp>-<kind>/ and are never cleaned up
automatically — they are user data for debugging, and cheap (text only).
"""

import json
import time
import traceback
from pathlib import Path

from shrinkray.logger import log

LOGS_DIRNAME = "logs"


def logs_dir(db_file: Path) -> Path:
    """Returns the logs directory for a database location (created lazily).

    Args:
        db_file (Path): The database file (logs/ is created next to it).

    Returns:
        Path: The logs directory.
    """
    directory = db_file.parent / LOGS_DIRNAME
    directory.mkdir(exist_ok=True)
    return directory


def save_failure(
    db_file: Path | None,
    kind: str,
    command: list[str] | None = None,
    stderr: str = "",
    workdir: Path | None = None,
    artifacts: list[str] | None = None,
    context: dict | None = None,
) -> Path | None:
    """Writes a failure bundle into logs/ and returns its path.

    Best-effort by design: telemetry must never break the error path it
    is reporting on, so any exception here is swallowed after a log line.

    Args:
        db_file (Path | None): The database file (the bundle lands in logs/
            next to it); None disables the bundle — telemetry needs a home
            the caller owns, never the process's working directory.
        kind (str): A short failure tag, e.g. 'vmaf-measure' or 'encode'.
        command (list[str] | None): The failing subprocess command.
        stderr (str): The captured stderr of that subprocess.
        workdir (Path | None): The subprocess workdir; artifacts are
            looked up relative to it.
        artifacts (list[str] | None): File names in the workdir worth
            preserving (e.g. 'vmaf.json') — copied if they exist.
        context (dict | None): Extra structured facts (movie, crf, ...).

    Returns:
        Path | None: The bundle directory, or None when writing failed.
    """
    try:
        if db_file is None:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bundle = logs_dir(db_file) / f"{stamp}-{kind}"
        attempts = 0
        while bundle.exists() and attempts < 100:
            attempts += 1
            bundle = bundle.with_name(f"{stamp}-{kind}-{attempts}")
        bundle.mkdir(parents=True)

        if command is not None:
            (bundle / "command.txt").write_text("\n".join(command) + "\n", encoding="utf-8")
        if stderr:
            (bundle / "stderr.txt").write_text(stderr, encoding="utf-8")
        else:
            # an EMPTY stderr is itself the evidence (2026-10-21: the failure
            # message was 'libvmaf failed: ' with nothing after the colon)
            (bundle / "stderr.txt").write_text("(stderr was empty)\n", encoding="utf-8")
        if context:
            (bundle / "context.json").write_text(
                json.dumps(context, indent=2, default=str), encoding="utf-8"
            )
        (bundle / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        if workdir is not None and artifacts:
            for name in artifacts:
                candidate = workdir / name
                if candidate.is_file() and candidate.stat().st_size > 0:
                    # a partial log is evidence too — copy, never move
                    (bundle / f"artifact-{name}").write_bytes(candidate.read_bytes())
        log(f"Failure evidence saved to {bundle}")
        return bundle
    except OSError as e:
        log(f"could not save failure evidence: {e}")
        return None
