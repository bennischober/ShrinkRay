# Terminal surface: live watch display, TUI (idea - not built)

Status: **idea, scoped down on purpose**. Recorded 2026-10-23 after a
friend pointed at their Textual TUI (orthos image-generation client,
gitlab.uni-ulm.de/fat37/orthos-examples). Nothing below is implemented.

## The concept question

"TUI" is just terminal UI - a class of interface, not a product
decision. ShrinkRay already HAS a terminal application: the typer CLI.
The real question is which terminal jobs need more than one-shot
commands. The interface matrix:

|                 | CLI (have)              | TUI (Textual-style)     | Web dashboard (have)      |
|-----------------|-------------------------|-------------------------|---------------------------|
| Model           | command -> output -> exit | persistent, fullscreen | persistent, browser       |
| Strength        | scripts, cron, exit codes | live monitoring over SSH | richness, job sheets    |
| NAS/SSH         | works                   | works, zero ports/browser | needs published port     |
| Automation      | excellent               | none (interactive)      | via API                   |

CLI and TUI are not competitors; they serve different jobs. The
dashboard already covers browsing/actions, so a full Textual TUI would
be a THIRD persistent interface duplicating it - the same
"second artifact to keep fresh" cost recorded in docs/docker.md.

## The one genuine gap: watch monitoring over SSH

Exactly one job is badly served today: watching the inbox/watch loop
from a headless NAS over SSH without a browser. `watch` mode is a log
stream - functional, but no at-a-glance state (inbox contents, known
set, disk free vs floor, current movie + percent).

## The right-sized step (when this gets built)

A **rich `Live` layout inside the existing `shrinkray watch` command** -
not a new interface, not a new dependency (rich is already in):

- header: inbox path, output dir, disk free vs DISK_FREE_FLOOR_BYTES
- table: inbox files, stability state, known/retry
- footer: current encode + percent bar (the A4 progress callback
  already produces the percent events)

Estimated ~50-100 lines, incremental upgrade of the terminal surface,
fully in our control.

## Full Textual TUI: trigger condition

Only worth it if terminal-first NAS administration becomes the primary
workflow (or the friend actually wants to build one). Trigger: someone
who lives in SSH wants library browsing + job control in the terminal
and the web dashboard is not enough. Textual itself is mature and
headless-testable (the orthos probe pattern - monkeypatched API +
run_test - is a good recipe and reusable for the Live layout too).

## Stolen with credit (orthos TUI patterns worth adopting)

- **PEP 723 inline script deps**: their `tui.py` is `uv run tui.py` with
  `# /// script` dependencies - self-contained, no project venv needed.
  Our helper scripts (scripts/verify_fullmovie.py) could adopt this so
  they run on any machine with uv alone. Small, real win.
- **Maintenance-rules + verify-recipe docs discipline**: constants that
  encode measured external behavior are documented as such ("don't
  change without a live test"), and each component carries its own
  headless verification recipe. Fits our AGENTS/TODO honesty style;
  candidate pattern for docs/ pages.

## Related

- docs/docker.md - the "second artifact" cost pattern this decision
  reuses; container entrypoint modes are the same deployment question
- Issue #179 - the PEP 723 idea for scripts/, filed from this page
- src/shrinkray/engine/watch.py - the watch loop that would get the Live layout
- src/shrinkray/__main__.py - A4 progress callback (percent events)
