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

import hashlib
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


# the stem cap keeps prefix + mkdtemp's 8-char random suffix inside the
# 255-byte filename limit even for a stem of multibyte characters
PROBE_SCRATCH_STEM_BYTES = 48


def probe_scratch_prefix(movie: Path) -> str:
    """Builds the attributable scratch-directory prefix for one probe run.

    Every scratch ``tempfile.mkdtemp`` the probe paths create (``plan.py``'s
    probe workdir, ``pipeline.py``'s probe workdir and the verification
    measurement's workdir) derives its name from this one grammar:
    ``shrinkray-probe-<truncated-source-stem>-<8-hex>-`` — the stem cut
    UTF-8-safely to :data:`PROBE_SCRATCH_STEM_BYTES`, the hash the first 8
    hex digits of sha256 over the source's resolved path. A hard-killed
    run's leftover directory in a shared scratch (``$TMPDIR=/tmp/shrinkray``
    in the container) is then attributable to its movie at a glance, which
    the anonymous ``metadata_quality_`` name could not offer (issue #435).

    The name is DECORATION: nothing in the engine reads it back — the probe
    cache's identity is the content-addressed key in the database
    (``search.probe_cache_key``), and no invalidation may ever key on the
    workdir name. ``mkdtemp`` appends its own random suffix, so two
    concurrent encodes of the same movie stay distinct; the shared prefix is
    never a reason for lock code at the call sites.

    Args:
        movie (Path): The source file the probe run measures.

    Returns:
        str: The ``mkdtemp`` prefix for that movie's scratch directories.
    """
    stem = movie.stem.encode("utf-8", "ignore")[:PROBE_SCRATCH_STEM_BYTES].decode("utf-8", "ignore")
    digest = hashlib.sha256(str(movie.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"shrinkray-probe-{stem}-{digest}-"
