"""The resident application: HTTP API over the engine and job queue.

The app owns long-running work (the job queue and the watch loop); the CLI
stays fully functional without it. Consumers (web UI, curl, scripts) talk
to the local HTTP API — same engine, same database, same guardrails.

Run with `shrinkray app`.
"""

import contextlib
import hmac
import json
import os
import os.path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from shrinkray import __version__, db
from shrinkray.config import ENCODER_PRESETS, Conf, load_config
from shrinkray.encode import cap_threads
from shrinkray.engine.watch import watch
from shrinkray.jobs import Job, JobKind, JobQueue, JobWorker, ProgressBus
from shrinkray.logger import log, log_error
from shrinkray.quality import DEFAULT_TARGET_VMAF, HDR_ESTIMATE_NOTE
from shrinkray.report import generate_report
from shrinkray.tools.process import apply_config

app = FastAPI(title="ShrinkRay", version=__version__)

# the shared-secret token's environment variable (issue #36). Never read from
# config.json: that file is tracked, and a token in it would be committed.
AUTH_TOKEN_ENV = "SHRINKRAY_TOKEN"

_T = TypeVar("_T")


def _configured_token() -> str | None:
    """Returns the operator's bearer token, or None when auth is off.

    Auth is opt-in: with no token set the dashboard is unauthenticated, which
    is the loopback development flow. `run_app` refuses a non-loopback bind
    without one, so "off" can never be a remotely reachable state.

    Returns:
        str | None: The token from $SHRINKRAY_TOKEN, or None.
    """
    token = os.environ.get(AUTH_TOKEN_ENV, "")
    return token or None


def _is_loopback(host: str) -> bool:
    """Whether a bind address is reachable only from this machine.

    Args:
        host (str): The requested bind address.

    Returns:
        bool: True for a loopback address.
    """
    return host in ("127.0.0.1", "::1", "localhost")


