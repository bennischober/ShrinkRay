# Security Policy

## Supported versions

Only the latest release (and `main`) receives security fixes. ShrinkRay is a
self-hosted tool; if you run an older release, update before reporting a
problem that a newer version may already have fixed.

## Reporting a vulnerability

Please report privately through
[GitHub Security Advisories](https://github.com/bennischober/ShrinkRay/security/advisories/new)
("Report a vulnerability"). Do not open a public issue for anything you
believe is exploitable.

What to include: the affected version or commit, how to reproduce it, and —
for anything involving a crafted media file — the smallest file that triggers
it. A report that cannot be reproduced will be closed as unclear after a
reasonable effort, so the reproduction matters more than the write-up.

## Response expectations

This is a best-effort, single-maintainer project: expect an initial response
within about a week. There is no bounty and no formal SLA. Fixes land through
the normal pull-request workflow; a fix release follows as soon as the fix is
verified. You will be credited in the release notes unless you ask not to be.

## Current security posture (read before exposing the dashboard)

The resident server has token-based authentication (issue #36). How it works:

- The token comes from $SHRINKRAY_TOKEN. Never from `config.json`, which is a
  tracked file.
- **Loopback default:** with no token set, the server assumes a local
  development session and binds to `127.0.0.1` only.
- **Public bind requires a token:** the app refuses to start on a non-loopback
  address with no token configured - an open dashboard on a LAN cannot happen
  by accident.
- **Generated tokens (containers, issue #267):** with $SHRINKRAY_TOKEN unset,
  the container generates a secret at boot, stores it in
  $SHRINKRAY_TOKEN_FILE's path and prints it to the console, so the
  dashboard is protected without operator setup.

Every route - all twelve GET routes and the single POST - sits behind the same
token middleware; there are no unauthenticated routes. The middleware is
registered before the routes, so a NEW route cannot be added unauthenticated
by accident. `/api/events` additionally accepts the token as `?token=`
because EventSource cannot set headers - the one documented exception.

The one state-changing route is `POST /api/reveal` (opens a file-manager
window on the SERVER host for an allowlisted path). It is capability-gated:
`/api/status` reports `reveal_available` (the opener binary exists), and
the dashboard hides the button where it cannot work (a container). It spawns
the opener, never deletes or moves anything.

The control surface is the TERMINAL (`shrinkray setup`), not a route: it
edits settings and pauses the backlog through state the resident process
shares in the data folder (the config file, a pause flag). No route starts,
cancels or reconfigures work (issue #134) - there is no enqueue, cancel or
config-write route to attack.

Binding the server to a LAN interface is your decision; with a token set it
is the supported shape (that is the container's default). This section must
stay in sync with the README's dashboard section - if one changes, change
both.

## Scope notes

- ShrinkRay shells out to `ffprobe`, `ffmpeg` and `HandBrakeCLI`; all
  subprocess calls use argument lists (never shell strings) and timeouts.
- Media file paths from API requests are validated against an allowlist of
  configured roots before any file operation (`validate_path` in
  `src/shrinkray/server/app.py`).
- Source media files are never deleted; replaced originals are renamed aside
  and kept.
