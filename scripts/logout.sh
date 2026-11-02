#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Forget your MOVEit sign-in.

Usage:   logout.sh [--url URL]
Example: ./scripts/logout.sh
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

[ $# -eq 0 ] || bad_usage
check_setup
run_tool logout
