#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Sign in to MOVEit with your browser, and show your home folder (/Home/...).
The sign-in expires after 20 minutes without use.

Usage:   login.sh [--url URL]
Example: ./scripts/login.sh
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

[[ $# -eq 0 ]] || bad_usage
check_setup
run_tool login
