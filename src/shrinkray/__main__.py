"""The command line interface of MetaDataScraper.

All engine logic lives in dedicated modules (scanner, encode, quality,
service, watch, report, db) — this module only defines the commands, their
options and the console presentation.
"""

import csv
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from shrinkray import db
from shrinkray.config import (
    DEFAULT_ENCODER_PRESET,
    DEFAULT_PROBE_ENCODER_PRESET,
    load_config,
)
from shrinkray.encode import cap_threads
from shrinkray.engine.scan import find_movies, get_category, get_name, scan_movies
from shrinkray.helpers import calc_time, generate_string, sanitize_number
from shrinkray.logger import log, log_error
from shrinkray.quality import (
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SAMPLE_EVERY,
    DEFAULT_TARGET_VMAF,
    HDR_ESTIMATE_NOTE,
)
from shrinkray.quality import (
    Cancelled as _EncodeCancelled,
)
from shrinkray.report import generate_report
from shrinkray.tools.process import apply_config

if TYPE_CHECKING:
    import sqlite3

    from rich.progress import TaskID

    from shrinkray.config import Conf

from shrinkray.service import (
    finished_movies,
    record_fixed_results,
    record_plan,
    record_quality_results,
)

app = typer.Typer(
    help="A short python script to get video meta data.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

OUTPUT_FILE = Path("movies.txt")
CSV_FILE = Path("output.csv")
DB_FILE = Path("metadata.db")

# re-exports for backwards compatibility (tests and third parties import
# these from __main__)
__all__ = [
    "app",
    "find_movies",
    "get_category",
    "get_name",
    "print_summary",
    "print_table",
    "scan_movies",
    "write_csv",
    "write_output",
]


def write_output(
    movies_by_category: dict,
    total_time: str | None,
    total_size: float,
    separator: str,
    output_file: Path = OUTPUT_FILE,
) -> None:
    """Writes the processed data to the output file.

    Args:
        movies_by_category (dict): Categories as keys and movie lists as values.
        total_time (str | None): Total duration as 'HH:MM:SS' string.
        total_size (float): Total size in GB.
        separator (str): The separator between the columns.
        output_file (Path): Path of the output file.
    """
    with open(output_file, "w+", encoding="UTF8") as text:
        # write csv / file header
        header = [
            "CATEGORY",
            "NAME",
            "DURATION",
            "SIZE",
            "CODEC",
            "BITRATE",
            "ASPECT RATIO",
            "RESOLUTION",
            "GB/h",
            "FPS",
            "BARS",
            "HDR",
            "OTHER CODECS",
            "AUDIO LANGUAGES",
        ]
        text.write(separator.join(header) + "\n")

        # iterate through parent dictionary
        for movies in movies_by_category.values():
            # write value for category / index in dictionary
            rows = ""

            for value in movies:
                # only show the category once per category block
                use_cat = value is movies[0]
                rows += generate_string(value, separator, use_cat)

            text.write(rows)

        # write total duration and size
        total = ["Total", str(total_time), sanitize_number(total_size)]
        total.extend([""] * (len(header) - len(total)))
        text.write(separator.join(total) + "\n")


def write_csv(separator: str, output_file: Path = OUTPUT_FILE, csv_file: Path = CSV_FILE) -> None:
    """Converts the output file into a real csv file.

    Args:
        separator (str): The separator between the columns.
        output_file (Path): Path of the txt input file.
        csv_file (Path): Path of the csv output file.
    """
    with open(output_file, encoding="utf-8") as in_file:
        lines = [line.rstrip("\n").split(separator) for line in in_file if line.strip()]
        with open(csv_file, "w+", newline="", encoding="iso-8859-1") as out_file:
            writer = csv.writer(out_file, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
            writer.writerows(lines)


def print_table(movies_by_category: dict, total_time: str | None, total_size: float) -> None:
    """Prints the scanned movies as a rich table to the console.

    Args:
        movies_by_category (dict): Categories as keys and movie lists as values.
        total_time (str | None): Total duration as 'HH:MM:SS' string.
        total_size (float): Total size in GB.
    """
    table = Table(
        title=f"Scanned {sum(len(m) for m in movies_by_category.values())} movies "
        f"in {len(movies_by_category)} categories",
        title_justify="left",
        row_styles=["", "dim"],
    )
    table.add_column("Category", style="cyan", no_wrap=True, max_width=16)
    table.add_column("Name", style="bold", max_width=28, overflow="ellipsis")
    table.add_column("Duration", justify="right", no_wrap=True)
    table.add_column("Size GB", justify="right", no_wrap=True)
    table.add_column("GB/h", justify="right", no_wrap=True)
    table.add_column("Codec", no_wrap=True)
    table.add_column("Res", justify="center", no_wrap=True)
    table.add_column("FPS", justify="right", no_wrap=True)
    table.add_column("Bars", justify="center", no_wrap=True)
    table.add_column("HDR", justify="center", no_wrap=True)

    for category, movies in movies_by_category.items():
        for index, movie in enumerate(movies):
            dimensions = movie["dimensions"]
            table.add_row(
                category if index == 0 else "",
                str(movie["name"]),
                str(movie["duration"]),
                f"{movie['size']:.2f}",
                f"{movie['compression_rate']:.2f}",
                str(movie["codec"]),
                f"{dimensions['width']}x{dimensions['height']}",
                str(movie["fps"]),
                "yes" if movie["crop"] else "",
                "yes" if movie["hdr"] else "",
            )
        # divider between categories
        table.add_section()

    # total row
    table.add_row(
        "",
        "Total",
        str(total_time),
        f"{total_size:.2f}",
        "",
        "",
        "",
        "",
        "",
        "",
        style="bold green",
    )

    console.print(table)


def print_summary(movies_by_category: dict, total_time: str | None, total_size: float) -> None:
    """Prints a summary of the scanned movies to the console.

    Args:
        movies_by_category (dict): Categories as keys and movie lists as values.
        total_time (str | None): Total duration as 'HH:MM:SS' string.
        total_size (float): Total size in GB.
    """
    movie_count = sum(len(movies) for movies in movies_by_category.values())
    console.print(
        f"[green]Scanned [bold]{movie_count}[/bold] movies "
        f"in [bold]{len(movies_by_category)}[/bold] categories."
    )
    console.print(
        f"Total duration: [bold]{total_time}[/bold], total size: [bold]{total_size:.2f} GB[/bold]"
    )


@app.command("app")
def app_command(
    db_file: Path = typer.Option(
        DB_FILE, "--db", help="Path of the sqlite database file (scan cache/history)."
    ),
    config_file: Path = typer.Option(
        None,
        "--config",
        help="Path of the config.json file (default: next to the db, or $SHRINKRAY_CONFIG).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8420, "--port", min=1, max=65535, help="Bind port."),
    root: list[Path] = typer.Option(
        [],
        "--root",
        help="Folder the API may operate on (repeatable). Defaults to all "
        "available drive roots (the app is localhost-only).",
    ),
) -> None:
    """Runs the resident app: job queue, watch loop and the local HTTP API.

    The app owns long-running work; terminal and web UI are just consumers
    of the same engine and database. Stop with Ctrl+C.
    """
    from shrinkray.app import run_app

    # empty list = run_app computes its default (all drive roots); only an
    # explicit --root narrows the allowlist down
    roots = [Path(r) for r in root] or None
    run_app(db_file, host=host, port=port, allowed_roots=roots, config_file=config_file)


def _totals(movies_by_category: dict) -> tuple:
    """Calculates the total duration and size of the scanned movies.

    Args:
        movies_by_category (dict): Categories as keys and movie lists as values.

    Returns:
        tuple: Total duration as 'HH:MM:SS' string and total size in GB.
    """
    total_time = None
    total_size = 0
    for movies in movies_by_category.values():
        for value in movies:
            if value["duration"] is not None:
                total_time = calc_time(total_time, value["duration"])
            total_size += value["size"]
    return total_time, total_size


@app.command()
def scan(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="Root path of the movie folder, e.g. D:\\Movies",
    ),
    csv_output: bool = typer.Option(
        False, "--csv", "-csv", help="Also write the data as csv file."
    ),
    output_file: Path = typer.Option(
        OUTPUT_FILE, "--output", "-o", help="Path of the output file."
    ),
    workers: int = typer.Option(
        0,
        "--workers",
        "-w",
        min=0,
        help="Number of parallel workers (0 = automatic, 1 = sequential).",
    ),
    no_table: bool = typer.Option(
        False, "--no-table", help="Do not print the results as table to the console."
    ),
    rescan: bool = typer.Option(
        False, "--rescan", help="Re-probe all files even if they are cached."
    ),
    hash_files: bool = typer.Option(
        False, "--hash", help="Compute a fast content hash for new/changed files."
    ),
    db_file: Path = typer.Option(
        DB_FILE, "--db", help="Path of the sqlite database file (scan cache/history)."
    ),
    config_file: Path = typer.Option(
        None,
        "--config",
        help="Path of the config.json file (default: next to the db, or $SHRINKRAY_CONFIG).",
    ),
) -> None:
    """Scans the movie folder and writes the metadata to a text file."""
    config = load_config(config_file=config_file)
    apply_config(config)
    connection = db.connect(db_file)
    try:
        movies_by_category, movies, error_messages = scan_movies(
            path,
            config,
            workers=workers if workers > 0 else None,
            connection=connection,
            rescan=rescan,
            compute_hash=hash_files,
        )
    finally:
        connection.close()

    if movies:
        total_time, total_size = _totals(movies_by_category)
        write_output(movies_by_category, total_time, total_size, config.separator, output_file)
        if not no_table:
            print_table(movies_by_category, total_time, total_size)
        print_summary(movies_by_category, total_time, total_size)

        if csv_output:
            csv_file = output_file.with_suffix(".csv")
            write_csv(config.separator, output_file, csv_file)
            console.print(f"Written '{csv_file}'.")

    # show errors
    for er in error_messages:
        log_error(er)


@app.command()
def encode(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="Root path of the movie folder, e.g. D:\\Movies",
    ),
    output_dir: Path = typer.Argument(..., help="Directory for the encoded files."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be encoded without encoding."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Re-encode files even if the output already exists."
    ),
    quality: bool = typer.Option(
        False,
        "--quality",
        help=(
            "Probe VMAF quality per movie and pick the largest CRF that keeps "
            "perceptual quality (slower, smaller files)."
        ),
    ),
    target_vmaf: float = typer.Option(
        DEFAULT_TARGET_VMAF,
        "--target-vmaf",
        min=1.0,
        max=100.0,
        help="VMAF quality floor for --quality mode.",
    ),
    crop_samples: int = typer.Option(
        10,
        "--crop-samples",
        min=1,
        help=(
            "cropdetect windows per movie (more samples can only shrink "
            "the crop, the safe direction for an irreversible crop)."
        ),
    ),
    sample_every: float = typer.Option(
        600.0,
        "--sample-every",
        min=0.0,
        help=(
            "Seconds of runtime per probe window (coverage stays a fixed "
            "fraction of the file; 0 falls back to --sample-count)."
        ),
    ),
    sample_count: int = typer.Option(
        3,
        "--sample-count",
        min=1,
        help="Probe windows, overriding --sample-every when set explicitly.",
    ),
    verify: bool = typer.Option(
        False,
        "--verify",
        help=(
            "Quality mode: measure the FULL output file against the source "
            "after each encode (ground truth beyond the 10s probes; "
            "sub-sampled 1/10 for files > 2 GB). Slow for large libraries."
        ),
    ),
    replan: bool = typer.Option(
        False,
        "--replan",
        help=(
            "Quality mode: ignore stored plans and re-probe every movie "
            "(a plan normally lets the encode skip probing)."
        ),
    ),
    replace: bool = typer.Option(
        False,
        "--replace",
        help=(
            "Swap each verified encode into its source's path, renaming the "
            "original aside and keeping it (never deleted). Off by default: "
            "without it the output is written to the output directory."
        ),
    ),
    probe_encoder_preset: str = typer.Option(
        DEFAULT_PROBE_ENCODER_PRESET,
        "--probe-encoder-preset",
        help=(
            "Quality mode: encoder speed preset for the PROBE encodes "
            "(a measurement setting; SVT-AV1 speed 0-10, higher = slower). "
            "The final encode "
            "runs --encoder-preset. Changing it invalidates stored plans "
            "(they are re-probed)."
        ),
    ),
    encoder_preset: str = typer.Option(
        DEFAULT_ENCODER_PRESET,
        "--encoder-preset",
        help=(
            "Quality mode: encoder speed preset for the FINAL encode "
            "(an output setting; SVT-AV1 speed 0-10, higher = slower, "
            "e.g. 3). The probes "
            "run --probe-encoder-preset."
        ),
    ),
    workers: int = typer.Option(
        0,
        "--workers",
        "-w",
        min=0,
        help=(
            "Number of concurrent encodes (fixed mode only; 0 = automatic, "
            "1 = sequential). Each encode gets a share of the CPU threads."
        ),
    ),
    threads: int = typer.Option(
        0,
        "--threads",
        min=0,
        help=(
            "Per-encode thread cap (0 = all cores). Keeps cores free for "
            "a media server or desktop work during long encodes."
        ),
    ),
    db_file: Path = typer.Option(
        DB_FILE, "--db", help="Path of the sqlite database file (scan cache/history)."
    ),
    config_file: Path = typer.Option(
        None,
        "--config",
        help="Path of the config.json file (default: next to the db, or $SHRINKRAY_CONFIG).",
    ),
) -> None:
    """Re-encodes the found movies with HandBrake to reduce their file size."""

    config = load_config(config_file=config_file)
    apply_config(config)
    connection = db.connect(db_file)
    try:
        # the output directory must never be a scan target (previous outputs
        # would be re-encoded/re-probed)
        _, movies, _ = scan_movies(path, config, connection=connection, exclude=output_dir)
        _record_encodes(
            connection,
            movies,
            output_dir,
            config,
            dry_run,
            overwrite,
            quality,
            target_vmaf,
            verify,
            workers,
            threads,
            db_file,
            replan,
            probe_encoder_preset,
            encoder_preset,
            crop_samples,
            sample_every,
            sample_count,
            replace,
        )
    finally:
        connection.close()


def _latest_encode_id(connection: "sqlite3.Connection", source: Path) -> int | None:
    """Returns the encode id that most recently superseded a source.

    The ledger row points at the encode it replaced, so the operator can trace
    a retained original back to the decision that produced its replacement.

    Args:
        connection: The database connection.
        source: The library file.

    Returns:
        int | None: The newest encode's id, or None when the file has no row.
    """
    row = connection.execute("SELECT id FROM files WHERE path = ?", (str(source),)).fetchone()
    if row is None:
        return None
    latest = db.get_latest_encode(connection, int(row["id"]))
    return int(latest["id"]) if latest is not None else None


def _swap_into_place(connection: "sqlite3.Connection", source: Path, output: Path) -> None:
    """Swaps a verified encode into the source's path, retaining the original.

    The swap gate is the check the encode already passed: the output exists,
    is non-empty and matches the source's duration. `--verify`'s full-file VMAF
    is recorded but is deliberately NOT a gate (a failed measurement is
    reported as unmeasured; it never invalidates an encode), so a gate that
    depends on it would silently skip every swap on a host without libvmaf.

    Size IS a gate, and it is re-checked here even though the engine already
    refused a grown encode: this is the last code before a file enters the
    library, and a bug upstream must not be able to make the library bigger.

    A swap failure never loses the source: `replace.swap` renames the
    original back when the second rename fails, and recovery handles a crash.

    Args:
        connection: The database connection.
        source: The library file the output replaces.
        output: The verified encoded file.
    """
    from shrinkray import replace as replace_module

    size_before = source.stat().st_size
    size_after = output.stat().st_size
    if size_after >= size_before:
        log_error(
            f"Not replacing '{source.name}': the output is not smaller "
            f"({size_before} -> {size_after} bytes)"
        )
        return
    try:
        staged = replace_module.stage(output, source)
        retained = replace_module.swap(source, staged)
    except replace_module.SwapError as error:
        log_error(f"Not replacing '{source.name}': {error}")
        return
    db.add_retained(
        connection, source, retained, _latest_encode_id(connection, source), size_before, size_after
    )
    output.unlink(missing_ok=True)
    console.print(
        f"[green]Replaced '{source.name}': the original is retained at "
        f"'{retained.name}' ({size_before / (1024**3):.2f} GB); delete it "
        f"when you are satisfied (see `shrinkray retained list`).[/green]"
    )


def _quality_progress_callback(progress: Progress, task: "TaskID") -> Callable[[dict], None]:
    """Builds a progress callback for the quality engine (CLI variant).

    The engine streams {'stage': 'encode', 'percent': 0-100, 'movie': name}
    during the final encode and {'stage': 'probe', 'detail': ...} step
    events during probing => the bar's description tells the probing stage
    until the first percent arrives.

    Args:
        progress: The rich Progress instance.
        task: The task id created for the quality run.

    Returns:
        Callable[[dict], None]: The callback to hand to encode_with_quality.
    """

    def callback(data: dict) -> None:
        if data.get("stage") == "encode" and "percent" in data:
            progress.update(
                task,
                description=f"Encoding '{data.get('movie', 'movie')}'",
                completed=float(data["percent"]),
            )
        elif data.get("stage") == "probe":
            # probing now streams its steps (samples, size probe, CRF
            # bisect) — the bar's description tracks them; its total is
            # the movie count, so only the description moves here
            progress.update(task, description=f"Probing: {data.get('detail', '...')}")

    return callback


def _record_encodes(
    connection: "sqlite3.Connection",
    movies: list[Path],
    output_dir: Path,
    config: "Conf",
    dry_run: bool,
    overwrite: bool,
    quality: bool,
    target_vmaf: float,
    verify: bool,
    workers: int,
    threads: int | None,
    db_file: Path | None,
    replan: bool = False,
    probe_encoder_preset: str = DEFAULT_PROBE_ENCODER_PRESET,
    encoder_preset: str = DEFAULT_ENCODER_PRESET,
    crop_samples: int = 10,
    sample_every: float = DEFAULT_SAMPLE_EVERY,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    replace: bool = False,
) -> None:
    """Runs the encoding and records the results in the database.

    Args:
        connection: The database connection.
        movies (list[Path]): The movie files to encode.
        output_dir (Path): Directory for the encoded files.
        config (Conf): The config settings.
        dry_run (bool): Only show what would be encoded.
        overwrite (bool): Whether existing output files may be replaced.
        quality (bool): Use VMAF-driven quality mode.
        target_vmaf (float): VMAF quality floor for quality mode.
        verify (bool): Full-file VMAF verification after each encode.
        workers (int): Number of concurrent encodes (fixed mode).
        db_file (Path): The database file (failure-evidence logs location).
        replan (bool): Ignore stored plans and re-probe (quality mode).
        probe_encoder_preset (str): The PROBE's speed preset (measurement).
        encoder_preset (str): The FINAL encode's speed preset (output).
    """
    from shrinkray.encode import encode_movies
    from shrinkray.quality import encode_with_quality

    # already encoded AND output still present? (unless --overwrite)
    # => batch resume: an interrupted run skips everything it finished before
    encoded_paths: set[str] = set()
    if not overwrite and not dry_run:
        encoded_paths = finished_movies(connection, movies, output_dir)

    if not dry_run and quality:
        try:
            # the whole quality run holds the encode slot once (probes and
            # final encodes all write into output_dir)
            from shrinkray.engine.lock import guard_encodes

            with guard_encodes(output_dir, holder="encode-cli"):
                # live progress for the whole quality run: a 4-hour encode
                # must not look like a hang. The engine streams
                # {'stage': 'encode', 'percent': ...} during the final
                # encode; probing has no percent => the bar shows that stage
                # as a spinning description.
                from rich.progress import (
                    BarColumn,
                    Progress,
                    SpinnerColumn,
                    TextColumn,
                    TimeElapsedColumn,
                )

                progress_columns = (
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(bar_width=None),
                    TimeElapsedColumn(),
                )
                with Progress(*progress_columns, console=console, transient=True) as progress:
                    progress_task = progress.add_task("Waiting for the first movie...", total=100)
                    quality_progress = _quality_progress_callback(progress, progress_task)
                    for movie in movies:
                        if str(movie) in encoded_paths:
                            log(f"Skipping '{movie.name}': already encoded (use --overwrite).")
                            continue
                        progress.update(
                            progress_task, description=f"Probing '{movie.name}'...", completed=0
                        )
                        result = encode_with_quality(
                            movie,
                            output_dir / movie.name,
                            target_vmaf=target_vmaf,
                            encoder=config.handbrake_encoder,
                            db_file=db_file,
                            encoder_threads=cap_threads(threads or config.handbrake_max_threads),
                            progress=quality_progress,
                            verify=verify,
                            force_reprobe=replan,
                            probe_encoder_preset=probe_encoder_preset,
                            encoder_preset=encoder_preset,
                            min_source_height=config.min_source_height,
                            min_source_size_gb=config.min_source_size_gb,
                            candidate_codecs=tuple(config.candidate_codecs),
                            crop_sample_count=crop_samples,
                            sample_every=sample_every,
                            sample_count=sample_count,
                        )
                        if result.success and result.verified_vmaf is not None:
                            console.print(
                                f"[green]Verified '{movie.name}': full-file VMAF "
                                f"{result.verified_vmaf:.2f}[/green]"
                            )
                        if result.success:
                            saved = result.saved_percent
                            # a successful encode is always a shrink (the engine
                            # refuses a grown output, issue #130), so this cannot
                            # read as a gain; assert it rather than print a lie
                            assert saved >= 0, f"success with saved={saved}%: {result.error}"
                            console.print(
                                f"[green]Encoded '{movie.name}': "
                                f"{result.input_size / (1024**3):.2f} GB -> "
                                f"{result.output_size / (1024**3):.2f} GB ({saved}% saved) "
                                f"at CRF {result.chosen_crf}, VMAF {result.achieved_vmaf}[/green]"
                            )
                            record_quality_results(connection, movie, result, config)
                            if replace:
                                _swap_into_place(connection, movie, output_dir / movie.name)
                        elif result.skipped_reason is not None:
                            # guardrail skip => record it so future runs do not re-probe
                            console.print(
                                f"[yellow]Skipped '{movie.name}': {result.error}[/yellow]"
                            )
                            record_quality_results(connection, movie, result, config)
                        else:
                            log_error(f"Encoding '{movie.name}' failed: {result.error}")
        except _EncodeCancelled as e:
            # the disk watchdog fired (free space hit the floor mid-encode)
            # or the job/user cancel event set in - the partial output is
            # already removed by the engine, so exit honestly
            log_error(str(e))
            raise typer.Exit(1) from e
        except (RuntimeError, FileNotFoundError) as e:
            log_error(str(e))
            raise typer.Exit(1) from e
        return

    results: list = []
    try:
        results = encode_movies(
            movies,
            output_dir,
            preset=config.handbrake_preset,
            encoder=config.handbrake_encoder,
            dry_run=dry_run,
            overwrite=overwrite,
            workers=workers if workers > 0 else None,
            max_threads=cap_threads(threads or config.handbrake_max_threads),
            db_file=db_file,
        )
    except (RuntimeError, FileNotFoundError) as e:
        log_error(str(e))
        raise typer.Exit(1) from e

    # record the encodes of this run in the database
    record_fixed_results(connection, results, config)
    if replace:
        for fixed_result in results:
            if fixed_result.success:
                _swap_into_place(connection, fixed_result.input, fixed_result.output)


@app.command()
def plan(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="Root path of the movie folder, e.g. D:\\Movies",
    ),
    quality: bool = typer.Option(
        True,
        "--quality/--fixed",
        help="Plan quality mode (VMAF-driven) or the fixed preset.",
    ),
    target_vmaf: float = typer.Option(
        DEFAULT_TARGET_VMAF,
        "--target-vmaf",
        min=1.0,
        max=100.0,
        help="VMAF quality floor for the quality plan.",
    ),
    crop_samples: int = typer.Option(
        10,
        "--crop-samples",
        min=1,
        help="cropdetect windows per movie (more samples can only shrink the crop).",
    ),
    sample_every: float = typer.Option(
        600.0,
        "--sample-every",
        min=0.0,
        help=(
            "Seconds of runtime per probe window (coverage stays a fixed "
            "fraction of the file; 0 falls back to --sample-count)."
        ),
    ),
    sample_count: int = typer.Option(
        3,
        "--sample-count",
        min=1,
        help="Probe windows, overriding --sample-every when set explicitly.",
    ),
    threads: int = typer.Option(
        0,
        "--threads",
        min=0,
        help="Per-encode thread cap for the probes (0 = all cores).",
    ),
    probe_encoder_preset: str = typer.Option(
        DEFAULT_PROBE_ENCODER_PRESET,
        "--probe-encoder-preset",
        help=(
            "Encoder speed preset for the probes (a measurement setting; "
            "SVT-AV1 speed 0-10). The plan's fingerprint keys on it."
        ),
    ),
    encoder_preset: str = typer.Option(
        DEFAULT_ENCODER_PRESET,
        "--encoder-preset",
        help=(
            "The planned FINAL encode's speed preset (an output setting; "
            "plan never encodes - recorded for provenance only)."
        ),
    ),
    db_file: Path = typer.Option(
        DB_FILE, "--db", help="Path of the sqlite database file (scan cache/history)."
    ),
    config_file: Path = typer.Option(
        None,
        "--config",
        help="Path of the config.json file (default: next to the db, or $SHRINKRAY_CONFIG).",
    ),
) -> None:
    """Probes the movies and predicts the encode outcome without encoding.

    Shows which movies would be encoded (with predicted CRF, quality and
    estimated size) and which would be skipped (with the reason). Runs the
    full probing pipeline, so it takes roughly as long as the probing part
    of a real encode run.
    """
    from shrinkray.quality import plan_quality

    config = load_config(config_file=config_file)
    apply_config(config)
    connection = db.connect(db_file)
    try:
        _, movies, _ = scan_movies(path, config, connection=connection)

        threads_cap = cap_threads(threads or config.handbrake_max_threads)

        table = Table(
            title=f"Encode plan for {len(movies)} movies (target VMAF {target_vmaf})",
            title_justify="left",
        )
        table.add_column("Movie", style="bold", max_width=32, overflow="ellipsis")
        table.add_column("Decision", justify="center", no_wrap=True)
        table.add_column("CRF", justify="right", no_wrap=True)
        table.add_column("VMAF", justify="right", no_wrap=True)
        table.add_column("Est. size", justify="right", no_wrap=True)
        table.add_column("Saved", justify="right", no_wrap=True)
        table.add_column("Note", max_width=40, overflow="fold")

        total_input = total_output = 0
        plans: list = []
        for movie in movies:
            try:
                result = plan_quality(
                    movie,
                    target_vmaf=target_vmaf,
                    encoder=config.handbrake_encoder,
                    db_file=db_file,
                    encoder_threads=threads_cap,
                    probe_encoder_preset=probe_encoder_preset,
                    encoder_preset=encoder_preset,
                )
            except (RuntimeError, FileNotFoundError) as e:
                table.add_row(movie.name, "[red]error[/red]", "", "", "", "", str(e))
                continue
            # quicksave: persist the decision (CRF or skip verdict) so a
            # later encode of this movie skips probing when unchanged
            record_plan(connection, movie, result, config)
            plans.append(result)

            if result.skipped_reason is not None:
                table.add_row(
                    movie.name,
                    "[yellow]skip[/yellow]",
                    str(result.chosen_crf or ""),
                    str(result.achieved_vmaf or ""),
                    "-",
                    "-",
                    result.skipped_reason,
                )
                total_input += result.input_size
                continue

            est_gb = result.output_size / 1024**3
            saved = result.saved_percent
            total_input += result.input_size
            total_output += result.output_size
            table.add_row(
                movie.name,
                "[green]encode[/green]",
                str(result.chosen_crf),
                f"{result.achieved_vmaf:.2f}" if result.achieved_vmaf else "",
                f"{est_gb:.2f} GB",
                f"{saved}%",
                "bars removed" if result.crop is not None else "",
            )

        console.print(table)
        if total_input > 0 and total_output > 0:
            console.print(
                f"[green]Plan: {len(plans)} movies, estimated total "
                f"{total_input / 1024**3:.2f} GB -> {total_output / 1024**3:.2f} GB "
                f"({round((1 - total_output / total_input) * 100, 1)}% saved)[/green]"
            )
        if any(r.hdr and r.skipped_reason is None for r in plans):
            # the HDR estimate bias (issue #98): say so where the estimate is shown
            console.print(f"[yellow]{HDR_ESTIMATE_NOTE}[/yellow]")
    finally:
        connection.close()


