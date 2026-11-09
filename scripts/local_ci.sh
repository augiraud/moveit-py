#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

fix=false
case "${1:-}" in
  "") ;;
  --fix) fix=true ;;
  -h | --help) echo "Usage: $0 [--fix]"; exit 0 ;;
  *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
esac

export UV_LOCKED=1

failed=""
step() {
  local name=$1
  shift
  echo "==> $name"
  "$@" || failed="$failed"$'\n'"  - $name"
}

if $fix; then
  uv run --no-build ruff format
  uv run --no-build ruff check --fix || true
fi

step "Format (ruff)" uv run --no-build ruff format --check
step "Lint (ruff)" uv run --no-build ruff check
step "Types (ty)" uv run --no-build ty check
if command -v shellcheck > /dev/null 2>&1; then
  step "Shell (shellcheck)" shellcheck -x -P SCRIPTDIR scripts/*.sh
else
  echo "==> Shell (shellcheck): skipped, shellcheck is not installed"
fi
step "Tests (pytest)" uv run --no-build pytest --cov --cov-report=term-missing --cov-report=xml

if [[ -n "$failed" ]]; then
  echo "Failed:$failed"
  exit 1
fi
echo "All checks passed."
