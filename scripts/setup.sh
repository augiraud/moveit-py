#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for cmd in git uv docker; do
  command -v "$cmd" >/dev/null || { echo "Error: $cmd is required. Install it, then run this script again." >&2; exit 1; }
done

if [ -f scripts/init_project.py ] && [ -t 0 ]; then
  uv run --no-project scripts/init_project.py
fi

uv sync --locked

if [ -z "${CI:-}" ]; then
  git config core.hooksPath .githooks
fi

echo "Setup complete. Try ./scripts/run.sh, and check everything with ./scripts/local_ci.sh"