@app.command("retained")
def retained(
    action: str = typer.Argument(
        "list",
        help="'list' (default) shows every retained original, or 'recover' to resolve an interrupted swap.",
    ),
    directory: Path = typer.Option(
        None,
        "--dir",
        help="recover: the directory to inspect (default: the whole allowlist is not scanned; pass a folder).",
    ),
    db_file: Path = typer.Option(DB_FILE, "--db", help="Path of the sqlite database file."),
) -> None:
    """Lists the originals a swap retained, or resolves an interrupted swap.

    ShrinkRay never deletes a source. A replaced original is renamed aside and
    listed here; deleting it is the operator's own action, in their own file
    manager, once they are satisfied with the result.
    """
    if action == "recover":
        if directory is None:
            log_error("recover needs --dir: the folder to inspect.")
            raise typer.Exit(1)
        from shrinkray.replace import recover_directory

        outcomes = recover_directory(directory)
        if not outcomes:
            console.print(f"[green]No interrupted swap in '{directory}'.[/green]")
            return
        for name, outcome in outcomes.items():
            console.print(f"{name}: {outcome}")
        return
    if action != "list":
        log_error(f"Unknown action '{action}': use 'list' or 'recover'.")
        raise typer.Exit(1)
    if not db_file.is_file():
        log_error(f"Database '{db_file}' not found! Run a scan first.")
        raise typer.Exit(1)
    connection = db.connect(db_file)
    try:
        rows = db.list_retained(connection)
        if not rows:
            console.print("No retained originals: nothing has been swapped in place yet.")
            return
        total = sum(int(row["size_before"]) for row in rows)
        for row in rows:
            missing = "" if Path(row["retained_path"]).exists() else " (already removed)"
            console.print(
                f"{row['retained_path']}  "
                f"{int(row['size_before']) / (1024**3):.2f} GB -> "
                f"{int(row['size_after']) / (1024**3):.2f} GB  "
                f"{row['created_at']}{missing}"
            )
        console.print(
            f"\n[green]{len(rows)} retained original(s), "
            f"{total / (1024**3):.2f} GB reclaimable.[/green] "
            "ShrinkRay never deletes them: remove them yourself when satisfied."
        )
    finally:
        connection.close()


