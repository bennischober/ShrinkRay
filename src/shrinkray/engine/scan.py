"""Movie discovery and metadata scanning (the scan engine).

This module contains the scanning core used by the CLI (and the resident
app): finding movie files, extracting their metadata in parallel and
caching the results in the database. It has no presentation logic —
printing tables and writing output files belongs to the CLI layer.
"""

from __future__ import annotations

import glob
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from shrinkray import db
from shrinkray.db import MovieRecord
from shrinkray.engine.crop import has_black_bars
from shrinkray.helpers import (
    SIZE_UNIT,
    convert_unit,
    create_movie_progress,
    get_other_codecs,
    timestr_to_int,
)
from shrinkray.logger import log, log_warning
from shrinkray.tools import process as resources
from shrinkray.tools.ffprobe import get_data

if TYPE_CHECKING:
    from shrinkray.config import Conf

UNCATEGORIZED = "Uncategorized"


def log_prune_result(result: db.PruneResult) -> None:
    """Logs the outcome of a disk/database reconciliation.

    A mass-flagging (implausibly large share of the scope flagged in one
    pass) gets a loud warning because it usually means an accident (renamed
    folder, detached drive) rather than deliberate deletions.

    Args:
        result (db.PruneResult): The reconcile outcome to report.
    """
    if result.pruned:
        log(
            f"Removed {result.pruned} movie(s) missing for {db.MISSING_GRACE} "
            "consecutive scans (encode history deleted)."
        )
    if result.flagged:
        log(
            f"Flagged {result.flagged} movie(s) as missing "
            f"({result.missing_now} flagged in total) — their history is kept "
            f"until they have been missing for {db.MISSING_GRACE} consecutive scans."
        )
    if result.recovered:
        log(f"Recovered {result.recovered} previously missing movie(s).")
    if result.mass:
        log_warning(
            f"Many movies ({result.flagged}) were flagged missing in one pass — "
            "if a whole folder was renamed or a drive was detached, re-scan the "
            "new path to recover the history."
        )


def get_name(filename: Path) -> str:
    """Determines the movie name from the file name.

    Multiple movies in one folder get their names from the file,
    otherwise the parent folder name is used (e.g. 'Movie (2020)' folders).

    Args:
        filename (Path): Path to the movie file.

    Returns:
        str: The movie name without extension.
    """
    if "title" in filename.name or "_t0" in filename.name:
        return filename.parent.name
    return filename.stem


def get_category(filename: Path, root: Path) -> str:
    """Determines the movie category from the folder structure.

    The category is the folder above the movie folder (relative to the root),
    e.g. 'Movies/Matrix/movie.mkv' => 'Movies'. If the file is not nested
    deep enough, 'Uncategorized' is used.

    Args:
        filename (Path): Path to the movie file.
        root (Path): The scan root directory.

    Returns:
        str: The category name.
    """
    try:
        relative = filename.parent.relative_to(root)
    except ValueError:
        # file is not inside the root (should not happen) => no category
        return UNCATEGORIZED

    parts = relative.parts
    if len(parts) == 0:
        # movie file directly in the root
        return UNCATEGORIZED
    if len(parts) == 1:
        # movie file in a folder directly under the root => use that folder
        return parts[0]
    return parts[0]


