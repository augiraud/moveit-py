#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
usage() {
  cat <<'USAGE'
Send a large file or directory to people by email, as several MOVEit packages of 50 MB zip parts.

Usage:   package-send-large.sh [--url URL] SUBJECT EMAILS PATH [FIRST_PACKAGE]
Example: ./scripts/package-send-large.sh "Measurements" alice@example.com,bob@example.com ./measurements

Subjects get a counter, e.g. "Measurements (1/4)". If a package fails, run the command then shown to send the rest.
USAGE
}

parse_prelude "$@"
# shellcheck disable=SC2086
set -- ${ARGS[@]+"${ARGS[@]}"}

{ [ $# -eq 3 ] || [ $# -eq 4 ]; } || bad_usage
subject=$1
emails=$2
path_arg=$3
first=${4:-1}
part_size_mb=${PART_SIZE_MB:-50}
per_package=${FILES_PER_PACKAGE:-10}

case "$emails" in
  *@*) ;;
  *) fail "EMAILS must be comma-separated emails, e.g. alice@example.com,bob@example.com" ;;
esac
[ -e "$path_arg" ] || fail "'$path_arg' does not exist"
case "$first" in
  [!1-9]* | *[!0-9]*) fail "FIRST_PACKAGE must be a package number, e.g. 3" ;;
esac
if ! command -v zip > /dev/null 2>&1; then
  echo "Error: zip is not installed, e.g. sudo apt install zip" >&2
  exit 1
fi
check_setup

if [ -d "$path_arg" ]; then
  path=$(cd "$path_arg" && pwd)
else
  path=$(cd "$(dirname "$path_arg")" && pwd)/$(basename "$path_arg")
fi
name=$(basename "$path")
parent=$(dirname "$path")
parts_dir=$path.parts

if [ -e "$parts_dir" ]; then
  echo "Using the parts already in $parts_dir"
elif [ "$first" != "1" ]; then
  fail "no parts to resume from in $parts_dir"
else
  echo "Zipping $name into $part_size_mb MB parts, in $parts_dir..."
  mkdir -p "$parts_dir"
  if ! (cd "$parent" && zip -q -r -s "${part_size_mb}m" "$parts_dir/$name.zip" "$name"); then
    rm -rf "$parts_dir"
    echo "Error: could not zip $name" >&2
    exit 1
  fi
fi

# Sort the numbered parts by their suffix, then put .zip last.
listing=$(
  for f in "$parts_dir/$name".z*; do
    [ -f "$f" ] || continue
    suffix=${f##*.}
    digits=${suffix#z}
    case "$digits" in
      "" | *[!0-9]*) key=999999999 ;;
      *) key=$((10#$digits)) ;;
    esac
    printf '%s\t%s\n' "$key" "$f"
  done | sort -n -k1,1 -s | cut -f2-
)
parts=()
while IFS= read -r line; do
  [ -n "$line" ] && parts+=("$line")
done <<LIST
$listing
LIST
count_parts=${#parts[@]}
[ "$count_parts" -gt 0 ] || fail "no parts found in $parts_dir"

total=$(((count_parts + per_package - 1) / per_package))
[ "$first" -le "$total" ] || fail "there are only $total packages"

p=$first
while [ "$p" -le "$total" ]; do
  start=$(((p - 1) * per_package))
  chunk=("${parts[@]:$start:$per_package}")
  echo "== Package $p/$total: ${#chunk[@]} files"
  note="Package $p/$total of $name, split into $count_parts zip parts. Download the files of all $total packages into one directory, then extract $name.zip with 7-Zip (7z x $name.zip)."
  rc=0
  run_tool send --to "$emails" --note "$note" -- "$subject ($p/$total)" "${chunk[@]}" || rc=$?
  if [ "$rc" -ne 0 ]; then
    {
      echo
      if [ "$p" -eq $((first + 1)) ]; then
        echo "Package $first/$total was sent."
      elif [ "$p" -gt "$first" ]; then
        echo "Packages $first-$((p - 1)) of $total were sent."
      fi
      echo "Package $p/$total failed, see above. To send it and the next ones, run:"
      printf '  %q' "$0"
      if [ -n "$URL_ARG" ]; then
        printf ' %q %q' --url "$URL_ARG"
      fi
      printf ' %q %q %q %q\n' "$subject" "$emails" "$path" "$p"
    } >&2
    if [ "$rc" -eq 3 ]; then
      exit 3
    fi
    exit 1
  fi
  p=$((p + 1))
done

rm -rf "$parts_dir"
echo "Sent $total packages."
