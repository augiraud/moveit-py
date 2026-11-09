#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Download the files of a package sent to you. Files already downloaded are skipped.
To copy a MOVEit folder instead, see folder-download.sh.

Usage:   package-receive.sh [--url URL] PACKAGE_ID LOCAL_DIR
Example: ./scripts/package-receive.sh 379341516 ./received

Package IDs are listed by package-list.sh.
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

[[ $# -eq 2 ]] || bad_usage
case "$1" in
  "" | *[!0-9]*) fail "PACKAGE_ID must be a number, e.g. 379341516 (see package-list.sh)" ;;
esac
check_setup
run_tool receive "$1" "$2"