@app.middleware("http")
async def require_token(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Rejects an unauthenticated request when a token is configured.

    A middleware rather than a per-route dependency: the point is that a NEW
    route cannot be added unauthenticated by forgetting a decorator. The SSE
    stream and the static UI are covered for the same reason.

    The comparison is constant-time (`hmac.compare_digest`) so a wrong token
    leaks nothing through timing, and the response never echoes the token.

    Args:
        request: The incoming request.
        call_next: The next middleware/handler.

    Returns:
        The response, or a 401 when the token is missing or wrong.
    """
    token = _configured_token()
    if token is None:
        return await call_next(request)
    presented = request.headers.get("authorization", "")
    prefix = "Bearer "
    if presented.startswith(prefix) and hmac.compare_digest(presented[len(prefix) :], token):
        return await call_next(request)
    # EventSource cannot set a request header, so the SSE stream passes the
    # token as a query parameter instead. It is accepted only here: a token in
    # a URL is logged by proxies, so every other route requires the header.
    query_token = request.query_params.get("token")
    if (
        request.url.path == "/api/events"
        and query_token is not None
        and hmac.compare_digest(query_token, token)
    ):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "missing or invalid bearer token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Logs every request with duration and status (the app's access log).

    Args:
        request: The incoming request.
        call_next: The next middleware/handler.

    Returns:
        The response.
    """
    import time as _time

    start = _time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:
        duration = (_time.perf_counter() - start) * 1000
        log_error(
            f"{request.method} {request.url.path} crashed after {duration:.0f}ms: "
            f"{type(e).__name__}: {e}"
        )
        raise
    duration = (_time.perf_counter() - start) * 1000
    if request.url.path.startswith("/api"):
        log(f"{request.method} {request.url.path} -> {response.status_code} ({duration:.0f}ms)")
    return response


# paths the API may operate on. Requests can only address folders inside
# these roots (plus the roots themselves) => the API cannot be tricked into
# scanning, writing or exposing arbitrary filesystem locations. Set via
# allow_roots() at startup, derived from the database location by default.
_allowed_roots: list[Path] = []


def allow_roots(roots: list[Path]) -> None:
    """Sets the folder roots the API may operate on.

    Args:
        roots (list[Path]): The allowed root folders (absolute paths).
    """
    global _allowed_roots
    _allowed_roots = [root.resolve() for root in roots]


def validate_path(path_str: str, what: str = "path") -> Path:
    """Validates a user-provided path against the allowed roots.

    A path is allowed when it is inside one of the allowed roots (or equal
    to one). Relative paths and paths that escape a root via '..' are
    rejected.

    Args:
        path_str (str): The user-provided path.
        what (str): Name of the parameter (for the error message).

    Returns:
        Path: The resolved absolute path.

    Raises:
        HTTPException: If the path is not inside an allowed root.
    """
    if not path_str or not path_str.strip():
        raise HTTPException(status_code=400, detail=f"{what} is empty")
    try:
        # realpath normalizes away '..' segments and symlinks
        normalized = os.path.realpath(path_str)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid {what}: {e}") from e

    for root in _allowed_roots:
        # CodeQL-recognized containment: the realpath must be the root
        # itself or a child of it (root with trailing separator)
        # strip the trailing separator: a drive root ('D:\\') already ends
        # in one, and appending os.sep would require a double separator
        root_str = os.path.realpath(str(root)).rstrip(os.sep)
        if normalized == root_str or normalized.startswith(root_str + os.sep):
            return Path(normalized)
    raise HTTPException(
        status_code=403,
        detail=f"{what} is outside the allowed roots: {_allowed_roots}",
    )


class RevealRequest(BaseModel):
    """Request body for the reveal endpoint.

    Attributes:
        path (str): The file whose containing folder should be revealed.
    """

    path: str


class JobRequest(BaseModel):
    """Request body for creating a job.

    Attributes:
        kind (JobKind): What to do (scan/plan/encode).
        path (str): The movie folder the job operates on.
        paths (list[str] | None): Additional movie folders (multi-directory
            support; merged with path).
        output_dir (str | None): Output directory (encode only).
        quality (bool): Use VMAF-driven quality mode (encode/plan).
        target_vmaf (float): The VMAF quality floor.
        overwrite (bool): Re-encode already encoded movies.
        verify (bool): Full-file VMAF verification after each quality encode.
        workers (int | None): Concurrent encodes (fixed mode).
        encoder_preset (str | None): Quality mode's encoder speed preset
            (None = the configured value).
        rescan (bool): Ignore the scan cache.
        hash (bool): Compute content hashes while scanning.
    """

    kind: JobKind
    path: str = ""
    paths: list[str] | None = None
    output_dir: str | None = None
    quality: bool = False
    target_vmaf: float = Field(DEFAULT_TARGET_VMAF, ge=1.0, le=100.0)
    overwrite: bool = False
    verify: bool = False
    workers: int | None = Field(None, ge=1, le=64)
    probe_encoder_preset: str | None = None
    encoder_preset: str | None = None
    rescan: bool = False
    hash: bool = False


class AppState:
    """The shared state of the resident app (queue, worker, watcher).

    The sqlite connection is shared across threads (FastAPI request threads,
    the worker, the watcher). Access is serialized by a re-entrant lock:
    every read/write of the database outside the worker's own job execution
    goes through with_lock(). WAL mode keeps writers and readers from
    blocking each other across processes.
    """

    def __init__(self, db_file: Path) -> None:
        """Initializes the app state.

        Jobs left 'running' by a previous app instance are marked failed
        (interrupted): the worker is single-threaded, so a running state in
        the database without a live worker is stale by definition.

        Args:
            db_file (Path): The sqlite database file.
        """
        self.db_file = db_file
        self.config_file: Path | None = None  # the app command's --config
        self.db_lock = threading.RLock()
        self.connection = db.connect(db_file)
        self.bus = ProgressBus()
        self.queue = JobQueue(self.connection, bus=self.bus)
        self.worker = JobWorker(self.connection, self.queue)
        with self.db_lock:
            self._recover_stale_jobs()
        self.watch_thread: threading.Thread | None = None
        self.watch_stop = threading.Event()

    def with_lock(self, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        """Runs fn(connection) under the database lock.

        Args:
            fn (callable): Function receiving the connection.

        Returns:
            The function's return value.
        """
        with self.db_lock:
            return fn(self.connection)

    def _recover_stale_jobs(self) -> None:
        """Marks jobs left running by a previous app instance as failed.

        The result column stays NULL (live progress was never persisted):
        the row becomes a plain failed-trail entry.
        """
        self.connection.execute(
            "UPDATE jobs SET state = ?, error = ?, result = NULL, "
            "finished_at = datetime('now') WHERE state = ?",
            (
                "failed",
                "interrupted by app restart",
                "running",
            ),
        )
        self.connection.commit()

    def shutdown(self) -> None:
        """Stops the worker and closes the database."""
        self.worker.stop()
        self.watch_stop.set()
        if self.watch_thread is not None:
            self.watch_thread.join(timeout=10)
        self.connection.close()


state: AppState | None = None


def get_state() -> AppState:
    """Returns the app state (initialized at startup).

    Returns:
        AppState: The app state.

    Raises:
        HTTPException: If the state is not initialized.
    """
    if state is None:
        raise HTTPException(status_code=503, detail="app not initialized")
    return state


def db_access() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency providing the app's database connection.

    Locking happens in the handlers (s.with_lock) so that acquire, work and
    release run on the same worker thread — a generator-yielded lock here
    would be released by a different threadpool thread than it was acquired
    on (the 'cannot release un-acquired lock' crash).

    Yields:
        sqlite3.Connection: The app's connection (unlocked).
    """
    s = get_state()
    yield s.connection


@app.get("/")
def index() -> Response:
    """Serves the built web UI.

    Returns:
        FileResponse: The UI's index.html (or a hint when not built).
    """
    # the built UI lives at the repo root (dev: ui/dist; the app resolves it
    # relative to the repository root so a checkout layout works everywhere)
    index_path = _ui_dist() / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="UI not built. Run: npm install && npm run build in ui/",
        )
    # no-cache, not no-store: the entry revalidates on every load so a
    # rebuilt UI reaches an open tab with a plain reload. The app is
    # resident — its UI updates underneath the tab (fixes, rebuilds) —
    # and a user should never need a hard-refresh ritual to see the
    # current build. Hashed assets under /assets are immutable (below),
    # so revalidation is one cheap 304, not a refetch.
    return FileResponse(index_path, headers={"Cache-Control": "no-cache"})


def _ui_dist() -> Path:
    """Resolves the built UI directory (repo-root ui/dist).

    Returns:
        Path: The dist directory (may not exist if the UI is not built).
    """
    # dev/checkout layout: repo root is three levels above this module
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "ui" / "dist"


@app.get("/assets/{asset_path:path}")
def ui_asset(asset_path: str) -> Response:
    """Serves the built UI's static assets.

    The asset path is contained to the build output: traversal attempts
    ('..', absolute paths, drive letters) resolve outside dist/ and are
    rejected.

    Args:
        asset_path (str): The asset file path within the build output.

    Returns:
        FileResponse: The asset file.
    """
    dist = os.path.realpath(str(_ui_dist() / "assets"))
    # realpath normalizes away '..' segments => the result must stay a child
    # of the assets directory, otherwise the path escapes the build output
    normalized = os.path.realpath(os.path.join(dist, asset_path))
    if not (normalized == dist or normalized.startswith(dist + os.sep)):
        raise HTTPException(status_code=404, detail="asset not found")
    if not os.path.isfile(normalized):
        raise HTTPException(status_code=404, detail="asset not found")
    # Vite fingerprints asset filenames with a content hash, so a new build
    # produces new URLs; content at a given URL never changes. immutable
    # lets the browser skip revalidation entirely — the entry page (no-cache)
    # is the single point of freshness.
    return FileResponse(
        normalized, headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )


def get_config() -> dict:
    """Returns the current configuration (as stored in config.json).

    Returns:
        dict: The configuration data.
    """
    s = get_state()
    config_path = _config_path(s)
    if not config_path.is_file():
        # return the effective defaults so the UI can edit them
        from shrinkray.config import Conf

        conf = Conf()
        return _conf_to_dict(conf)
    with open(config_path, encoding="utf-8") as f:
        return cast("dict[str, Any]", json.load(f))


def _config_path(s: AppState) -> Path:
    """Resolves the config.json path for the resident app.

    One location for dashboard and engine (issue #58): the resolution
    order is the app's explicit config file, then $SHRINKRAY_CONFIG, then
    the db directory (the operator's chosen user-data location).

    Args:
        s (AppState): The app state.

    Returns:
        Path: The config.json path.
    """
    from shrinkray.config import resolve_config_path

    return resolve_config_path(
        config_file=s.config_file,
        db_dir=s.db_file.parent,
    )


def _conf_to_dict(conf: Conf) -> dict:
    """Converts a Conf object into the config.json dict shape.

    Built FROM THE SCHEMA MODEL (issue #61): every field the model has is
    returned - including settings the hand-written dict had already drifted
    past (handbrake.stall_minutes, the two encoder speed presets) - and the
    canonical key names ('separator', not the deprecated 'seperator').

    Args:
        conf (Conf): The configuration object.

    Returns:
        dict: The config.json-shaped dict.
    """
    from shrinkray.config import (
        BlackBarsSection,
        CodecSection,
        ConfigModel,
        EligibilitySection,
        FfmpegSection,
        FileSection,
        HandbrakeSection,
        HdrSection,
        OutputSection,
        QualitySection,
        ResourcesSection,
    )

    model = ConfigModel(
        file=FileSection(
            extensions=conf.extensions,
            output=OutputSection(separator=conf.separator),
        ),
        ffmpeg=FfmpegSection(
            black_bars=BlackBarsSection(
                MIN_WIDTH=conf.black_bar_min_width,
                MIN_HEIGHT=conf.black_bar_min_height,
            ),
            crop_sample_count=conf.crop_sample_count,
        ),
        handbrake=HandbrakeSection(
            preset=conf.handbrake_preset,
            encoder=conf.handbrake_encoder,
            probe_encoder_preset=conf.handbrake_probe_encoder_preset,
            encoder_preset=conf.handbrake_encoder_preset,
            max_threads=conf.handbrake_max_threads,
            stall_minutes=conf.handbrake_stall_minutes,
        ),
        quality=QualitySection(
            sample_every=conf.sample_every,
            sample_count=conf.sample_count,
            sample_seconds=conf.sample_seconds,
        ),
        resources=ResourcesSection(idle_priority=conf.idle_priority),
        codec=CodecSection(
            hdr=HdrSection(
                color_space=conf.hdr_color_spaces,
                color_transfer=conf.hdr_color_transfers,
            )
        ),
        eligibility=EligibilitySection(
            min_source_height=conf.min_source_height,
            min_source_size_gb=conf.min_source_size_gb,
            candidate_codecs=conf.candidate_codecs,
        ),
        language=conf.language,
    )
    return model.model_dump()


@app.get("/api/config")
def read_config() -> dict:
    """Returns the current configuration (as stored in config.json).

    Returns:
        dict: The configuration data.
    """
    return get_config()


@app.put("/api/config")
def update_config(data: dict) -> dict:
    """Updates the configuration (writes config.json).

    Validate first, write atomically (issue #60): the request body is
    validated in memory (Conf) BEFORE any file is touched — a rejected
    config never reaches the disk (previously a rejected write stayed on
    disk and every later load_config() failed). The write itself is a
    temp-file + os.replace pair: atomic on POSIX and Windows for a
    same-volume rename, so a crash or a concurrent reader never sees a
    half-written config.json.

    Args:
        data (dict): The configuration in config.json shape.

    Returns:
        dict: The saved configuration.
    """
    s = get_state()
    config_path = _config_path(s)

    # 1. validate in memory: no file is written until the config parses
    import shrinkray.config as config_module

    try:
        config_module.Conf(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}") from e

    # 2. write atomically: temp file in the same directory (same volume),
    # fsync'd, then an atomic rename over the target; no backup/restore
    # dance — a failure before the replace leaves the original untouched
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            temp.write(json.dumps(data, indent=2))
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, config_path)
    except Exception as e:
        # best effort cleanup of the temp file; the original config.json
        # was never touched (the replace either happened or did not)
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise HTTPException(status_code=500, detail=f"config write failed: {e}") from e

    # 3. swap the in-memory singleton through the public reset (issue #58)
    config_module.reset_config()
    config_module.load_config(s.db_file.parent)
    return data


@app.get("/api/retained")
def retained_list(conn: sqlite3.Connection = Depends(db_access)) -> list[dict]:
    """Lists the originals a swap retained (issue #35).

    The list is what tells the operator which files they may delete. ShrinkRay
    never removes them: the deletion is their own action, in their own file
    manager, once they are satisfied with the result.

    Args:
        conn: The database connection (db_access dependency).

    Returns:
        list[dict]: One entry per retained original, newest first.
    """
    s = get_state()
    rows = s.with_lock(lambda c: db.list_retained(c))
    return [
        {
            "source_path": row["source_path"],
            "retained_path": row["retained_path"],
            "size_before": row["size_before"],
            "size_after": row["size_after"],
            "created_at": row["created_at"],
            "present": Path(row["retained_path"]).exists(),
        }
        for row in rows
    ]


@app.get("/api/status")
def status(conn: sqlite3.Connection = Depends(db_access)) -> dict:
    """Returns the app status: queue depth, running job, watcher state.

    Args:
        conn: The database connection (db_access dependency).

    Returns:
        dict: The status summary.
    """
    s = get_state()
    running = s.with_lock(
        lambda c: [j for j in s.queue.all_jobs(limit=10) if j.state.value in ("running", "queued")]
    )
    from shrinkray.resources import rss_mib

    rss = rss_mib()
    return {
        "queue": [j.to_dict() for j in running],
        "watcher": "running" if s.watch_thread and s.watch_thread.is_alive() else "stopped",
        "database": str(s.db_file),
        "rss_mib": round(rss, 1) if rss is not None else None,
    }


@app.get("/api/jobs")
def list_jobs(limit: int = 50, conn: sqlite3.Connection = Depends(db_access)) -> list[dict]:
    """Returns the recent job history, newest first.

    Args:
        limit (int): Maximum number of jobs.
        conn: The database connection (db_access dependency).

    Returns:
        list[dict]: The jobs.
    """
    s = get_state()
    return s.with_lock(lambda c: [j.to_dict() for j in s.queue.all_jobs(limit=limit)])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, conn: sqlite3.Connection = Depends(db_access)) -> dict:
    """Returns one job by id.

    Args:
        job_id (int): The job id.
        conn: The database connection (db_access dependency).

    Returns:
        dict: The job.
    """
    s = get_state()
    job = s.with_lock(lambda c: s.queue.get(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.post("/api/jobs")
def create_job(request: JobRequest) -> dict:
    """Enqueues a new job.

    Args:
        request (JobRequest): The job parameters.

    Returns:
        dict: The created job.
    """
    s = get_state()
    if not request.path:
        raise HTTPException(status_code=400, detail="path is required for this job kind")
    path = validate_path(request.path, "path")
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"path does not exist: {path}")

    # multi-directory: merge the primary path with any additional paths
    all_paths = [path]
    for extra in request.paths or []:
        extra_path = validate_path(extra, "paths")
        if not extra_path.exists():
            raise HTTPException(status_code=400, detail=f"path does not exist: {extra_path}")
        all_paths.append(extra_path)
    # deduplicate, keep order
    seen: set[Path] = set()
    unique_paths: list[Path] = []
    for p in all_paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    payload: dict = {"paths": [str(p) for p in unique_paths]}
    if request.kind == JobKind.ENCODE:
        if not request.output_dir:
            raise HTTPException(status_code=400, detail="encode requires output_dir")
        payload["output_dir"] = str(validate_path(request.output_dir, "output_dir"))
        payload["quality"] = request.quality
        payload["target_vmaf"] = request.target_vmaf
        payload["overwrite"] = request.overwrite
        payload["workers"] = request.workers
        payload["verify"] = request.verify
        if request.encoder_preset is not None:
            # reject before enqueueing: HandBrake must never see a value
            # outside the encoder's speed-preset vocabulary (issue #31)
            if request.encoder_preset not in ENCODER_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"encoder_preset: '{request.encoder_preset}' is not a valid "
                    f"speed preset (valid: {', '.join(ENCODER_PRESETS)})",
                )
            payload["encoder_preset"] = request.encoder_preset
        if request.probe_encoder_preset is not None:
            if request.probe_encoder_preset not in ENCODER_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"probe_encoder_preset: '{request.probe_encoder_preset}' is not "
                    f"a valid speed preset (valid: {', '.join(ENCODER_PRESETS)})",
                )
            payload["probe_encoder_preset"] = request.probe_encoder_preset
    elif request.kind == JobKind.PLAN:
        payload["target_vmaf"] = request.target_vmaf
        if request.encoder_preset is not None:
            if request.encoder_preset not in ENCODER_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"encoder_preset: '{request.encoder_preset}' is not a valid "
                    f"speed preset (valid: {', '.join(ENCODER_PRESETS)})",
                )
            payload["encoder_preset"] = request.encoder_preset
        if request.probe_encoder_preset is not None:
            if request.probe_encoder_preset not in ENCODER_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"probe_encoder_preset: '{request.probe_encoder_preset}' is not "
                    f"a valid speed preset (valid: {', '.join(ENCODER_PRESETS)})",
                )
            payload["probe_encoder_preset"] = request.probe_encoder_preset
    else:  # scan
        payload["rescan"] = request.rescan
        payload["hash"] = request.hash
        payload["workers"] = request.workers

    job = s.queue.enqueue(Job(kind=request.kind, payload=payload))
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    """Cancels a queued or running job.

    Args:
        job_id (int): The job id.

    Returns:
        dict: The acknowledgement.
    """
    s = get_state()
    if not s.queue.cancel(job_id):
        raise HTTPException(status_code=404, detail="job not found or already finished")
    return {"cancelled": job_id}


# response_model=None: the handler returns either a dict payload or a 304
# Response, which is not a valid Pydantic field type. FastAPI would otherwise
# try to infer a model from the annotation and refuse to start.
@app.get("/api/snapshot", response_model=None)
def snapshot(
    jobs_limit: int = 100,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> dict | Response:
    """Returns the full UI state in one request: status, jobs and library.

    The poll endpoint: instead of three requests (status, jobs, library)
    per tick, the UI fetches everything with one round-trip and one lock
    pass. Library and job history are ETag-cached — the client sends its
    known tags as If-None-Match (comma-separated, standard list syntax)
    and unchanged collections come back as `null` (client keeps its copy)
    while status stays fresh. The jobs fingerprint covers (id, state) of
    the returned window: a job row never mutates except on a state
    transition (result/error are written with the final state), so the
    pair is a sound change signal. The three individual endpoints remain
    available (curl, scripts, tests).

    Args:
        jobs_limit (int): Maximum number of jobs in the history.
        if_none_match (str | None): The client's known ETags.

    Returns:
        dict: {"status": ..., "jobs": [...] | None, "jobs_etag": str,
        "library": [...] | None, "library_etag": str}
    """
    import hashlib

    from shrinkray.resources import rss_mib

    s = get_state()

    def read(c: sqlite3.Connection) -> dict:
        """Reads jobs, status and both fingerprints in one pass."""
        jobs = s.queue.all_jobs(limit=jobs_limit)
        running = [j for j in jobs if j.state.value in ("running", "queued")]
        fingerprint = c.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(f.rowid), 0),
                   COALESCE((SELECT MAX(id) FROM encodes), 0),
                   COALESCE((SELECT MAX(mtime) FROM files), 0),
                   COALESCE((SELECT COUNT(*) FROM files WHERE missing_scans > 0), 0)
            FROM files f
            """
        ).fetchone()
        return {
            "jobs": jobs,
            "running": running,
            "fingerprint": tuple(fingerprint),
            # (id, state) is a sound change signal: a running row never
            # mutates (live progress is flow on /api/events, the row keeps
            # result = NULL), and a terminated row is written exactly once
            # by the state transition itself.
            "jobs_fp": tuple((j.id, j.state.value) for j in jobs),
            "rss_mib": rss_mib(),
        }

    data = s.with_lock(read)

    def tag(prefix: str, value: object) -> str:
        return '"' + prefix + hashlib.md5(str(value).encode()).hexdigest()[:16] + '"'

    etag_lib = tag("lib-", data["fingerprint"])
    etag_jobs = tag("jobs-", data["jobs_fp"])
    # If-None-Match may carry a list: '"lib-x", "jobs-y"'
    sent = {t.strip() for t in (if_none_match or "").split(",") if t.strip()}
    library_unchanged = etag_lib in sent
    jobs_unchanged = etag_jobs in sent
    library = None
    if not library_unchanged:
        library = s.with_lock(
            lambda c: [
                {
                    "path": r["path"],
                    "name": r["name"],
                    "category": r["category"],
                    "duration": r["duration"],
                    "codec": r["codec"],
                    "resolution": [r["width"], r["height"]],
                    "size": r["size"],
                    "hdr": bool(r["hdr"]),
                    "missing": bool(r["missing_scans"]),
                    "missing_since": r["missing_since"],
                    "encode": {
                        "preset": r["preset"],
                        "crf": r["chosen_crf"],
                        "vmaf": r["achieved_vmaf"],
                        "input_size": r["input_size"],
                        "output_size": r["output_size"],
                        "saved_percent": r["saved_percent"],
                        "encoder_preset": r["encoder_preset"],
                        "encode_seconds": r["encode_seconds"],
                        "probe_count": r["probe_count"],
                        "probe_seconds": r["probe_seconds"],
                        "tool_versions": r["tool_versions"],
                        # generation-drift data (schema v7; NULL for legacy rows)
                        "generation": r["generation"],
                        "input_bitrate": r["input_bitrate"],
                        "output_bitrate": r["output_bitrate"],
                    }
                    if r["preset"]
                    else None,
                }
                for r in c.execute(
                    """
                    SELECT f.path, f.name, f.category, f.duration, f.codec, f.width, f.height,
                           f.size, f.hdr, f.missing_scans, f.missing_since,
                           e.preset, e.chosen_crf, e.achieved_vmaf, e.input_size, e.output_size,
                           e.saved_percent, e.encoder_preset, e.probe_encoder_preset,
                           e.encode_seconds,
                           e.probe_count, e.probe_seconds, e.tool_versions,
                           e.generation, e.input_bitrate, e.output_bitrate
                    FROM files f
                    LEFT JOIN encodes e ON e.id = (
                        SELECT id FROM encodes WHERE file_id = f.id ORDER BY id DESC LIMIT 1
                    )
                    ORDER BY f.category, f.name
                    """
                ).fetchall()
            ]
        )
    return {
        "status": {
            "queue": [j.to_dict() for j in data["running"]],
            "watcher": "running" if s.watch_thread and s.watch_thread.is_alive() else "stopped",
            "database": str(s.db_file),
            "rss_mib": round(data["rss_mib"], 1) if data["rss_mib"] is not None else None,
        },
        "jobs": None if jobs_unchanged else [j.to_dict() for j in data["jobs"]],
        "jobs_etag": etag_jobs,
        "library": library,
        "library_etag": etag_lib,
    }


@app.get("/api/events")
def events() -> StreamingResponse:
    """Streams live job progress as server-sent events.

    The one flow channel (state stays on the ETag-cached poll): each
    subscriber gets a bounded mailbox drained into SSE frames —
    ``data: {"job_id": N, ...progress}`` per engine tick, a ``hello`` frame
    seeding the latest known progress per job on connect (page load,
    reconnect), and a ping comment every 15 s to keep intermediaries from
    closing an idle connection and to detect dead tabs. The generator
    blocks on its mailbox with a timeout so the ping interleaves; the bus
    is unregistered in the finally block. No auth beyond localhost.

    Returns:
        StreamingResponse: text/event-stream; the generator runs until the
        client disconnects.
    """
    s = get_state()
    sid, box, seed = s.bus.subscribe()

    def stream() -> Iterator[str]:
        """Drains the mailbox into SSE frames until the client leaves."""
        yield from _sse_stream(s.bus, sid, box, seed)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_stream(
    bus: ProgressBus, sid: int, box: "queue.Queue[dict]", seed: list, ping_after: float = 15.0
) -> Iterator[str]:
    """Builds the SSE frame generator for one subscriber.

    Extracted from the endpoint so tests can drive the generator directly
    (an infinite stream cannot be read to completion through an HTTP test
    client without blocking forever).

    Args:
        bus (ProgressBus): The bus to unsubscribe from on exit.
        sid (int): The subscriber id.
        box (queue.Queue): The subscriber's mailbox.
        seed (list): The latest event per job at subscribe time.
        ping_after (float): Mailbox wait before emitting a ping comment.

    Yields:
        str: SSE frames (retry, hello, data, ping comments).
    """
    try:
        yield "retry: 3000\n\n"
        # always hello: authoritative seed on connect AND reconnect —
        # carries the latest event of every live job (empty after a
        # quiet stretch, which resets the client's map correctly)
        yield "event: hello\n"
        yield "data: " + json.dumps({"jobs": {str(jid): d for jid, d in seed}}) + "\n\n"
        while True:
            try:
                job_id, data = box.get(timeout=ping_after)
                yield "data: " + json.dumps({"job_id": job_id, **data}) + "\n\n"
            except queue.Empty:
                yield ": ping\n\n"
    finally:
        bus.unsubscribe(sid)


# response_model=None: the handler returns either the library payload or a 304
# Response, which is not a valid Pydantic field type.
@app.get("/api/library", response_model=None)
def library(
    conn: sqlite3.Connection = Depends(db_access),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> dict | Response:
    """Returns the scanned library from the database.

    Supports ETag-based conditional requests: the ETag is a fingerprint
    over the library contents (file count, max rowid, latest encode id,
    max mtime), so an unchanged library answers 304 without a payload -
    the UI's 3-second poll becomes nearly free.

    Args:
        conn: The database connection (db_access dependency).
        if_none_match (str | None): The client's If-None-Match header value.

    Returns:
        Response: JSON list, or 304 Not Modified when unchanged.
    """
    import hashlib

    s = get_state()
    fingerprint = s.with_lock(
        lambda c: c.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(f.rowid), 0),
                   COALESCE((SELECT MAX(id) FROM encodes), 0),
                   COALESCE((SELECT MAX(mtime) FROM files), 0),
                   COALESCE((SELECT COUNT(*) FROM files WHERE missing_scans > 0), 0)
            FROM files f
            """
        ).fetchone()
    )
    etag = '"lib-' + hashlib.md5(str(tuple(fingerprint)).encode()).hexdigest()[:16] + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    rows = s.with_lock(
        lambda c: c.execute(
            """
            SELECT f.path, f.name, f.category, f.duration, f.codec, f.width, f.height,
                   f.size, f.hdr, f.missing_scans, f.missing_since,
                   e.preset, e.chosen_crf, e.achieved_vmaf, e.input_size, e.output_size,
                   e.saved_percent, e.encoder_preset, e.probe_encoder_preset,
                   e.encode_seconds,
                   e.probe_count, e.probe_seconds, e.tool_versions
            FROM files f
            LEFT JOIN encodes e ON e.id = (
                SELECT id FROM encodes WHERE file_id = f.id ORDER BY id DESC LIMIT 1
            )
            ORDER BY f.category, f.name
            """
        ).fetchall()
    )
    body = [
        {
            "path": r["path"],
            "name": r["name"],
            "category": r["category"],
            "duration": r["duration"],
            "codec": r["codec"],
            "resolution": [r["width"], r["height"]],
            "size": r["size"],
            "hdr": bool(r["hdr"]),
            "missing": bool(r["missing_scans"]),
            "missing_since": r["missing_since"],
            "encode": {
                "preset": r["preset"],
                "crf": r["chosen_crf"],
                "vmaf": r["achieved_vmaf"],
                "input_size": r["input_size"],
                "output_size": r["output_size"],
                "saved_percent": r["saved_percent"],
                "encoder_preset": r["encoder_preset"],
                "probe_encoder_preset": r["probe_encoder_preset"],
                "encode_seconds": r["encode_seconds"],
                "probe_count": r["probe_count"],
                "probe_seconds": r["probe_seconds"],
                "tool_versions": r["tool_versions"],
            }
            if r["preset"]
            else None,
        }
        for r in rows
    ]
    return JSONResponse(body, headers={"ETag": etag})


@app.get("/api/movie")
def movie_detail(path: str, conn: sqlite3.Connection = Depends(db_access)) -> dict:
    """Returns one movie's full record: file facts plus its COMPLETE encode
    history (the library list only carries the latest encode per title)
    and the stored plan (the prediction display's data, with a file-level
    staleness flag).

    History rows carry the schema-v7 provenance chain (generation,
    input/output bitrate, chain links) so the UI can show generation
    drift per title.

    Args:
        path (str): The movie file's path (allowlist-checked).
        conn: The database connection (db_access dependency).

    Returns:
        dict: {"file": {..., "generation": N}, "history": [one entry per
        encode touching this title — its own rows plus the chain rows whose
        output produced it or whose input it produced — in generation
        order, ties by id], "plan": {crf, achieved_vmaf, skipped_reason,
        estimated_size, created_at, encoder, encoder_preset, target_vmaf,
        stale} | None} — 404 when the path is unknown to the database.
    """
    p = validate_path(path, "path")
    s = get_state()
    # one lock pass for both reads — the snapshot's discipline applies to
    # every endpoint: a write between two separate reads would hand back a
    # file row and an encode history that never coexisted
    with s.db_lock:
        row = s.connection.execute(
            """
            SELECT f.id, f.path, f.name, f.category, f.duration, f.codec,
                   f.width, f.height, f.size, f.hdr, f.missing_scans,
                   f.missing_since
            FROM files f WHERE f.path = ?
            """,
            (str(p),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="movie not found in the library")
        history = [
            {
                "preset": r["preset"],
                "crf": r["chosen_crf"],
                "vmaf": r["achieved_vmaf"],
                "input_size": r["input_size"],
                "output_size": r["output_size"],
                "saved_percent": r["saved_percent"],
                "encoder_preset": r["encoder_preset"],
                "probe_encoder_preset": r["probe_encoder_preset"],
                "encode_seconds": r["encode_seconds"],
                "probe_count": r["probe_count"],
                "probe_seconds": r["probe_seconds"],
                "tool_versions": r["tool_versions"],
                "id": r["id"],
                # which library file the row belongs to (the chain crosses
                # files: a row for the parent encode may appear here)
                "file_path": r["file_path"],
                "file_name": r["file_name"],
                # schema v7: the provenance chain (generation + drift data)
                "input_bitrate": r["input_bitrate"],
                "output_bitrate": r["output_bitrate"],
                "generation": r["generation"],
                "source_file": r["source_file"],
                "output_file": r["output_file"],
                "encoded_at": r["encoded_at"],
            }
            for r in s.connection.execute(
                """
                SELECT e.*, f.path AS file_path, f.name AS file_name
                FROM encodes e
                JOIN files f ON f.id = e.file_id
                WHERE e.file_id = ? OR e.output_file = ? OR e.source_file = ?
                ORDER BY e.generation, e.id
                """,
                (row["id"], str(p), str(p)),
            ).fetchall()
        ]
        # the movie's own generation: its path as a previous row's output
        # means this file IS that encode's output (generation N+1)
        gen_row = s.connection.execute(
            """
            SELECT generation FROM encodes
            WHERE output_file = ?
            ORDER BY id DESC LIMIT 1
            """,
            (str(p),),
        ).fetchone()
        generation = int(gen_row["generation"]) + 1 if gen_row is not None else 1

        # the stored plan ("quicksave"): what a future encode would do —
        # the prediction display's data. Staleness is FILE-level only
        # (mtime+size vs plan time); a settings mismatch simply makes the
        # encode ignore the plan and measure fresh, which shows there.
        plan_row = s.connection.execute(
            """
            SELECT p.chosen_crf, p.achieved_vmaf, p.skipped_reason,
                   p.estimated_size, p.created_at, p.input_mtime, p.input_size,
                   p.encoder, p.encoder_preset, p.target_vmaf
            FROM plans p JOIN files f ON f.id = p.file_id
            WHERE f.path = ?
            """,
            (str(p),),
        ).fetchone()
        plan = None
        if plan_row is not None:
            try:
                file_ok = (
                    p.is_file()
                    and abs(p.stat().st_mtime - plan_row["input_mtime"]) <= 1.0
                    and p.stat().st_size == plan_row["input_size"]
                )
            except OSError:
                file_ok = False
            plan = {
                "crf": plan_row["chosen_crf"],
                "achieved_vmaf": plan_row["achieved_vmaf"],
                "skipped_reason": plan_row["skipped_reason"],
                "estimated_size": plan_row["estimated_size"],
                "created_at": plan_row["created_at"],
                "encoder": plan_row["encoder"],
                "encoder_preset": plan_row["encoder_preset"],
                "probe_encoder_preset": plan_row["encoder_preset"],
                "target_vmaf": plan_row["target_vmaf"],
                "stale": not file_ok,
                # the HDR estimate bias note (issue #98): derived, not stored -
                # the files row's hdr flag is the same one the plan used
                "estimate_note": (
                    HDR_ESTIMATE_NOTE
                    if row["hdr"] and plan_row["estimated_size"] and not plan_row["skipped_reason"]
                    else None
                ),
            }
    return {
        "file": {
            "path": row["path"],
            "name": row["name"],
            "category": row["category"],
            "duration": row["duration"],
            "codec": row["codec"],
            "resolution": [row["width"], row["height"]] if row["width"] else None,
            "size": row["size"],
            "hdr": bool(row["hdr"]),
            "missing": bool(row["missing_scans"]),
            "missing_since": row["missing_since"],
            "generation": generation,
        },
        "history": history,
        "plan": plan,
    }


@app.post("/api/reveal")
def reveal(request: RevealRequest) -> dict:
    """Reveals a file's containing folder in the system file manager.

    The path must be a file inside the allowed roots (the same allowlist
    as every other endpoint); the reveal itself is the OS file manager
    opening a window - a local, user-visible action.

    Args:
        request (RevealRequest): The file whose folder to reveal.

    Returns:
        dict: The acknowledgement.
    """
    path = validate_path(request.path, "path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    folder = str(path.parent)
    try:
        if os.name == "nt":
            # getattr because mypy's linux stubs lack os.startfile (windows-only API)
            startfile = getattr(os, "startfile", None)
            if startfile is not None:
                startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder], check=False, timeout=10)
        else:
            subprocess.run(["xdg-open", folder], check=False, timeout=10)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"could not open folder: {e}") from e
    return {"revealed": folder}


@app.get("/report")
def report_page() -> Response:
    """Serves the generated HTML report (regenerating it first).

    The report path is contained to the database folder (an app-controlled
    location, not user-supplied per request).

    Returns:
        FileResponse: The report file.
    """
    s = get_state()
    report_dir = os.path.realpath(str(s.db_file.parent))
    report_path = os.path.join(report_dir, "report.html")
    try:
        generate_report(s.db_file, Path(report_path))
    except Exception as e:
        log_error(f"report generation failed: {e}")
        raise HTTPException(status_code=500, detail="report generation failed") from e
    # CodeQL-recognized containment: realpath must stay inside report_dir
    normalized = os.path.realpath(report_path)
    if not (normalized.startswith(report_dir + os.sep)):
        raise HTTPException(status_code=500, detail="report path invalid")
    return FileResponse(normalized, media_type="text/html")


@app.post("/api/watch/{action}")
def watch_control(
    action: str,
    inbox: str | None = None,
    output_dir: str | None = None,
    body: dict | None = None,
) -> dict:
    """Starts or stops the watch loop inside the app.

    Accepts inbox/output_dir either as query parameters or as a JSON body
    (the UI sends a body).

    Args:
        action (str): 'start' or 'stop'.
        inbox (str | None): The inbox folder (start only).
        output_dir (str | None): The output folder (start only).
        body (dict | None): JSON body alternative to the query parameters.

    Returns:
        dict: The watcher state.
    """
    s = get_state()

    if action == "stop":
        s.watch_stop.set()
        return {"watcher": "stopping"}

    if action == "start":
        body = body or {}
        inbox = inbox or body.get("inbox")
        output_dir = output_dir or body.get("output_dir")
        if s.watch_thread and s.watch_thread.is_alive():
            return {"watcher": "already running"}
        if not inbox or not output_dir:
            raise HTTPException(status_code=400, detail="start requires inbox and output_dir")
        inbox_path = validate_path(inbox, "inbox")
        output_path = validate_path(output_dir, "output_dir")

        def run_watch() -> None:
            try:
                watch(
                    inbox_path,
                    output_path,
                    once=False,
                    poll_interval=60.0,
                    max_threads=cap_threads(load_config().handbrake_max_threads),
                    stop_event=s.watch_stop,
                    db_file=s.db_file,
                    min_source_height=load_config().min_source_height,
                    min_source_size_gb=load_config().min_source_size_gb,
                    candidate_codecs=tuple(load_config().candidate_codecs),
                    crop_sample_count=load_config().crop_sample_count,
                )
            except Exception as e:  # the thread must never die silently
                log_error(f"watch loop crashed: {e}")

        s.watch_stop.clear()
        s.watch_thread = threading.Thread(target=run_watch, name="watcher", daemon=True)
        s.watch_thread.start()
        return {"watcher": "running"}

    raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")


def _default_roots() -> list[Path]:
    """Computes the default allowed roots: all available drive roots.

    A localhost-only single-user tool can safely operate on whole drives;
    the strict default (database folder only) made every real library path
    a 403. Explicit --allow roots still narrow this down.

    Returns:
        list[Path]: The drive roots that exist (e.g. C:/, D:/, G:/).
    """
    import string

    roots = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:/")
        if drive.exists():
            roots.append(drive)
    return roots or [Path.cwd()]


def _recover_interrupted_swaps() -> None:
    """Resolves swaps interrupted by a crash, per allowlisted root.

    An unattended daemon will be killed mid-swap eventually, so the recovery
    path is part of the feature, not a follow-up. Each root is inspected once
    at startup; `replace.recover_directory` restores an interrupted original
    and discards only its own orphaned staging copies.
    """
    from shrinkray.replace import recover_directory

    for root in _allowed_roots:
        try:
            outcomes = recover_directory(root)
        except OSError as error:
            log_error(f"swap recovery could not inspect '{root}': {error}")
            continue
        for name, outcome in outcomes.items():
            log(f"swap recovery: '{name}' {outcome}")


def run_app(
    db_file: Path,
    host: str = "127.0.0.1",
    port: int = 8420,
    allowed_roots: list[Path] | None = None,
    config_file: Path | None = None,
) -> None:
    """Boots the resident app (blocking; the server owns the process).

    Args:
        db_file (Path): The sqlite database file.
        host (str): The bind address (localhost by default).
        port (int): The bind port.
        allowed_roots (list[Path] | None): Folders the API may operate on.
            Defaults to all available drive roots (localhost-only app).
        config_file (Path | None): The config.json path (None = next to
            the db, or $SHRINKRAY_CONFIG; the dashboard and the engine
            must read the same file, issue #58).

    Raises:
        ValueError: If the app is bound to a non-loopback address with no
            $SHRINKRAY_TOKEN configured (issue #36). An open dashboard on a
            LAN is reachable by every device on that LAN, so this is a
            startup failure, not a warning.
    """
    if not _is_loopback(host) and _configured_token() is None:
        raise ValueError(
            f"refusing to bind {host} without authentication: set "
            f"${AUTH_TOKEN_ENV} to a secret token, or bind 127.0.0.1. "
            "A published port with no auth is reachable by every device "
            "on the network (issue #36)."
        )
    global state
    allow_roots(allowed_roots or _default_roots())
    state = AppState(db_file)
    state.config_file = config_file
    # the process policy is applied EXPLICITLY at the entry point (issue #97
    # phase 2): loading config.py no longer mutates process state as a side
    # effect
    apply_config(load_config(config_file=config_file))
    _recover_interrupted_swaps()
    state.worker.start()
    log(f"ShrinkRay app listening on http://{host}:{port}")

    def monitor_memory() -> None:
        """Logs the app's own RSS every 5 minutes (self-monitoring honesty).

        "We stay lean" is a claim only a number can back up: this thread
        reads the resident set size and logs it, so a slow leak becomes
        visible in the log instead of a user's dying NAS.
        """
        from shrinkray.resources import rss_mib

        while True:
            rss = rss_mib()
            if rss is not None:
                log(f"memory: resident {rss:.1f} MiB")
            threading.Event().wait(300)

    monitor = threading.Thread(target=monitor_memory, name="memory-monitor", daemon=True)
    monitor.start()

    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        state.shutdown()
        log("app stopped")
