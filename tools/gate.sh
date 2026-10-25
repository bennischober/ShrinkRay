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
echo "==> mypy (linux platform)"
uv run mypy --platform linux
echo "==> mypy (win32 platform)"
uv run mypy --platform win32
echo "==> pytest"
uv run pytest -q "${extra[@]}"
echo "==> lint-imports"
uv run lint-imports
echo "GATE PASSED"
