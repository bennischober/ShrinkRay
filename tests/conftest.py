"""Session-wide guard: the test suite must not pollute the working tree.

A test that touches a fictional path creates real entries in the caller's
working directory (issue #38: an encode payload of 'Z:/out' is a relative
path on POSIX, so the job engine created a literal 'Z:' directory next to
the checkout). `git status --porcelain` is blind to empty directories —
the exact shape of that defect — so the listing comes from
`git ls-files --others --directory`, which reports untracked directories
including empty ones.

The check compares before/after the session: pre-existing untracked entries
(local agent config, tool caches) are not the suite's doing and never flagged.
In a checkout shared by concurrent agents, an entry created by another process
during the run is flagged too — the message names it, so it is diagnosable.
"""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _untracked_entries() -> set[str] | None:
    """Returns the checkout's untracked entries (empty directories included).

    Returns None when there is nothing to guard against (no git, no checkout,
    e.g. an installed sdist): the check simply does not run then.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--directory", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return set(result.stdout.splitlines())


@pytest.fixture(autouse=True)
def _isolate_scratch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's doctor tests must not depend on the ambient scratch env.

    The product's `doctor` reads `TMPDIR`/`TEMP`/`TMP` (issue #440): on a dev
    machine whose scratch holds real probe debris, every doctor test's
    expectation would flip (the round-16 reviewer measured 6 failures from
    one stale workdir in `$TMPDIR`). The suite isolates the three variables
    per test; a test that WANTS a configured scratch patches it explicitly
    (`patch.dict(os.environ, {...})` inside the test, which wins over this
    isolation).
    """
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)


def pytest_configure() -> None:
    """Drops the stability overrides before any test module imports watch.

    `watch.py` reads `SHRINKRAY_STABLE_CHECK_INTERVAL` / `SHRINKRAY_STABLE_MIN_AGE`
    at IMPORT time (issue #537's test seam), so a per-test fixture is too late: a
    value exported in the developer's shell would already have pinned the
    constants and flipped the tests that assert the defaults
    (`test_file_stability` pins 30.0; `test_watch`'s is_stable cases assume
    60.0). conftest is imported before collection, so this runs before any test
    module imports watch. The SIGHUP test exports the overrides into its CHILD's
    environment explicitly and is unaffected.
    """
    for var in ("SHRINKRAY_STABLE_CHECK_INTERVAL", "SHRINKRAY_STABLE_MIN_AGE"):
        os.environ.pop(var, None)


@pytest.fixture(scope="session", autouse=True)
def _no_working_tree_pollution() -> Iterator[None]:
    """Fails the session when a test leaves a new untracked entry behind."""
    before = _untracked_entries()
    yield
    if before is None:
        return
    after = _untracked_entries()
    if after is None:
        return
    created = sorted(after - before)
    assert not created, (
        f"the test suite left untracked paths in the checkout: {created}"
        " - tests write into tempfile.TemporaryDirectory()/tmp_path, never"
        " into the working tree"
    )
