"""Swap a verified encode into the source's place, retaining the original.

ShrinkRay changes a file's bytes, never its identity: the path, the filename
and the set of files stay as they are. That is why the swap happens in this
module and not by writing into a second output directory - a library only gets
smaller when the verified output takes the original's place.

The library is sacred, so this module has no code path that deletes a source.
A swapped original is renamed aside and retained; the operator deletes it
manually when they are satisfied. The one `unlink` in this module removes a
*partial temp file this module itself created*, never a source.

The swap is deliberately small and serial:

1. `stage` copies the encoded output next to the source, under a temp name.
2. `swap` renames the source aside (the retained name), then renames the
   staged file into the source's path.

Both renames are same-directory `os.replace`/`os.rename` calls, so each one is
atomic on any POSIX or Windows filesystem: after a crash at any instant the
source path holds either the original or the new file, never neither.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from shrinkray.logger import log, log_error

# the suffix of a retained original. Fixed, not configurable: it works on any
# mount, needs no configuration and is impossible to get wrong. The leading dot
# keeps it out of the way in a file listing, and ".shrinkray-" makes clear what
# put it there.
RETAINED_SUFFIX = ".shrinkray-original"

# the suffix of the in-flight copy next to the source. Distinct from the
# retained suffix so recovery can tell "staging was interrupted" from "the swap
# already happened".
STAGING_SUFFIX = ".shrinkray-staging"


class SwapError(RuntimeError):
    """A swap was refused or failed; the source is untouched."""


@dataclass(frozen=True)
class SwapResult:
    """The outcome of a completed swap.

    Attributes:
        source_path: The path that now holds the encoded file.
        retained_path: Where the original was moved.
        size_before: The original's size in bytes.
        size_after: The new file's size in bytes.
    """

    source_path: Path
    retained_path: Path
    size_before: int
    size_after: int


def is_staging_path(path: Path) -> bool:
    """Decides whether a path lives inside a staging directory or is a
    staging/retained sibling.

    The in-place working directory is created INSIDE the library (the swap's
    renames must stay same-filesystem), and the encode it holds is named like
    the movie it will become - so a scan or a watcher pass that does not skip
    it treats an in-flight or crashed run's output as a library movie
    (issue #237). The two suffixes cover the swap's own siblings; the prefix
    covers the working directory. One predicate, shared by both walkers
    (`engine.scan.find_movies`, `engine.watch.find_new_movies`), so the two
    scans cannot drift apart again.

    Args:
        path (Path): The candidate path.

    Returns:
        bool: True when the path is ShrinkRay's own working state, never a
            library movie.
    """
    if path.name.endswith((RETAINED_SUFFIX, STAGING_SUFFIX)):
        return True
    return any(part.startswith(".shrinkray-") for part in path.parts)


def retained_path_for(source: Path) -> Path:
    """Returns the name a source's original is retained under.

    Args:
        source (Path): The library file.

    Returns:
        Path: The retained sibling path (source name + RETAINED_SUFFIX).
    """
    return source.with_name(source.name + RETAINED_SUFFIX)


def staging_path_for(source: Path) -> Path:
    """Returns the temp name the encoded output is staged under.

    The staging file lives in the source's own directory so the final rename is
    same-filesystem (an atomic rename, never a copy that could be interrupted
    half-way).

    Args:
        source (Path): The library file.

    Returns:
        Path: The staging sibling path (source name + STAGING_SUFFIX).
    """
    return source.with_name(source.name + STAGING_SUFFIX)


def stage(output: Path, source: Path) -> Path:
    """Copies a verified output next to its source, under the staging name.

    The copy, not a move: the caller's output file stays where it is until the
    swap succeeds, so a failed copy cannot lose the encode. The copy is flushed
    to disk before returning (`os.fsync`), because a swap must not proceed on a
    file the kernel still holds in a write-back cache.

    Args:
        output (Path): The verified encoded file.
        source (Path): The library file the output will replace.

    Returns:
        Path: The staged copy's path.

    Raises:
        SwapError: If the copy fails or the staged file is not the expected
            size.
    """
    staged = staging_path_for(source)
    try:
        with output.open("rb") as src, staged.open("wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    except OSError as error:
        _discard(staged)
        raise SwapError(f"could not stage the output next to the source: {error}") from error
    expected = output.stat().st_size
    actual = staged.stat().st_size if staged.exists() else -1
    if actual != expected:
        _discard(staged)
        raise SwapError(f"staged copy is {actual} bytes, expected {expected}")
    return staged


def swap(source: Path, staged: Path) -> Path:
    """Moves the original aside and the staged file into the source's path.

    Two same-directory renames, in this order:

    1. source -> retained name
    2. staged -> source

    A crash between them leaves the original under its retained name and the
    new file staged; `recover` resolves that state. A crash before the first
    rename leaves the source untouched. There is no instant at which the source
    path is missing.

    Args:
        source (Path): The library file to replace.
        staged (Path): The staged copy of the verified output.

    Returns:
        Path: The retained original's path.

    Raises:
        SwapError: If a rename fails. The source is restored when the first
            rename succeeded and the second did not.
    """
    retained = retained_path_for(source)
    if retained.exists():
        # a previous swap's original is still there: never overwrite it, the
        # operator has not reclaimed that space yet and it is their only copy
        raise SwapError(
            f"'{retained.name}' already exists; move or delete the retained "
            "original before swapping again"
        )
    try:
        os.rename(source, retained)
    except OSError as error:
        raise SwapError(f"could not move the original aside: {error}") from error
    try:
        os.rename(staged, source)
    except OSError as error:
        # put the original back: the source path must never be left empty
        try:
            os.rename(retained, source)
        except OSError as restore_error:
            log_error(
                f"swap left '{source.name}' missing: restore failed "
                f"({restore_error}); the original is at '{retained}'"
            )
            raise SwapError(
                f"could not move the new file into place ({error}); the original is at '{retained}'"
            ) from error
        raise SwapError(f"could not move the new file into place: {error}") from error
    return retained


def _discard(path: Path) -> None:
    """Removes a partial staging file this module created.

    Never a source: only ever called with a `staging_path_for` result.

    Args:
        path (Path): The staging file to remove.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        log_error(f"could not remove the staging file '{path}': {error}")


def recover(source: Path) -> str:
    """Resolves an interrupted swap in one source's directory.

    The three states a crash can leave, and what each means:

    - source present, no staging, no retained: nothing happened.
    - source present AND retained present: the swap never started (or a
      previous one completed and the operator kept the original).
    - source absent, retained present, staging present: the first rename
      succeeded and the second did not. Restore the original: an interrupted
      swap must not lose a source, and the staged file was never verified
      as being in place.
    - source absent, retained present, no staging: the second rename
      succeeded but the ledger row is missing. Report it so the caller can
      record it; the library is consistent either way.
    - staging present, no source, no retained: the staging copy is orphaned
      debris from a failed copy. Discard it.

    Args:
        source (Path): The library file.

    Returns:
        str: A short description of the state found ("clean" when the
        directory holds no interrupted swap).
    """
    retained = retained_path_for(source)
    staged = staging_path_for(source)
    source_exists = source.exists()
    retained_exists = retained.exists()
    staged_exists = staged.exists()
    if source_exists and not staged_exists:
        return "clean"
    if source_exists and staged_exists:
        _discard(staged)
        return "discarded an orphaned staging copy"
    if retained_exists and staged_exists:
        os.rename(retained, source)
        _discard(staged)
        log(f"recovered an interrupted swap of '{source.name}': original restored")
        return "restored the original"
    if staged_exists:
        _discard(staged)
        return "discarded an orphaned staging copy"
    if retained_exists:
        return "source missing with its original retained"
    return "clean"


def recover_directory(directory: Path) -> dict[str, str]:
    """Resolves every interrupted swap in one directory.

    Args:
        directory (Path): The directory to inspect (non-recursive).

    Returns:
        dict[str, str]: source name => what recovery did for it.
    """
    outcomes: dict[str, str] = {}
    if not directory.is_dir():
        return outcomes
    for staged in directory.glob(f"*{STAGING_SUFFIX}"):
        source = staged.with_name(staged.name[: -len(STAGING_SUFFIX)])
        outcome = recover(source)
        if outcome != "clean":
            outcomes[source.name] = outcome
    return outcomes
