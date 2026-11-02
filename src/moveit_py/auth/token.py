"""MoveIt API token."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from moveit_py.config import EXPIRY_MARGIN

_FIELDS = frozenset({"access_token", "refresh_token", "expires_at"})


@dataclass(frozen=True)
class Token:
    """Access and refresh tokens. Keep the token strings out of repr()."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float  # Unix seconds, wall clock

    @classmethod
    def from_response(cls, json: Mapping[str, Any], now: float) -> Token:
        """Build a token from a token endpoint response."""
        return cls(str(json["access_token"]), str(json["refresh_token"]), now + float(json["expires_in"]))

    def is_expired(self, now: float, margin: float = EXPIRY_MARGIN) -> bool:
        """Check whether the token has expired, allowing for the given margin."""
        return now >= self.expires_at - margin

    def to_json(self) -> dict[str, Any]:
        """The cache representation of the token."""
        return {"access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at}

    @classmethod
    def from_json(cls, data: object) -> Token | None:
        """Parse a cache entry, or return None if it is invalid."""
        if not isinstance(data, dict) or set(data) != _FIELDS:
            return None
        access, refresh, expires = data["access_token"], data["refresh_token"], data["expires_at"]
        if not isinstance(access, str) or not isinstance(refresh, str):
            return None
        if isinstance(expires, bool) or not isinstance(expires, int | float):
            return None
        if not math.isfinite(expires):
            return None
        return cls(access, refresh, float(expires))
