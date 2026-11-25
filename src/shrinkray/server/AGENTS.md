# src/shrinkray/server — the network surface

Why this file (distinct domain): nothing else in the package listens on a
socket, and this subtree owns the security boundary (token, path containment) plus the
in-memory job queue and progress bus. Read `src/shrinkray/AGENTS.md` first for the
layering; root AGENTS.md owns the "dashboard is a status window" product rule. Control
(configure, pause/resume) belongs to the terminal and reaches the resident process
through `/data`, never through a route here (issue #244) — a control endpoint is a
product-rule violation, not a feature.

## MODULE MAP

| Question | Module | Notes |
|---|---|---|
| Routes, auth, containment, boot | `app.py` | 16 routes (15 GET + 1 POST `/api/reveal`); `require_token`, `validate_path`, `AppState`, `db_access`, `run_app`, `start_watch`, `auto_backlog` |
| Job queue, worker, live progress | `jobs.py` | `JobState`/`JobKind` (scan, plan, decision, crf-search, encode), `ProgressBus`, `JobQueue`, `execute_scan`/`execute_plan`/`execute_encode`, `JobWorker` |
| Prometheus text exposition | `metrics.py` | `render_metrics`; hand-rolled, no `prometheus-client` dependency |

## THE TWO CHANNELS (do not mix them)

- `/api/snapshot` is the only STATE channel: status + jobs + library in one round
  trip, each collection ETag-fingerprinted, an unchanged collection comes back as
  `null` so the client keeps its copy (`app.py`).
- `/api/events` is the only FLOW channel: `retry: 3000`, an `event: hello` frame
  seeding the latest progress per job on every connect, one `data:` frame per engine
  tick, a `: ping` every 15 s (`app.py`). Mailboxes are bounded
  (`jobs.py`, drop-oldest) and progress is never persisted.

## THE PROGRESS EVENT (versioned, issue #631)

One schema for every stage, built by `engine/progress.py` and mirrored in
`ui/src/lib/api.ts`:

```json
{
  "v": 1,
  "stage": "decision | bars | samples | crf-search | encode | verify | swap | paused | scan",
  "stage_job_id": 12,
  "run": {"done": 3, "total": 12},
  "file": {"movie": "a.mkv", "path": "/media/a.mkv", "stage": "crf-search", "step": 4, "steps": 7},
  "probe": {"index": 2, "count": 6, "crf": 27, "vmaf": 95.9, "predicted_bytes": null, "passed": false, "cached": true, "measured_at": "2026-11-14 08:12:00"},
  "probes": [{"index": 1, "count": 6, "crf": 30, "vmaf": 94.8, "passed": false, "cached": true, "measured_at": "2026-11-14 08:12:00"}],
  "target_vmaf": 96,
  "chosen": {"crf": 26, "vmaf": 96.2, "predicted_bytes": 9400000000, "reason": null, "source": "cache"},
  "source": "cache",
  "measured_at": "2026-11-14 08:12:00",
  "encode": {"percent": 42.31, "fps": 123.45, "avg_fps": 120.0, "eta_seconds": 754},
  "sample": {"index": 2, "count": 7},
  "detail": "CRF 27: VMAF 95.9"
}
```

- `run` and `file` MERGE: a percent tick carries only `encode`, so the folder's
  `3/12` and the probes measured so far survive. A new file's identity resets the
  per-file trail; the server's `ProgressBus._last` keeps the merged latest event
  per job, and the `hello` frame seeds it (a reload mid-search rebuilds the probe
  list). `ProgressBus.publish` is the one merge point (`engine/progress.merge`).
- `stage_job_id` is the per-file stage row (#602) when one exists. A stage row's
  own event KEEPS its own `job_id` (#639); the folder job's event carries the
  row's id in `stage_job_id`, so the browser can follow one file.
- `probe`/`chosen` belong to `crf-search`; `probe.count` is `null` while the
  search can still early-stop (any live total would be invented), and the final
  probe event carries the search's actual step count (`len(probes)`). The final event's
  `probe` is the REAL last probe (`probes[-1]`), never the chosen one: the merge
  replaces probes by index, so reusing the chosen row would delete the last measured
  row and duplicate the decision. The decision rides only in `chosen`; the search's
  final `chosen` has the chosen CRF/VMAF and the stage driver later replaces it with
  the predicted size/reason. The stored-plan reuse path is also a `crf-search` event,
  with the plan's chosen result.
- Cache provenance (maintainer 2026-11-14, issue #631): a probe replayed from
  `probe_cache` carries `cached: true` and its original `measured_at`; a live probe
  carries `cached: false` and `measured_at: null`. The top-level and `chosen.source`
  are `measured | cache | plan` (`plan` for a stored-plan reuse, `cache` only when every
  probe was reused, otherwise `measured`), and the stage result carries the same
  `source`/`measured_at`. `plans.probes` stores the per-probe measured_at/cached so a
  later reuse can say when the measurement was taken.
  `chosen.reason` is `null` for a prediction, else the skip reason (`no size gain`,
  `target unreachable`, `saving below minimum`). The search does not compute a
  predicted size per probe, so `probe.predicted_bytes` is always `null`; the plan's
  `estimated_size` is the whole-file prediction.
- `encode.percent`/`fps`/`avg_fps`/`eta_seconds` are parsed from HandBrake's
  own line by `tools/handbrake.parse_progress_line`; a field the line does not
  print is `null`, never a guess. `sample` is the sample-extraction position and
  never rides `run.done`/`run.total`.
- `/api/movie` returns `plan.probes` as
  `[{crf, vmaf, measured_at, cached}]` from the persisted `plans.probes` column; the
  title page renders that trail.
- `/api/status` and `/api/snapshot` carry `recent_encodes`: the last five
  applied encodes ordered by `encoded_at DESC`. The queue outlook's rough time reads
  this recency list; `/api/library` is category/name-ordered and must never be the
  pace source.

## SECURITY CONTRACT

- The token comes from `$SHRINKRAY_TOKEN` (`app.py`) — never from `config.json`,
  which is tracked. It is a MIDDLEWARE, not a per-route dependency, so a new route
  cannot be added unauthenticated. Comparison is `hmac.compare_digest`.
  `/api/events` additionally accepts `?token=` because `EventSource` cannot set a
  header — only there. The SPA shell (`GET /`, `/assets/*`, the favicon routes) is
  the one path-shape exemption (issue #600): the browser navigation that loads the
  document cannot send the header, the shell is the login surface for the
  paste-token prompt (issue #36) and carries no operator data; it is GET
  only, and every API route stays gated.
- Auth off is loopback-only by construction: `run_app` raises `ValueError` for a
  non-loopback bind with no token, and refuses to start at all on an
  unusable toolchain (`service.all_problems()`).
- `validate_path` realpaths and then requires an allowlisted root prefix
  (`app.py`); the asset route does its own realpath containment to
  `ui/dist/assets`. Both reject before touching the filesystem.
- `db_access` yields the connection UNLOCKED; handlers take `AppState.with_lock`
  themselves so acquire and release stay on one thread (`app.py`).

## CONVENTIONS

- No route starts, cancels or reconfigures work. `start_watch` is called by `run_app`
  on boot and by nothing else (`app.py`, issue #134); `auto_backlog` is a
  scheduler gated on `app.auto_backlog`, and a restart's identical pending job
  (same kind and payload) is kept, not re-enqueued (issue #626). A new mutating
  route is a product decision, not an implementation detail.
- The worker is single-threaded by design (`jobs.py`): heavy encodes do not profit from
  running in parallel, so queueing more jobs would only add latency.
- A boot resume step (`AppState.resume_interrupted_jobs`) re-queues the rows a
  shutdown or crash left `interrupted`, before the worker starts and independent
  of `app.auto_backlog` (issue #622): the same row keeps its id and order, a
  pending cancel request wins, and a per-file stage row is history, never resumed.
- `JobQueue` concurrency shape is fixed, not style: `_lock` is never held while waiting
  for the connection gate (`take_next` marks the job under `_lock`, releases it, then
  takes the gate for the RUNNING commit); a failed commit rolls
  back and restores the row to `QUEUED`; only `QUEUED` jobs are taken, and only a
  `QUEUED` job is cancelled as queued — a running job's cancellation is its cancel
  event, and a job in the take window is not cancellable as queued (returns False)
  (`jobs.py`, #660 review rounds — the shape that has repeatedly broken).
- `JobWorker._run` logs and backs off a failed iteration; only
  `WORKER_MAX_CONSECUTIVE_FAILURES = 5` consecutive failures end it, with the reason
  recorded on `fatal_error` for `/api/status` (`jobs.py`, #649) — a silent dead worker
  is the bug, not a crash.
- Live progress goes through `ProgressBus` only; it is flow, never state, never a row
  (`jobs.py`). Every engine call receives a `ProgressCallback` and the same dict
  shape the console prints.
- `render_metrics` uses a gauge for anything that can fall (job counts, retained
  totals) and a counter only for never-deleted aggregates (`metrics.py`); an
  unreadable filesystem omits the disk gauge instead of reporting a misleading zero.
- `$SHRINKRAY_WATCH_OUTPUT` is validated at boot and exits loudly when unwritable
  (`app.py`) — never deferred to the first encode.

## ANTI-PATTERNS

- Do not add an auth-exempt route. `/metrics` is registered after `require_token`
  precisely so it is covered (`app.py`); the documented exceptions are the SSE
  query-token allowance and the SPA-shell path shape (issue #600, GET only) —
  anything else exempted is a defect.
- Do not make `db_access` yield a lock — a generator-yielded lock is released by a
  different threadpool thread (`app.py`).
- `JobRequest` (`app.py`) is dead: no route and no importer. Do not wire it up
  without a product decision; job creation today is the boot backlog
  (`auto_backlog`) and the watcher's discovered-file enqueue
  (`submit_watch_candidate`, `app.py`).