@app.command()
def report(
    db_file: Path = typer.Option(DB_FILE, "--db", help="Path of the sqlite database file."),
    output: Path = typer.Option(
        Path("report.html"), "--output", "-o", help="Path of the HTML report."
    ),
) -> None:
    """Generates a self-contained HTML report from the database."""
    if not db_file.is_file():
        log_error(f"Database '{db_file}' not found! Run a scan first.")
        raise typer.Exit(1)

    generate_report(db_file, output)
    console.print(f"[green]Report written to '{output}'.[/green]")


@app.command("compare")
def compare(
    movie: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="The movie file whose encode history (generation chain) to show.",
    ),
    db_file: Path = typer.Option(DB_FILE, "--db", help="Path of the sqlite database file."),
) -> None:
    """Shows the provenance chain of a movie: every generation with its data.

    Each row documents one generation: codec, bitrate, size, CRF/VMAF (when
    quality mode) and the generation number. Re-encoding an encode chains a
    new row automatically — this is how generation drift becomes visible
    per title (e.g. rip 18.4 Mbit/s -> encode 14.9 Mbit/s).
    """
    if not db_file.is_file():
        log_error(f"Database '{db_file}' not found! Run a scan first.")
        raise typer.Exit(1)

    connection = db.connect(db_file)
    try:
        movie_str = str(movie.resolve())
        # the chain: rows whose output is this file, plus rows whose input
        # is this file (a re-encode of it) — ordered by generation
        rows = connection.execute(
            """
            SELECT e.*, f.name FROM encodes e
            JOIN files f ON f.id = e.file_id
            WHERE e.output_file = ? OR e.source_file = ? OR f.path = ?
            ORDER BY e.generation, e.id
            """,
            (movie_str, movie_str, movie_str),
        ).fetchall()
        real = [r for r in rows if r["output_size"] > 0]
        if not real:
            console.print(
                f"[yellow]No encodes recorded for '{movie.name}' yet — "
                "run an encode first.[/yellow]"
            )
            return
        table = Table(title=f"Provenance chain of {movie.name}", title_justify="left")
        table.add_column("Gen", justify="right")
        table.add_column("Codec")
        table.add_column("Bitrate", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("CRF", justify="right")
        table.add_column("VMAF", justify="right")
        table.add_column("Encoded", justify="right")
        for r in real:
            bitrate = f"{r['input_bitrate']:.2f}" if r["input_bitrate"] is not None else "—"
            vmaf = f"{r['achieved_vmaf']:.2f}" if r["achieved_vmaf"] is not None else "—"
            table.add_row(
                str(r["generation"]),
                r["source_codec"] or "—",
                bitrate,
                f"{r['input_size'] / (1024**3):.2f} GB",
                str(r["chosen_crf"] or "—"),
                vmaf,
                str(r["encoded_at"] or "—"),
            )
        console.print(table)
    finally:
        connection.close()


@app.command()
def doctor(
    db_file: Path = typer.Option(DB_FILE, "--db", help="Path of the sqlite database file."),
    probe_all: bool = typer.Option(
        False,
        "--probe-all",
        help=(
            "ffprobe EVERY file row to detect truncated/corrupt files "
            "(slow: ~50ms per file). Default checks existence only."
        ),
    ),
) -> None:
    """Checks the library's health: db reality vs disk reality.

    Reports dead paths (files the db knows but the disk lost), encode
    rows whose output vanished, and with --probe-all corrupt/truncated
    files. Read-only: doctor never repairs anything (delete-flagging
    stays the scan's job with its grace period).
    """
    if not db_file.is_file():
        log_error(f"Database '{db_file}' not found! Run a scan first.")
        raise typer.Exit(1)

    from shrinkray.service import doctor as run_doctor

    connection = db.connect(db_file)
    try:
        findings = run_doctor(connection, probe_all=probe_all)
    finally:
        connection.close()

    if not findings:
        console.print("[green]Library is healthy: no findings.[/green]")
        return
    table = Table(title=f"Doctor findings ({len(findings)})", title_justify="left")
    table.add_column("File", overflow="fold")
    table.add_column("Issue", overflow="fold")
    table.add_column("Detail", overflow="fold")
    for finding in findings:
        table.add_row(finding.movie, finding.issue, finding.detail or "—")
    console.print(table)
    raise typer.Exit(1)  # non-zero exit: scripts/cron can detect unhealth


@app.command()
def watch_mode(
    inbox: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="Directory to watch for new movies.",
    ),
    output_dir: Path = typer.Argument(..., help="Directory for the encoded files."),
    target_vmaf: float = typer.Option(
        DEFAULT_TARGET_VMAF,
        "--target-vmaf",
        min=1.0,
        max=100.0,
        help="VMAF quality floor.",
    ),
    crop_samples: int = typer.Option(
        10,
        "--crop-samples",
        min=1,
        help="cropdetect windows per movie (more samples can only shrink the crop).",
    ),
    sample_every: float = typer.Option(
        600.0,
        "--sample-every",
        min=0.0,
        help=(
            "Seconds of runtime per probe window (coverage stays a fixed "
            "fraction of the file; 0 falls back to --sample-count)."
        ),
    ),
    sample_count: int = typer.Option(
        3,
        "--sample-count",
        min=1,
        help="Probe windows, overriding --sample-every when set explicitly.",
    ),
    poll: int = typer.Option(60, "--poll", min=5, help="Seconds between inbox checks."),
    config_file: Path = typer.Option(
        None,
        "--config",
        help="Path of the config.json file (default: $SHRINKRAY_CONFIG or ./config.json).",
    ),
) -> None:
    """Watches a folder and encodes newly arrived movies automatically.

    Sources stay untouched (outputs land in the output directory), so the
    library only changes when you swap the files yourself. Stop with Ctrl+C;
    the running encode finishes first.
    """
    from shrinkray.engine.watch import watch

    config = load_config(config_file=config_file)
    apply_config(config)
    try:
        watch(
            inbox,
            output_dir,
            target_vmaf=target_vmaf,
            encoder=config.handbrake_encoder,
            probe_encoder_preset=config.handbrake_probe_encoder_preset,
            encoder_preset=config.handbrake_encoder_preset,
            poll_interval=float(poll),
            max_threads=cap_threads(config.handbrake_max_threads),
            db_file=DB_FILE,
            min_source_height=config.min_source_height,
        )
    except (RuntimeError, FileNotFoundError) as e:
        log_error(str(e))
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
