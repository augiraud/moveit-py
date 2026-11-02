"""Load configuration from the environment and an optional env file."""

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

DEFAULT_ENV_FILE = "moveit.env"
ENV_FILE_VARIABLE = "MOVEIT_ENV_FILE"

# Fixed parameters.
DEFAULT_THREADS = 4
API_TIMEOUT = 3600
AUTH_TIMEOUT = 60
EXPIRY_MARGIN = 60
KEEP_ALIVE_MARGIN = 300
KEEP_ALIVE_INTERVAL = 30
PAGE_SIZE = 200
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
HASH_CHUNK_SIZE = 1024 * 1024
MAX_ATTACHMENT_SIZE = 1024**3
RECENT_SENT_PACKAGES = 20
SSO_CLIENT = "outlook"


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse one line of an env file into (key, value), or None if the line is ignored."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    line = line.removeprefix("export ")
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    return key, value.strip().strip("'\"")


def load_env_file(environ: MutableMapping[str, str] | None = None, path: Path | None = None) -> None:
    """Load an env file without overriding variables already set.

    Use the given path, MOVEIT_ENV_FILE, or moveit.env in the current directory. Ignore a missing or unreadable file.
    """
    if environ is None:
        environ = os.environ
    if path is None:
        path = Path(environ.get(ENV_FILE_VARIABLE) or DEFAULT_ENV_FILE)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        parsed = parse_env_line(line)
        if parsed is not None and parsed[0] not in environ:
            environ[parsed[0]] = parsed[1]


def default_token_file(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the default path of the token cache. An empty XDG_CONFIG_HOME counts as unset."""
    if environ is None:
        environ = os.environ
    if home is None:
        home = Path.home()
    base = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "moveit-py" / "tokens.json"
