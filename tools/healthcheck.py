"""The container image's HEALTHCHECK (issues #165, #242).

The image runs either mode: with no arguments it starts the resident server,
with arguments it runs one CLI command and exits. Health only means something
for the server - "/api/status answers" - so a one-shot container (no server,
usually no token) reports healthy instead of turning `unhealthy` for the hours
an encode takes. PID 1's argv tells the two apart: the server is the `serve`
command (or its hidden `app` alias).

The app requires the token on every request of a non-loopback bind (issue
#36), and the container binds 0.0.0.0, so the check sends it: $SHRINKRAY_TOKEN
when set, else the token the server generated into $SHRINKRAY_TOKEN_FILE (#249).
"""

import os
import sys
import urllib.request
from pathlib import Path

SERVER_COMMANDS = {b"serve", b"app"}
PID1_CMDLINE = Path("/proc/1/cmdline")


def runs_server(cmdline: Path = PID1_CMDLINE) -> bool:
    """Tells whether PID 1 is the resident server rather than a CLI command.

    Args:
        cmdline (Path): The NUL-separated argv file to read (PID 1's in the
            container; tests pass their own).

    Returns:
        bool: True when one argv element is a server command.
    """
    argv = cmdline.read_bytes().split(b"\0")
    return any(arg in SERVER_COMMANDS for arg in argv)


def token() -> str:
    """Returns the token the server checks: the same order as the server."""
    configured = os.environ.get("SHRINKRAY_TOKEN", "")
    if configured:
        return configured
    token_file = os.environ.get("SHRINKRAY_TOKEN_FILE", "")
    if token_file and Path(token_file).is_file():
        return Path(token_file).read_text(encoding="utf-8").strip()
    return ""


def main(cmdline: Path = PID1_CMDLINE) -> int:
    """Returns the healthcheck's exit code: 0 healthy, non-zero unhealthy."""
    if not runs_server(cmdline):
        return 0
    request = urllib.request.Request(
        "http://127.0.0.1:8420/api/status",
        headers={"Authorization": "Bearer " + token()},
    )
    with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310 - fixed http://127.0.0.1 URL
        return 0 if response.status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
