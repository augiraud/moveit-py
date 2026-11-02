#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
List your MOVEit packages, newest first. Unopened ones are marked with *.

Usage:    package-list.sh [--url URL] [--sent]
Examples: ./scripts/package-list.sh           # packages sent to you
          ./scripts/package-list.sh --sent    # packages you sent
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

if [ $# -eq 0 ]; then
  check_setup
  run_tool packages
elif [ $# -eq 1 ] && [ "$1" = "--sent" ]; then
  check_setup
  run_tool packages --sent
else
  bad_usage
fi
