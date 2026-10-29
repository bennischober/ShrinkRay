#!/usr/bin/env bash
# The repository's gate (AGENTS.md): every check a change must pass that runs
# offline, in ONE place. ci.yml, release.yml, the dev image's CMD and this file
# all run the same script, so the contract cannot drift between them. CI adds
# only what needs the network (`uv audit --locked`) and the separate ui/ job.
#
# Usage: tools/gate.sh [--no-cov]
#   --no-cov   skip coverage failure thresholds (the release job's test run
#              measures coverage separately; use it only where the caller
#              documents why)
set -euo pipefail
cd "$(dirname "$0")/.."

extra=()
if [ "${1:-}" = "--no-cov" ]; then
    extra=(--no-cov)
fi

echo "==> uv lock --check"
uv lock --check
echo "==> ruff check"
uv run ruff check src tests
echo "==> ruff format --check"
uv run ruff format --check src tests
# mypy's compiled core (mypyc) segfaults under beta interpreters (issue #407:
# CPython 3.15.0b3 exits 139 before printing anything, on every invocation, while
# the same mypy 2.4.0 passes under every stable CPython). A 139 is always
# environmental - type errors are exits 1/2 - so the gate re-runs the failing
# platform under a stable interpreter instead of leaving the gate unusable on a
# host whose .python-version resolves to a beta. The re-run is LOUD on purpose:
# the warning names both interpreters, and the re-run's own verdict is what the
# gate reports (a real type error still fails the gate).
run_mypy() {
    local platform="$1"
    echo "==> mypy ($platform platform)"
    # capture the rc BEFORE branching: after `if cmd; then` with no else, $? is
    # 0 (bash gives a childless if the status of the failed condition's branch,
    # i.e. zero) - `local rc=$?` after `fi` read 0 even for a segfault (measured,
    # issue #407's own demonstration)
    local rc=0
    uv run mypy --platform "$platform" || rc=$?
    if [ "$rc" -eq 0 ]; then
        return 0
    fi
    if [ "$rc" -ne 139 ]; then
        return "$rc"
    fi
    echo "WARNING: mypy segfaulted (exit 139) under $(uv run python --version 2>/dev/null || echo 'the default interpreter')." >&2
    for candidate in 3.14 3.13 3.12; do
        echo "==> mypy ($platform platform, retry under CPython $candidate)"
        if UV_PYTHON="$candidate" uv run mypy --platform "$platform"; then
            echo "WARNING: the $platform mypy pass above ran under CPython $candidate, not the project's .python-version interpreter (issue #407)." >&2
            return 0
        fi
    done
    echo "ERROR: mypy segfaulted and no stable fallback interpreter (3.14/3.13/3.12) is installed; install one or run the gate under a stable Python." >&2
    return 139
}
run_mypy linux
run_mypy win32
echo "==> pytest"
uv run pytest -q "${extra[@]}"
echo "==> lint-imports"
uv run lint-imports
echo "GATE PASSED"
