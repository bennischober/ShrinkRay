"""The docs' routes and commands are mechanically aligned with the code.

#238 and #288 each found user-facing docs naming removed routes and commands -
caught by hand, after the fact. This test is the mechanical check behind the
"no unsupported claim in a doc" invariant: every /api/... path the docs name
must be a registered FastAPI route, and every shrinkray <command> the docs
mention must be a registered typer command.

Exclusions, by the issue's rules: CHANGELOG.md is excluded BY FILE (history
deliberately names removed surfaces), and a sentence that ASSERTS
non-existence ("no longer exist") is not a claim about a live surface - it is
the doc telling the truth about history, which is exactly what we want docs
to do. Anything else naming a missing surface fails.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# the user-facing docs the check covers (CHANGELOG.md deliberately absent:
# it is history, and history names removed routes on purpose)
DOC_FILES = [
    REPO / "README.md",
    REPO / "PRODUCT.md",
    REPO / "SECURITY.md",
    *sorted((REPO / "docs").glob("*.md")),
]

ROUTE_RE = re.compile(r"(?:(GET|POST|PUT|DELETE|PATCH)\s+)?(/api/[a-z0-9_/{},.-]+)")
COMMAND_RE = re.compile(r"\bshrinkray ([a-z][a-z0-9-]*)")
# a sentence that says the surface is gone is not claiming it exists
NONEXISTENCE_RE = re.compile(r"no longer|does not exist|do not exist|don't exist")


def _sentences(text: str) -> list[str]:
    """Splits the text into sentence-ish spans (a match is judged in its own)."""
    return re.split(r"(?<=[.!?:])\s+", text)


def _registered_routes() -> set[tuple[str, str]]:
    """The app's registered (METHOD, path) pairs (from the app, not a grep)."""
    from shrinkray.server.app import app

    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or {"GET"}
        for method in methods:
            pairs.add((method, route.path))
    return pairs


def _registered_commands() -> set[str]:
    """The typer CLI's registered command names (from the app, not a grep)."""
    import typer

    from shrinkray.__main__ import app as cli_app

    return set(typer.main.get_command(cli_app).commands)


class TestDocsNameRealSurfaces(unittest.TestCase):
    def test_every_api_route_in_the_docs_is_registered(self) -> None:
        routes = _registered_routes()
        self.assertGreaterEqual(len(routes), 10, "the server exposes no routes?")
        checked = 0
        for doc in DOC_FILES:
            text = doc.read_text(encoding="utf-8")
            for match in ROUTE_RE.finditer(text):
                method, raw = match.group(1), match.group(2)
                path = raw.rstrip(".,;:)")
                sentence = next((s for s in _sentences(text) if path in s), "")
                if NONEXISTENCE_RE.search(sentence):
                    continue  # the doc says it is gone - that is history, not a claim
                claimed = f"{method} {path}" if method else path
                with self.subTest(doc=doc.name, claimed=claimed):
                    if method:
                        # a method named IS a claim about that pair (POST /api/jobs
                        # is not saved by a GET /api/jobs existing)
                        self.assertIn(
                            (method, path),
                            routes,
                            f"{doc.name} names {claimed}, which the server does not register",
                        )
                    else:
                        self.assertIn(
                            path,
                            {p for _, p in routes},
                            f"{doc.name} names {path}, which the server does not register",
                        )
                checked += 1
        self.assertGreaterEqual(checked, 5, "the docs stopped naming API routes?")

    def test_every_shrinkray_command_in_the_docs_is_registered(self) -> None:
        commands = _registered_commands()
        self.assertGreaterEqual(len(commands), 5, "the CLI exposes no commands?")
        checked = 0
        for doc in DOC_FILES:
            text = doc.read_text(encoding="utf-8")
            for match in COMMAND_RE.finditer(text):
                name = match.group(1)
                sentence = next((s for s in _sentences(text) if f"shrinkray {name}" in s), "")
                if NONEXISTENCE_RE.search(sentence):
                    continue  # the doc says it is gone - that is history, not a claim
                with self.subTest(doc=doc.name, command=name):
                    self.assertIn(
                        name,
                        commands,
                        f"{doc.name} names 'shrinkray {name}', which the CLI does not define",
                    )
                checked += 1
        self.assertGreaterEqual(checked, 5, "the docs stopped naming commands?")


if __name__ == "__main__":
    unittest.main()
