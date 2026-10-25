# ui/ — ShrinkRay Console (Svelte 5 + Vite)

Score 14 (file count 3, subdirs 2, code ratio 2, own config 1, entry module 2, symbol
density 2, exports 2; reference centrality below threshold) — a second toolchain and
design system beside the Python core, so it earns its own file. Repo-wide rules (gate,
commit, issue workflow) live in the root `AGENTS.md`; the design record is the ROOT
`DESIGN.md` (there is no `ui/DESIGN.md`).

## OVERVIEW
A read-only status window over the resident app: one `/api/snapshot` poll carries
status + jobs + library, `/api/events` streams progress, `/api/movie` fetches one
title's full history. It starts nothing. `npm run build` writes `ui/dist`, served by
`src/shrinkray/server/app.py` (`_ui_dist()`; `/` no-cache, `/assets/*` immutable).

## STRUCTURE
```
ui/
├── src/App.svelte              # shell: rail, hash routing, poll loop, all shared state
├── src/lib/api.ts              # the ONLY network client + hand-mirrored API types
├── src/lib/progress.svelte.ts  # the ONLY EventSource (rune store, flow not state)
├── src/lib/pages/              # Overview, Library, Jobs, Retained, Config, MovieDetail
│                               # + HintButton/HintMark (the shared hint glyphs)
├── src/tokens.css              # tokens + self-hosted Plex @font-face, both themes
└── src/fonts/  public/         # woff2 faces, favicon.svg, icons.svg
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Add/change a server call | `src/lib/api.ts` | `api` at :328; mirror the FastAPI field |
| New page or route | `src/App.svelte` | `PAGES` :47, rail nav, shortcut map :130 |
| Shared state, poll cadence | `src/App.svelte` | 1 s busy / 3 s idle / 10 s hidden (:258) |
| Live progress | `src/lib/progress.svelte.ts` | `hello` seeds the map, `data` ticks |
| Token prompt and 401 | `src/lib/api.ts` :206 | `TOKEN_KEY`, `isAuthFailure` |
| Colors, type, spacing | `src/tokens.css` | `:root[data-theme='dark'\|'light']` |

## CONVENTIONS
- **Svelte 5 runes only**: `$state`, `$derived`, `$props`, `$effect`. No stores, no
  SvelteKit, no router package — pages are a hash (`#/library`, `#/movie`).
- **Pages are dumb**: `src/lib/pages/*.svelte` take props and call `on*` handlers up to
  `App.svelte`, which owns all shared state. Pages never import another page; only the
  hint glyphs (`HintButton`, `HintMark`) are shared inside that directory.
- **One network boundary**: every `fetch` lives in `api.ts`; `progress.svelte.ts` owns the
  only `EventSource` (token as `?token=` — it cannot set a header).
- **Flow is not state**: SSE carries progress only; the snapshot poll is the state channel.
  Progress is never persisted and never polled.
- **Styling**: consume `var(--token)` from `tokens.css`, scoped `<style>` per component.
  The theme is `document.documentElement.dataset.theme` (`App.svelte:42`); tokens select on
  `:root[data-theme='dark'|'light']`. Faces are self-hosted in `src/fonts` — no CDN.
- **Persistence split**: `localStorage` `shrinkray.token`, `ms.theme`, `ms.paths`, `ms.out`,
  `ms.vmaf`, `ms.workers`; `sessionStorage` `sr.detail`, `ms.query`, `ms.category`,
  `ms.decision`, `ms.sortKey`, `ms.sortAsc`, `ms.jobsKind`, `ms.jobsState`.
- **Gate**: `npm run check` (svelte-check + `tsc`), `npm run test`, `npm run build` — vitest is
  happy-dom with `conditions: ['browser']` over `src/**/*.test.ts` (`api`, progress, JobsPage).

## ANTI-PATTERNS
- **Calling an endpoint that does not exist.** The server has 12 GET routes plus one
  `POST /api/reveal`; `POST /api/jobs` and `POST /api/watch/start|stop` are gone and the
  root `README.md:290-292` still advertises them. The dashboard starts no work (issue #134).
- `fetch`/`EventSource` in a component, or a locally re-declared API shape: extend
  `api.ts` instead — it mirrors the FastAPI JSON, so new server fields land there first.
- Branching a component on theme, hardcoding a hex, or adding shadows/glow/blur. Depth is
  tone, seams and hatching only.
- Red for anything but failure/irreversible work; amber for anything but progress.
  Informational feedback uses the quiet notice, never `role="alert"`.
- A new page missing from `PAGES`, the rail nav or the keyboard map.
- Refetching what the snapshot already delivered: the detail page filters `allJobs` in memory.

## NOTES
- `api` declares `status`, `library`, `libraryChanged`, `jobs`, `job` with no caller; the
  live methods are `snapshot`, `retained`, `movie`, `config`, `reveal`. Adding a caller is
  fine; changing a shape without the server is not.
- `App.svelte` hardcodes Windows defaults (`D:\Movies`, `D:\Encoded`) and VMAF 96;
  `SHORTCUT_LEGEND` says `1–5: pages` while the map (:130) binds only 1-4 (Retained has none).
- Two dead surfaces: `requestEncodeAll` (:319) has no caller and the preflight renders only
  CLOSE, and `App.svelte` passes `cancelling` but never `oncancel`, so the Overview CANCEL
  button is inert. Do not cite either as a working flow.
- `--poster-ground`/`--poster-line` (`tokens.css:58`) outlived `PosterCard.svelte`, deleted
  in #180; the root `DESIGN.md` still describes the demoted poster grid. `ui/README.md` is
  project documentation (rewritten 2026-10-24), not the Vite template.