def resolve_input_paths(
    argument: str, extensions: list[str], exclude: Path | None = None
) -> tuple[Path, list[Path]]:
    """Resolves a CLI path argument to a category root and its movie files.

    The Mode A contract is "a file, a folder, or a glob". All three shapes are
    accepted here so no command has to care which one it was handed:

    - a **file** is one movie, and its parent folder is the category root;
    - a **directory** is walked recursively, and is its own category root;
    - a **glob** (any of `*`, `?`, `[`) is expanded recursively, and its
      category root is the pattern's leading literal directory.

    The glob shape exists because the shell does not expand a quoted pattern: on
    Windows a quoted `movies/*.mkv` reaches Python verbatim, so the CLI must do
    the matching itself. A pattern that matches nothing is an error, not an empty
    run - silently doing nothing is the failure mode this refuses.

    The argument is decided by what it *is*, not by what characters it contains:
    `*` and `?` cannot appear in a file name on POSIX or Windows, but `[` can
    (scene-release brackets: `[EN+DE]`, `[1080p]`), so the filesystem is checked
    first and glob expansion is only the fallback for a path that does not exist.

    Args:
        argument (str): The path, directory or glob pattern from the CLI.
        extensions (list[str]): Movie extensions, e.g. ['.mkv', '.mp4'].
        exclude (Path | None): Directory to exclude (e.g. the output dir).

    Returns:
        tuple[Path, list[Path]]: The category root and the resolved movie
            files, sorted and deduplicated.

    Raises:
        FileNotFoundError: If the path does not exist, or the glob matches
            no movie file.
    """
    path = Path(argument)
    if path.exists():
        if path.is_file():
            # a single movie is one item: no walk, and no extension check -
            # the operator named the file explicitly
            return path.parent.resolve(), [path.resolve()]
        if not path.is_dir():
            # exists but is neither file nor directory (a fifo, a socket):
            # keep the old refusal for anything a walk could not use
            raise FileNotFoundError(f"'{argument}' does not exist")
        return path.resolve(), find_movies(path, extensions, exclude=exclude)
    if not any(char in argument for char in "*?["):
        raise FileNotFoundError(f"'{argument}' does not exist")

    # a glob: expand it here, because a quoted pattern arrives unexpanded
    suffixes = {"." + extension.lower().lstrip(".") for extension in extensions}
    matches = [
        Path(match).resolve()
        for match in glob.glob(argument, recursive=True)
        if Path(match).is_file() and Path(match).suffix.lower() in suffixes
    ]
    if exclude is not None:
        excluded = exclude.resolve()
        matches = [match for match in matches if not _is_within(match, excluded)]
    if not matches:
        raise FileNotFoundError(
            f"'{argument}' matched no movie file (extensions: {', '.join(sorted(suffixes))})"
        )
    # the category root is the pattern's leading literal directory, so a glob
    # over one folder still groups its movies the way a directory scan would
    root = Path(argument).parent
    while any(char in str(root) for char in "*?["):
        root = root.parent
    return root.resolve(), sorted(dict.fromkeys(matches))


def _is_within(path: Path, directory: Path) -> bool:
    """Reports whether a path sits inside a directory.

    Args:
        path (Path): The path to test.
        directory (Path): The directory to test against.

    Returns:
        bool: True when path is inside directory.
    """
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def find_movies(root: Path, extensions: list[str], exclude: Path | None = None) -> list[Path]:
    """Finds all movie files with the given extensions recursively.

    Args:
        root (Path): Root directory to search in.
        extensions (list[str]): Extensions to search for, e.g. ['.mkv', '.mp4'].
        exclude (Path | None): Directory to exclude from the search (e.g. the
            output directory of an encode run).

    Returns:
        list[Path]: List of movie files.
    """
    excluded = exclude.resolve() if exclude is not None else None
    movies: list[Path] = []
    for extension in extensions:
        extension = extension.lower().lstrip(".")
        # glob is case sensitive on linux => check both cases
        candidates = list(root.glob(f"**/*.{extension}"))
        candidates.extend(root.glob(f"**/*.{extension.upper()}"))
        for candidate in candidates:
            if excluded is not None:
                try:
                    candidate.resolve().relative_to(excluded)
                    continue  # inside the excluded directory
                except ValueError:
                    pass
            # absolute paths: the db cache is keyed by absolute path, so a
            # relative scan root must not produce relative cache misses
            movies.append(candidate.resolve())
    # on windows glob is case insensitive => the same file is found twice.
    # sorted(): os.walk order is filesystem-dependent (alphabetical on NTFS,
    # hash order on ext4) => sort for deterministic output on every platform
    return sorted(dict.fromkeys(movies))


