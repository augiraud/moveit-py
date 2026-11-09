# Sourced by the other scripts.
# shellcheck shell=bash
# shellcheck disable=SC2034  # URL_ARG and ARGS are read by the sourcing scripts

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MOVEIT_ENV_FILE=${MOVEIT_ENV_FILE:-$PROJECT_DIR/moveit.env}
export MOVEIT_ENV_FILE

fail() {
  echo "Error: $1" >&2
  exit 2
}

check_setup() {
  if ! command -v uv > /dev/null 2>&1; then
    echo "Error: uv is not installed, see README.md" >&2
    exit 1
  fi
  if [[ -z "${MOVEIT_URL:-}" ]] &&
    ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?MOVEIT_URL=.' "$MOVEIT_ENV_FILE" 2> /dev/null; then
    fail "no MOVEit server: create moveit.env from moveit.env.example, or use --url https://moveit.example.com"
  fi
}

run_tool() {
  uv run --no-build --quiet --project "$PROJECT_DIR" moveit "$@"
}

# Parse --help and --url. The calling script defines usage().
# Usage: parse_prelude "$@"; set -- ${ARGS[@]+"${ARGS[@]}"}
# Sets ARGS (the remaining arguments) and URL_ARG (the --url value, or empty).
parse_prelude() {
  URL_ARG=""
  case "${1:-}" in
    -h | --help)
      usage
      exit 0
      ;;
  esac
  if [[ "${1:-}" = "--url" ]]; then
    if [[ $# -lt 2 || -z "$2" ]]; then
      fail "--url needs a URL"
    fi
    URL_ARG=$2
    export MOVEIT_URL=$2
    shift 2
  fi
  ARGS=("$@")
}

# Print the usage on stderr, exit 2.
bad_usage() {
  usage >&2
  exit 2
}
