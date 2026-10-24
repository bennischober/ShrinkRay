"""The job layer of the resident app: queue, states and persistence.

A job is one unit of work the resident app executes (a scan, a plan, an
encode run, a watch pass). Jobs are persisted in the database so their
history and state survive restarts; the worker thread executes one job at
a time (heavy encodes do not profit from queueing more in parallel — the
work-queue scheduler inside encode_movies parallelizes within a job).

Thread discipline: the worker thread owns the database connection while a
job runs; the queue itself is protected by a lock and both the API layer
and the worker talk to it through JobQueue's methods only.
"""

import contextlib
import json
import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from shrinkray.config import Conf
from shrinkray.encode import cap_threads
from shrinkray.quality import DEFAULT_TARGET_VMAF, Cancelled
from shrinkray.tools.process import apply_config

# how many terminated (done/failed/cancelled) job rows the history keeps.
# Terminated rows are the job trail (outcomes, errors, retry affordances);
# anything beyond the cap is hard-deleted on the next terminal write. The
# library's own history lives in files/encodes and is NOT affected.
# the engine's live-progress callback: stage detail and counters, never persisted
ProgressCallback = Callable[[dict], None]

JOB_RETENTION = 100

# live progress is FLOW, not state: it is never persisted (a running job's
# row keeps result = NULL) and dies with the process. The bus fans each
# engine callback out to every SSE subscriber; each subscriber gets a
# bounded mailbox (drop-oldest — progress is replaceable, only the latest
# percent matters) and a seed of the latest event per job on connect, so a
# late-joining tab renders the running band instantly.
MAILBOX_SIZE = 64


