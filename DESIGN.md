---
name: ShrinkRay Console
description: A home-lab operator console for a measured movie library — dark cinema ground and light working bench on one shared structure
colors:
  # Dark (default) theme values below. Every token has a light-theme twin:
  # the canonical source is ui/src/tokens.css ([data-theme] blocks); the
  # light value for each token is carried in .impeccable/design.json
  # (colorMeta.<key>.light) and named in the Colors section.
  ground: "#101216"
  rail: "#0b0d10"
  panel: "#16191e"
  panel-raised: "#1b1f25"
  panel-active: "#1c2a28"
  seam: "#262c34"
  row-line: "#1f242b"
  ink: "#e6e9ec"
  ink-dim: "#9aa3ad"
  ink-faint: "#838b93"
  teal: "#2dd4bf"
  teal-ink: "#2dd4bf"
  teal-glow: "rgba(45, 212, 191, 0.12)"
  amber: "#e0a45e"
  red: "#f27265"
  ok: "#57c98a"
  poster-ground: "#1a1e24"
  poster-line: "#2a3138"
typography:
  brand:
    fontFamily: "IBM Plex Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "3px"
  hero:
    fontFamily: "IBM Plex Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.2
  page-title:
    fontFamily: "IBM Plex Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "19px"
    fontWeight: 700
    lineHeight: 1.2
  poster-title:
    fontFamily: "IBM Plex Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "0.3px"
  stat:
    fontFamily: "IBM Plex Mono, Consolas, ui-monospace, monospace"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.2
    fontFeature: "'tnum' 1"
  body:
    fontFamily: "IBM Plex Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "IBM Plex Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "10.5px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "1.4px"
  data:
    fontFamily: "IBM Plex Mono, Consolas, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "'tnum' 1"
  data-strong:
    fontFamily: "IBM Plex Mono, Consolas, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.45
    fontFeature: "'tnum' 1"
rounded:
  chip: "2px"
  md: "4px"
  pill: "5px"
spacing:
  xs: "6px"
  sm: "10px"
  md: "14px"
  lg: "18px"
  xl: "26px"
components:
  button-primary:
    backgroundColor: "{colors.teal}"
    textColor: "{colors.ground}"
    rounded: "{rounded.md}"
    padding: "5px 14px"
  button-secondary:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "5px 14px"
  button-secondary-hover:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.teal-ink}"
    rounded: "{rounded.md}"
    padding: "5px 14px"
  button-quiet:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.ink-dim}"
    rounded: "{rounded.md}"
    padding: "5px 14px"
  chip-state:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.chip}"
    padding: "2px 8px"
  input-text:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
  stat-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "9px 12px"
  rail-item-active:
    backgroundColor: "{colors.panel-active}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "7px 12px"
---

# Design System: ShrinkRay Console

## Overview

**Creative North Star: "The ShrinkRay Console"**

The console is a home-lab operator surface: a shell discipline borrowed from home-automation dashboards meets a media library's recognition, pointed at one measured movie library. Every domain is one click away in the sidebar rail; the overview answers "what needs me?" in five seconds (failures, missing files, what the last runs reclaimed, where work stands); the library is *recognized* at a glance in the table (the poster-grid experiment was demoted away after round 3) and *operated* in bulk through the table and selection. The world is a deliberate 180° from round 1 (the Drafting Table sheet): the rejected single-page document arrangement is replaced by a console shell, and the previously rejected costume clichés are re-aimed — this is a console by structure and restraint, never by gaming-RGB, glow, or terminal cosplay.

The system is **dual-theme by token, not by layout**: a dark cinema ground (`#101216`, the default) and a light working bench (`#eef0f2`), switched by `[data-theme]` on `<html>` and persisted. Both themes render the *same* structure — components never branch on theme; they only consume tokens. Color stays meaning-first: teal = measured/confirmed/selected, amber = work in progress, red = failure/destruction only. State is still expressed as **density** — solid fill means done, hatching means in progress, an empty swatch means untouched, a dashed outline means missing.

The register is quiet, dense and honest. Numbers carry units and fixed precision, absent values are an em-dash, an encode that made the file *larger* is reported as "+X% larger" in red rather than spun. Irreversible work carries a reserved confirmation ceremony (the alertdialog shape the Do's and Don'ts section describes; it renders nowhere today — the dashboard starts nothing). The surface serves one expert user on localhost; it optimizes for long operating sessions at readable density, not for marketing impressions.

**Key Characteristics:**

