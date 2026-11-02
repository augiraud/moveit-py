#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Send files to people by email, as a MOVEit package. Directories are zipped.
To copy files into a MOVEit folder instead, see folder-upload.sh.

Usage:   package-send.sh [--url URL] SUBJECT EMAILS PATH...
Example: ./scripts/package-send.sh "Results" alice@example.com,bob@example.com ./results ./report.pdf

Running it twice sends the package twice. For files larger than 50 MB, see package-send-large.sh.
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

[ $# -ge 3 ] || bad_usage
subject=$1
emails=$2
shift 2
case "$emails" in
  *@*) ;;
  *) fail "EMAILS must be comma-separated emails, e.g. alice@example.com,bob@example.com" ;;
esac
for p in "$@"; do
  [ -e "$p" ] || fail "'$p' does not exist"
done
check_setup
run_tool send --to "$emails" -- "$subject" "$@"
