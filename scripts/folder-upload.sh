#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Copy a local directory into a MOVEit folder. Files already there are skipped. Nobody is notified:
to send files to people, see package-send.sh.

Usage:   folder-upload.sh [--url URL] LOCAL_DIR MOVEIT_FOLDER
Example: ./scripts/folder-upload.sh ./results /Home/abcdef123456/results

Your home folder (/Home/...) is shown by login.sh.
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

[[ $# -eq 2 ]] || bad_usage
[[ -d "$1" ]] || fail "LOCAL_DIR '$1' is not a directory"
case "$2" in
  /*) ;;
  *) fail "MOVEIT_FOLDER must start with /, e.g. /Home/abcdef123456/results" ;;
esac
check_setup
run_tool upload "$1" "$2"