- Console shell: 190px sidebar rail (brand, nav with count badges, theme + watcher toggles) beside a capped content column
- Dual theme via `[data-theme]` — dark cinema ground (default) and light working bench, one shared structure, token-switched only
- Meaning-first accents: teal (measured/confirmed/selected), amber (in progress), red (failure/destruction), green (encoded/done)
- Density-state system: solid / hatched / empty / dashed chips and row fills carry decision state
- Overview stat band of button-cards; running-work band with progress trace; attention panels for failures, missing files and setup
- Dense library table (the catalog's working view — sortable, selection-hatched, decision chips; the poster-grid experiment was demoted away after round 3: 354 near-identical typographic tiles differentiated state but not identity)
- Bulk bar with the leader-note estimate ("→ PLAN-PREDICTED −{n} GB · {n} BASE", `LibraryPage.svelte`'s copy) drawn in as the annotation moment
- Movie provenance page carrying every schema-v5 encode metric (VMAF, CRF, presets, encode time, probes, toolchain)
- IBM Plex Sans speaks, IBM Plex Mono measures (tabular numerals); 4px console chrome, hairline seams, no shadows

## Colors

A two-ground palette where nearly everything is ink-on-console and four accent hues carry all the meaning; the dark values below are the defaults, with the light-theme twin named beside each.

### Primary
- **Console Teal** (`--teal`, dark `#2dd4bf` / light `#0f766e`): The system's voice of "confirmed". Selection hatch and borders, checkbox accents, the primary action fill, the active rail item tint, the overview's running-band hatch and trace fill, measured VMAF values, focus rings, text carets, and text selection (at 12%/10% alpha via `--teal-glow`). On dark it brightens to `#2dd4bf` so accent survives the low ground; on light it deepens to `#0f766e`.
- **Teal Ink** (`--teal-ink`, dark `#2dd4bf` / light `#0b5952`): Teal as *text*. On dark it equals the fill teal; on light it is the darker, contrast-safe `#0b5952`. Always use this token for teal text — never the fill value directly.
- **Teal Glow** (`--teal-glow`, dark `rgba(45, 212, 191, 0.12)` / light `rgba(15, 118, 110, 0.1)`): The wash layer — text selection, the selection hatch (`--hatch-select`), the running-band hatch.

### Secondary
- **Work Amber** (`--amber`, dark `#e0a45e` / light `#8a4307`): The figure in progress. Running-row hatching, `IN WORK` chips, the trace fill's leading edge, warning count badges on the rail, the HDR tag. (The poster corner tag wore amber too — reserved with the poster spec.) Amber is a status, never a decoration. (Round-6: the light value darkened from `#b45309` — it measured 3.89:1 on the light rail, below the 4.5:1 floor; `#8a4307` clears it on every light ground.)

### Tertiary
- **Alarm Red** (`--red`, dark `#f27265` / light `#b3382c`): Failed and destructive only. Failure badges, error strips and red hatch backgrounds, the confirmation ceremony borders (the reserved ceremony shape — it renders nowhere today), the missing-file chip's dashed swatch. No destructive-outline button style or red-accented toggle is defined in `ui/src` today; if a destructive control returns, it wears the reserved ceremony, not a new red costume.

### Confirmation Green
- **Signal Green** (`--ok`, dark `#57c98a` / light `#15803d`): Encoded/done — the solid `done` swatch on the decision chips: `LibraryPage.svelte` renders it for ENCODED titles (`{ label: 'ENCODED', cls: 'done' }` → `.chip.done .swatch { background: var(--ok) }`), and `OverviewPage.svelte`'s plan rows use the same chip for a prediction overtaken by a measurement. What is RESERVED is the poster corner tag: no poster/card component exists in `ui/src` (the catalog is a table, `LibraryPage.svelte`'s own comment), and a DONE job state renders teal via `JobsPage.svelte`'s `.ok { color: var(--teal-ink) }`, not green. Green answers "is this title handled?"; teal answers "is it selected/measured?". They are not interchangeable.

### Neutral
- **Cinema Ground / Working Bench** (`--ground`, dark `#101216` / light `#eef0f2`): The page itself; also the text color on the primary button (inverted per theme).
- **Rail** (`--rail`, dark `#0b0d10` / light `#dfe3e7`): The sidebar rail's darker ground — one step below the page, so navigation reads as chrome.
- **Panel** (`--panel`, dark `#16191e` / light `#f8f9fa`): Cards, sheets, tables, chips, buttons at rest.
- **Raised Panel** (`--panel-raised`, dark `#1b1f25` / light `#ffffff`): Hover/active cards and the default button fill — one step up from panel.
- **Active Panel** (`--panel-active`, dark `#1c2a28` / light `#e2f0ee`): Teal-tinted ground for "under work" — active rail items, the armed bulk bar, selected table rows.
- **Seam** (`--seam`, dark `#262c34` / light `#c9ced3`): The hairline workhorse — container borders, control borders, dividers, scrollbar thumbs.
- **Row Line** (`--row-line`, dark `#1f242b` / light `#dde1e4`): Inner rules between table rows and panel sections (lighter than seam, so structure reads inside a panel).
- **Ink** (`--ink`, dark `#e6e9ec` / light `#1e2328`): Primary text.
- **Dim Ink** (`--ink-dim`, dark `#9aa3ad` / light `#5b6470`): Secondary text, legend labels, quiet states, rail labels at rest.
- **Faint Ink** (`--ink-faint`, dark `#838b93` / light `#565d66`): Disabled/ghost text, placeholders, job IDs. Chosen as the contrast floor in both themes. (Round-6: the light value darkened from `#6d757d` — it measured 3.62:1 on the light rail and 4.09:1 on the ground, below the 4.5:1 floor; `#565d66` clears it on every light ground while staying lighter than `--ink-dim` on panels.)
- **Poster Ground / Poster Line** (`--poster-ground` dark `#1a1e24` / light `#ffffff`; `--poster-line` dark `#2a3138` / light `#c9ced3`): RESERVED with the poster spec (the typographic poster area of library cards and its bottom rule) — no component consumes these tokens today (the grid was demoted away; the catalog is a table).
- **Row Alt** (`--row-alt`, dark `#14171b` / light `#f1f3f5`): Defined in `tokens.css` but currently unreferenced — tables separate rows with hairline rules, not zebra fills. Reserved; do not reach for it casually.

### Named Rules
**The Meaning-Only Accent Rule.** Teal, amber, red and green encode state — selection/measurement, progress, failure, completion. They never appear as decoration; if a color isn't telling the user something about their data or work, it isn't there.

**The Red Lock Rule.** Red is reserved for failure and irreversible destruction. Nothing else — not badges, not emphasis, not charts — may use it. The rule extends to *ceremony costumes*: a confirmation strip's border/hatch color must match the reversibility of its verb — the encode-all preflight is amber-bordered with a teal primary confirm (encoding is reversible), while the bars-crop strip stays red (round-6: the preflight borrowed `.bars-confirm` wholesale and diluted red's meaning).

**The Teal-Ink Rule.** Teal as text is always `--teal-ink`, never the raw fill token, so accent text stays contrast-safe in *both* themes.

