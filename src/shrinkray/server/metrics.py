"""The Prometheus text exposition format for the resident app (issue #168).

ShrinkRay is an observable source, not its own sink: this module renders the
numbers, and the operator points their own Prometheus/Grafana at `/metrics`. The
format is hand-rolled because the metric set below is small and fixed - a comment
plus `name value` lines per family - so `prometheus-client` would be a dependency
bought for a handful of string concatenations.

Nothing here measures anything new. Every value is a query over state that already
exists (the jobs table, the encodes table, the retained ledger, the catalog), which
is what keeps this surface from changing what an encode does.
"""

import shutil
import sqlite3
from pathlib import Path

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_STATES = ("queued", "running", "done", "failed", "cancelled")


def _family(name: str, kind: str, help_text: str, samples: list[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", *samples]


def render_metrics(connection: sqlite3.Connection, library_root: Path | None = None) -> str:
    """Renders the exposition body for `/metrics`.

    A `gauge` is used for every count that can fall as well as rise - the job
    state counts, because `_prune_history` deletes terminated rows, and the
    retained totals, because the operator removes originals by hand. Only the two
    never-deleted aggregates are counters.

    Args:
        connection (sqlite3.Connection): The app's database connection.
        library_root (Path | None): The filesystem the disk-free gauge reports
            on. None omits the gauge rather than reporting a misleading zero.

    Returns:
        str: The exposition text, newline-terminated.
    """
    out: list[str] = []

    by_state = {
        row["state"]: row["n"]
        for row in connection.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
    }
    out += _family(
        "shrinkray_jobs",
        "gauge",
        "Jobs currently recorded, by lifecycle state.",
        [f'shrinkray_jobs{{state="{st}"}} {by_state.get(st, 0)}' for st in _STATES],
    )

    by_kind = {
        row["kind"]: row["n"]
        for row in connection.execute("SELECT kind, COUNT(*) AS n FROM jobs GROUP BY kind")
    }
    out += _family(
        "shrinkray_jobs_by_kind",
        "gauge",
        "Jobs currently recorded, by kind.",
        [f'shrinkray_jobs_by_kind{{kind="{k}"}} {by_kind[k]}' for k in sorted(by_kind)],
    )

    out += _family(
        "shrinkray_queue_depth",
        "gauge",
        "Jobs waiting for the worker.",
        [f"shrinkray_queue_depth {by_state.get('queued', 0)}"],
    )
    out += _family(
        "shrinkray_job_running",
        "gauge",
        "1 while a job is executing, 0 when the worker is idle.",
        [f"shrinkray_job_running {1 if by_state.get('running', 0) else 0}"],
    )

    encodes = connection.execute("SELECT COUNT(*) AS n FROM encodes").fetchone()["n"]
    out += _family(
        "shrinkray_encodes_total",
        "counter",
        "Encodes recorded (encode rows are never pruned).",
        [f"shrinkray_encodes_total {encodes}"],
    )

    reclaimed = connection.execute(
        "SELECT COALESCE(SUM(input_size - output_size), 0) AS b FROM encodes"
    ).fetchone()["b"]
    out += _family(
        "shrinkray_bytes_reclaimed_total",
        "counter",
        "Bytes the recorded encodes saved in total.",
        [f"shrinkray_bytes_reclaimed_total {int(reclaimed)}"],
    )

    stalled = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM jobs, json_each(jobs.result, '$.outcomes')
        WHERE json_extract(json_each.value, '$.error') LIKE 'stalled%'
        """
    ).fetchone()["n"]
    out += _family(
        "shrinkray_encodes_stalled_total",
        "counter",
        "Encodes the stall watchdog terminated (a hung child, never retried).",
        [f"shrinkray_encodes_stalled_total {stalled}"],
    )

    retained = connection.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size_before), 0) AS b FROM retained"
    ).fetchone()
    out += _family(
        "shrinkray_retained_originals",
        "gauge",
        "Originals a verified swap kept; the operator reclaims them.",
        [f"shrinkray_retained_originals {retained['n']}"],
    )
    out += _family(
        "shrinkray_retained_bytes",
        "gauge",
        "Bytes held by those retained originals.",
        [f"shrinkray_retained_bytes {int(retained['b'])}"],
    )

    files = connection.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    out += _family(
        "shrinkray_library_files",
        "gauge",
        "Files in the catalog.",
        [f"shrinkray_library_files {files}"],
    )

    if library_root is not None:
        out += _family(
            "shrinkray_disk_free_bytes",
            "gauge",
            "Free bytes on the library filesystem.",
            _disk_free_sample(library_root),
        )

    return "\n".join(out) + "\n"


def _disk_free_sample(library_root: Path) -> list[str]:
    """Renders the disk-free sample, omitting it when the filesystem cannot be read.

    An unreadable filesystem (an offline network share) must not be reported as
    zero free space: that reads as a full disk and would page an operator over a
    mount that is merely absent. The family header stays, the sample does not.

    Args:
        library_root (Path): The filesystem to measure.

    Returns:
        list[str]: One sample line, or a comment explaining the omission.
    """
    try:
        free = shutil.disk_usage(library_root).free
    except OSError as e:
        return [f"# disk free unavailable: {e}"]
    return [f"shrinkray_disk_free_bytes {free}"]
