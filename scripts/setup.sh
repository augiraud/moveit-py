#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for cmd in git uv; do
  command -v "$cmd" >/dev/null || { echo "Error: $cmd is required. Install it, then run this script again." >&2; exit 1; }
done

if [[ -f scripts/init_project.py && -t 0 ]]; then
  uv run --no-build --no-project scripts/init_project.py
fi

uv sync --locked --no-build

if [[ -z "${CI:-}" ]]; then
  git config core.hooksPath .githooks
fi

echo "Setup complete. Check everything with ./scripts/local_ci.sh"