**The One Structure, Two Grounds Rule.** Themes switch token values and nothing else. A component must never branch on theme, hardcode a theme's hex, or change layout between `[data-theme='dark']` and `[data-theme='light']`.

## Typography

**Display Font:** IBM Plex Sans (self-hosted woff2, weights 400–700; fallbacks `Segoe UI, system-ui, sans-serif`)
**Body Font:** IBM Plex Sans (same family — there is no separate display face)
**Label/Mono Font:** IBM Plex Mono (self-hosted woff2, 400 and 500–600; fallbacks `Consolas, ui-monospace, monospace`)

**Character:** An operator-console pairing. IBM Plex Sans speaks — page titles, labels, prose — with labels in small tracked caps like equipment annotations; IBM Plex Mono measures — every byte, percent, CRF and VMAF score in tabular numerals so columns align like an instrument readout. Faces are self-hosted because a localhost tool must not depend on a CDN.

### Hierarchy
- **Hero** (700, 22px, ~1.2): The movie-detail page title and the stat values' scale mate — the largest type in the console, used once per view.
- **Page Title** (700, 19px, `--fs-page`): The `h2` heading of every page (Overview, Library, Jobs, Config), over a legend annotation line.
- **Stat** (600, 22px, mono, tabular): The big number in overview stat cards — count, GB, mean VMAF, hours.
- **Brand** (700, 16px, 3px letter-spacing, uppercase by content): `SHRINKRAY` in the rail, over the `LIBRARY CONSOLE` legend line.
- **Poster Title** (700, 17px, 0.3px tracking, 1.3 line-height, dim ink): RESERVED with the poster spec — the movie name set large and centered on the card's 2:3 field, clamped to 4 lines (no poster component renders today).
- **Body** (400, 13px, 1.45): General UI text, empty-state sentences, rail labels (13px/500).
- **Label** (400, 10.5px, 1.4px letter-spacing, uppercase — `.legend`): The annotation voice: figure headings, column headers, button text, key/value keys, badge counts' neighbors, the page-head annotation. Always dim or body ink; accent-colored only where meaning applies.
- **Data** (400, 12px, `.mono`, `font-variant-numeric: tabular-nums`): All measurements — sizes in GB, percents, resolutions, CRF/VMAF, job IDs, elapsed times. **Data Strong** at 600 for names, selected counts, and emphasized values.
- **Micro** (10px–9.5px): in-table HDR tags (10px) and job action buttons (`var(--fs-data)` = 12px at `JobsPage.svelte`'s rows). (The poster corner tag was 9.5px with 1px tracking — no poster component renders today; the size is reserved with the poster tokens.)

### Named Rules
**The Two Voices Rule.** IBM Plex Sans talks, IBM Plex Mono measures. Anything that is a number, path, or machine value is mono tabular; anything addressed to the human is IBM Plex Sans. Never set measurements in the sans.

**The Every Number Carries Its Unit Rule.** Data is rendered with explicit units and fixed precision ("4.12 GB", "95.25", "2h 14m"), and an absent value is an em-dash (`—`), never blank or zero. Growth is reported honestly: an encode that enlarged the file prints "+X.X% larger" in red, not a masked percentage.

**The One Spelling Rule.** A unit has exactly one spelling across the whole UI — bitrate is `Mbps` (never `MBit/s`), VMAF carries one decimal (never two in one panel and one in another). Two spellings for one quantity read as two quantities.

**The Signed Delta Rule.** Comparisons print signed and colored (`−41.2% size · +3.8 VMAF`), never parenthesized prose — polarity must be readable without parsing: teal when the change is good (smaller, better quality), red when it is not. Matches the battle-proven diff-view convention (size-report bots, codec dashboards).

## Layout

The console is a fixed shell plus a content column: `display: flex` on the root, a **190px sidebar rail** (sticky, full viewport height, `--rail` ground, 1px right seam, padding `18px 12px`, gap 20px) and a **main column** (`flex: 1`, `max-width: 1500px`, padding `20px 26px 40px`, gap 14px). The rail reads top to bottom: brand stamp, section nav, then (pinned to the foot) the theme toggle and watcher toggle.

Pages are flex columns with a 12–14px gap sharing one anatomy: **page head** (19px title + legend annotation) → **toolbar/band** → **work area**. The two workhorse pages:

- **Overview**: stat band (button-cards in an `auto-fit, minmax(150px, 1fr)` grid, gap 10px) → running-work band (or a dashed "no active work" quiet strip) → attention panels in a fixed 2×2 grid (ATTENTION — FAILURES · MISSING FILES · SETUP · MEASURED QUALITY; single column below 640px — auto-fit wrapped them 3+1 and orphaned the fourth).
- **Library**: toolbar (search, category *select*, decision-state filter, RESCAN ALL) → bulk bar → data table (the single work view; sortable headers, selection hatching). The bulk bar sits *above* the list — work is never below the fold of its own list. The Decision chip column renders untouched as a swatch with a legible hairline-dash body (the em-dash vocabulary) — a bare transparent box reads as a broken cell across 350 rows. Titles carry inline markers for state that isn't a column: `HDR` (amber) and `GEN n` (teal, **only when n ≥ 2** — generation means something only when a file is an encode of an encode; a gen-1 chip on every encoded row would be noise).

The **table view**: wrapped in a seam-bordered scroll area, sticky header (`z-index: 1`, panel fill), header cells `8px 12px` in legend voice, body cells `7px 12px` separated by `--row-line`, checkbox column 34px, numeric columns right-aligned, name column max 340px with ellipsis. Selected rows fill with `--hatch-select`; the in-work row fills with `--hatch-progress`.

Spacing clusters on the token steps (6/10/14/18/26px) plus in-context values: bulk bar `8px 14px`, stat cards `9px 12px`, panel heads `9px 14px`, table cells `7–8px 12px`, chips `2px 8px`. (The poster meta's `9px 10px 10px` is reserved with the poster spec.) Density is compact but airy — hairlines separate, padding never crowds.

Responsive: below **900px** the rail folds into a top bar (nav goes horizontal with scroll, the active item gains a 2px teal inset underline, main padding drops to 14px); the movie page's two-column fact panels stack below **820px**. (The confirmation strip's full-width rule is reserved with the ceremony spec - no confirmation strip renders today.) There are no other breakpoints — this is a desktop console.

### Named Rules
**The One-Click-Away Rule.** Every domain lives in the rail and is reachable in one click from anywhere; the rail's badges surface counts (library size, running jobs, failures) so navigation carries status, not just labels.

**The Five-Second Rule.** The overview must answer "does anything need me?" — failures, missing files, active work — in one viewport glance, without scrolling into the library.

## Elevation & Depth

**No shadows — the system is flat and states it.** Depth is conveyed by four means only:

- **Tonal layering**: rail → ground → panel → panel-raised, plus the teal-tinted `--panel-active` for "under work" — a lightness ladder, not a lighting model.
- **Line weight**: 1px hairline seams for containers and controls; heavier *meaning* borders in accent colors where a surface exists (teal on the armed bulk bar; the amber trace edge at `JobsPage.svelte`'s running rows). Cards and confirmations render no accent border today (no card component; the ceremony is reserved).
- **Density hatching**: the four repeating-linear-gradient hatch patterns (see Shapes) fill regions to signal state.
- **Overlay scrim**: RESERVED with the ceremony spec (the deletion confirmation would sit on `rgba(0, 0, 0, 0.5)` at `z-index: 60`; no confirmation surface renders today). The sticky table header holds `z-index: 1`.

### Shadow Vocabulary
None. Do not introduce box-shadows, glows or blur. The single sanctioned exception is the **inset rule**: below 900px the active rail item marks itself with `box-shadow: inset 0 -2px 0 var(--teal)` — an underline drawn as a rule, not a shadow cast.

### Named Rules
**The Hairline Rule.** Separation is drawn, not lit. Any need for depth is answered with a seam, a tone step, or a heavier rule — never a shadow or glow.

## Shapes

The form language is small, consistent console chrome. Corners are **4px** (`--radius`) on every card, panel, button and input; the **2px** radius belongs to chips and the HDR tag (the poster corner tag wore it; no poster component renders today); the **5px** pill radius belongs to progress traces (a 10px-tall track fully rounded) and scrollbar thumbs. Nothing rounds further — no large radii, no circles except checkbox glyphs.

- **Hatch patterns** (45° stripes, 4px on / 4px off unless noted):
  - `--hatch-work` — neutral grey at 18% alpha: the empty progress-trace track.
  - `--hatch-progress` — amber at 22% alpha: running rows, `IN WORK` chips, the jobs-log trace fill (terminated by a 2px amber right edge).
  - `--hatch-skip` — amber at 18% alpha, vertical 90°, 6px/6px: skipped-state chips (deliberately coarser so skip reads differently from progress).
  - `--hatch-select` — teal glow: selected rows, `PLANNED` chips.
  - Low-alpha accent hatches (6–8%) also tint failure/error strips and the running overview band.
- **Dashed borders mean "not finished / not available"**: the jobs-log's empty strips and the overview's quiet strip, plus the missing chip's dashed swatch. A dashed seam is an absence — never a style accent.
- **Path truncation**: long paths use `direction: rtl` so the *tail* of the path stays visible — the folder and file name are the information.
- **Scrollbars**: thin, `--seam` thumb on transparent track (10px, darkening to faint ink on hover).
- **Toggles as glyphs**: the theme toggle prints `◐ DARK` / `◑ LIGHT`; the watcher prints `● WATCHER` / `○ WATCHER` — state as a typographic mark, not an icon font.

## Components

For each component: character line, then shape, color assignment, states, and distinctive behavior.

### Sidebar Rail
The console's chrome spine. Width 190px, rail ground, right seam, sticky. **Brand**: `SHRINKRAY` (16px/700/3px tracking) over the `LIBRARY CONSOLE` legend. **Nav** (`aria-label="Sections"`): full-width items (13px/500, padding `7px 12px`, 4px radius, dim ink) — Overview, Library, Jobs, Config; hover raises to panel fill and ink; the active item sits on `--panel-active`. **Badges** ride right-aligned in mono 10.5px: Library always shows the title count (dim); Jobs shows running jobs in amber; Overview shows failures in red. Badges are conditional — absent when zero, so the rail stays quiet when healthy. **Foot**: theme toggle (`◐ DARK` / `◑ LIGHT`) and watcher toggle (`● WATCHER` when running, teal border + teal-ink text; `○ WATCHER` when stopped).

### Buttons (`.act`)
- **Shape:** 4px radius, 1px border, legend voice (10.5px, 1.4px tracking, uppercase). Three context sizes: compact `4px 12px` (overview strips), standard `5px 14px` (bulk bar, tables), roomy `6px 16px` (hero actions, config).
- **Secondary (default):** raised-panel fill, 1px ink border, ink text. Hover: teal border, teal-ink text.
- **Primary:** solid teal fill, ground-colored text (inverts per theme — dark ground text on bright teal, light ground text on deep teal). Hover: `brightness(1.08)`. No primary button exists in the current UI (the resident app renders no encode affordance — the library works on its own); the style is reserved for the loudest verb when one returns.
- **Quiet:** seam border, dim ink (CLEAR and FOLDER render as `.act quiet`; the overview's DISMISS is a `.link`).
- **Disabled:** 40% opacity, default cursor.

### Density State Chips (`.chip`)
- **Style:** 2px-radius pill, panel background, 1px seam border, legend voice, padding `2px 8px`; a 10px square `.swatch` with a 1px dim-ink border leads the label.
- **State variants (the density system):** `done` — solid green swatch (encoded); `progress` — amber hatch (in progress); `skip` — coarse vertical amber hatch (skipped); `untouched` — empty swatch; `failed` — solid red; `neutral` — seam fill (group reasons like `SAME HASH`, categories); `planned` — teal hatch with teal border; `missing` — **dashed red border, transparent fill** (absent from disk, schema v6). The running row swaps its decision chip for a live `IN WORK {percent}%` variant.
- **Poster corner tags** carried the same states in micro (9.5px mono tags at the poster's bottom-right) — the poster grid was demoted away (`LibraryPage.svelte`'s own comment: "the catalog is a table; the grid was demoted away"); the spec is reserved with the poster tokens (`--poster-ground`/`--poster-line`, referenced by no component today).

### Overview Stat Band
The overview's first answer. Button-cards (they navigate — TITLES and RECLAIMED go to the library) on panel fill with seam borders, padding `9px 12px` (`OverviewPage.svelte`'s `.stat`); each is a legend label over a 22px mono value over a 10.5px mono dim sub-line ("184.2 GB of 512 GB source", "{n} measured"). Two stats are **conditional**: MISSING appears only when files are absent (red value), ENCODE TIME only when schema-v5 durations exist. Warning values colorize: missing in red. MEAN VMAF renders an em-dash until something is measured — the band never fakes a figure. The band **hugs its cards** (`fit-content`, cards capped 220px): an auto-fit grid left unused tracks as dead seam ground (round-6). A fifth card, **RECLAIMABLE**, appears when the last plan still has open titles: the plan-predicted savings on them (`{GB}` over `{n} planned · plan #{id}`) — a plan figure that names its source, answering "what remains" beside "what was won".

### Running-Work Band & Trace
The figure in progress, pinned between stat band and attention panels: teal-glow hatch over panel, seam border, padding `12px 16px`. Head: `IN WORK · {KIND}` in amber legend voice against a mono `3/12 · 41%` reading. Title (14px/600), then the **trace** — a 10px pill track on `--hatch-work` with a solid teal fill, `role="progressbar"` with live `aria-valuenow/min/max` — then the stage detail in dim mono plus `started {when}` and a live **ETA** (from measured throughput when schema-v5 data exists, proportional fallback otherwise). When nothing runs, the band is replaced by a **quiet strip**: dashed seam border, "no active work" in mono dim, and a `LIBRARY →` action — or, when a finished encode run exists, the **LAST RUN band** (solid teal-tinted): `LAST RUN #{id} · {n} encoded · −X GB · min VMAF · wall time`, the run's completion stated, never dissolved. Empty is honest, never decorative.

### Attention Panels
The overview's four-panel 2×2 row (min 320px panels): **ATTENTION — FAILURES** (`#{id} {kind}` in red mono, error text translated to operator language (`humanizeError`) and ellipsized, a DETAILS link when the failed job maps to a library title, DISMISS per row and DISMISS ALL in the head when more than one; empty state "nothing failed — the last runs are clean"), **MISSING FILES** (name in red mono, "absent since {date}", a details link into the movie page, "{n} more →" beyond four), **SETUP** (SCAN FROM / WRITE TO / DATABASE / WATCHER / RESIDENT MEMORY fact rows with legend keys and mono values — the app's self-measured RSS renders nothing when unmeasured), and **MEASURED QUALITY** (mean VMAF with min/count sub-line when n > 1 — a single measured title is already on screen, the head never repeats it). Panels share one pattern: seam border, panel fill, a `9px 14px` head with legend title and a teal-ink link (hover underline), rows separated by `--row-line`.

### Category Bars (where the bytes live)
The overview's inventory chart, below the attention 2×2: one row per category — name, a bar track, mono facts (`{n} · {GB} · −{reclaimed} ({pct}%)`). **The chart is never amputated**: the top-8 cap orders by size and an in-panel expander renders the remainder inline ("SHOW {n} MORE" / "SHOW LESS") — a "more →" that jumps to another page left the chart literally cut off (round-6). **Both overlays share one scale** (the global max): bar = category size, teal hatch = reclaimed bytes at the same scale with a 1.5% visibility floor — a hatch must never outrun the bar it belongs to (share-of-category scaling claimed track space the category did not have), and the exact reclaimed figures live in the row's nums and tooltip. The legend names the encodings: `BAR = SIZE · TEAL HATCH = RECLAIMED`.

- **Memory sparkline** (`OverviewPage` SETUP): `rss_mib` is measured live per snapshot; the client keeps the last 40 samples so the SETUP row shows the trend polyline beside the instant figure (`{last} MiB` + min/max/n in the tooltip). Two samples minimum — before that it is the plain figure, never a fabricated line.

### Poster Card (reserved shape)
The library card whose poster *is typography* — reserved with the poster tokens: no poster/card component exists in `ui/src` today (the grid was demoted away; the catalog is a table). The spec below is what the component wore and would wear again if the poster grid returns. **Structure**: an `article` with a 2:3 poster area (gradient from `--poster-ground` to `--ground` at 160°, bottom `--row-line` rule) and a meta block below. On the poster: the title centered in 17px/700 dim ink (4-line clamp, `aria-hidden` — the meta block carries the real name), a 15px teal-accent selection checkbox top-left, and a state **corner tag** bottom-right (density chip states in micro; `IN WORK 34%` live). **Meta** (a button that opens the movie page): name 12.5px/600 (2-line clamp), then facts in 10.5px mono dim — `1920×1080 · 4.12 GB · VMAF 95.3` with the VMAF in teal-ink when measured, plus an amber HDR tag. **States**: hover raises the border to dim ink; selected gets a teal border on `--panel-active`; in-work gets an amber border; missing dims to 55% opacity with its dashed corner tag.

### Library Table (the catalog today)
The work area is the table — the poster grid's GRID/TABLE toggle was demoted with it (no segmented control renders; `grep -rn 'GRID' ui/src` → 0). The table adds a **Decision** chip column, right-aligned mono CRF/VMAF/Saved columns (measured VMAF in teal-ink 600; positive savings in green), row hatch fills for selected/in-work rows, and a `role="alert"`-free honest empty row. Filters, selection and the bulk bar persist as today's snapshot state.

### Bulk Bar (the leader-note strip)
The console's action strip, always above the list (resident app: the library works on its own — the strip has no PLAN/ENCODE verbs; issue #331's read-only rule). **Idle**: seam border, panel fill — "{n} titles — tick checkboxes for bulk work" and the standing note "the app works through the library on its own — this page only shows where it stands". **Armed** (any selection): teal border on `--panel-active`, entering with the leader-draw animation — the selection checkbox with `{n} SELECTED` in strong mono, then the **leader note** `→ PLAN-PREDICTED −X.X GB · Y GB BASE` in teal-ink mono (the plan's prediction over the selection against their base size; when *every* selected title is already encoded the basis is zero and the note says so — "no estimate (selection already encoded)" — it never invents a floor number), then CLEAR. The estimate always precedes the work: bulk work is never blind.

### Movie Provenance Page (schema-v5/v7 metrics)
The full record for one title, opened from the table (a `name-btn` row click; replaces round 1's drawer) and closed with `← LIBRARY` / `← OVERVIEW` (returns to where it was opened from) or Escape; deep-linkable via `#/movie` (path in sessionStorage, F5 restores). **Hero**: the title at 22px, the full path in break-all mono, chips (category, HDR as `neutral`; `GENERATION {n}` as `progress` when the file is itself an encode of an encode; `MISSING SINCE {date}` when absent), and actions right — FOLDER (opens the containing folder via `POST /api/reveal` in the system explorer; the dashboard renders no PLAN or ENCODE control — job control is the resident process's, issue #134). **Two fact panels** side by side (stacking below 820px):
- **SOURCE** — RESOLUTION (`1920×1080`), DURATION, CODEC, SIZE, SOURCE BITRATE (Mbps, derived from size/duration), GENERATION when > 1; legend keys over mono values, em-dash for absent.
- **LATEST ENCODE — MEASURED** — every recorded schema-v5 metric: RESULT (15px/600, "{n}% saved" in teal-ink or "+{n}% larger" in red), SIZE (`5.55 GB → 2.04 GB`), VMAF (teal-ink 600) with the gauge (teal fill, amber target mark at the configured floor), CRF, PRESET, ENCODER PRESET, ENCODE TIME (`fmtSeconds`), OUTPUT BITRATE, PROBES, TOOLCHAIN. Not-encoded state: "not encoded yet — plan predicts, encode measures".
- **PROVENANCE CHAIN** (full-width, schema v7) — one row per encode touching the title, ordered newest first: the chain query matches the CLI `compare` command (the title's own rows plus rows whose output produced it or whose input it produced), so a re-encoded file's page shows its parent row too (`{when} · {parent file name}`). GEN badge (the generation number; teal-bordered only when that row *produced the file being viewed* — the chain link, never a generation-number comparison), WHEN, CRF, VMAF, BITRATE (`in → out` MBit/s — measured from ffprobe on v7 rows, `~`-prefixed size/duration derivation on pre-v7 rows, tooltip says which), SIZE, SAVED, TIME, and per-run deltas vs the predecessor (the delta line states it compares consecutive runs — siblings of one generation are two runs of the same source, not lineage). Head counts distinct GENERATIONS, not rows ("2 ENCODES · 1 GENERATION" for two encodes of one original); rows key on the encodes-row id, never the timestamp (same-second runs collide). The drift signal (rip 5.0 MBit/s → encode 1.0) is the point of the panel — and, from ≥2 rows, it renders as the **drift chart**: a descending **bitrate staircase**, one bar per step of the chain — SOURCE (the first measurable input bitrate) → GEN 1 → GEN 2 …, bar height = that step's OUTPUT bitrate on one shared scale, value labeled on top of each bar (`~` marks a derived value), steps labeled by generation (the table's own vocabulary). Round-6.8 finding (user review): per-encode IN/OUT bar *pairs* duplicated every data point — along a chain out(k) IS in(k+1) — and read as "bitrate went up"; one descending staircase tells the story unambiguously, and a rising step is the anomaly the chart exists to expose. Runs without numbers (failed encodes) stay in the table below — the chart is the measured summary, the table the complete record; slots in the VMAF lane only for encodes that produced numbers. VMAF lane sits BELOW the staircase on a fixed 85–100 domain (clamped below 85; the table carries exact numbers — a 70–100 domain gave distance-to-target 0.47px per point, unreadable), labeled bounds (VMAF 100 / 85), amber target mark inside the track, under-target dots teal-filled with an amber ring (an amber dot on the amber target line disappears). A min-max axis would let one bad encode stretch it silently. Pre-v7 values draw dashed (legend: DASHED = DERIVED); the summary sentence uses the first AVAILABLE in/out pair (`bitrate ~1.2 → 0.6 MBit/s across 3 encodes`). Chart width is measured from the container and capped at ~220px per step (left-aligned via `preserveAspectRatio="xMinYMid"` — a full-panel stretch left three pillars ~480px apart, a centered cap floated the chart mid-panel; the label of the tallest bar must keep headroom inside the viewBox, and a thin connector across step tops draws the descent as one falling line). Hand-rolled SVG, token-colored, no chart library. The sim data feeding this chart in review copies must be numerically coherent (bitrates computed from the row's own sizes) — the chart's job is to expose exactly that contradiction.
- **JOB TRAIL** (full-width) — mono rows `#{id} {kind} {state}` with states colored and ellipsized errors.

### Jobs Log
The run log: one panel, entries separated by `--row-line`, each `10px 16px`. Head row: `#{id}` in faint ink, the kind (600; "encode · quality mode"), the state in mono (done teal-ink, failed red, running amber, queued dim) with live elapsed time ("1h05m", ticked per second). **Running entries** tint amber-hatch and grow a 20px trace: `--hatch-work` track, `--hatch-progress` fill with a 2px amber leading edge, and the reading ("{movie} · {stage} · {percent}") overlaid right in mono — `role="progressbar"` with live values. Queued entries print their parameters as a dim note ("quality · VMAF>95 · bars removed"); failed entries print the error in break-word red.

### Config Page
Setup is configuration, not a footer strip — a 760px-capped, horizontally centered column of **read-only panels** (issue #331: the dashboard is a status window; the editable panels that survived #236 wrote localStorage and changed nothing the app does, so the page became a mirror of `config.json`): **SCAN SOURCES — WHERE THE LIBRARY LIVES** (mono path chips, no remove affordance, with the standing note "the resident mode encodes IN PLACE: a verified output takes the source's path and the original is kept beside it (renamed .shrinkray-original) — nothing is deleted"), **OUTPUT — WHERE ENCODES ARE WRITTEN** (mono value; "the library itself (in place)" when unset), **QUALITY TARGET — THE ENCODER'S FLOOR** (the live `quality.target_vmaf` with its HintMark pointing at `config.json` and `shrinkray setup`), **RESOURCES — BEHAVIOR ON A SHARED MACHINE** (idle-priority on/off and the max-threads cap, shown values, with the "leaves half the machine for a media server" hint), **TOOL SETTINGS — FROM CONFIG.JSON** (MOVIE EXTENSIONS as plain chips, HANDBRAKE encoder/encoder preset, ELIGIBILITY min resolution + candidate codecs, QUALITY sample settings) — every panel head stamps `read-only`, a config that exists but cannot be parsed renders its error as a dim strip inside the TOOL SETTINGS panel (its data comes from the config read; a MISSING config.json is not an error — the route returns the effective defaults), while an edit that fails to validate surfaces on the status's `config_error` field, not this page. Editing happens in `config.json` on disk or the terminal (`shrinkray setup`), never through this page — the page reflects what the engine will actually use at the next job.

### Inputs / Fields
Seam border, panel or raised-panel fill, 4px radius, mono text (paths, numbers, extensions), teal caret, faint-ink placeholder (opacity forced to 1). Search inputs focus to a teal border. Add/delete-style entries (where a page still offers them) use **dashed** borders — the unfinished-entry affordance; the config page has none (read-only, issue #331). Number fields (VMAF target 70–100, bar-detection pixels) stay mono for alignment.

### Browser Surfaces
The chrome itself is themed per `[data-theme]`: `color-scheme: dark|light` (native controls follow), `::selection` in `--teal-glow`, input carets teal, placeholders faint ink, thin scrollbars with seam thumbs (hover: faint ink), and a global `:focus-visible` — a 2px solid teal outline at 1px offset. Keyboard operators are first-class.

## Data Retrieval Architecture

Two tiers, one discipline (the snapshot's own docstring is the contract):

- **Tier 1 — the poll.** `/api/snapshot` is the only endpoint the UI polls (adaptive 1s/3s/10s): status + jobs + library in **one round-trip under one lock pass**, library and job history both ETag-cached (`null` when unchanged). The jobs fingerprint is plain `(id, state)` — sound because **live progress is not state**: it never touches the database (a running row keeps `result = NULL`), so a running row never mutates and a terminated row is written exactly once by its state transition. Nothing else ever polls.
- **Tier 1.5 — the stream.** `/api/events` (SSE) is the one FLOW channel: engine ticks fan out through an in-memory `ProgressBus` (bounded drop-oldest mailboxes, seed cache = latest event per job) into `data:` frames; a `hello` frame on every connect/reconnect seeds the client authoritatively, a ping comment every 15 s keeps intermediaries from closing idle connections. Progress is published, never persisted — the web app's progress lives exactly where the console's does (a callback), just over a wire. State is polled and cached; flow is streamed and dropped.
- **Tier 2 — on-demand detail.** Data only one view needs, fetched once when that view opens, never polled, no ETag: `/api/movie` (provenance page — file facts + COMPLETE encode history), `/api/config`. `POST` verbs (`encode`, `plan`, `reveal`) are commands, not retrieval.
- **Discipline every retrieval endpoint follows:** one request = one lock pass (a write between two separate reads would return data that never coexisted), argument lists and allowlist validation for anything path-shaped, and **no refetching what the snapshot already delivered** — the provenance page filters the app's in-memory job list locally instead of re-hitting `/api/jobs`.

## Do's and Don'ts

### Do:
- **Do** set every measurement in mono tabular figures with its unit and an em-dash for absent values (`4.12 GB`, `95.25`, `2h 14m`, `—`).
- **Do** use the legend voice for all labels: 10.5px, 1.4px tracking, uppercase, dim ink.
- **Do** express state as density — solid green = done, amber hatch = in progress, coarse amber hatch = skipped, teal hatch = selected/planned, dash-bodied swatch = untouched, dashed red = missing. A state marker must have a visible body; transparency reads as absence, not as a state.
- **Do** keep every domain one click away in the rail, with badges that carry counts and turn amber/red only when attention is needed. Counts live on exactly one surface each — the rail badges and the stat band; the sysline carries only what neither shows.
- **Do** separate failure from feedback: errors wear the red `role="alert"` strip; informational notes ("already queued", "select at least one") get the quiet seam-bordered notice. Red is for failure only.
- **Do** make the keyboard layer discoverable: the rail foot carries the shortcut legend (`1–5 pages · / search`) — invisible shortcuts are no shortcuts.
- **Do** let the library answer state questions: a decision filter (all/untouched/planned/encoded/skipped/missing) sits beside the category filter; sort state survives reload.
- **Do** show the leader-note estimate before any bulk verb, and keep bulk verbs in the bulk bar, per-movie verbs on the provenance page.
- **Do** make the overview answer failures, missing files and active work in one glance — with honest empty states ("nothing failed", "no active work", "the library is clean").
- **Do** theme through tokens only: both grounds must render the same structure, and `color-scheme` must follow `[data-theme]`.
- **Do** gate irreversible work behind a per-run confirmation (`role="alertdialog"` ceremony — the reserved shape the Dialogs & ceremonies bullet below describes; it renders nowhere today because the dashboard starts nothing); the armed state must never persist, and bar removal must never be a per-run choice again (issue #128: every re-encode crops the bars it finds).
- **Do** brief the loudest verb: a whole-library encode gets a one-line preflight strip (scope, source size, measured-throughput estimate that names its sample basis) before it can fire; Escape and NOT NOW are the safe exits. An extrapolation from a single sample must also *say so in its voice* ("rough extrapolation from a single 0.19 GB title — real runs will differ"), not borrow the confident "measured throughput" phrasing — naming the basis is necessary, not sufficient. The preflight wears the **amber** ceremony with a teal primary confirm — red borders are for irreversible work only (bars crop, delete); a reversible verb in the destruction costume trains the eye to ignore red. When no time figure can be projected, say so: "basis too thin to project wall time" beats silence next to a multi-TB scope.
- **Do** let reality overtake predictions: plan rows whose movies have since encoded read ENCODED and dim out, and the panel head stamps the plan's age — a stale plan must never present itself as the current queue.
- **Do** mark persistence models in the UI: a command the page cannot run (config edits, issue #331) is not offered at all — the config page's panels are read-only mirrors of `config.json`, and editing happens on disk or via `shrinkray setup`; a page that shows values it cannot change must SAY read-only at the panel head rather than silently accepting clicks that change nothing. Identical silence with opposite truths is a trust bug.
- **Do** let estimates fall silent when their basis is zero: a selection of already-encoded titles gets "no estimate (selection already encoded)", never a floor constant dressed up as a saving.

### Don't:
- **Don't** use shadows, glows, or blur — depth is tone, line weight, and hatch only (the 900px inset underline is the single documented exception).
- **Don't** apply red to anything but failure/irreversible destruction, amber to anything but progress/warning, green to anything but done/keep.
- **Don't** branch a component on theme or hardcode a theme's hex — one structure, two grounds.
- **Don't** reintroduce the round-1 failures: no decorative empty figures (empty states are honest text in dashed panels), no exploding category-chip rows (filter with the select), no work area below its list, no settings buried off-page (config is a first-class page with a rail slot).
- **Don't** dress the console in media-server or terminal costume — no poster artwork, no gaming-RGB, no CRT/scanline effects; recognition comes from the typographic table (the poster-grid experiment was demoted away; the reserved spec keeps the typographic principle).
- **Don't** round corners beyond the 4px chrome / 2px chip radii, or introduce gradients beyond the hatch patterns (the poster field's gradient is reserved with the poster spec).
- **Don't** round numbers into vagueness or spin a bad result — growth prints "+X% larger" in red, and a real 43 MB win never prints as "−0.0 GB (0%)"; sub-1 GB reclaimed reads in MB, a sub-1% share prints "<1%", and an extrapolated estimate names its sample basis.

## Accessibility

Baseline commitments observed in the built components (single-user localhost, but keyboard operability and readable contrast are the product's stated floor):

- **Focus**: global `:focus-visible` — 2px solid teal outline, 1px offset, in both themes. Checkboxes use `accent-color` (teal for selection). The movie page takes focus on open (`tabindex="-1"`) and closes on Escape.
- **Dialogs & ceremonies**: the UI's dialog-adjacent surface today is the detail page: it receives focus on open (`tabindex="-1"`), closes on Escape, and restores focus to the triggering rail item (App.svelte's closeDetail). The bar-crop confirmation ceremony (a `role="alertdialog"` strip, trap Tab, Escape → the safe verb, focus restored to the trigger, the destructive control stating the pixels it removes in `aria-label`/`title`) is the reserved shape a destructive action must wear if the product ever offers one — it renders nowhere today.
- **Live status**: API errors use `role="alert"` with a labeled dismiss; both traces are `role="progressbar"` with `aria-valuenow/min/max` and labels ("encode progress", "{kind} progress").
- **Navigation & data**: the rail nav is `aria-label="Sections"`; search, category select, and every checkbox carry `aria-label`s (including "Select {movie}"); icon-only ✕ buttons are labeled (the dismiss ✕ names what it dismisses); table sort headers carry `aria-sort`; page navigation is hash-routed (`#/library` etc.) with keyboard shortcuts `1`–`5` (pages) and `/` (search focus).
- **Contrast**: accent text always goes through `--teal-ink` (dark `#2dd4bf`, light `#0b5952`); secondary text is `--ink-dim`; `--ink-faint` is the contrast floor chosen per theme (round-6: verified ≥ 4.5:1 on *every* ground of its own theme, not just the panel — light `#565d66` after the rail sweep caught `#6d757d` at 3.62:1); accent hues are deepened on light (`#8a4307`, `#b3382c`, `#15803d`) so they survive the bench.
- **Motion**: both authored animations — `leader-draw` (240ms, the leader note) and `page-fade` (200ms, page entry) — are wrapped in `prefers-reduced-motion: no-preference` and degrade to plain appearance; the hint glyphs' opacity transitions run 120ms ease (`HintButton.svelte`/`HintMark.svelte`). No card border transition exists (no card component renders).
