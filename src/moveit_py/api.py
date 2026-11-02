"""MoveIt API client and paging helper."""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests
from requests_toolbelt import MultipartEncoder

from moveit_py.config import API_TIMEOUT, PAGE_SIZE
from moveit_py.errors import ResponseException
from moveit_py.files import sha256_file

ERROR_BODY_EXCERPT = 300


class Response(Protocol):
    """The parts of an HTTP response used by the client."""

    @property
    def status_code(self) -> int:
        """HTTP status code."""
        ...

    @property
    def text(self) -> str:
        """Body decoded as text."""
        ...

    @property
    def content(self) -> bytes:
        """Body as bytes."""
        ...

    def json(self) -> Any:  # noqa: ANN401
        """Decode the body as JSON; raises ValueError if it is not JSON."""
        ...

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        """Iterate over the body in chunks."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


class ApiClient(Protocol):
    """Authenticated access to the MoveIt API. Endpoints are relative to /api/v1."""

    def rest_call(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = API_TIMEOUT,
        file: Path | None = None,
        app_json: bool = False,
        stream: bool = False,
    ) -> Response:
        """Perform a request and return the raw response."""
        ...

    def rest_call_json(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = API_TIMEOUT,
        file: Path | None = None,
        app_json: bool = False,
    ) -> Any:  # noqa: ANN401
        """Perform a request and return the decoded JSON; raise ResponseException on non-JSON or error status."""
        ...


def json_from_response(response: Response, method: str, endpoint: str) -> Any:  # noqa: ANN401
    """Decode a response body as JSON, raising ResponseException on a non-JSON body or an error status."""
    try:
        body = response.json()
    except ValueError as error:  # json.JSONDecodeError is a ValueError
        excerpt = response.text[:ERROR_BODY_EXCERPT]
        raise ResponseException(
            f"Unexpected response to {method} {endpoint} (HTTP {response.status_code}): {excerpt}"
        ) from error
    if response.status_code >= 400:  # noqa: PLR2004
        raise ResponseException(
            f"Error response to {method} {endpoint} (HTTP {response.status_code}): {json.dumps(body)}"
        )
    return body


def get_all_pages(client: ApiClient, endpoint: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    """Get the items of all pages of an endpoint."""
    base = dict(params or {})
    first = client.rest_call_json(endpoint, params={**base, "perPage": PAGE_SIZE})
    items: list[Any] = list(first["items"])
    total_pages = int(first.get("paging", {}).get("totalPages", 1))
    for page in range(2, total_pages + 1):
        data = client.rest_call_json(endpoint, params={**base, "page": page, "perPage": PAGE_SIZE})
        items.extend(data["items"])
    return items


HTTP_UNAUTHORIZED = 401
OCTET_STREAM = "application/octet-stream"


class AccessTokens(Protocol):
    """What the client needs from the token provider."""

    def get_access_token(self) -> str:
        """Return a valid access token."""
        ...

    def invalidate(self, access_token: str) -> None:
        """The server rejected this access token."""
        ...


class SessionLike(Protocol):
    """The part of `requests.Session` that the client uses."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Any = None,  # noqa: ANN401
        json: Any = None,  # noqa: ANN401
        timeout: float | None = None,
        stream: bool = False,
    ) -> Response:
        """Send a request."""
        ...


@dataclass(frozen=True)
class _Call:
    """A request that may be retried once."""

    method: str
    url: str
    params: Mapping[str, Any] | None
    data: Any
    timeout: float
    file: Path | None
    file_hash: str | None
    app_json: bool
    stream: bool


class MoveitClient:
    """MoveIt API client."""

    def __init__(self, server: str, provider: AccessTokens, session: SessionLike | None = None) -> None:
        self._base = f"{server.rstrip('/')}/api/v1"
        self._provider = provider
        self._session: SessionLike = session or requests.Session()
        provider.get_access_token()  # authenticate now

    def rest_call(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = API_TIMEOUT,
        file: Path | None = None,
        app_json: bool = False,
        stream: bool = False,
    ) -> Response:
        """Perform a request; on a 401, refresh the token and send it once more."""
        file_hash = sha256_file(file) if file is not None else None
        call = _Call(method, self._base + endpoint, params, data, timeout, file, file_hash, app_json, stream)
        token = self._provider.get_access_token()
        response = self._attempt(call, token)
        if response.status_code != HTTP_UNAUTHORIZED:
            return response
        response.close()
        self._provider.invalidate(token)
        return self._attempt(call, self._provider.get_access_token())

    def rest_call_json(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = API_TIMEOUT,
        file: Path | None = None,
        app_json: bool = False,
    ) -> Any:  # noqa: ANN401
        """Perform a request and return the decoded JSON; raise ResponseException on non-JSON or error status."""
        response = self.rest_call(endpoint, params, data, method, timeout, file, app_json)
        return json_from_response(response, method, endpoint)

    def _attempt(self, call: _Call, token: str) -> Response:
        headers = {"Authorization": f"Bearer {token}"}
        if call.file is None:
            return self._session.request(
                call.method,
                call.url,
                params=call.params,
                headers=headers,
                json=call.data if call.app_json else None,
                data=None if call.app_json else call.data,
                timeout=call.timeout,
                stream=call.stream,
            )
        with call.file.open("rb") as stream:
            encoder = _multipart(call.file, stream, call.file_hash or "")
            headers["Content-Type"] = encoder.content_type
            return self._session.request(
                call.method,
                call.url,
                params=call.params,
                headers=headers,
                data=encoder,
                timeout=call.timeout,
                stream=call.stream,
            )


def _multipart(path: Path, stream: Any, file_hash: str) -> MultipartEncoder:  # noqa: ANN401
    """Put the hash before the file: the server checks it before reading the file."""
    return MultipartEncoder(
        fields=[
            ("hashtype", "sha-256"),
            ("hash", file_hash),
            ("file", (path.name, stream, OCTET_STREAM)),
        ]
    )
