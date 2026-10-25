"""Extracts a version's release notes from CHANGELOG.md.

The matching "## [<version>]" section of CHANGELOG.md IS the release note
(the changelog-discipline rule in AGENTS.md makes every PR write its entry
there), so the release job renders that section into the GitHub release body
instead of maintaining a second document. The section ends at the next "## "
heading or at the link-reference block ("[x]: https://...") that follows the
notes; trailing blank lines are stripped.

Usage:
    python tools/release_notes.py <version> [--changelog CHANGELOG.md]

Prints the section body to stdout; exits 0 with a generic fallback body when
the section is missing (a mis-tagged release must still publish, with a note
pointing at the changelog), exits 1 on a missing changelog file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_FALLBACK = (
    "## ShrinkRay {tag}\n\n"
    "See [CHANGELOG](https://github.com/{repo}/blob/main/CHANGELOG.md) and the "
    "[README](https://github.com/{repo}#readme)."
)


def extract_section(changelog: str, version: str) -> str | None:
    """Returns the "## [<version>]" section's body, or None when absent.

    Args:
        changelog (str): The full CHANGELOG.md text.
        version (str): The version WITHOUT the "v" prefix (a tag "v0.4.0"
            names the heading "## [0.4.0]").

    Returns:
        str | None: The section body with trailing blank lines stripped, or
            None when no heading matches.
    """
    target = f"## [{version}]"
    lines = changelog.splitlines()
    body: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith(target):
            # a heading like "## [0.4.0] - 2026-10-25" matches; a longer
            # version must not ("## [0.4]" must not match "## [0.4.1]")
            in_section = True
            continue
        if in_section:
            if line.startswith("## ") or (line.startswith("[") and "]: " in line):
                break
            body.append(line)
    if not in_section:
        return None
    while body and not body[-1].strip():
        body.pop()
    while body and not body[0].strip():
        body.pop(0)
    return "\n".join(body) + "\n" if body else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="the release version, with or without the v prefix")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="the changelog file (default: CHANGELOG.md in the working directory)",
    )
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    if not args.changelog.is_file():
        print(f"{args.changelog} not found", file=sys.stderr)
        return 1
    body = extract_section(args.changelog.read_text(encoding="utf-8"), version)
    if body is None:
        repo = "bennischober/ShrinkRay"
        print(_FALLBACK.format(tag=f"v{version}", repo=repo))
        return 0
    print(body, end="" if body.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
