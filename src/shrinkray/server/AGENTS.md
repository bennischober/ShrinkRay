# src/shrinkray/server — the network surface

Why this file (score 12, distinct domain): nothing else in the package listens on a
socket, and this subtree owns the security boundary (token, path containment) plus the
in-memory job queue and progress bus. 4 modules / 2541 L, and `shrinkray.server` is
named 61 times outside the subtree. Read `src/shrinkray/AGENTS.md` first for the
layering; root AGENTS.md owns the "dashboard is a status window" product rule.

## MODULE MAP

| Question | Module | Notes |
|---|---|---|
| Routes, auth, containment, boot | `app.py` (1355 L) | 13 routes (12 GET + 1 POST `/api/reveal`); `require_token` (`:80`), `validate_path` (`:171`), `AppState` (`:255`), `db_access` (`:342`), `run_app` (`:1174`), `start_watch` (`:1278`), `auto_backlog` (`:1320`) |
| Job queue, worker, live progress | `jobs.py` (1022 L) | `JobState`/`JobKind` (`:47`/`:57`), `ProgressBus` (`:144`), `JobQueue` (`:230`), `execute_scan`/`_plan`/`_encode` (`:621`/`:703`/`:786`), `JobWorker` (`:929`) |
| Prometheus text exposition | `metrics.py` (164 L) | `render_metrics` (`:27`); hand-rolled, no `prometheus-client` dependency |

## THE TWO CHANNELS (do not mix them)

- `/api/snapshot` is the only STATE channel: status + jobs + library in one round
  trip, each collection ETag-fingerprinted, an unchanged collection comes back as
  `null` so the client keeps its copy (`app.py:715-720`).
- `/api/events` is the only FLOW channel: `retry: 3000`, an `event: hello` frame
  seeding the latest progress per job on every connect, one `data:` frame per engine
  tick, a `: ping` every 15 s (`app.py:819-855`). Mailboxes are bounded
  (`jobs.py:44`, drop-oldest) and progress is never persisted.

## SECURITY CONTRACT

- The token comes from `$SHRINKRAY_TOKEN` (`app.py:41`) — never from `config.json`,
  which is tracked. It is a MIDDLEWARE, not a per-route dependency, so a new route
  cannot be added unauthenticated. Comparison is `hmac.compare_digest` (`:104`).
  `/api/events` additionally accepts `?token=` because `EventSource` cannot set a
  header — only there (`:109-113`).
- Auth off is loopback-only by construction: `run_app` raises `ValueError` for a
  non-loopback bind with no token (`:1202`), and refuses to start at all on an
  unusable toolchain (`service.all_problems()`).
- `validate_path` realpaths and then requires an allowlisted root prefix
  (`app.py:196-202`); the asset route does its own realpath containment to
  `ui/dist/assets` (`:407-411`). Both reject before touching the filesystem.
- `db_access` yields the connection UNLOCKED; handlers take `AppState.with_lock`
  themselves so acquire and release stay on one thread (`app.py:342-356`).

## CONVENTIONS

- No route starts, cancels or reconfigures work. `start_watch` is called by `run_app`
  on boot and by nothing else (`app.py:1281`, issue #134); `auto_backlog` is a
  scheduler gated on `app.auto_backlog` (`:1326`). A new mutating route is a product
  decision, not an implementation detail.
- The worker is single-threaded by design (`jobs.py:6`): parallel work happens inside
  `encode_movies`, so queueing more jobs would only add latency.
- Live progress goes through `ProgressBus` only; it is flow, never state, never a row
  (`jobs.py:38`). Every engine call receives a `ProgressCallback` and the same dict
  shape the console prints.
- `render_metrics` uses a gauge for anything that can fall (job counts, retained
  totals) and a counter only for never-deleted aggregates (`metrics.py:30-35`); an
  unreadable filesystem omits the disk gauge instead of reporting a misleading zero.
- `$SHRINKRAY_WATCH_OUTPUT` is validated at boot and exits loudly when unwritable
  (`app.py:1229-1243`) — never deferred to the first encode.

## ANTI-PATTERNS

- Do not add an auth-exempt route. `/metrics` is registered after `require_token`
  precisely so it is covered (`app.py:533`); the SSE query-token allowance is the one
  documented exception.
- Do not make `db_access` yield a lock — a generator-yielded lock is released by a
  different threadpool thread (`app.py:344-350`).
- `JobRequest` (`app.py:220`) is dead: no route and no importer. Do not wire it up
  without a product decision; job creation today is `auto_backlog` at boot only.
