"""progress: the one structured progress event the engine emits (issue #631).

The event is FLOW, never state: it travels from the engine's callback through the
server's ``ProgressBus`` to the browser's SSE stream and is never persisted. The
schema is documented in ``server/AGENTS.md`` and mirrored in
``ui/src/lib/api.ts``; this module is the single constructor so an emission site
cannot invent a slightly different shape.

The event merges per job: ``run`` (the folder job's file counter) and ``file``
(the running file's identity and stepper position) are separate fields, so an encode
percent tick can never erase the folder's ``3/12``. A new file's identity resets
the per-file detail, which the server's bus merge and the browser model both do.
"""

from __future__ import annotations

from typing import Any

# bump when a field changes meaning: the browser model reads this to keep an old
# server's events from being rendered as the new schema
PROGRESS_VERSION = 1

# the stepper's order (issue #631's requirement 4). ``scan`` and ``paused`` are
# stages but not steps: a scan has no per-file pipeline, and a pause holds the
# step the file already reached.
STAGE_STEPS = {
    "decision": 1,
    "bars": 2,
    "samples": 3,
    "crf-search": 4,
    "encode": 5,
    "verify": 6,
    "swap": 7,
}
STAGE_COUNT = len(STAGE_STEPS)

# the stage enum, kept in sync with the job kinds of #602 plus the pipeline's
# own stages and the pause hold
STAGES = (
    "scan",
    "decision",
    "bars",
    "samples",
    "crf-search",
    "encode",
    "verify",
    "swap",
    "paused",
)


def file_state(
    movie: str,
    path: str,
    stage: str,
    *,
    step: int | None = None,
    steps: int | None = None,
) -> dict[str, Any]:
    """Builds the ``file`` merge field for one running file.

    Args:
        movie (str): The file's name (what the operator sees).
        path (str): The file's full path.
        stage (str): The stage the file is on (see ``STAGES``).
        step (int | None): The stepper position; derived from ``stage`` when
            omitted. ``paused`` passes None so the model keeps the step it had.
        steps (int | None): The stepper's total; the stage count by default.

    Returns:
        dict: The ``file`` object of a progress event.
    """
    if step is None:
        step = STAGE_STEPS.get(stage)
    if steps is None:
        steps = STAGE_COUNT
    return {
        "movie": movie,
        "path": path,
        "stage": stage,
        "step": step,
        "steps": steps,
    }


def event(
    stage: str,
    *,
    file: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    **detail: Any,
) -> dict[str, Any]:
    """Builds one versioned structured progress event.

    Args:
        stage (str): The current stage (``STAGES``).
        file (dict | None): The per-file merge field (``file_state``).
        run (dict | None): The folder job's file counter.
        **detail: The stage's own fields (``probe``, ``chosen``, ``encode``,
            ``sample``, ``target_vmaf``, ``detail``, ...).

    Returns:
        dict: The event dict the progress callback receives.
    """
    data: dict[str, Any] = {"v": PROGRESS_VERSION, "stage": stage}
    if run is not None:
        data["run"] = run
    if file is not None:
        data["file"] = file
    data.update(detail)
    return data


def with_run(data: dict[str, Any], done: int, total: int | None) -> dict[str, Any]:
    """Returns the event with the folder job's file counter merged in.

    The engine does not know how many files a folder job holds; the server does.
    A copy keeps the engine's event untouched (the same dict is published to the
    folder job and its stage row).

    Args:
        data (dict): The engine's event.
        done (int): Files finished before the running one.
        total (int | None): Files in the folder job (None = unknown).

    Returns:
        dict: The event with ``run`` set.
    """
    merged = dict(data)
    merged["run"] = {"done": done, "total": total}
    return merged


def merge(last: dict[str, Any] | None, data: dict[str, Any]) -> dict[str, Any]:
    """Merges one progress event into the latest known state (issue #631).

    ``run``, ``file``, ``encode``, ``sample`` and ``probe`` merge key by key, so
    a percent tick never erases the folder counter and a probe keeps its count.
    A new file's identity resets the previous file's probe trail, chosen CRF and
    encode numbers: the hello frame must not seed a finished file's measurements
    into the next file's row. The probe list accumulates completed probes by
    index, so a reload mid-search rebuilds the full trail from the seed.

    Args:
        last (dict | None): The job's latest merged event (None: first event).
        data (dict): The new event (already carries its publish key's fields).

    Returns:
        dict: The merged latest event for this job.
    """
    merged = dict(last or {})
    incoming_file = data.get("file")
    if isinstance(incoming_file, dict):
        previous_file = merged.get("file")
        if isinstance(previous_file, dict):
            old_identity = (previous_file.get("movie"), previous_file.get("path"))
            new_identity = (incoming_file.get("movie"), incoming_file.get("path"))
            if (
                None not in old_identity
                and None not in new_identity
                and old_identity != new_identity
            ):
                for key in ("probe", "probes", "chosen", "encode", "sample", "detail"):
                    merged.pop(key, None)
        base_file = previous_file if isinstance(previous_file, dict) else {}
        merged["file"] = {**base_file, **incoming_file}

    for key in ("run", "encode", "sample"):
        value = data.get(key)
        if isinstance(value, dict):
            base = merged.get(key)
            merged[key] = {**base, **value} if isinstance(base, dict) else dict(value)

    probe = data.get("probe")
    if isinstance(probe, dict):
        base = merged.get("probe")
        merged["probe"] = {**base, **probe} if isinstance(base, dict) else dict(probe)
        index = probe.get("index")
        if isinstance(index, int) and probe.get("vmaf") is not None:
            probes = list(merged.get("probes") or [])
            probes = [p for p in probes if p.get("index") != index]
            probes.append(merged["probe"])
            probes.sort(key=lambda p: p.get("index") or 0)
            merged["probes"] = probes
    elif isinstance(merged.get("probe"), dict) and merged["probe"].get("vmaf") is not None:
        # a manually seeded latest probe (or a legacy hello frame) still
        # belongs to the trail
        probes = list(merged.get("probes") or [])
        if not any(p.get("index") == merged["probe"].get("index") for p in probes):
            probes.append(merged["probe"])
            probes.sort(key=lambda p: p.get("index") or 0)
            merged["probes"] = probes

    # every other field is a plain replace: an unknown key must survive the
    # merge (a consumer's own field is not ours to drop), and ``stage`` moves
    # the stepper forward
    for key, value in data.items():
        if key not in ("run", "file", "encode", "sample", "probe"):
            merged[key] = value
    return merged