def movie_record_from_data(filename: Path, data: dict) -> MovieRecord:
    """Converts the get_data output into a database record.

    Args:
        filename (Path): Path to the movie file.
        data (dict): The metadata dictionary from get_data.

    Returns:
        MovieRecord: The record for the database.
    """
    stat = filename.stat()
    return MovieRecord(
        path=filename.resolve(),
        mtime=stat.st_mtime,
        size=stat.st_size,
        hash=data.get("hash"),
        name=data["name"],
        category=data["category"],
        duration=data["duration"],
        duration_raw=data["duration_raw"],
        fps=data["fps"],
        codec=data["codec"],
        bitrate=data["bitrate"],
        aspect_ratio=data["aspect_ratio"],
        width=data["dimensions"]["width"],
        height=data["dimensions"]["height"],
        crop=data["crop"],
        hdr=int(data["hdr"]),
        other_codecs=data["other_codecs"],
        audio_languages=data["audio_languages"],
    )


def record_from_cache(row: sqlite3.Row) -> dict:
    """Converts a cached database row into the movie data dictionary used by the output.

    Size and compression rate are recomputed from the row data since they
    are derived values that are not stored in the database.

    Args:
        row (sqlite3.Row): The cached file row.

    Returns:
        dict: The movie data dictionary.
    """
    size = convert_unit(row["size"], SIZE_UNIT.GB)

    # it's not really the compression rate, it's rather size/time
    compression_rate: float = 0.0
    if row["duration_raw"] is not None:
        compression_rate = round(size / (row["duration_raw"] / 3600), 2)
    elif row["duration"] is not None:
        compression_rate = round(size / (timestr_to_int(row["duration"]) / 3600), 2)

    # fps comes back as float from the database => '25' instead of '25.0'
    fps = row["fps"]
    if fps is not None and float(fps).is_integer():
        fps = int(fps)

    return {
        "name": row["name"],
        "category": row["category"],
        "duration": row["duration"],
        "duration_raw": row["duration_raw"],
        "fps": fps,
        "codec": row["codec"],
        "bitrate": row["bitrate"],
        "aspect_ratio": row["aspect_ratio"],
        "dimensions": {"width": row["width"], "height": row["height"]},
        "crop": row["crop"],
        "hdr": bool(row["hdr"]),
        "other_codecs": row["other_codecs"],
        "audio_languages": row["audio_languages"],
        "size": size,
        "compression_rate": compression_rate,
    }


def process_movie(
    filename: Path, root: Path, config: Conf, compute_hash: bool = False
) -> tuple[dict | None, str | None]:
    """Collects the metadata of a single movie file.

    Args:
        filename (Path): Path to the movie file.
        root (Path): The scan root directory (for the category).
        config (Conf): The config settings.
        compute_hash (bool): Compute a fast content hash of the file.

    Returns:
        tuple: The movie data dictionary (or None on error) and an error
            message (or None on success).
    """
    name = get_name(filename)
    category = get_category(filename, root)
    try:
        ret = get_data(filename, config)
    except Exception as e:
        # move errors to the caller and show them at the end
        return None, f"A reading error occured in '{name}' at '{filename}'. Exception: {e}"

    file_hash = db.compute_hash(filename) if compute_hash else None

    size = convert_unit(os.path.getsize(filename), SIZE_UNIT.GB)

    # it's not really the compression rate, it's rather size/time
    compression_rate: float = 0.0
    if ret["duration_raw"] is not None:
        compression_rate = round(size / (ret["duration_raw"] / 3600), 2)
    elif ret["duration"] is not None:
        compression_rate = round(size / (timestr_to_int(ret["duration"]) / 3600), 2)

    # check for cropping
    ret["crop"] = has_black_bars(
        filename,
        ret["dimensions"],
        min_width=config.black_bar_min_width,
        min_height=config.black_bar_min_height,
    )

    # add additional data to dictionary
    ret["name"] = name
    ret["category"] = category
    ret["size"] = size
    ret["compression_rate"] = compression_rate  # is SIZE/TIME
    ret["other_codecs"] = get_other_codecs(ret["other_codecs"])
    ret["hash"] = file_hash

    return ret, None


