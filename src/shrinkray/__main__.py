"""The command line interface of MetaDataScraper.

All engine logic lives in dedicated modules (scanner, encode, quality,
service, watch, report, db) — this module only defines the commands, their
options and the console presentation.
"""

import contextlib
import signal
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from shrinkray import db
from shrinkray.config import (
    DEFAULT_ENCODER_PRESET,
    DEFAULT_PROBE_ENCODER_PRESET,
    load_config,
    seed_config_from_env,
)
from shrinkray.engine.encoders import HDR_ESTIMATE_NOTE
from shrinkray.engine.lock import guard_encodes
from shrinkray.engine.pipeline import cap_threads
from shrinkray.engine.scan import (
    find_movies,
    get_category,
    get_name,
    resolve_input_paths,
    scan_movies,
)
from shrinkray.engine.search import DEFAULT_TARGET_VMAF
from shrinkray.logger import log, log_error
from shrinkray.replace import STAGING_DIR_PREFIX
from shrinkray.tools.process import (
    Cancelled as _EncodeCancelled,
)
from shrinkray.tools.process import apply_config

if TYPE_CHECKING:
    import sqlite3

    from rich.progress import TaskID

    from shrinkray.config import Conf

from shrinkray.service import (
    finished_movies,
    record_plan,
    record_quality_results,
)

