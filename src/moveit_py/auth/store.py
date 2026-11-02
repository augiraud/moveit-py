"""Store one token per server in a JSON file."""

import contextlib
import json
import os
import threading
from pathlib import Path

from moveit_py.auth.token import Token


class TokenStore:
    """Token cache persisted to a file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, url: str) -> Token | None:
        """The cached token of the server, or None."""
        return self.load_all().get(url)

    def load_all(self) -> dict[str, Token]:
        """Load all valid entries. Treat a missing, unreadable or invalid file as empty."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        tokens: dict[str, Token] = {}
        for url, entry in data.items():
            token = Token.from_json(entry)
            if token is not None:
                tokens[url] = token
        return tokens

    def save(self, url: str, token: Token) -> None:
        """Save the token for this server, keeping the other entries."""
        tokens = self.load_all()
        tokens[url] = token
        self._write(tokens)

    def delete(self, url: str) -> bool:
        """Remove the server entry. Return whether it existed."""
        tokens = self.load_all()
        if url not in tokens:
            return False
        del tokens[url]
        self._write(tokens)
        return True

    def _write(self, tokens: dict[str, Token]) -> None:
        text = json.dumps({url: token.to_json() for url, token in tokens.items()}, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
            tmp.replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