class JobState(str, Enum):
    """The lifecycle states of a job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, Enum):
    """The kinds of work a job can represent.

    LEGACY marks a stored row whose kind this version no longer knows (the
    duplicate analysis, removed with issue #37). Such a row is history only:
    it is rendered as a generic finished job and never executed or enqueued.
    """

    SCAN = "scan"
    PLAN = "plan"
    ENCODE = "encode"
    LEGACY = "legacy"


@dataclass
class Job:
    """One unit of work in the queue.

    Attributes:
        id (int | None): The database id (None until persisted).
        kind (JobKind): What kind of work this is.
        payload (dict): The parameters (paths, options) as JSON-able dict.
        state (JobState): The current lifecycle state.
        result (dict | None): The engine summary after completion.
        error (str | None): The failure message if the job failed.
    """

    kind: JobKind
    payload: dict
    id: int | None = None
    state: JobState = JobState.QUEUED
    result: dict | None = None
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        """Returns the job as a JSON-able dictionary (for the API/UI).

        Returns:
            dict: The job data.
        """
        return {
            "id": self.id,
            "kind": self.kind.value,
            "payload": self.payload,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _row_to_job(row: sqlite3.Row) -> Job:
    """Builds a Job from a database row.

    A row whose kind this version no longer knows is not an error: a user's
    database keeps rows written by an older build (the duplicate analysis,
    removed with issue #37), and reading the history must not crash. Those
    rows become LEGACY and are never executed.

    Args:
        row (sqlite3.Row): The jobs table row.

    Returns:
        Job: The reconstructed job.
    """
    try:
        kind = JobKind(row["kind"])
    except ValueError:
        kind = JobKind.LEGACY
    return Job(
        id=int(row["id"]),
        kind=kind,
        payload=json.loads(row["payload"]),
        state=JobState(row["state"]),
        result=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class ProgressBus:
    """In-memory pub-sub for live job progress (flow, never persisted).

    The worker thread publishes each engine callback; SSE handlers
    subscribe with a bounded mailbox and drain it into the browser. The
    latest event per job is kept as the seed for late subscribers (a newly
    opened tab renders the running band without waiting for the next
    engine tick). Thread discipline mirrors the JobQueue/cancel-event
    registry: producers and consumers are arbitrary threads, all state
    changes under one lock, mailboxes are bounded queue.Queue instances
    (drop-oldest, because progress is replaceable — only the latest
    percent matters).
    """

    def __init__(self) -> None:
        """Initializes the bus (no subscribers, empty seed cache)."""
        self._lock = threading.Lock()
        self._subs: dict[int, queue.Queue] = {}
        self._last: dict[int, dict] = {}
        self._next_id = 0

    def publish(self, job_id: int, data: dict) -> None:
        """Fans a progress event out to every subscriber (worker thread).

        Non-blocking by construction: a slow consumer's mailbox drops its
        oldest event instead of stalling the engine.

        Args:
            job_id (int): The running job's id.
            data (dict): The engine's progress data (JSON-able).
        """
        with self._lock:
            self._last[job_id] = data
            for box in self._subs.values():
                if box.full():
                    with contextlib.suppress(queue.Empty):
                        box.get_nowait()  # drop-oldest
                box.put_nowait((job_id, data))

    def subscribe(self) -> tuple[int, "queue.Queue", list[tuple[int, dict]]]:
        """Registers a subscriber and returns its mailbox and seed events.

        Returns:
            tuple[int, queue.Queue, list[tuple[int, dict]]]: The subscriber
            id, its bounded mailbox, and the latest event per job (the
            seed; may be empty).
        """
        with self._lock:
            self._next_id += 1
            sid = self._next_id
            box: queue.Queue = queue.Queue(maxsize=MAILBOX_SIZE)
            self._subs[sid] = box
            return sid, box, list(self._last.items())

    def unsubscribe(self, sid: int) -> None:
        """Removes a subscriber (SSE handler's finally block).

        Args:
            sid (int): The subscriber id from subscribe().
        """
        with self._lock:
            self._subs.pop(sid, None)

    def forget(self, job_id: int) -> None:
        """Drops a job from the seed cache (terminal state reached).

        Called by the queue's terminal writes: a finished job's progress is
        dead live state — without this the cache would grow with every job
        id over the app's lifetime.

        Args:
            job_id (int): The job to drop from the cache.
        """
        with self._lock:
            self._last.pop(job_id, None)

    def latest(self) -> dict[int, dict]:
        """Returns the latest event per job (seed-cache inspection, tests).

        Returns:
            dict[int, dict]: Job id -> latest progress data.
        """
        with self._lock:
            return dict(self._last)


class JobQueue:
    """A persistent job queue backed by the sqlite database.

    The queue holds pending jobs in memory (fast, lock-protected) and
    mirrors every state change into the jobs table (durable, visible to
    the UI and the CLI). The worker thread consumes one job at a time.
    """

    def __init__(self, connection: sqlite3.Connection, bus: "ProgressBus | None" = None) -> None:
        """Initializes the queue on an open database connection.

        Args:
            connection (sqlite3.Connection): The database connection.
            bus (ProgressBus | None): The live-progress bus (None in
                CLI/tests without a resident app — progress is dropped).
        """
        self._connection = connection
        self._bus = bus
        self._lock = threading.Lock()
        self._pending: list[Job] = []
        self._cancel_events: dict[int, threading.Event] = {}
        self._wake = threading.Event()
        self._load_pending()

    def _load_pending(self) -> None:
        """Loads queued jobs from the database (after a restart).

        A queued row of a kind this version no longer knows (the duplicate
        analysis, removed with issue #37) is retired as failed instead of
        being executed: the operator sees why it never ran rather than a
        queue that silently cannot drain.
        """
        rows = self._connection.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY id", (JobState.QUEUED.value,)
        ).fetchall()
        pending: list[Job] = []
        retired = 0
        for row in rows:
            job = _row_to_job(row)
            if job.kind is JobKind.LEGACY:
                self._connection.execute(
                    "UPDATE jobs SET state = ?, error = ?, finished_at = datetime('now') "
                    "WHERE id = ?",
                    (JobState.FAILED.value, "job kind removed in this version", job.id),
                )
                retired += 1
            else:
                pending.append(job)
        if retired:
            self._connection.commit()
        with self._lock:
            self._pending = pending

    def enqueue(self, job: Job) -> Job:
        """Adds a job to the queue and persists it.

        Args:
            job (Job): The job to add.

        Returns:
            Job: The job with its database id set.
        """
        cursor = self._connection.execute(
            "INSERT INTO jobs (kind, payload, state) VALUES (?, ?, ?)",
            (job.kind.value, json.dumps(job.payload), JobState.QUEUED.value),
        )
        self._connection.commit()
        job.id = int(cursor.lastrowid or 0)
        with self._lock:
            self._pending.append(job)
        self._wake.set()
        return job

    def take_next(self) -> Job | None:
        """Returns the next queued job and marks it running.

        Returns:
            Job | None: The job, or None if the queue is empty.
        """
        with self._lock:
            if not self._pending:
                return None
            job = self._pending.pop(0)
            job.state = JobState.RUNNING
        self._connection.execute(
            "UPDATE jobs SET state = ?, started_at = datetime('now') WHERE id = ?",
            (JobState.RUNNING.value, job.id),
        )
        self._connection.commit()
        return job

    def report_progress(self, job: Job, progress: dict) -> None:
        """Publishes a running job's live progress (flow, never persisted).

        The event goes to the bus (SSE subscribers + seed cache) only — the
        job's row keeps ``result = NULL`` while running, so the database
        stores no live state and every progress consumer is a subscriber.
        Only the worker thread calls this.

        Args:
            job (Job): The running job.
            progress (dict): The current progress data (JSON-able).
        """
        job.result = None
        if self._bus is not None:
            self._bus.publish(job.id or 0, progress)

    def finish(self, job: Job, result: dict) -> None:
        """Marks a job as done and stores its result.

        Args:
            job (Job): The finished job.
            result (dict): The engine summary (JSON-able).
        """
        job.state = JobState.DONE
        job.result = result
        self._connection.execute(
            "UPDATE jobs SET state = ?, result = ?, finished_at = datetime('now') WHERE id = ?",
            (JobState.DONE.value, json.dumps(result), job.id),
        )
        self._prune_history()
        if self._bus is not None and job.id is not None:
            self._bus.forget(job.id)
        self._connection.commit()

    def _prune_history(self) -> None:
        """Hard-deletes terminated jobs beyond the retention cap.

        Called on every terminal write, so the table stays bounded without
        a background sweeper: the newest ``JOB_RETENTION`` rows survive,
        older terminated ones go. The table's whole content is the UI's
        job trail (the DETAIL expansions, retry affordances, run outcomes)
        — nothing outside the app reads it, and the library's own history
        lives in ``encodes``/``files``, not here.
        """
        self._connection.execute(
            """
            DELETE FROM jobs
             WHERE state IN ('done', 'failed', 'cancelled')
               AND id NOT IN (
                   SELECT id FROM jobs
                    WHERE state IN ('done', 'failed', 'cancelled')
                    ORDER BY id DESC
                    LIMIT ?
               )
            """,
            (JOB_RETENTION,),
        )

    def fail(self, job: Job, error: str) -> None:
        """Marks a job as failed and stores the error.

        The result column is cleared: a failed job's row is the durable
        trail (state, error, timestamps) — anything live was flow on the
        bus and died with the process.

        Args:
            job (Job): The failed job.
            error (str): The failure message.
        """
        job.state = JobState.FAILED
        job.error = error
        self._connection.execute(
            "UPDATE jobs SET state = ?, error = ?, result = NULL, "
            "finished_at = datetime('now') WHERE id = ?",
            (JobState.FAILED.value, error, job.id),
        )
        self._prune_history()
        if self._bus is not None and job.id is not None:
            self._bus.forget(job.id)
        self._connection.commit()

    def mark_cancelled(self, job: Job, result: dict | None = None) -> None:
        """Marks a job as cancelled (user request, not a failure).

        A graceful cancel carries the engine's partial result (e.g. the
        outcomes completed before the cancel) — the UI renders those on
        cancelled rows. Without one the result column stays empty, exactly
        like fail().

        Args:
            job (Job): The cancelled job.
            result (dict | None): The engine's partial result, if any.
        """
        job.state = JobState.CANCELLED
        self._connection.execute(
            "UPDATE jobs SET state = ?, result = ?, finished_at = datetime('now') WHERE id = ?",
            (JobState.CANCELLED.value, json.dumps(result) if result is not None else None, job.id),
        )
        self._prune_history()
        if self._bus is not None and job.id is not None:
            self._bus.forget(job.id)
        self._connection.commit()

    def cancel(self, job_id: int) -> bool:
        """Requests cancellation of a queued job (or signals a running one).

        Queued jobs are removed from the queue directly; running jobs get
        their cancel event set (the engine checks it between movies).

        Args:
            job_id (int): The job to cancel.

        Returns:
            bool: True if a job was found for the id.
        """
        with self._lock:
            for index, job in enumerate(self._pending):
                if job.id == job_id:
                    job.state = JobState.CANCELLED
                    self._pending.pop(index)
                    self._connection.execute(
                        "UPDATE jobs SET state = ?, finished_at = datetime('now') WHERE id = ?",
                        (JobState.CANCELLED.value, job_id),
                    )
                    self._connection.commit()
                    return True
            event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
            return True
        return False

    def register_cancel_event(self, job_id: int, event: threading.Event) -> None:
        """Registers the cancel event of a running job.

        Args:
            job_id (int): The running job's id.
            event (threading.Event): The event the engine polls.
        """
        with self._lock:
            self._cancel_events[job_id] = event

    def unregister_cancel_event(self, job_id: int) -> None:
        """Removes the cancel event of a finished job.

        Args:
            job_id (int): The job's id.
        """
        with self._lock:
            self._cancel_events.pop(job_id, None)

    def cancel_event(self, job_id: int) -> threading.Event | None:
        """Returns the cancel event of a job, if registered.

        Args:
            job_id (int): The job's id.

        Returns:
            threading.Event | None: The event, or None.
        """
        with self._lock:
            return self._cancel_events.get(job_id)

    def wait_for_work(self, timeout: float) -> bool:
        """Waits until a job might be available (or the timeout passes).

        Args:
            timeout (float): Maximum seconds to wait.

        Returns:
            bool: True if woken by a new job (check take_next), False on timeout.
        """
        woken = self._wake.wait(timeout)
        self._wake.clear()
        return woken

    def all_jobs(self, limit: int = 100) -> list[Job]:
        """Returns the recent jobs (history), newest first.

        Args:
            limit (int): Maximum number of jobs to return.

        Returns:
            list[Job]: The recent jobs.
        """
        rows = self._connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def get(self, job_id: int) -> Job | None:
        """Returns one job by id.

        Args:
            job_id (int): The job's id.

        Returns:
            Job | None: The job, or None if not found.
        """
        row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row is not None else None


def payload_paths(payload: dict) -> list[Path]:
    """Resolves the job payload's movie targets (multi-directory aware).

    The payload form the UI submits is ``path`` = first target plus
    ``paths`` = the remaining targets, so the resolver returns the UNION of
    both keys, deduplicated - not one preferred over the other (issue #100:
    preferring ``paths`` dropped the first target of every multi-target
    batch, so plan/encode skipped folder one and the per-movie history
    filter showed it "no jobs"). The legacy single-``path`` form is the
    degenerate case of the same shape.

    Args:
        payload (dict): The job payload.

    Returns:
        list[Path]: The movie targets, first-submission order preserved.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()
    for key in ("path", "paths"):
        value = payload.get(key)
        if not value:
            continue
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            path = Path(item)
            if path not in seen:
                seen.add(path)
                resolved.append(path)
    return resolved


def resolve_movies(
    payload: dict,
    config: Conf,
    connection: sqlite3.Connection | None = None,
    exclude: Path | None = None,
) -> list[Path]:
    """Resolves the payload's movies: scan directories, pass files through.

    A payload path may be a directory (scanned recursively, per the scan
    engine) or a single movie file (per-movie action from the UI; passed
    through directly). The return type is always a list of paths - the
    engines (plan_quality, encode_with_quality) take Path objects.

    Args:
        payload (dict): The job payload (paths list or legacy path string).
        config (Conf): The config settings (directory scans only).
        connection (sqlite3.Connection | None): Database connection for the
            scan cache.
        exclude (Path | None): Directory to exclude from the scan.

    Returns:
        list[Path]: The movie files.
    """
    from shrinkray.engine.scan import scan_movies

    movies: list[Path] = []
    for path in payload_paths(payload):
        if path.is_file():
            # single movie file (per-movie action) => pass through directly
            movies.append(path)
        else:
            _, part_movies, _ = scan_movies(path, config, connection=connection, exclude=exclude)
            movies.extend(part_movies)
    return movies


def _db_file_of(connection: sqlite3.Connection) -> Path:
    """Returns the database file a connection is bound to.

    Args:
        connection (sqlite3.Connection): The connection.

    Returns:
        Path: The database file path (relative as stored; the logs helper
            treats it as user data location).
    """
    row = connection.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else Path("metadata.db")


def execute_scan(
    job: Job,
    connection: sqlite3.Connection,
    cancel: threading.Event,
    progress: ProgressCallback | None = None,
) -> dict:
    """Executes a scan job.

    Args:
        job (Job): The job (payload: path, workers, rescan, hash, exclude).
        connection (sqlite3.Connection): The database connection.
        cancel (threading.Event): Cooperative cancellation signal.
        progress (callable | None): Callback progress(dict) for live updates.

    Returns:
        dict: The scan summary.
    """
    from shrinkray.config import load_config
    from shrinkray.engine.scan import scan_movies

    payload = job.payload
    if cancel.is_set():
        return {"cancelled": True}
    if progress:
        progress({"stage": "scan", "detail": "scanning library"})

    config = load_config()
    apply_config(config)
    movies: list = []
    errors: list = []
    by_category: dict = {}

    def on_movie(index: int, data: dict | None, error: str | None) -> None:
        """Persists each finished movie immediately (progressive UI)."""
        if progress and not cancel.is_set():
            progress(
                {
                    "stage": "scan",
                    "detail": error or f"scanned {data['name']}" if data else "scanning",
                    "done": index,
                    "total": None,
                    "movie": data["name"] if data else None,
                }
            )

    for path in payload_paths(payload):
        if path.is_file():
            # single movie file (per-movie action)
            from shrinkray.scanner import process_movie

            data, error = process_movie(path, path.parent, config)
            if data is not None:
                movies.append(data)
                by_category.setdefault(data["category"], []).append(data)
            else:
                errors.append(error or "unknown error")
            if on_movie is not None:
                on_movie(0, data, error)
        else:
            part_by_cat, part_movies, part_errors = scan_movies(
                path,
                config,
                workers=payload.get("workers"),
                connection=connection,
                rescan=payload.get("rescan", False),
                compute_hash=payload.get("hash", False),
                exclude=Path(payload["exclude"]) if payload.get("exclude") else None,
                on_movie=on_movie,
            )
            movies.extend(part_movies)
            errors.extend(part_errors)
            for cat, mvs in part_by_cat.items():
                by_category.setdefault(cat, []).extend(mvs)
    return {
        "movies": len(movies),
        "categories": len(by_category),
        "errors": errors,
        "cancelled": cancel.is_set(),
    }


def execute_plan(
    job: Job,
    connection: sqlite3.Connection,
    cancel: threading.Event,
    progress: ProgressCallback | None = None,
) -> dict:
    """Executes a plan job (probing only, no encodes).

    Args:
        job (Job): The job (payload: path, target_vmaf).
        connection (sqlite3.Connection): The database connection.
        cancel (threading.Event): Cooperative cancellation signal.
        progress (callable | None): Callback progress(dict) for live updates.

    Returns:
        dict: The per-movie plan summary.
    """
    from shrinkray.config import load_config
    from shrinkray.quality import plan_quality

    payload = job.payload
    config = load_config()
    apply_config(config)
    movies = resolve_movies(payload, config, connection=connection)

    plans: list[dict] = []
    total = len(movies)
    for index, movie in enumerate(movies):
        if cancel.is_set():
            plans.append({"movie": movie.name, "state": "cancelled"})
            continue
        if progress:
            progress(
                {
                    "stage": "probe",
                    "detail": f"probing {movie.name}",
                    "done": index,
                    "total": total,
                    "movie": movie.name,
                }
            )
        try:
            result = plan_quality(
                movie,
                target_vmaf=payload.get("target_vmaf", DEFAULT_TARGET_VMAF),
                encoder=config.handbrake_encoder,
                probe_encoder_preset=payload.get(
                    "probe_encoder_preset", config.handbrake_probe_encoder_preset
                ),
                encoder_preset=payload.get("encoder_preset", config.handbrake_encoder_preset),
                cancel=cancel,
                db_file=_db_file_of(connection),
                encoder_threads=cap_threads(config.handbrake_max_threads),
                progress=progress,
                min_source_height=config.min_source_height,
                min_source_size_gb=config.min_source_size_gb,
                candidate_codecs=tuple(config.candidate_codecs),
                crop_sample_count=config.crop_sample_count,
            )
        except (RuntimeError, FileNotFoundError) as e:
            plans.append({"movie": movie.name, "state": "error", "error": str(e)})
            continue
        # persist the decision for later encodes ("quicksave"): chosen CRF
        # AND skip verdicts short-circuit a future encode's probing when
        # the fingerprint matches. Best-effort: an unscanned movie has no
        # file row, the plan is simply not reusable then.
        from shrinkray.service import record_plan

        record_plan(connection, movie, result, config)
        entry = {
            "movie": movie.name,
            "state": "skip" if result.skipped_reason else "encode",
            "crf": result.chosen_crf,
            "vmaf": result.achieved_vmaf,
            "estimated_size": result.output_size,
            "saved_percent": result.saved_percent,
        }
        if result.skipped_reason:
            entry["reason"] = result.skipped_reason
        plans.append(entry)
    return {"plans": plans, "cancelled": cancel.is_set()}


def execute_encode(
    job: Job,
    connection: sqlite3.Connection,
    cancel: threading.Event,
    progress: ProgressCallback | None = None,
) -> dict:
    """Executes an encode job (quality or fixed mode).

    Args:
        job (Job): The job (payload: path, output_dir, quality, target_vmaf,
            overwrite, workers).
        connection (sqlite3.Connection): The database connection.
        cancel (threading.Event): Cooperative cancellation signal.
        progress (callable | None): Callback progress(dict) for live updates.

    Returns:
        dict: The per-movie outcome summary.
    """
    from shrinkray.config import load_config
    from shrinkray.encode import encode_movies
    from shrinkray.engine.lock import EncodeBusyError
    from shrinkray.quality import encode_with_quality
    from shrinkray.service import (
        finished_movies,
        record_fixed_results,
        record_quality_results,
    )

    payload = job.payload
    config = load_config()
    apply_config(config)
    output_dir = Path(payload["output_dir"])
    # resolve_movies (not a bare scan_movies): a payload path may be a
    # single movie FILE (per-movie action from the UI) — scanning it as a
    # directory finds nothing and the job reported done with 0 outcomes
    movies = resolve_movies(payload, config, connection=connection, exclude=output_dir)

    outcomes: list[dict] = []
    total = len(movies)
    if payload.get("quality", False):
        encoded_paths: set[str] = (
            set() if payload.get("overwrite") else finished_movies(connection, movies, output_dir)
        )
        for index, movie in enumerate(movies):
            if cancel.is_set():
                outcomes.append({"movie": movie.name, "state": "cancelled"})
                continue
            if str(movie) in encoded_paths:
                outcomes.append({"movie": movie.name, "state": "already_encoded"})
                continue
            if progress:
                progress(
                    {
                        "stage": "probe",
                        "detail": f"probing {movie.name}",
                        "done": index,
                        "total": total,
                        "movie": movie.name,
                    }
                )
            try:
                result = encode_with_quality(
                    movie,
                    output_dir / movie.name,
                    target_vmaf=payload.get("target_vmaf", DEFAULT_TARGET_VMAF),
                    encoder=config.handbrake_encoder,
                    probe_encoder_preset=payload.get(
                        "probe_encoder_preset", config.handbrake_probe_encoder_preset
                    ),
                    encoder_preset=payload.get("encoder_preset", config.handbrake_encoder_preset),
                    progress=progress,
                    cancel=cancel,
                    db_file=_db_file_of(connection),
                    encoder_threads=cap_threads(config.handbrake_max_threads),
                    verify=payload.get("verify", False),
                    min_source_height=config.min_source_height,
                    min_source_size_gb=config.min_source_size_gb,
                    candidate_codecs=tuple(config.candidate_codecs),
                    crop_sample_count=config.crop_sample_count,
                )
            except EncodeBusyError as e:
                outcomes.append({"movie": movie.name, "state": "skipped", "error": str(e)})
                continue
            outcome = record_quality_results(connection, movie, result, config)
            entry = {
                "movie": movie.name,
                "state": (
                    "encoded" if outcome.encoded else "skipped" if outcome.skipped else "failed"
                ),
                "input_size": result.input_size,
                "output_size": result.output_size,
                "crf": result.chosen_crf,
                "vmaf": result.achieved_vmaf,
                "error": result.error,
            }
            outcomes.append(entry)
            if progress:
                progress(
                    {
                        "stage": "encode",
                        "detail": f"{movie.name}: {entry['state']}",
                        "done": index + 1,
                        "total": total,
                        "movie": movie.name,
                    }
                )
    else:
        if progress:
            progress(
                {"stage": "encode", "detail": f"encoding {total} movies", "done": 0, "total": total}
            )
        try:
            results = encode_movies(
                movies,
                output_dir,
                preset=config.handbrake_preset,
                encoder=config.handbrake_encoder,
                overwrite=payload.get("overwrite", False),
                workers=payload.get("workers"),
                max_threads=cap_threads(config.handbrake_max_threads),
                progress=progress,
                db_file=_db_file_of(connection),
            )
        except EncodeBusyError as e:
            # another encoder owns the output dir (watch loop / CLI); the job
            # reports it honestly instead of clobbering
            return {
                "outcomes": [{"movie": "*", "state": "skipped", "error": str(e)}],
                "cancelled": False,
            }
        fixed = record_fixed_results(connection, results, config)
        outcomes = [
            {
                "movie": o.movie,
                "state": "encoded" if o.encoded else "failed",
            }
            for o in fixed
        ]

    return {"outcomes": outcomes, "cancelled": cancel.is_set()}


class JobWorker:
    """The background worker executing jobs from the queue, one at a time.

    Runs as a daemon thread: the process can always exit without waiting
    for long encodes (the running job's state stays 'running' in the db and
    is reported as interrupted on the next start).
    """

    def __init__(self, connection: sqlite3.Connection, queue: JobQueue) -> None:
        """Initializes the worker.

        Args:
            connection (sqlite3.Connection): The database connection the
                worker owns while executing jobs.
            queue (JobQueue): The queue to consume.
        """
        self.connection = connection
        self.queue = queue
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Starts the worker thread."""
        self._thread = threading.Thread(target=self._run, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """Signals the worker to stop after the current job and joins it.

        Args:
            join_timeout (float): Seconds to wait for the thread to finish.
        """
        self._stop.set()
        self.queue._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        """Consumes jobs until stopped."""
        while not self._stop.is_set():
            job = self.queue.take_next()
            if job is None:
                self.queue.wait_for_work(timeout=1.0)
                continue
            self._execute(job)

    def _execute(self, job: Job) -> None:
        """Runs one job and records its outcome.

        Args:
            job (Job): The job to execute.
        """
        if job.id is None:  # not persisted => programming error
            self.queue.fail(job, "job has no id")
            return
        cancel = threading.Event()
        self.queue.register_cancel_event(job.id, cancel)

        def progress(data: dict) -> None:
            """Persists live progress for the API/UI (worker thread only)."""
            if not cancel.is_set():
                self.queue.report_progress(job, data)

        try:
            if job.kind == JobKind.SCAN:
                result = execute_scan(job, self.connection, cancel, progress=progress)
            elif job.kind == JobKind.PLAN:
                result = execute_plan(job, self.connection, cancel, progress=progress)
            elif job.kind == JobKind.ENCODE:
                result = execute_encode(job, self.connection, cancel, progress=progress)
            else:
                self.queue.fail(job, f"unknown job kind: {job.kind}")
                return
            if result.get("cancelled"):
                # a graceful cancel carries the engine's partial result —
                # the UI renders outcomes on cancelled encode rows
                self.queue.mark_cancelled(job, result)
            else:
                self.queue.finish(job, result)
        except Cancelled:
            # the engine raised between checkpoints (subprocess terminated);
            # partial probe/output artifacts are cleaned by the engine
            self.queue.mark_cancelled(job)
        except Exception as e:
            from shrinkray.telemetry import save_failure

            save_failure(
                db_file=_db_file_of(self.connection),
                kind=f"job-{job.kind.value}",
                context={"job_id": job.id, "payload": job.payload},
            )
            self.queue.fail(job, f"{type(e).__name__}: {e}")
        finally:
            self.queue.unregister_cancel_event(job.id)
