# Terminal surface: the TUI is the control surface (decided, not built)

Status: **decided 2026-10-25 (issue #244), nothing implemented.** This replaces the
2026-10-23 note that scoped a TUI down to a live watch display (see *What this
supersedes* below). PRODUCT.md's "The terminal is where the operator configures and
controls" and "Deployment target" sections are the contract; this file records the
reasoning and the open design work.

## The decision

ShrinkRay has two interactive surfaces, split by role:

|               | Web dashboard (have)             | Terminal / TUI (decided)                          |
|---------------|----------------------------------|---------------------------------------------------|
| Job           | observe the resident mode        | configure and control                             |
| Reached by    | any browser on the LAN (token)   | a shell on the host or in the container           |
| Can change    | nothing (read-only, issue #134)  | every setting, pause/resume, run one file         |
| Unraid button | WebUI                            | Console                                           |

The trigger the old note named for a full TUI has arrived: terminal-first
administration is the primary workflow of the deployment target. An Unraid operator
opens the container's console, reads a printed banner, and types the command it names
— the established pattern of Unraid apps. The old objection (a TUI would be a third
persistent interface duplicating the dashboard) no longer holds, because the two do
not overlap: the dashboard cannot control anything, and the TUI is not a status
window.

## What the terminal surface is

- **`shrinkray setup`** — a TUI that edits every setting an operator may change:
  required VMAF, thread caps, eligibility gates (resolution floor upward only, minimum
  file size), and the rest of `config.json`; plus pause/resume of the backlog where a
  resident process runs. It names the cost of a change before saving it (a setting in
  the plan fingerprint re-measures the library). A saved change applies at the next
  file. On a native install it edits that install's config.
- **`shrinkray encode <path>`** — the existing Mode A command, unchanged, as the
  single-file path. By path for now; a file picker inside the TUI may come later. It
  warns at start that closing the terminal cancels the encode, and the resident
  watcher pauses while it runs.
- **The console banner** — printed when a shell opens in the container: what to type
  to configure, to encode one file, to recover originals, and where the dashboard is
  (with the generated token).

## Constraints the build must keep

- **No network route for control.** The TUI reaches the resident process through the
  state they already share on disk in `/data` (the config file, a pause flag). The
  dashboard stays read-only by construction.
- **The CLI never requires the resident process.** The "watcher pauses during a manual
  encode" signal is written by the CLI to shared state and honoured by the watcher;
  the CLI does not call the server. A crashed CLI must not leave the watcher paused
  forever (the encode lock's stale-holder rule is the model: dead pid = released).
- **Today's encode lock does not do this.** It is per output directory
  (`engine/lock.py:47`), so a one-shot encode and the watcher can run concurrently on
  different directories, and on a collision the watcher skips the file rather than
  pausing (`engine/watch.py:578`). The pause is a new, container-wide signal.
- **Settings, not rules.** The TUI exposes what PRODUCT.md leaves open. It cannot
  lower the 1080p floor, add a second encode mode, keep bars, or pick another output
  codec.
- **Headless-testable.** Whatever library the TUI uses must be testable without a
  terminal in the flat `unittest` suite (Textual's `run_test` pilot is the known
  recipe), and adding it as a dependency needs the RULES.md §7 justification.

## Open design work (follow-up issues)

- The TUI library and the screen layout.
- The shape of the pause flag and the config reload in the resident process.
- The banner mechanism in the image (a shell rc file printed on interactive start).

## What this supersedes

The 2026-10-23 note (after a friend's Textual client) concluded that a full TUI
would duplicate the dashboard and proposed only a rich `Live` layout inside a
`shrinkray watch` command for SSH monitoring. That command no longer exists (the
watch loop runs inside `serve`, issue #202), and the dashboard became read-only
(issue #134), which removed the duplication argument. Two patterns from that note
remain adopted: PEP 723 inline script dependencies (`scripts/verify_fullmovie.py`,
issue #179) and documenting a headless verification recipe per component.

## Related

- PRODUCT.md — "The terminal is where the operator configures and controls",
  "Deployment target: an Unraid Community App"
- docs/docker.md — the image, `/data`, and the reopened `PUID`/`PGID` decision
- Issue #242 — the image's entrypoint split, which lets the console run any command
