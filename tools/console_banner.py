"""What the container console prints when a shell opens (issue #251).

Opening a console on the container (Unraid's Console button, or `docker exec
-it <container> sh`) is how an operator drives ShrinkRay; the banner is their
manual. Every line is computed from the container's real state - whether the
server runs, the token it checks, whether it is paused - and names only a
command that exists, so it never sends anyone somewhere that is not there.

Printed by tools/container-console.sh, once per console, for interactive
shells only: a `docker exec <container> <command>` keeps a clean stdout.
"""

from pathlib import Path

from healthcheck import PID1_CMDLINE, runs_server, token

from shrinkray.engine.pause import PAUSE_FLAG, pause_reason, state_dir_for_cli


def banner(cmdline: Path = PID1_CMDLINE) -> str:
    """Builds the banner for the container's current state.

    Args:
        cmdline (Path): PID 1's argv file (tests pass their own).

    Returns:
        str: The lines to print.
    """
    server = runs_server(cmdline)
    data = state_dir_for_cli(None) or Path("/data")
    flag = data / PAUSE_FLAG
    lines = [
        "ShrinkRay - the resident encoder is running."
        if server
        else "ShrinkRay - this container runs a one-shot command, not the resident server.",
        "  Configure, pause, resume: shrinkray setup",
        "  Encode one file now:      shrinkray encode /media/<path>",
        f"  List retained originals:  shrinkray retained list --db {data / 'metadata.db'}",
    ]
    if server:
        reason = pause_reason(data)
        if flag.exists():
            lines.append(f"  Paused now:               resume in shrinkray setup (or rm {flag})")
        elif reason is not None:
            lines.append(f"  Waiting:                  {reason}")
        current = token()
        lines.append(
            "  Dashboard:                the WebUI button (port 8420), token: "
            + (current or "none - the dashboard is open to the network")
        )
    lines.append("  Everything else:          shrinkray --help")
    return "\n".join(lines)


if __name__ == "__main__":
    print(banner())
