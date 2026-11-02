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
  uv run ruff format
  uv run ruff check --fix || true
fi

step "Format (ruff)" uv run ruff format --check
step "Lint (ruff)" uv run ruff check
step "Types (ty)" uv run ty check
step "Tests (pytest)" uv run pytest --cov --cov-report=term-missing --cov-report=xml
step "Docker image" ./scripts/docker_build.sh

if [ -n "$failed" ]; then
  echo "Failed:$failed"
  exit 1
fi
echo "All checks passed."
