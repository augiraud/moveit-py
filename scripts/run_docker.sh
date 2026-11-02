#!/usr/bin/env bash
set -euo pipefail
"$(dirname "$0")/docker_build.sh" --quiet >/dev/null
exec docker run --rm python-template "$@"
