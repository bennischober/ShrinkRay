"""The job layer of the resident app: queue, states and persistence.

A job is one unit of work the resident app executes (a scan, a plan, an
encode run, a watch pass). Jobs are persisted in the database so their
history and state survive restarts; the worker thread executes one job at
a time (heavy encodes do not profit from queueing more in parallel), and an
encode job works through its files one at a time.

Thread discipline: the worker thread owns the database connection while a
job runs; the queue itself is protected by a lock and both the API layer
and the worker talk to it through JobQueue's methods only.
"""

import contextlib
import json
import queue
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from shrinkray.config import Conf, encode_window_open, refresh_config
from shrinkray.engine.pipeline import cap_threads
from shrinkray.logger import file_scope, log
from shrinkray.replace import STAGING_DIR_PREFIX
from shrinkray.tools.process import Cancelled, apply_config, apply_reloaded_config

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
    DECISION = "decision"
    CRF_SEARCH = "crf-search"
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

    def stage(self, kind: JobKind, payload: dict) -> Job:
        """Records one per-file stage row a running folder job executes.

        The folder job stays the runner (issue #602): the decision, the CRF
        search and the encode of one file are rows of their own so the Jobs
        page can follow each stage, but the worker already holds the folder
        job - the row is written RUNNING directly instead of being queued
        and picked up a second time.

        Args:
            kind (JobKind): The stage's kind (decision, crf-search, encode).
            payload (dict): The stage's payload (the file it is about).

        Returns:
            Job: The stage row, with its database id set.
        """
        job = Job(kind=kind, payload=payload, state=JobState.RUNNING)
        cursor = self._connection.execute(
            "INSERT INTO jobs (kind, payload, state, started_at) VALUES (?, ?, ?, datetime('now'))",
            (kind.value, json.dumps(payload), JobState.RUNNING.value),
        )
        self._connection.commit()
        job.id = int(cursor.lastrowid or 0)
        return job

    def finish_stage(self, job: Job, result: dict) -> None:
        """Marks a per-file stage row done with its outcome.

        Args:
            job (Job): The stage row from stage().
            result (dict): The stage's outcome (JSON-able).
        """
        self.finish(job, result)

    def fail_stage(self, job: Job, error: str) -> None:
        """Marks a per-file stage row failed with its reason.

        Args:
            job (Job): The stage row from stage().
            error (str): The failure message.
        """
        self.fail(job, error)

    def take_next(self, only_kinds: set[JobKind] | None = None) -> Job | None:
        """Returns the next queued job and marks it running.

        Args:
            only_kinds (set[JobKind] | None): When set, only jobs of these
                kinds are taken; other queued jobs stay queued (issue #434's
                encode window holds plan/encode work while scan proceeds).

        Returns:
            Job | None: The job, or None if the queue is empty (or holds no
                job of the requested kinds).
        """
        with self._lock:
            if not self._pending:
                return None
            index = None
            for i, job in enumerate(self._pending):
                if only_kinds is None or job.kind in only_kinds:
                    index = i
                    break
            if index is None:
                return None
            job = self._pending.pop(index)
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
    db_file: Path | None = None,
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
        db_file (Path | None): The database file, read per movie to project
            an existing timeline crop summary into the scan's flag (issue
            #181); None derives it from the connection.

    Returns:
        list[Path]: The movie files.
    """
    from shrinkray.engine.scan import scan_movies

    movies: list[Path] = []
    seen: set[Path] = set()
    for path in payload_paths(payload):
        if path.is_file():
            # single movie file (per-movie action) => pass through directly
            candidates = [path]
        else:
            # the crop-cache projection needs the db FILE: the connection may
            # be None here (a stateless scan), and _db_file_of needs one
            derived = db_file if db_file is not None else None
            if derived is None and connection is not None:
                derived = _db_file_of(connection)
            _, part_movies, _ = scan_movies(
                path,
                config,
                connection=connection,
                exclude=exclude,
                db_file=derived,
            )
            candidates = part_movies
        for movie in candidates:
            # overlapping payload paths (a root and a subfolder of it both
            # named) scan the same file twice: the second sighting is a
            # duplicate that would encode twice (the multi-root review case
            # on PR #389 found b.mkv three times in one payload)
            key = movie.resolve()
            if key in seen:
                continue
            seen.add(key)
            movies.append(movie)
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


def _hold_while_paused(
    connection: sqlite3.Connection,
    cancel: threading.Event,
    progress: ProgressCallback | None,
    movie: Path,
) -> None:
    """Holds a plan or encode job before its next movie while paused.

    The operator's pause and a manual encode (issue #250) stop the next file,
    never the running one; the job resumes where it stood.

    Args:
        connection (sqlite3.Connection): The job's connection (its database
            folder holds the pause state).
        cancel (threading.Event): A cancel ends the wait.
        progress (ProgressCallback | None): Told why the job waits.
        movie (Path): The movie that waits.
    """
    from shrinkray.engine.pause import wait_while_paused

    def report(reason: str) -> None:
        if progress:
            progress({"stage": "paused", "detail": reason, "movie": movie.name})

    wait_while_paused(_db_file_of(connection).resolve().parent, cancel, on_pause=report)


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

    # the category roots: the resident app's allowed roots - the same roots
    # the watcher scans (app.allowed_roots; the lazy import keeps the
    # module-level import direction app -> jobs acyclic)
    from shrinkray.server.app import allowed_roots

    roots = [root for root in allowed_roots() if root.exists()]

    for path in payload_paths(payload):
        if path.is_file():
            # single movie file (per-movie action). RECORD it the way the
            # watch path records (issue #489): process_movie computes, it
            # never writes - the row goes through scan_movies's write-back
            # (the same recording the directory branch and the encode job's
            # payload scan use). The category root is the MOST SPECIFIC
            # allowed root containing the file - a rule that depends only on
            # the folder structure, never on the roots' submission order. A
            # file outside every root keeps parent-as-root, this branch's
            # original and documented CLI semantics (scan.resolve_input_paths).
            from shrinkray.engine.scan import scan_movies

            resolved = path.resolve()
            scan_root = max(
                (root for root in roots if resolved.is_relative_to(root)),
                key=lambda r: len(r.parts),
                default=path.parent,
            )
            part_by_cat, part_movies, part_errors = scan_movies(
                scan_root,
                config,
                workers=payload.get("workers"),
                connection=connection,
                rescan=payload.get("rescan", False),
                compute_hash=payload.get("hash", False),
                exclude=Path(payload["exclude"]) if payload.get("exclude") else None,
                on_movie=on_movie,
                files=[resolved],
                walked=False,
                db_file=_db_file_of(connection),
            )
            movies.extend(part_movies)
            errors.extend(part_errors)
            for cat, mvs in part_by_cat.items():
                by_category.setdefault(cat, []).extend(mvs)
            # on_movie is scan_movies's callback now: the per-movie progress
            # fires inside it (the old branch's own call referenced the
            # removed data/error locals)
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
                db_file=_db_file_of(connection),
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
    queue: "JobQueue | None" = None,
) -> dict:
    """Executes a plan job (probing only, no encodes).

    Args:
        job (Job): The job (payload: path, target_vmaf).
        connection (sqlite3.Connection): The database connection.
        cancel (threading.Event): Cooperative cancellation signal.
        progress (callable | None): Callback progress(dict) for live updates.
        queue (JobQueue | None): The queue that records the per-file stage
            rows (issue #602); None for a direct executor call.

    Returns:
        dict: The per-movie plan summary.
    """
    from shrinkray.config import load_config, refresh_config
    from shrinkray.engine.plan import (
        decide_quality,
        plan_quality,
        search_quality,
        skipped_result,
    )

    payload = job.payload
    config = load_config()
    apply_config(config)
    movies = resolve_movies(payload, config, connection=connection, db_file=_db_file_of(connection))

    plans: list[dict] = []
    total = len(movies)
    for index, movie in enumerate(movies):
        if cancel.is_set():
            plans.append({"movie": movie.name, "state": "cancelled"})
            continue
        _hold_while_paused(connection, cancel, progress, movie)
        if cancel.is_set():
            plans.append({"movie": movie.name, "state": "cancelled"})
            continue
        # a config saved while the job runs applies from the next file on
        # (issue #247)
        config = refresh_config()
        apply_reloaded_config(config)
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
        # one precedence for every entry point (issue #526): the resident
        # job reads the same keys as the CLI - an unset count leaves density
        # (sample_every) in charge instead of the old fixed default of 3
        sample_every, sample_count, sample_seconds = config.probe_settings()
        # the per-file pipeline is its own job rows (issue #602): the
        # decision (the gates) and the CRF search are separate kinds, so the
        # Jobs page can follow each file stage by stage. This folder job stays
        # their runner; a direct executor call (the CLI, tests) has no queue
        # and keeps the single-call shape.
        if queue is not None:
            decision_row = queue.stage(
                JobKind.DECISION, {"paths": [str(movie)], "pipeline": "plan"}
            )
            try:
                # the [i/N] prefix on this file's decision lines (issue #471)
                with file_scope(index + 1, total):
                    decision = decide_quality(
                        movie,
                        min_source_height=config.min_source_height,
                        min_source_width=config.min_source_width,
                        min_source_size_gb=config.min_source_size_gb,
                        candidate_codecs=tuple(config.candidate_codecs),
                        # one precedence for every entry point (issue #526):
                        # the resident job reads the same keys as the CLI
                        sample_every=sample_every,
                        sample_count=sample_count,
                        sample_seconds=sample_seconds,
                    )
            except (RuntimeError, FileNotFoundError) as e:
                queue.fail_stage(decision_row, str(e))
                plans.append({"movie": movie.name, "state": "error", "error": str(e)})
                continue
            queue.finish_stage(
                decision_row,
                {
                    "eligible": decision.eligible,
                    "reason": decision.skipped_reason,
                    "duration": decision.duration,
                },
            )
            if not decision.eligible:
                # a gate skip is still recorded (issue #602): the skip verdict
                # rides the plans row so a later encode reuses it instead of
                # re-probing the file. Best-effort, exactly as before.
                from shrinkray.service import record_plan

                try:
                    record_plan(
                        connection,
                        movie,
                        skipped_result(decision, payload.get("target_vmaf", config.target_vmaf)),
                        config,
                    )
                except (RuntimeError, FileNotFoundError) as e:
                    plans.append({"movie": movie.name, "state": "error", "error": str(e)})
                    continue
                plans.append(
                    {
                        "movie": movie.name,
                        "state": "skip",
                        "crf": None,
                        "vmaf": None,
                        "estimated_size": 0,
                        "saved_percent": 0.0,
                        "reason": decision.skipped_reason,
                    }
                )
                continue
            search_row = queue.stage(
                JobKind.CRF_SEARCH, {"paths": [str(movie)], "pipeline": "plan"}
            )
            # the plan path's search row shows the same live progress as the
            # folder job (issue #639): the search is usually the longest stage
            plan_queue = queue
            plan_row: list[Job | None] = [search_row]

            def report_plan_search(
                data: dict,
                row_ref: list[Job | None] = plan_row,
                job_queue: JobQueue = plan_queue,
            ) -> None:
                if progress is not None:
                    progress(data)
                row = row_ref[0]
                if row is not None:
                    job_queue.report_progress(row, data)

            try:
                with file_scope(index + 1, total):
                    result = search_quality(
                        decision,
                        target_vmaf=payload.get("target_vmaf", config.target_vmaf),
                        min_saving_percent=config.min_saving_percent,
                        probe_encoder_preset=payload.get(
                            "probe_encoder_preset", config.handbrake_probe_encoder_preset
                        ),
                        encoder_preset=payload.get(
                            "encoder_preset", config.handbrake_encoder_preset
                        ),
                        cancel=cancel,
                        db_file=_db_file_of(connection),
                        encoder_threads=cap_threads(config.handbrake_max_threads),
                        progress=report_plan_search,
                        crop_sample_count=config.crop_sample_count,
                    )
            except Cancelled:
                queue.mark_cancelled(search_row)
                raise
            except (RuntimeError, FileNotFoundError) as e:
                queue.fail_stage(search_row, str(e))
                plans.append({"movie": movie.name, "state": "error", "error": str(e)})
                continue
            queue.finish_stage(
                search_row,
                {
                    "crf": result.chosen_crf,
                    "vmaf": result.achieved_vmaf,
                    "reason": result.skipped_reason,
                },
            )
        else:
            # a direct executor call (the one-shot CLI, tests): no queue, so
            # the single-call engine path keeps the old shape
            try:
                with file_scope(index + 1, total):
                    result = plan_quality(
                        movie,
                        target_vmaf=payload.get("target_vmaf", config.target_vmaf),
                        min_saving_percent=config.min_saving_percent,
                        probe_encoder_preset=payload.get(
                            "probe_encoder_preset", config.handbrake_probe_encoder_preset
                        ),
                        encoder_preset=payload.get(
                            "encoder_preset", config.handbrake_encoder_preset
                        ),
                        cancel=cancel,
                        db_file=_db_file_of(connection),
                        encoder_threads=cap_threads(config.handbrake_max_threads),
                        progress=progress,
                        min_source_height=config.min_source_height,
                        min_source_width=config.min_source_width,
                        min_source_size_gb=config.min_source_size_gb,
                        candidate_codecs=tuple(config.candidate_codecs),
                        crop_sample_count=config.crop_sample_count,
                        sample_every=sample_every,
                        sample_count=sample_count,
                        sample_seconds=sample_seconds,
                    )
            except (RuntimeError, FileNotFoundError) as e:
                plans.append({"movie": movie.name, "state": "error", "error": str(e)})
                continue
        # persist the decision for later encodes ("quicksave"): chosen CRF
        # AND skip verdicts short-circuit a future encode's probing when
        # the fingerprint matches. Best-effort: an unscanned movie has no
        # file row, the plan is simply not reusable then. The record call
        # sits INSIDE the per-movie error handling (issue #436's round-2
        # review): its identity gate raises when the file changed while the
        # run was deciding it, and one changed file is one error entry,
        # never an aborted plan job.
        from shrinkray.service import record_plan

        try:
            record_plan(connection, movie, result, config)
        except (RuntimeError, FileNotFoundError) as e:
            plans.append({"movie": movie.name, "state": "error", "error": str(e)})
            continue
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
    queue: "JobQueue | None" = None,
) -> dict:
    """Executes an encode job (the measured quality path - the only mode).

    Args:
        job (Job): The job (payload: path, output_dir, quality, target_vmaf,
            overwrite).
        connection (sqlite3.Connection): The database connection.
        cancel (threading.Event): Cooperative cancellation signal.
        progress (callable | None): Callback progress(dict) for live updates.
        queue (JobQueue | None): The queue that records the per-file stage
            rows (issue #602); None for a direct executor call.

    Returns:
        dict: The per-movie outcome summary.

    Raises:
        ValueError: The payload does not ask for the measured quality path.
    """
    from shrinkray.config import load_config

    payload = job.payload
    # One encode mode (invariant 6, issue #329): there is no unmeasured branch
    # to fall into. A payload without quality:true fails the job before any
    # staging or scan, so the worker records it FAILED where the operator sees it.
    if not payload.get("quality", False):
        raise ValueError(
            "encode payload has no quality flag: ShrinkRay encodes only "
            "through the measured quality path (issue #202/#329)"
        )
    config = load_config()
    apply_config(config)
    # an omitted output_dir is the IN-PLACE form (issue #192): the backlog's
    # payload carries no output directory, and the job crashed on the
    # unconditional index on every container boot. In place the encode lands
    # in a working directory INSIDE the scanned root - the same filesystem, so
    # the swap's two renames stay atomic - and a verified output takes the
    # source's own path, the original renamed aside and kept.
    raw_output_dir = payload.get("output_dir")
    output_dir = Path(raw_output_dir) if raw_output_dir else None
    if output_dir is None:
        first = payload_paths(payload)
        if not first:
            return {
                "outcomes": [{"movie": "*", "state": "skipped", "error": "no paths in payload"}],
                "cancelled": False,
            }
        root = first[0].parent
        # the backlog's payload names a DIRECTORY (the scanned root, e.g.
        # /media): staging inside its PARENT would create the working dir at
        # `/` in the container and the job would fail with PermissionError on
        # every boot (issue #335). The watcher stages inside roots[0] itself
        # (watch.py); the job must agree. A single-file payload keeps the
        # parent form, so its staging stays on the file's own filesystem.
        if first[0].is_dir():
            root = first[0]
    else:
        root = None
    staging: contextlib.AbstractContextManager[str] = (
        contextlib.nullcontext(str(output_dir))
        if output_dir is not None
        else tempfile.TemporaryDirectory(prefix=STAGING_DIR_PREFIX, dir=root)
    )
    with staging as staging_str:
        write_dir = Path(staging_str)
        # one payload scan per encode job (issue #374): _encode_measured
        # resolves the movies itself; the exclusion is the directory the
        # job writes into - the user's output_dir in two-directory mode,
        # the per-run staging directory in place.
        outcomes = _encode_measured(
            job,
            connection,
            cancel,
            progress,
            config,
            payload,
            output_dir=write_dir,
            in_place=output_dir is None,
            queue=queue,
        )
    return outcomes


def _encode_measured(
    job: Job,
    connection: sqlite3.Connection,
    cancel: threading.Event,
    progress: ProgressCallback | None,
    config: "Conf",
    payload: dict,
    output_dir: Path,
    in_place: bool,
    queue: "JobQueue | None" = None,
) -> dict:
    """Runs one encode job's body (the measured quality path) against write_dir.

    Resolves the payload's movies itself (issue #374): one scan per encode
    job, excluding the directory the job writes into.

    Args:
        job (Job): The folder job that runs the per-file stages.
        connection: The worker's database connection.
        cancel: Cooperative cancellation signal.
        progress: Callback progress(dict) for live updates.
        config: The config to encode with (refreshed per file inside).
        payload (dict): The job's payload.
        output_dir (Path): Where encoded files are written (a per-run staging
            directory when the job encodes in place).
        in_place (bool): True when the payload named no output directory: a
            verified output is swapped into the source's path (issue #192).

    Returns:
        dict: The per-movie outcome summary.
    """
    from shrinkray.config import refresh_config
    from shrinkray.engine.lock import EncodeBusyError, guard_encodes
    from shrinkray.engine.pipeline import encode_with_quality
    from shrinkray.engine.plan import (
        decide_quality,
        search_quality,
        skipped_result,
        stored_plan_row,
    )
    from shrinkray.service import (
        finished_movies,
        record_plan,
        record_quality_results,
        swap_encode_into_place,
    )

    # The job's ONE payload scan (issue #374): execute_encode passes no
    # movies, so the walk the caller used to throw away is gone and the one
    # that remains is the call that consumes it. The exclusion is the
    # directory the job writes into (output_dir): the user's output
    # directory in two-directory mode - its outputs must never re-enter a
    # payload scanning the same root - and the per-run staging directory in
    # place. resolve_movies (not a bare scan_movies): a payload path may be
    # a single movie FILE (per-movie action from the UI) — scanning it as a
    # directory finds nothing and the job reported done with 0 outcomes
    movies = resolve_movies(
        payload, config, connection=connection, exclude=output_dir, db_file=_db_file_of(connection)
    )

    outcomes: list[dict] = []
    total = len(movies)
    encoded_paths: set[str] = (
        set() if payload.get("overwrite") else finished_movies(connection, movies, output_dir)
    )
    for index, movie in enumerate(movies):
        if cancel.is_set():
            outcomes.append({"movie": movie.name, "state": "cancelled"})
            continue
        _hold_while_paused(connection, cancel, progress, movie)
        if cancel.is_set():
            outcomes.append({"movie": movie.name, "state": "cancelled"})
            continue
        # a config saved while the job runs applies from the next file on
        # (issue #247); the running encode keeps the settings it started with
        config = refresh_config()
        apply_reloaded_config(config)
        if str(movie) in encoded_paths:
            outcomes.append({"movie": movie.name, "state": "already_encoded"})
            continue
        # the per-file stages exist for the RESIDENT pipeline (issue #602): the
        # Jobs page shows a decision row, a CRF-search row and an encode row per
        # file. A direct executor call (the one-shot CLI, tests) has no queue
        # and keeps the single-call shape below.
        encode_row: Job | None = None
        # every stage row shows the same live progress as the folder job
        # (issue #639): a row that receives no events reads "starting..." for
        # the whole stage. The holder names the row that is running now; the
        # default argument binds it per iteration (ruff's B rules reject a direct
        # loop-variable capture in a closure).
        active_row: list[Job | None] = [None]

        def report_stage_progress(data: dict, row_ref: list[Job | None] = active_row) -> None:
            if progress is not None:
                progress(data)
            row = row_ref[0]
            if queue is not None and row is not None:
                queue.report_progress(row, data)

        sample_every, sample_count, sample_seconds = config.probe_settings()
        if queue is not None:
            decision_row = queue.stage(
                JobKind.DECISION, {"paths": [str(movie)], "pipeline": "encode"}
            )
            try:
                decision = decide_quality(
                    movie,
                    min_source_height=config.min_source_height,
                    min_source_width=config.min_source_width,
                    min_source_size_gb=config.min_source_size_gb,
                    candidate_codecs=tuple(config.candidate_codecs),
                    sample_every=sample_every,
                    sample_count=sample_count,
                    sample_seconds=sample_seconds,
                )
            except (RuntimeError, FileNotFoundError) as e:
                queue.fail_stage(decision_row, str(e))
                outcomes.append({"movie": movie.name, "state": "skipped", "error": str(e)})
                continue
            queue.finish_stage(
                decision_row,
                {
                    "eligible": decision.eligible,
                    "reason": decision.skipped_reason,
                    "duration": decision.duration,
                },
            )
            if not decision.eligible:
                # a gate skip is recorded exactly as before the split: the skip
                # verdict rides the encodes row through the same recorder
                skipped = record_quality_results(
                    connection,
                    movie,
                    skipped_result(decision, payload.get("target_vmaf", config.target_vmaf)),
                    config,
                    final_output=movie if in_place else None,
                )
                outcomes.append({"movie": movie.name, "state": "skipped", "error": skipped.error})
                continue
            search_row = queue.stage(
                JobKind.CRF_SEARCH, {"paths": [str(movie)], "pipeline": "encode"}
            )
            active_row[0] = search_row
            # a valid stored plan means the folder job's search already
            # measured these bytes and these settings (issue #602's
            # no-double-probe rule): the search stage reports the reuse
            # and the encode boundary below reuses the same plan.
            stored = stored_plan_row(
                movie,
                decision,
                target_vmaf=payload.get("target_vmaf", config.target_vmaf),
                min_saving_percent=config.min_saving_percent,
                probe_encoder_preset=payload.get(
                    "probe_encoder_preset", config.handbrake_probe_encoder_preset
                ),
                crop_sample_count=config.crop_sample_count,
                db_file=_db_file_of(connection),
            )
            if stored is not None:
                queue.finish_stage(
                    search_row,
                    {
                        "crf": stored["chosen_crf"],
                        "vmaf": stored["achieved_vmaf"],
                        "reason": stored["skipped_reason"],
                        "source": "plan",
                    },
                )
            else:
                try:
                    prediction = search_quality(
                        decision,
                        target_vmaf=payload.get("target_vmaf", config.target_vmaf),
                        min_saving_percent=config.min_saving_percent,
                        probe_encoder_preset=payload.get(
                            "probe_encoder_preset", config.handbrake_probe_encoder_preset
                        ),
                        encoder_preset=payload.get(
                            "encoder_preset", config.handbrake_encoder_preset
                        ),
                        cancel=cancel,
                        db_file=_db_file_of(connection),
                        encoder_threads=cap_threads(config.handbrake_max_threads),
                        progress=report_stage_progress,
                        crop_sample_count=config.crop_sample_count,
                    )
                except Cancelled:
                    queue.mark_cancelled(search_row)
                    raise
                except (RuntimeError, FileNotFoundError) as e:
                    queue.fail_stage(search_row, str(e))
                    outcomes.append({"movie": movie.name, "state": "skipped", "error": str(e)})
                    continue
                queue.finish_stage(
                    search_row,
                    {
                        "crf": prediction.chosen_crf,
                        "vmaf": prediction.achieved_vmaf,
                        "reason": prediction.skipped_reason,
                    },
                )
                # the prediction is recorded so the encode below REUSES it instead of
                # measuring again (issue #602); the encode boundary still
                # re-validates the gates and the fingerprint, so a stale search never
                # encodes. Best-effort like the plan job's write.
                try:
                    record_plan(connection, movie, prediction, config)
                except (RuntimeError, FileNotFoundError) as e:
                    # best-effort exactly as before the split: a failed plan write
                    # must never stop the encode (the boundary re-plans when no
                    # plan is stored), so the search row stays done and the
                    # failure is only reported
                    log(f"could not record the plan for '{movie.name}': {e}")
                if prediction.skipped_reason is not None:
                    # the search's own skip (no size gain, target unreachable,
                    # saving below the threshold) is recorded as before the split
                    skipped = record_quality_results(
                        connection,
                        movie,
                        prediction,
                        config,
                        final_output=movie if in_place else None,
                    )
                    outcomes.append(
                        {"movie": movie.name, "state": "skipped", "error": skipped.error}
                    )
                    continue
            encode_row = queue.stage(JobKind.ENCODE, {"paths": [str(movie)], "pipeline": "encode"})
            active_row[0] = encode_row
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
            # hold the encode slot on the MOVIE'S OWN DIRECTORY (in place): that
            # is where the staging copy and the swap's two renames happen, so it
            # is the one key the CLI, the watcher and this job can all agree on.
            # A library root is not an identity - a job on /media and a watcher
            # on /media/Movies take two different locks for one file (#379).
            # Two-directory mode keys on the output directory it writes into.
            # Per movie, never nested.
            lock_target = movie.parent if in_place else output_dir
            # the [i/N] prefix covers the record and swap lines too (issue
            # #471 round 1, finding 4): they are per-file lines
            with (
                file_scope(index + 1, total),
                guard_encodes(lock_target, holder="encode-job"),
            ):
                result = encode_with_quality(
                    movie,
                    output_dir / movie.name,
                    target_vmaf=payload.get("target_vmaf", config.target_vmaf),
                    min_saving_percent=config.min_saving_percent,
                    encoder=config.handbrake_encoder,
                    probe_encoder_preset=payload.get(
                        "probe_encoder_preset", config.handbrake_probe_encoder_preset
                    ),
                    encoder_preset=payload.get("encoder_preset", config.handbrake_encoder_preset),
                    progress=report_stage_progress,
                    cancel=cancel,
                    db_file=_db_file_of(connection),
                    encoder_threads=cap_threads(config.handbrake_max_threads),
                    min_source_height=config.min_source_height,
                    min_source_width=config.min_source_width,
                    min_source_size_gb=config.min_source_size_gb,
                    candidate_codecs=tuple(config.candidate_codecs),
                    crop_sample_count=config.crop_sample_count,
                    sample_every=sample_every,
                    sample_count=sample_count,
                    sample_seconds=sample_seconds,
                    audio_languages=config.handbrake_audio_languages,
                    subtitle_languages=config.handbrake_subtitle_languages,
                )
                # the ledger write and the swap stay INSIDE the slot
                # (issue #230): stage() copies into the deterministic
                # '<source>.shrinkray-staging' and swap() renames it over
                # the source - unguarded, a concurrent writer keyed on the
                # same root could interleave both writers on one path
                outcome = record_quality_results(
                    connection,
                    movie,
                    result,
                    config,
                    # in place the artifact's real location is the source's own
                    # path once the swap succeeds (issue #232); the row records it
                    # up front and a DECLINED swap clears it, because the encode is
                    # then discarded with its staging directory
                    final_output=movie if in_place else None,
                )
                if in_place and outcome.encoded:
                    swap = swap_encode_into_place(
                        connection,
                        movie,
                        output_dir / movie.name,
                        outcome.encode_id,
                        # a verification finding (issue #240: a lost track, an
                        # unverifiable probe) declines the swap
                        error=result.error,
                        # a source that changed during the encode declines it
                        # too (issue #585): the run identity is the swap's
                        # last comparison before the library
                        input_size=result.input_size,
                        input_mtime=getattr(result, "input_mtime", None),
                    )
                    declined = not swap.replaced
                else:
                    declined = False
        except EncodeBusyError as e:
            # a busy slot is routine contention, not a failure (issue #602):
            # the row reads done with the skip, like the folder outcome
            if queue is not None and encode_row is not None:
                queue.finish_stage(encode_row, {"skipped": True, "error": str(e)})
            outcomes.append({"movie": movie.name, "state": "skipped", "error": str(e)})
            continue
        except Cancelled:
            # a stop must not leave the stage row running (issue #602): the
            # worker marks the folder job cancelled, this row too
            if queue is not None and encode_row is not None:
                queue.mark_cancelled(encode_row)
            raise
        except Exception as e:
            # an unexpected failure must not leave the stage row RUNNING: the
            # folder job fails too, and the row carries the same reason
            if queue is not None and encode_row is not None:
                queue.fail_stage(encode_row, str(e))
            raise
        if queue is not None and encode_row is not None:
            queue.finish_stage(
                encode_row,
                {
                    "encoded": outcome.encoded,
                    "declined": declined,
                    "error": outcome.error,
                },
            )
        entry = {
            "movie": movie.name,
            "state": (
                "skipped"
                if declined
                else "encoded"
                if outcome.encoded
                else "skipped"
                if outcome.skipped
                else "failed"
            ),
            "input_size": result.input_size,
            "output_size": None if declined else result.output_size,
            "crf": result.chosen_crf,
            "vmaf": result.achieved_vmaf,
            "error": (
                f"the swap was declined: {swap.reason} (the encode is discarded)"
                if declined
                else result.error
            ),
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
        # the running job's cancel event: a shutdown must terminate the
        # encode child, not leave it running unattended (issue #431
        # review, round 2 finding 1 - the measurement path has opted out of
        # the stall watchdog, so the cancel event is its only kill path)
        self._running_cancel: threading.Event | None = None

    def start(self) -> None:
        """Starts the worker thread."""
        self._thread = threading.Thread(target=self._run, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 30.0) -> None:
        """Signals the worker to stop and joins it.

        The running job's cancel event is set too (issue #431): a shutdown
        must terminate the running encode child, not wait out a
        multi-hour measurement the watchdog no longer bounds. The default
        join covers the child's kill window (terminate_tree's 5 s SIGTERM +
        5 s SIGKILL and the pipe drainers' joins) plus the job's unwinding,
        so the caller closing the database afterwards does not race the
        CANCELLED write (review round 3, finding 2).

        Args:
            join_timeout (float): Seconds to wait for the thread to finish.
        """
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        if (
            self._thread is not None
            and self._thread.is_alive()
            and self._running_cancel is not None
        ):
            # the worker took a job but had not published its event yet
            # (review round 4, finding 3): set whatever it published since,
            # then wait out the unwind - the caller closes the database
            # right after this returns, and the CANCELLED write must not
            # race it (review round 5, finding 3)
            self._running_cancel.set()
            self._thread.join(timeout=join_timeout)

    def request_stop(self) -> None:
        """Signals the worker to stop without joining it.

        The non-blocking half of stop() (issue #431, review round 6,
        finding 2): the server's exit hook runs in signal context, where a
        join may not run at all, so arming and joining must be separate
        moves. Sets the same flags stop() sets - the stop event, the running
        job's cancel event (the only kill path the watchdog-opted-out
        measurement child has) and the queue's wake - and returns; the
        worker ends itself and its child on those flags.
        """
        self._stop.set()
        if self._running_cancel is not None:
            self._running_cancel.set()
        self.queue._wake.set()

    def _run(self) -> None:
        """Consumes jobs until stopped."""
        while not self._stop.is_set():
            # the cancel event exists before the job is taken (review round 3,
            # finding 3): a stop landing between take_next and _execute's
            # publish would otherwise miss a multi-hour job entirely
            cancel = threading.Event()
            self._running_cancel = cancel
            # the daily encode window (issue #434): plan and encode jobs are
            # held back while outside it - scan jobs (cheap cataloging)
            # proceed, and an in-flight job always runs to completion. The
            # config is re-read each tick, so the queue resumes when the
            # window opens.
            conf = refresh_config()
            if not encode_window_open(conf.encode_window_start, conf.encode_window_end):
                job = self.queue.take_next(only_kinds={JobKind.SCAN})
            else:
                job = self.queue.take_next()
            if job is None:
                self._running_cancel = None
                self.queue.wait_for_work(timeout=1.0)
                continue
            self._execute(job, cancel)

    def _execute(self, job: Job, cancel: threading.Event | None = None) -> None:
        """Runs one job and records its outcome.

        Args:
            job (Job): The job to execute.
            cancel (threading.Event | None): The runner's cancel event for
                this job (issue #431); None creates one (direct _execute
                callers in tests).
        """
        if job.id is None:  # not persisted => programming error
            self.queue.fail(job, "job has no id")
            return
        if cancel is None:
            cancel = threading.Event()
        self._running_cancel = cancel
        self.queue.register_cancel_event(job.id, cancel)
        # the transcript's phase boundaries (issue #471): a job that runs for
        # hours between two progress lines is invisible in docker logs
        # without these
        started = time.perf_counter()
        log(f"Job #{job.id} ({job.kind.value}) started")
        result: dict | None = None
        failure: str | None = None
        cancelled = False

        def progress(data: dict) -> None:
            """Persists live progress for the API/UI (worker thread only)."""
            if not cancel.is_set():
                self.queue.report_progress(job, data)

        try:
            if job.kind == JobKind.SCAN:
                result = execute_scan(job, self.connection, cancel, progress=progress)
            elif job.kind == JobKind.PLAN:
                result = execute_plan(
                    job, self.connection, cancel, progress=progress, queue=self.queue
                )
            elif job.kind == JobKind.ENCODE:
                result = execute_encode(
                    job, self.connection, cancel, progress=progress, queue=self.queue
                )
            else:
                self.queue.fail(job, f"unknown job kind: {job.kind}")
                return
            if result.get("cancelled"):
                # a graceful cancel carries the engine's partial result —
                # the UI renders outcomes on cancelled encode rows
                cancelled = True
                self.queue.mark_cancelled(job, result)
            else:
                self.queue.finish(job, result)
        except Cancelled:
            # the engine raised between checkpoints (subprocess terminated);
            # partial probe/output artifacts are cleaned by the engine
            cancelled = True
            self.queue.mark_cancelled(job)
        except Exception as e:
            from shrinkray.telemetry import save_failure

            save_failure(
                db_file=_db_file_of(self.connection),
                kind=f"job-{job.kind.value}",
                context={"job_id": job.id, "payload": job.payload},
            )
            self.queue.fail(job, f"{type(e).__name__}: {e}")
            failure = f"{type(e).__name__}: {e}"
        finally:
            self._running_cancel = None
            self.queue.unregister_cancel_event(job.id)
            # one end line per job, whatever the outcome (issue #471): the
            # duration is measured, never subtracted from console stamps; the
            # line carries the outcome counts and, on failure, the error - a
            # failed job's transcript must not read like success
            log(
                f"Job #{job.id} ({job.kind.value}) finished in "
                f"{time.perf_counter() - started:.0f}s"
                + (f" - {self._outcome_counts(result)}" if result else "")
                + (", cancelled" if cancelled else "")
                + (f" - FAILED: {failure}" if failure else "")
            )

    @staticmethod
    def _outcome_counts(result: dict) -> str:
        """Counts the result's per-item outcomes for the job end line.

        Args:
            result (dict): The executor's result dict.

        Returns:
            str: A short summary like ``6 to encode, 2 skipped, 0 failed``.
        """
        items = result.get("plans") or result.get("outcomes") or []
        if not items:
            movies = result.get("movies")
            errors = result.get("errors") or []
            if movies is not None:
                return f"{movies} scanned, {len(errors)} errors"
            return ""
        encode_like = sum(
            1 for x in items if x.get("state") in ("encode", "encoded", "already_encoded")
        )
        skipped = sum(1 for x in items if x.get("state") in ("skip", "skipped"))
        failed = sum(1 for x in items if x.get("state") in ("error", "failed"))
        cancelled = sum(1 for x in items if x.get("state") == "cancelled")
        if "plans" in result:
            summary = f"{encode_like} to encode, {skipped} skipped, {failed} failed"
        else:
            summary = f"{encode_like} encoded, {skipped} skipped, {failed} failed"
        return summary + (f", {cancelled} cancelled" if cancelled else "")
