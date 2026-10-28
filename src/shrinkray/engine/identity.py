"""Run identity for the measured paths (issue #436).

The engine used to derive "which bytes is this run about" ad hoc: every
stage gated, sampled, recorded and verified with its own `stat()` call, and
no single point bound what was measured to what gets recorded. A file
replaced while a run is in flight could leave the database holding
decisions about bytes that no longer exist on disk.

The identity is captured ONCE at run start (one `stat()`: size + mtime)
before any gate or sample, and rechecked loudly — raising, never
log-and-continue — before a decision is committed or an encode starts.
Same-size replacements with a changed mtime are caught; the deeper
same-size, mtime-preserved case is #438's boundary.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunIdentity:
    """The identity of the bytes one engine run decided about.

    Attributes:
        size (int): The file's st_size at run start.
        mtime (float): The file's st_mtime at run start.
    """

    size: int
    mtime: float


def capture_run_identity(movie: Path) -> RunIdentity:
    """Reads the run identity in one stat, before gates and sampling.

    Args:
        movie (Path): The file the run is about to measure.

    Returns:
        RunIdentity: The size and mtime of the bytes the run measures.
    """
    stat = movie.stat()
    return RunIdentity(size=stat.st_size, mtime=stat.st_mtime)


def ensure_unchanged(movie: Path, identity: RunIdentity, stage: str) -> None:
    """Raises when the file's bytes no longer match the run's identity.

    The recheck protects decision integrity (issue #436): a verdict derived
    from bytes-A must never be committed under bytes-B's identity, and a CRF
    chosen from bytes-A must never be encoded against bytes-B — that encode
    would be an unmeasured decision for its real input (invariant 5: a
    decision that cannot be measured is reported as unmeasured and the file
    is left alone). Loud abort, not log-and-continue.

    Args:
        movie (Path): The file to recheck.
        identity (RunIdentity): The run-start identity to compare against.
        stage (str): Where the recheck sits (names the abort message).

    Raises:
        RuntimeError: When size or mtime changed since the capture.
    """
    stat = movie.stat()
    if stat.st_size != identity.size or stat.st_mtime != identity.mtime:
        raise RuntimeError(
            f"'{movie.name}' changed while the run was measuring it "
            f"(was {identity.size} B at mtime {identity.mtime}, now "
            f"{stat.st_size} B at mtime {stat.st_mtime}) - the {stage} would "
            "record a decision about bytes it never measured; the file is "
            "left alone and re-decided on the next run"
        )
