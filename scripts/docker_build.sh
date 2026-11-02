#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker build --build-arg PYTHON_VERSION="$(cat .python-version)" --tag python-template "$@" .
