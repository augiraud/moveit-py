#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Copy a MOVEit folder into a local directory. Files already downloaded are skipped.
To download files sent to you, see package-receive.sh.

Usage:    folder-download.sh [--url URL] MOVEIT_FOLDER LOCAL_DIR [FILTER]
Examples: ./scripts/folder-download.sh /Data/project/results ./results
          ./scripts/folder-download.sh /Data/project/results ./results '\.zip$'   # only .zip files

FILTER is a regular expression matched against the MOVEit file paths.
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

{ [ $# -eq 2 ] || [ $# -eq 3 ]; } || bad_usage
case "$1" in
  /*) ;;
  *) fail "MOVEIT_FOLDER must start with /, e.g. /Data/project/results" ;;
esac
check_setup
if [ -n "${3:-}" ]; then
  run_tool download "$1" "$2" --filter "$3"
else
  run_tool download "$1" "$2"
fi