def scan_movies(
    root: Path,
    config: Conf,
    workers: int | None = None,
    connection: sqlite3.Connection | None = None,
    rescan: bool = False,
    compute_hash: bool = False,
    exclude: Path | None = None,
    on_movie: Callable[[int, dict | None, str | None], None] | None = None,
) -> tuple[dict, list[Path], list[str]]:
    """Scans the root directory for movies and collects their metadata.

    The movies are processed in parallel (thread pool) since the per-movie
    work consists of ffprobe/cropdetect subprocess calls. Results are
    collected by index => the output order stays deterministic.

    If a database connection is given, unchanged files (mtime + size) are
    taken from the cache instead of being probed again, and all results are
    written back to the database.

    Args:
        root (Path): Root directory to search in.
        config (Conf): The config settings.
        workers (int | None): Number of parallel workers. Defaults to
            min(8, cpu count).
        connection (sqlite3.Connection | None): Database connection for the
            scan cache.
        rescan (bool): Re-probe all files even if they are cached.
        compute_hash (bool): Compute a fast content hash for new/changed files.
        exclude (Path | None): Directory to exclude (e.g. the encode output).
        on_movie (callable | None): Called as on_movie(index, data, error)
            after each movie completes, for progressive UI updates.

    Returns:
        tuple: Movies by category, all found movies and error messages.
    """
    movies_by_category: dict[str, list[dict]] = {}

    # search for movie files with the configured extensions, recursive
    movies = find_movies(root, config.extensions, exclude=exclude)
    if not movies:
        # nothing found: the folder may have been emptied or renamed — the
        # database still needs to reconcile (flagging, not deleting, thanks
        # to the grace period) before the early return
        if connection is not None:
            prune = db.prune_missing(connection, set(), scope=str(root))
            log_prune_result(prune)
        log_warning(f"No movie files found in '{root}' with extensions {config.extensions}!")
        return movies_by_category, movies, []

    error_messages: list[str | None] = [None] * len(movies)
    results: list[dict | None] = [None] * len(movies)

    # cache lookup => which files can be taken from the database?
    cached: dict[str, object] = {}
    if connection is not None and not rescan:
        cache = db.get_cached_records(connection)
        for index, filename in enumerate(movies):
            row = cache.get(str(filename))
            if row is not None and db.is_unchanged(row, filename):
                results[index] = record_from_cache(row)
                cached[str(filename)] = row
                if on_movie is not None:
                    on_movie(index, results[index], None)

    to_scan = [filename for index, filename in enumerate(movies) if results[index] is None]

    with create_movie_progress() as progress:
        task = progress.add_task("Scanning movies", total=len(movies))
        progress.advance(task, advance=len(cached))
        if cached:
            log(f"{len(cached)} movies taken from the cache.")

        if not to_scan:
            pass  # everything cached
        elif workers == 1:
            # sequential fallback => no thread pool overhead
            for index, filename in enumerate(movies):
                if results[index] is not None:
                    continue
                results[index], error_messages[index] = process_movie(
                    filename, root, config, compute_hash=compute_hash
                )
                if on_movie is not None:
                    on_movie(index, results[index], error_messages[index])
                progress.advance(task)
        else:
            if workers is None:
                workers = min(8, resources.cpu_count())
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        process_movie, filename, root, config, compute_hash=compute_hash
                    ): index
                    for index, filename in enumerate(movies)
                    if results[index] is None
                }
                for future, index in futures.items():
                    results[index], error_messages[index] = future.result()
                    if on_movie is not None:
                        on_movie(index, results[index], error_messages[index])
                    progress.advance(task)

    # collect in file order => deterministic output
    error_list = [error for error in error_messages if error is not None]
    for result in results:
        if result is None:
            continue
        movies_by_category.setdefault(result["category"], []).append(result)

    # write back to the database
    if connection is not None:
        for index, result in enumerate(results):
            if result is None:
                continue
            record = movie_record_from_data(movies[index], result)
            db.upsert_file(connection, record)
        # reconcile with disk: flag files that vanished (grace period before
        # deletion), recover reappeared ones; only rows inside the scanned
        # root are touched (other roots in the database keep their history)
        prune = db.prune_missing(connection, {str(m) for m in movies}, scope=str(root))
        log_prune_result(prune)

    return movies_by_category, movies, error_list
