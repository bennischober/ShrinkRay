# The ShrinkRay dashboard (ui/)

A Svelte 5 + Vite + TypeScript single-page app served by the resident Python
app (`src/shrinkray/server/app.py` serves `ui/dist` at `/`). It is the
resident mode's status window: what is running, planned, remaining and
reclaimed — it observes, it does not steer (see PRODUCT.md's "Ecosystem
position" and issue #134). Configuring and controlling belong to the terminal
(PRODUCT.md, issue #244).

## Commands

```sh
npm ci          # install exact versions from package-lock.json
npm run dev     # Vite dev server (proxies API calls to the Python app)
npm run check   # svelte-check (types + templates)
npm run test    # Vitest + Testing Library (api contract, SSE store, pages)
npm run build   # production build into ui/dist/
```

CI's `ui` job (`../.github/workflows/ci.yml`) runs `check`, `test` and
`build`; a change under `ui/` is unverified until all three pass.

## Structure

- `src/lib/api.ts` — the API contract helpers, duplicated by hand from the
  FastAPI models (AGENTS.md: add both sides in one change).
- `src/lib/progress.svelte.ts` — the SSE progress store.
- `src/lib/pages/` — one page per dashboard view (Overview, Library, Jobs,
  Retained, Config, MovieDetail).
- `src/tokens.css` — the design tokens; the design system and its rationale
  live in [../DESIGN.md](../DESIGN.md).