app = typer.Typer(
    help="Quality-measured video library compressor: re-encodes a collection "
    "at measured quality (VMAF-driven CRF search) and removes baked-in black bars.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

DB_FILE = Path("metadata.db")

# re-exports for backwards compatibility (tests and third parties import
# these from __main__)
__all__ = [
    "app",
    "find_movies",
    "get_category",
    "get_name",
    "scan_movies",
]


@app.callback()
def _before_every_command() -> None:
    """Creates the container's persistent config before any command reads it.

    Every command runs this first, so the image's first start (the server or a
    one-shot run) writes $SHRINKRAY_CONFIG from $SHRINKRAY_CONFIG_SEED (issue
    #247). Without both variables, i.e. outside the container, it does nothing.
    """
    seed_config_from_env()


@app.command("app", hidden=True)
@app.command("serve")
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
    from shrinkray.server.app import run_app

    # empty list = run_app computes its default (all drive roots); only an
    # explicit --root narrows the allowlist down
    roots = [Path(r) for r in root] or None
    run_app(db_file, host=host, port=port, allowed_roots=roots, config_file=config_file)


@contextlib.contextmanager
def _cancel_on_terminal_close() -> Iterator[threading.Event]:
    """Turns closing the terminal, ctrl+c and SIGTERM into a cancel.

    Closing the terminal cancels the encode (PRODUCT.md, issue #326). The
    engine's children run in their own session, so they never see the
    terminal's SIGHUP: without a handler the CLI died, its HandBrake child
    kept encoding orphaned, and the staging directory was never cleaned up.
    A signal only sets the yielded event. The engine checks it every poll
    tick, terminates its own child tree and raises Cancelled, so the CLI
    unwinds through its normal cleanup paths. A repeated signal changes
    nothing: closing a terminal under bash delivers SIGHUP twice, and an
    exception raised from the handler would unwind past the child's
    termination, orphaning it. The previous handlers come back on every
    exit path, and a signal already ignored (`nohup`, a shell's `cmd &`)
    stays ignored. After a cancel, SIGHUP and SIGTERM stay ignored instead,
    so a late repeat cannot kill the exiting command; ctrl+c is restored.
    Callers enter the scope only around the engine work, where a child
    runs; a scan before it keeps the default signal behaviour.

    Yields:
        threading.Event: Set when the first signal arrives.
    """
    cancel = threading.Event()

    def _on_signal(signum: int, frame: FrameType | None) -> None:
        cancel.set()

    if sys.platform == "win32":
        signals = [signal.SIGINT, signal.SIGTERM]
    else:
        signals = [signal.SIGHUP, signal.SIGINT, signal.SIGTERM]
    previous: dict[signal.Signals, Any] = {}
    try:
        for sig in signals:
            # an ignored signal stays ignored: `nohup` ignores SIGHUP so the
            # run survives logout, and a shell ignores SIGINT for `cmd &`
            if signal.getsignal(sig) is signal.SIG_IGN:
                continue
            # signal.signal refuses outside the main thread (ValueError): the
            # command then runs without the handler, as before issue #326
            with contextlib.suppress(OSError, ValueError):
                previous[sig] = signal.signal(sig, _on_signal)
        yield cancel
    finally:
        for sig, handler in previous.items():
            if cancel.is_set() and sig is not signal.SIGINT:
                # cancelled: the command is on its way to exit 1, and a closed
                # terminal's second SIGHUP (or a repeated SIGTERM) arriving
                # after the scope must not turn that into a kill by signal
                signal.signal(sig, signal.SIG_IGN)
                continue
            # None: the previous handler was not installed from Python
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)


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
def encode(
    path: str = typer.Argument(
        ...,
        help="A movie file, a folder, or a glob pattern (e.g. D:\\Movies).",
    ),
    output_dir: Path | None = typer.Argument(
        None,
        help=(
            "Directory for the encoded files. Omit it to encode IN PLACE: each "
            "verified output takes the source's own path and the original is "
            "renamed aside and kept (never deleted)."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be encoded without encoding."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Re-encode files even if the output already exists."
    ),
    quality: bool = typer.Option(
        True,
        "--quality/--fixed",
        hidden=True,
        help=(
            "Accepted for one release: quality mode IS the only encode mode "
            "(the fixed-preset path was removed with issue #202). --fixed "
            "is also accepted but ignored; both flags change nothing."
        ),
    ),
    target_vmaf: float | None = typer.Option(
        None,
        "--target-vmaf",
        min=1.0,
        max=100.0,
        help="The required VMAF. Default: the quality.target_vmaf config value "
        f"({DEFAULT_TARGET_VMAF:g} unless changed).",
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
    sample_every: float | None = typer.Option(
        None,
        "--sample-every",
        min=0.0,
        help=(
            "Seconds of runtime per probe window (coverage stays a fixed "
            "fraction of the file; 0 falls back to --sample-count). Default: "
            "the quality.sample_every config value."
        ),
    ),
    sample_count: int | None = typer.Option(
        None,
        "--sample-count",
        min=1,
        help="Probe windows, overriding --sample-every when set explicitly. Default: the quality.sample_count config value.",
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
            "original aside and keeping it (never deleted). Implied when no "
            "output directory is given, so it only matters alongside one."
        ),
    ),
    probe_encoder_preset: str | None = typer.Option(
        None,
        "--probe-encoder-preset",
        help=(
            "Quality mode: encoder speed preset for the PROBE encodes "
            "(a measurement setting; SVT-AV1 speed -1 to 10, higher = faster - a lower preset is slower and compresses better). "
            "Default: the handbrake.probe_encoder_preset config value. "
            "Changing it invalidates stored plans "
            "(they are re-probed)."
        ),
    ),
    encoder_preset: str | None = typer.Option(
        None,
        "--encoder-preset",
        help=(
            "Quality mode: encoder speed preset for the FINAL encode "
            "(an output setting; SVT-AV1 speed -1 to 10, higher = faster; a lower preset is slower and better, "
            "e.g. 3). Default: the handbrake.encoder_preset config value. "
            "The probes "
            "run --probe-encoder-preset."
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
    db_file: Path | None = typer.Option(
        None,
        "--db",
        help=(
            "Path of the sqlite database file (scan cache/history). Omit it and "
            "the run stays stateless: no database is created or read."
        ),
    ),
    config_file: Path = typer.Option(
        None,
        "--config",
        help="Path of the config.json file (default: next to the db, or $SHRINKRAY_CONFIG).",
    ),
) -> None:
    """Re-encodes the found movies with HandBrake to reduce their file size."""

    config = load_config(config_file=config_file)
    # the flag wins; without it the config's required VMAF applies (issue #252)
    target_vmaf = config.target_vmaf if target_vmaf is None else target_vmaf
    # the presets follow the same rule (issue #328): the config keys
    # (handbrake.probe_encoder_preset / handbrake.encoder_preset - what the
    # TUI saves and the resident app honours) are the defaults, an explicit
    # flag wins. They were hardcoded constants here, so the same saved
    # config produced different encodes in Mode A than in Mode B.
    probe_encoder_preset = (
        config.handbrake_probe_encoder_preset
        if probe_encoder_preset is None
        else probe_encoder_preset
    )
    encoder_preset = config.handbrake_encoder_preset if encoder_preset is None else encoder_preset
    apply_config(config)
    # encode probes and re-encodes: it needs the whole toolchain (issue #194)
    from shrinkray.service import all_problems

    encode_problems = all_problems()
    if encode_problems:
        for problem in encode_problems:
            log_error(problem)
        raise typer.Exit(1)
    connection = db.connect(db_file) if db_file is not None else None
    try:
        # the output directory must never be a scan target (previous outputs
        # would be re-encoded/re-probed)
        try:
            root, files = resolve_input_paths(path, config.extensions, exclude=output_dir)
        except FileNotFoundError as e:
            log_error(str(e))
            raise typer.Exit(1) from e
        _, movies, _ = scan_movies(
            root, config, connection=connection, exclude=output_dir, files=files
        )
        # in place: the encode needs a real path, so it lands in a working
        # directory INSIDE the library root - the same filesystem, so the swap's
        # two renames stay atomic and cheap. The output directory only says
        # where the bytes are written; the swap target is always the source.
        work: contextlib.AbstractContextManager[str] = (
            contextlib.nullcontext(str(output_dir))
            if output_dir is not None
            else tempfile.TemporaryDirectory(prefix=STAGING_DIR_PREFIX, dir=root)
        )
        # the signal scope is the outer context: a signal that arrives while
        # the staging directory (possibly a multi-GB partial output) is being
        # removed is absorbed, so the cleanup always completes
        with _cancel_on_terminal_close() as cancel, work as staging:
            _record_encodes(
                connection,
                movies,
                Path(staging),
                config,
                dry_run,
                overwrite,
                quality,
                target_vmaf,
                verify,
                threads,
                db_file,
                replan,
                probe_encoder_preset,
                encoder_preset,
                crop_samples,
                sample_every,
                sample_count,
                output_dir is None or replace,
                cancel=cancel,
            )
    finally:
        if connection is not None:
            connection.close()


def _swap_into_place(
    connection: "sqlite3.Connection | None",
    source: Path,
    output: Path,
    encode_id: int | None = None,
    error: str | None = None,
) -> None:
    """Swaps a verified encode into the source's path, reporting it to the console.

    Thin CLI wrapper over `service.swap_encode_into_place` (issue #192): the
    in-place contract is one code path for the CLI and the job worker; this
    adds only the console sentence. The swap gates and the ledger write live
    there.

    Args:
        connection: The database connection, or None for a stateless run (the
            swap still happens, but no ledger row is written).
        source: The library file the output replaces.
        output: The verified encoded file.
        encode_id: The encode row the run just wrote (None: stateless).
        error: A verification finding from the encode (a swap on an unverified
            artifact would make the finding permanent; None: nothing to check).
    """
    from shrinkray import service

    outcome = service.swap_encode_into_place(connection, source, output, encode_id, error)
    if not outcome.replaced:
        return
    retained = outcome.retained_path
    if retained is None:  # unreachable when replaced, for mypy
        return
    gb = (outcome.size_after or 0) / (1024**3)
    if connection is not None:
        console.print(
            f"[green]Replaced '{source.name}': the original is retained at "
            f"'{retained.name}' ({gb:.2f} GB); delete it "
            f"when you are satisfied (see `shrinkray retained list`).[/green]"
        )
    else:
        console.print(
            f"[green]Replaced '{source.name}': the original is retained at "
            f"'{retained.name}' ({gb:.2f} GB); delete it "
            f"when you are satisfied. This run used no database, so the swap is "
            f"not in `retained list`.[/green]"
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
    connection: "sqlite3.Connection | None",
    movies: list[Path],
    output_dir: Path,
    config: "Conf",
    dry_run: bool,
    overwrite: bool,
    quality: bool,
    target_vmaf: float,
    verify: bool,
    threads: int | None,
    db_file: Path | None,
    replan: bool = False,
    probe_encoder_preset: str = DEFAULT_PROBE_ENCODER_PRESET,
    encoder_preset: str = DEFAULT_ENCODER_PRESET,
    crop_samples: int = 10,
    sample_every: float | None = None,
    sample_count: int | None = None,
    replace: bool = False,
    cancel: "threading.Event | None" = None,
) -> None:
    """Runs the encoding and records the results in the database.

    Args:
        connection: The database connection.
        movies (list[Path]): The movie files to encode.
        output_dir (Path): Directory for the encoded files.
        config (Conf): The config settings.
        dry_run (bool): Only show what would be encoded.
        overwrite (bool): Whether existing output files may be replaced.
        quality (bool): Accepted as a no-op alias (quality mode IS the only mode).
        target_vmaf (float): VMAF quality floor for quality mode.
        verify (bool): Full-file VMAF verification after each encode.
        db_file (Path): The database file (failure-evidence logs location).
        replan (bool): Ignore stored plans and re-probe (quality mode).
        probe_encoder_preset (str): The PROBE's speed preset (measurement).
        encoder_preset (str): The FINAL encode's speed preset (output).
        cancel (threading.Event | None): Set by the terminal-close/interrupt
            handler (issue #326): the engine checks it between steps and
            terminates the running child tree, so the CLI unwinds and the
            TemporaryDirectory cleanup runs.
    """
    # the config values are the defaults: a flag not given falls back to
    # quality.sample_every / quality.sample_count (issue #236 part 2 - the
    # keys were echoed but read by nothing)
    if sample_every is None:
        sample_every = config.sample_every
    if sample_count is None:
        sample_count = config.sample_count
    sample_seconds = config.sample_seconds
    from shrinkray.engine.pipeline import encode_with_quality

    # already encoded AND output still present? (unless --overwrite)
    # => batch resume: an interrupted run skips everything it finished before
    encoded_paths: set[str] = set()
    if not overwrite and not dry_run and connection is not None:
        encoded_paths = finished_movies(connection, movies, output_dir)

    if not dry_run:
        try:
            # the slot is taken PER MOVIE, on the movie's own directory (issue
            # #230, #379): that is where the staging copy and the swap's two
            # renames happen, so it is the key the job and the watcher agree on.
            # A library root is not an identity - roots nest.
            from shrinkray.engine.pause import manual_encode, state_dir_for_cli

            if sys.stdin.isatty() and sys.stdout.isatty():
                # the single-file case runs in a console (issue #250): say
                # what closing it does before hours of work start
                console.print(
                    "[yellow]Keep this terminal open: closing it cancels the encode. "
                    "The source file stays untouched until the finished result is "
                    "swapped in.[/yellow]"
                )
            # a resident watcher on the same data folder yields while this
            # runs (issue #250); the marker goes away when the run ends
            with manual_encode(state_dir_for_cli(db_file)):
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
                        # the slot is keyed on the MOVIE'S OWN DIRECTORY in place:
                        # the staging copy and the swap's two renames happen there,
                        # so it is the key the job and the watcher can agree on
                        # (#379); two-directory mode keys on the output directory.
                        # Per movie, never nested.
                        lock_target = movie.parent if replace else output_dir
                        with guard_encodes(lock_target, holder="encode-cli"):
                            result = encode_with_quality(
                                movie,
                                output_dir / movie.name,
                                target_vmaf=target_vmaf,
                                encoder=config.handbrake_encoder,
                                db_file=db_file,
                                encoder_threads=cap_threads(
                                    threads or config.handbrake_max_threads
                                ),
                                progress=quality_progress,
                                verify=verify,
                                force_reprobe=replan,
                                probe_encoder_preset=probe_encoder_preset,
                                encoder_preset=encoder_preset,
                                min_source_height=config.min_source_height,
                                min_source_width=config.min_source_width,
                                min_source_size_gb=config.min_source_size_gb,
                                candidate_codecs=tuple(config.candidate_codecs),
                                crop_sample_count=crop_samples,
                                sample_every=sample_every,
                                sample_count=sample_count,
                                sample_seconds=sample_seconds,
                                cancel=cancel,
                                audio_languages=config.handbrake_audio_languages,
                                subtitle_languages=config.handbrake_subtitle_languages,
                            )
                            if cancel is not None and cancel.is_set():
                                # a signal after the engine's last check (the
                                # encode finished, or a short probe it killed in
                                # our process group): honour it before anything
                                # is recorded or swapped - the source stays as is
                                raise _EncodeCancelled("cancelled by user")
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
                                if connection is not None:
                                    outcome = record_quality_results(
                                        connection,
                                        movie,
                                        result,
                                        config,
                                        # in place the artifact's real location is the
                                        # source's own path once the swap succeeds
                                        # (issue #232); the row records it up front and
                                        # a DECLINED swap clears it, because the encode
                                        # is then discarded with its staging directory
                                        final_output=movie if replace else None,
                                    )
                                else:
                                    outcome = None
                                if replace:
                                    _swap_into_place(
                                        connection,
                                        movie,
                                        output_dir / movie.name,
                                        outcome.encode_id if outcome else None,
                                        error=result.error,
                                    )
                                if result.error is not None:
                                    # a verification finding must not hide behind
                                    # the green line (review finding 1: an error
                                    # the user cannot see is a bug); with replace,
                                    # the swap above declined and kept the original
                                    console.print(
                                        f"[yellow]Encoded '{movie.name}' with a finding: "
                                        f"{result.error}[/yellow]"
                                    )
                            elif result.skipped_reason is not None:
                                # guardrail skip => record it so future runs do not re-probe
                                console.print(
                                    f"[yellow]Skipped '{movie.name}': {result.error}[/yellow]"
                                )
                                if connection is not None:
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


@app.command()
def plan(
    path: str = typer.Argument(
        ...,
        help="A movie file, a folder, or a glob pattern (e.g. D:\\Movies).",
    ),
    quality: bool = typer.Option(
        True,
        "--quality/--fixed",
        hidden=True,
        help="Accepted for one release: planning is always quality mode "
        "(the fixed-preset plan was removed with issue #202); both flags "
        "change nothing.",
    ),
    target_vmaf: float | None = typer.Option(
        None,
        "--target-vmaf",
        min=1.0,
        max=100.0,
        help="The required VMAF. Default: the quality.target_vmaf config value "
        f"({DEFAULT_TARGET_VMAF:g} unless changed).",
    ),
    crop_samples: int = typer.Option(
        10,
        "--crop-samples",
        min=1,
        help="cropdetect windows per movie (more samples can only shrink the crop).",
    ),
    sample_every: float | None = typer.Option(
        None,
        "--sample-every",
        min=0.0,
        help=(
            "Seconds of runtime per probe window (coverage stays a fixed "
            "fraction of the file; 0 falls back to --sample-count). Default: "
            "the quality.sample_every config value."
        ),
    ),
    sample_count: int | None = typer.Option(
        None,
        "--sample-count",
        min=1,
        help="Probe windows, overriding --sample-every when set explicitly. Default: the quality.sample_count config value.",
    ),
    threads: int = typer.Option(
        0,
        "--threads",
        min=0,
        help="Per-encode thread cap for the probes (0 = all cores).",
    ),
    no_table: bool = typer.Option(
        False, "--no-table", help="Do not print the results as table to the console."
    ),
    probe_encoder_preset: str | None = typer.Option(
        None,
        "--probe-encoder-preset",
        help=(
            "Encoder speed preset for the probes (a measurement setting; "
            "SVT-AV1 speed -1 to 10). Default: the handbrake.probe_encoder_preset "
            "config value. The plan's fingerprint keys on it."
        ),
    ),
    encoder_preset: str | None = typer.Option(
        None,
        "--encoder-preset",
        help=(
            "The planned FINAL encode's speed preset (an output setting; "
            "plan never encodes - recorded for provenance only). Default: "
            "the handbrake.encoder_preset config value."
        ),
    ),
    db_file: Path | None = typer.Option(
        None,
        "--db",
        help=(
            "Path of the sqlite database file (scan cache/history). Omit it and "
            "the run stays stateless: no database is created or read."
        ),
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
    from shrinkray.engine.plan import plan_quality

    config = load_config(config_file=config_file)
    # the flag wins; without it the config's required VMAF applies (issue #252)
    target_vmaf = config.target_vmaf if target_vmaf is None else target_vmaf
    # the presets follow the same rule (issue #328): the config keys are the
    # defaults, an explicit flag wins (the TUI saves them; the resident app
    # and now the CLI honour them alike)
    probe_encoder_preset = (
        config.handbrake_probe_encoder_preset
        if probe_encoder_preset is None
        else probe_encoder_preset
    )
    encoder_preset = config.handbrake_encoder_preset if encoder_preset is None else encoder_preset
    apply_config(config)
    # plan probes every movie's CRF: it needs the whole toolchain (issue #194)
    from shrinkray.service import all_problems

    plan_problems = all_problems()
    if plan_problems:
        for problem in plan_problems:
            log_error(problem)
        raise typer.Exit(1)
    connection = db.connect(db_file) if db_file is not None else None
    # the probes are the expensive part: closing the terminal cancels them
    # too (issue #326); entered once the input is resolved
    signals = contextlib.ExitStack()
    try:
        try:
            root, files = resolve_input_paths(path, config.extensions)
        except FileNotFoundError as e:
            log_error(str(e))
            raise typer.Exit(1) from e
        _, movies, _ = scan_movies(root, config, connection=connection, files=files)

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
        errors = 0
        cancel = signals.enter_context(_cancel_on_terminal_close())
        for movie in movies:
            # a signal that landed while no child ran (recording the previous
            # plan) is honoured here: plan_quality's crop pass would not see it
            if cancel.is_set():
                raise _EncodeCancelled("cancelled by user")
            try:
                result = plan_quality(
                    movie,
                    target_vmaf=target_vmaf,
                    encoder=config.handbrake_encoder,
                    db_file=db_file,
                    encoder_threads=threads_cap,
                    probe_encoder_preset=probe_encoder_preset,
                    encoder_preset=encoder_preset,
                    # the same gates encode applies: a plan that ignores them
                    # predicts encodes that encode then skips (issue #246)
                    min_source_height=config.min_source_height,
                    min_source_width=config.min_source_width,
                    min_source_size_gb=config.min_source_size_gb,
                    candidate_codecs=tuple(config.candidate_codecs),
                    # the sample settings follow the config too (issue #236
                    # part 2): the CLI flags override when given, the keys
                    # were echoed but never read before
                    sample_every=sample_every,
                    sample_count=sample_count,
                    sample_seconds=config.sample_seconds,
                    cancel=cancel,
                )
            except (RuntimeError, FileNotFoundError) as e:
                table.add_row(movie.name, "[red]error[/red]", "", "", "", "", str(e))
                errors += 1
                continue
            # quicksave: persist the decision (CRF or skip verdict) so a
            # later encode of this movie skips probing when unchanged
            if connection is not None:
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

        if not no_table:
            console.print(table)
        if errors:
            if plans:
                console.print(
                    f"[yellow]{errors} of {len(movies)} movie(s) could not be planned; "
                    "see the error rows above.[/yellow]"
                )
            else:
                log_error(f"None of the {errors} movie(s) could be planned; see the errors above.")
                raise typer.Exit(1)
        if total_input > 0 and total_output > 0:
            console.print(
                f"[green]Plan: {len(plans)} movies, estimated total "
                f"{total_input / 1024**3:.2f} GB -> {total_output / 1024**3:.2f} GB "
                f"({round((1 - total_output / total_input) * 100, 1)}% saved)[/green]"
            )
        if any(r.hdr and r.skipped_reason is None for r in plans):
            # the HDR estimate bias (issue #98): say so where the estimate is shown
            console.print(f"[yellow]{HDR_ESTIMATE_NOTE}[/yellow]")
    except _EncodeCancelled as e:
        # the plan is cut short: no table and no totals for a partial run,
        # and a nonzero exit, the same contract as an encode's cancel
        log_error(str(e))
        raise typer.Exit(1) from e
    finally:
        signals.close()
        if connection is not None:
            connection.close()


@app.command("setup")
def setup(
    config_file: Path | None = typer.Option(
        None,
        "--config",
        help="The config.json to edit (default: $SHRINKRAY_CONFIG, else ./config.json).",
    ),
    db_file: Path | None = typer.Option(
        None,
        "--db",
        help="The resident app's database: its folder holds the pause flag "
        "(default in the container: /data).",
    ),
) -> None:
    """Configures ShrinkRay and pauses or resumes it, in a terminal UI.

    Edits config.json through the same schema the app loads it with, names a
    change that re-measures the library before saving it, and switches the
    resident app's pause. The dashboard only observes; this is where the
    operator steers (issue #252).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        log_error(
            "setup is interactive: run it in a terminal (e.g. docker exec -it <container> sh)."
        )
        raise typer.Exit(1)
    from shrinkray.config import resolve_config_path
    from shrinkray.engine.pause import state_dir_for_cli
    from shrinkray.tui import SetupApp

    SetupApp(resolve_config_path(config_file=config_file), state_dir_for_cli(db_file)).run()


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

    With no database (a fresh install) the library check is skipped and
    the TOOLCHAIN check still runs: "run doctor" is the last install
    step on every route (issue #241) - a host that cannot measure a
    single file is not usable, database or not.
    """
    from shrinkray.service import all_problems
    from shrinkray.service import doctor as run_doctor

    # the toolchain is checked FIRST: "library is healthy" on a host that
    # cannot measure a single file is the honest-failure rule broken
    problems = all_problems()
    for problem in problems:
        # log_error reaches the log; console.print reaches the operator's
        # terminal AND the test runner's capture (the install check's whole
        # point is the message being seen, issue #241)
        log_error(problem)
        console.print(f"[red]{problem}[/red]")

    if not db_file.is_file():
        # a fresh install: the library check cannot run (and must not
        # create a database just to report on it); the toolchain result
        # above is the install check (issue #241)
        if problems:
            console.print(
                "[red]The toolchain cannot do the one thing this tool does "
                "(see the problems above); fix the toolchain before the "
                "first encode.[/red]"
            )
            raise typer.Exit(1)
        console.print(
            "[green]Toolchain is healthy.[/green] No database found at "
            f"'{db_file}' - the library check is skipped until a run "
            "creates one."
        )
        return

    connection = db.connect(db_file)
    try:
        findings = run_doctor(connection, probe_all=probe_all)
    finally:
        connection.close()

    if not findings and not problems:
        console.print("[green]Library is healthy: no findings.[/green]")
        return
    if not findings:
        console.print(
            "[green]The library itself is healthy: no findings.[/green] The "
            "toolchain above cannot do the one thing this tool does, so this is "
            "not a usable host."
        )
        raise typer.Exit(1)
    table = Table(title=f"Doctor findings ({len(findings)})", title_justify="left")
    table.add_column("File", overflow="fold")
    table.add_column("Issue", overflow="fold")
    table.add_column("Detail", overflow="fold")
    for finding in findings:
        table.add_row(finding.movie, finding.issue, finding.detail or "—")
    console.print(table)
    raise typer.Exit(1)  # non-zero exit: scripts/cron can detect unhealth


def calc_time(lhs: str | None, rhs: str | None) -> str | None:
    """Adds two 'HH:MM:SS' time strings and returns the sum.

    Args:
        lhs (str | None): The first time as 'HH:MM:SS' string (or None).
        rhs (str | None): The second time as 'HH:MM:SS' string (or None).

    Returns:
        str | None: The sum as 'HH:MM:SS' string.
    """
    if lhs is None:
        return rhs
    if rhs is None:
        return lhs

    lhs_parts = lhs.split(":")
    if len(lhs_parts) < 2:
        return rhs

    rhs_parts = rhs.split(":")
    if len(rhs_parts) < 2:
        return lhs

    try:
        # add seconds first
        seconds = int(float(lhs_parts[2])) + int(float(rhs_parts[2]))
        # check for seconds > 60
        minutes = int(seconds / 60)
        # add rest to seconds
        seconds = seconds % 60

        # add existing minutes
        minutes += int(lhs_parts[1]) + int(rhs_parts[1])
        hours = int(minutes / 60)
        minutes = minutes % 60

        # add existing hours
        hours += int(lhs_parts[0]) + int(rhs_parts[0])
    except ValueError:
        return lhs

    # checks for putting a 0 before the time value
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


if __name__ == "__main__":
    # the module entry point (issue #262): AGENTS.md names `python -m
    # shrinkray` next to the console script, and an import without this
    # guard exited 0 with no output - a script running it believed it had
    # encoded. The console script calls app itself; this guard is the
    # module form of the same call.
    app()
