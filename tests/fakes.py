"""Test doubles shared by the test modules: fake HTTP responses and a route-table fake API client."""

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moveit_py.api import json_from_response

WAF_PAGE = "<html><head><title>Request Rejected</title></head><body>The requested URL was rejected.</body></html>"
DUMMY = b"dummy_content"
DUMMY_SHA1 = hashlib.sha1(DUMMY).hexdigest()  # noqa: S324 - the server's file hashes are SHA-1


class FakeResponse:
    """An in-memory HTTP response satisfying the `Response` protocol."""

    def __init__(self, body: bytes | str | None = b"", status_code: int = 200, json_body: Any = None) -> None:  # noqa: ANN401
        if json_body is not None:
            body = json.dumps(json_body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.status_code = status_code
        self.content: bytes = body or b""
        self.closed = False

    @property
    def text(self) -> str:
        """Body decoded as UTF-8."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:  # noqa: ANN401
        """Decode the body; raises json.JSONDecodeError (a ValueError) if it is not JSON."""
        return json.loads(self.content)

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        """Iterate over the body in chunks."""
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self) -> None:
        """Mark the response as closed."""
        self.closed = True


@dataclass
class RecordedRequest:
    """One request received by a fake client."""

    method: str
    endpoint: str
    params: Mapping[str, Any] | None
    data: Any
    file: Path | None
    app_json: bool
    stream: bool


Route = FakeResponse | Callable[[RecordedRequest], FakeResponse]


class FakeApiClient:
    """A fake `ApiClient`: routes keyed by (method, endpoint), and a request log."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        self.requests: list[RecordedRequest] = []

    def add(self, method: str, endpoint: str, route: Route) -> None:
        """Register a response (or a function building one from the request)."""
        self.routes[(method, endpoint)] = route

    def add_json(self, method: str, endpoint: str, body: Any, status_code: int = 200) -> None:  # noqa: ANN401
        """Register a JSON response."""
        self.add(method, endpoint, FakeResponse(json_body=body, status_code=status_code))

    def count(self, method: str, endpoint: str) -> int:
        """Number of requests received for a route."""
        return sum(1 for r in self.requests if (r.method, r.endpoint) == (method, endpoint))

    def rest_call(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = 0,
        file: Path | None = None,
        app_json: bool = False,
        stream: bool = False,
    ) -> FakeResponse:
        """Log the request and answer from the route table (404 if there is no route)."""
        request = RecordedRequest(method, endpoint, params, data, file, app_json, stream)
        self.requests.append(request)
        route = self.routes.get((method, endpoint))
        if route is None:
            return FakeResponse(json_body={"error": "not found"}, status_code=404)
        if isinstance(route, FakeResponse):
            return route
        return route(request)

    def rest_call_json(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = 0,
        file: Path | None = None,
        app_json: bool = False,
    ) -> Any:  # noqa: ANN401
        """Like rest_call, decoded as JSON (ResponseException on non-JSON or error status)."""
        return json_from_response(
            self.rest_call(endpoint, params, data, method, timeout, file, app_json), method, endpoint
        )


@dataclass
class HttpCall:
    """One request received by `FakeHttp`."""

    method: str
    url: str
    data: dict[str, str] | None
    timeout: float


@dataclass
class FakeHttp:
    """Injectable `http_post`/`http_get` callables answering from queues; an exception in a queue is raised."""

    post_responses: list[FakeResponse | Exception] = field(default_factory=list)
    get_responses: list[FakeResponse | Exception] = field(default_factory=list)
    calls: list[HttpCall] = field(default_factory=list)

    def post(self, url: str, data: dict[str, str], timeout: float) -> FakeResponse:
        """Log a POST and answer with the next queued response."""
        self.calls.append(HttpCall("POST", url, dict(data), timeout))
        return self._next(self.post_responses)

    def get(self, url: str, timeout: float) -> FakeResponse:
        """Log a GET and answer with the next queued response."""
        self.calls.append(HttpCall("GET", url, None, timeout))
        return self._next(self.get_responses)

    @staticmethod
    def _next(queue: list[FakeResponse | Exception]) -> FakeResponse:
        assert queue, "unexpected request"
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def posts(self) -> list[HttpCall]:
        """The POST requests received so far."""
        return [call for call in self.calls if call.method == "POST"]


@dataclass
class SessionRequest:
    """One request received by `FakeSession`; a multipart body is read fully into `body`."""

    method: str
    url: str
    params: Mapping[str, Any] | None
    headers: Mapping[str, str]
    body: bytes | None
    json: Any
    timeout: float | None
    stream: bool

    @property
    def authorization(self) -> str | None:
        """The Authorization header."""
        return self.headers.get("Authorization")


class FakeSession:
    """A `requests.Session` stand-in answering from a queue (the last response repeats); an exception is raised."""

    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses: list[FakeResponse | Exception] = list(responses)
        self.requests: list[SessionRequest] = []

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
    ) -> FakeResponse:
        """Log the request and answer with the next queued response."""
        body = data.read() if hasattr(data, "read") else data
        self.requests.append(SessionRequest(method, url, params, dict(headers or {}), body, json, timeout, stream))
        assert self.responses, "unexpected request"
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeTokens:
    """A fake token provider handing out `first`, `second`, ... and recording the calls."""

    def __init__(self, error_after: int | None = None, error: Exception | None = None) -> None:
        self.issued = 0
        self.current = ""
        self.invalidated: list[str] = []
        self.error_after = error_after
        self.error = error

    def get_access_token(self) -> str:
        """The current token; a new one after `invalidate`."""
        if (
            self.error is not None
            and self.error_after is not None
            and not self.current
            and self.issued >= self.error_after
        ):
            raise self.error
        if not self.current:
            names = ["first", "second", "third", "fourth", "fifth"]
            self.current = names[self.issued]
            self.issued += 1
        return self.current

    def invalidate(self, access_token: str) -> None:
        """Record the rejected token; the next call issues a new one."""
        self.invalidated.append(access_token)
        if access_token == self.current:
            self.current = ""


def good_file_hash_response() -> FakeResponse:
    """`GET /files/{id}` of a file with a hash."""
    return FakeResponse(json_body={"hash": DUMMY_SHA1})


class FakeServer(FakeApiClient):
    """A fake API server: the request log and route table of `FakeApiClient`, failure hooks, and `_default` answers.

    Test hooks: `requests` (log), `routes` (exact (method, endpoint) overrides), `failures` (endpoint -> exception to
    raise, or response to return, for every method; a list is consumed one item per request, `None` meaning
    "answer normally").
    """

    def __init__(self) -> None:
        super().__init__()
        self.failures: dict[str, Exception | FakeResponse | list[Exception | FakeResponse | None]] = {}

    def rest_call(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        data: Any = None,  # noqa: ANN401
        method: str = "GET",
        timeout: float = 0,
        file: Path | None = None,
        app_json: bool = False,
        stream: bool = False,
    ) -> FakeResponse:
        """Log the request, apply the failure hooks, then answer like the server."""
        request = RecordedRequest(method, endpoint, params, data, file, app_json, stream)
        self.requests.append(request)
        failure = self.failures.get(endpoint)
        if isinstance(failure, list):
            failure = failure.pop(0) if failure else None
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        route = self.routes.get((method, endpoint))
        if route is not None:
            return route if isinstance(route, FakeResponse) else route(request)
        return self._default(request)

    def _default(self, request: RecordedRequest) -> FakeResponse:
        return FakeResponse(json_body={"error": "not found"}, status_code=404)


class FakeFolderServer(FakeServer):
    """Fake folder server with paging, hashes, downloads, folder creation and uploads.

    Extra hook: `uploads` (endpoint -> bytes of the uploaded file).
    """

    PAGE_1 = ("/remote/good_file_1", "/remote/bad_file", "/remote/good_file_2")
    PAGE_2 = ("/remote/sub/good_file_3", "/remote_other/good_file_4", "/upload/already_uploaded")

    def __init__(self) -> None:
        super().__init__()
        self.uploads: dict[str, bytes] = {}
        self.add("GET", "/files", self._files)
        self.add("GET", "/folders", self._folders)
        self.add_json("GET", "/folders/12345", {"id": "12345", "path": "/upload"})

    @staticmethod
    def _item(path: str) -> dict[str, Any]:
        return {"id": path.rsplit("/", 1)[-1], "path": path, "size": 13}

    def _files(self, request: RecordedRequest) -> FakeResponse:
        page = int((request.params or {}).get("page", 1))
        paths = self.PAGE_1 if page == 1 else self.PAGE_2
        return self._paged([self._item(p) for p in paths], page, 2)

    def _folders(self, request: RecordedRequest) -> FakeResponse:
        return self._paged([{"id": "12345", "path": "/upload"}], 1, 1)

    @staticmethod
    def _paged(items: list[dict[str, Any]], page: int, total_pages: int) -> FakeResponse:
        paging = {"page": page, "perPage": 200, "totalItems": 6, "totalPages": total_pages}
        return FakeResponse(json_body={"items": items, "paging": paging})

    def _default(self, request: RecordedRequest) -> FakeResponse:
        parts = request.endpoint.strip("/").split("/")
        if request.method == "GET" and parts[0] == "files" and len(parts) == 2:
            return self._file_info(parts[1])
        if request.method == "GET" and parts[0] == "files" and parts[-1] == "download":
            return self._download(parts[1])
        if request.method == "POST" and parts[0] == "folders" and parts[-1] == "subfolders":
            name = (request.data or {}).get("name", "")
            return FakeResponse(json_body={"id": f"id-{name}", "name": name}, status_code=201)
        if request.method == "POST" and parts[0] == "folders" and parts[-1] == "files":
            if request.file is not None:
                self.uploads[request.endpoint] = request.file.read_bytes()
            return FakeResponse(json_body={"id": "uploaded"}, status_code=201)
        return FakeResponse(json_body={"error": "not found"}, status_code=404)

    @staticmethod
    def _file_info(file_id: str) -> FakeResponse:
        if file_id.startswith("good_file_"):
            return good_file_hash_response()
        return FakeResponse(json_body={"id": file_id})

    @staticmethod
    def _download(file_id: str) -> FakeResponse:
        if file_id == "waf_file":
            return FakeResponse(WAF_PAGE)
        if file_id == "missing_file":
            return FakeResponse(json_body={"error": "not found"}, status_code=404)
        return FakeResponse(DUMMY)


def paged_response(items: list[Any], request: RecordedRequest) -> FakeResponse:
    """The page of `items` asked by the `page` and `perPage` parameters, in the server's paged format."""
    params = request.params or {}
    page, per_page = int(params.get("page", 1)), int(params.get("perPage", 200))
    total_pages = max(1, -(-len(items) // per_page))
    start = (page - 1) * per_page
    paging = {"page": page, "perPage": per_page, "totalItems": len(items), "totalPages": total_pages}
    return FakeResponse(json_body={"items": items[start : start + per_page], "paging": paging})


INBOX_ID, SENT_ID = 1, 3
FIRST_CONTENT, SECOND_CONTENT = b"first file", b"second file"


class FakePackageServer(FakeServer):
    """Fake package server with requirements, mailboxes, contacts, attachments and downloads.

    Every fixture is a plain attribute to override in tests:

    - `requirements` (dict), `mailboxes` (list of `{id, name}`), `contacts` (list of contact dicts);
    - `package_items` (mailbox id -> list items of `GET /packages`), `package_details` ((mailbox id, package id) ->
      body of `GET /packages/{id}`; any other pair gives 404), `contents` (attachment id -> bytes);
    - `created_id` (ID answered by `POST /packages`, 201);
    - records: `uploaded` (attachment id -> bytes), `deleted_attachments` (ids), `created` (JSON of each
      `POST /packages`), `requests` (log), plus the `routes` and `failures` hooks of `FakeServer`.

    A `POST /packages` answered normally creates the package: `store_sent_package` adds it to the Sent mailbox. A route
    or failure hook replacing the answer can call it to simulate a package created although the answer says otherwise.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requirements: dict[str, Any] = {
            "allowSendPackages": True,
            "allowAttachFiles": True,
            "maxFilesPerPackageLimit": 3,
            "defaultUserPackageExpirationHours": 168,
            "maxUserPackageExpirationHours": 240,
            "defaultAttachmentDownloads": 10,
            "maxAttachmentDownloads": 10,
            "defaultSecureNote": False,
            "allowChangeSecureNote": True,
            "isClassificationFieldRequired": False,
            "classificationTypes": [{"id": 5, "value": "Unclassified"}, {"id": 6, "value": "Protected"}],
        }
        self.mailboxes: list[dict[str, Any]] = [
            {"id": INBOX_ID, "name": "Inbox"},
            {"id": SENT_ID, "name": "Sent"},
            {"id": 5, "name": "Trash"},
        ]
        self.contacts: list[dict[str, Any]] = [
            {"name": name, "email": email, "identifier": identifier, "type": "User"}
            for name, email, identifier in (
                ("Alice", "alice@example.com", "user-alice"),
                ("Alice Two", "alice.two@example.com", "user-alice2"),
                ("Bob", "Bob@Example.com", "user-bob"),
            )
        ]
        self.contents: dict[str, bytes] = {"att-1": FIRST_CONTENT, "att-2": SECOND_CONTENT}
        self.package_items: dict[int, list[dict[str, Any]]] = {
            INBOX_ID: [
                {
                    "id": 100,
                    "subject": "Results",
                    "sendStamp": "2026-09-17T10:52:26",
                    "isNew": True,
                    "sender": {"fullName": "Alice", "email": "alice@example.com"},
                    "toRecipients": ["Me"],
                }
            ],
            SENT_ID: [
                {
                    "id": 101,
                    "subject": "Report",
                    "sendStamp": "2026-09-16T08:00:00",
                    "isNew": False,
                    "sender": {"fullName": "Me", "email": "me@example.com"},
                    "toRecipients": ["Bob", "Carol"],
                }
            ],
        }
        self.package_details: dict[tuple[int, int], dict[str, Any]] = {
            (INBOX_ID, 100): {
                "id": 100,
                "subject": "Results",
                "attachments": [
                    {"id": "att-1", "name": "a b.txt", "size": len(FIRST_CONTENT)},
                    {"id": "att-2", "name": "results.zip", "size": len(SECOND_CONTENT)},
                ],
            },
            (SENT_ID, 101): {"id": 101, "subject": "Report", "attachments": []},
        }
        self.created_id: int = 42
        self.uploaded: dict[str, bytes] = {}
        self.deleted_attachments: list[str] = []
        self.created: list[Any] = []

    def _default(self, request: RecordedRequest) -> FakeResponse:
        parts = request.endpoint.strip("/").split("/")
        handlers: dict[tuple[str, str], Callable[[RecordedRequest, list[str]], FakeResponse]] = {
            ("GET", "packages"): self._packages,
            ("POST", "packages"): self._create_package,
            ("GET", "files"): self._files,
            ("DELETE", "packages"): self._delete_attachment,
            ("GET", "mailboxes"): lambda *_: FakeResponse(json_body=self.mailboxes),
            ("GET", "contacts"): self._contacts,
        }
        handler = handlers.get((request.method, parts[0]))
        if request.method == "POST" and parts == ["packages", "attachments"]:
            return self._upload(request)
        if handler is None:
            return super()._default(request)
        return handler(request, parts)

    def _packages(self, request: RecordedRequest, parts: list[str]) -> FakeResponse:
        params = request.params or {}
        mailbox_id = int(params.get("mailboxId", 0))
        if parts == ["packages", "requirements"]:
            return FakeResponse(json_body=self.requirements)
        if len(parts) == 1:
            items = self.package_items.get(mailbox_id, [])
            if params.get("newOnly") == "true":
                items = [item for item in items if item.get("isNew")]
            return paged_response(items, request)
        detail = self.package_details.get((mailbox_id, int(parts[1]))) if parts[1].isdigit() else None
        if detail is None:
            return FakeResponse(json_body={"error": "not found"}, status_code=404)
        return FakeResponse(json_body=detail)

    def _contacts(self, request: RecordedRequest, parts: list[str]) -> FakeResponse:
        search = str((request.params or {}).get("searchString", "")).lower()
        return paged_response([c for c in self.contacts if search in c["email"].lower()], request)

    def _upload(self, request: RecordedRequest) -> FakeResponse:
        name = request.file.name if request.file is not None else ""
        if request.file is not None:
            self.uploaded[f"att-{name}"] = request.file.read_bytes()
        return FakeResponse(json_body={"id": f"att-{name}"}, status_code=201)

    def _delete_attachment(self, request: RecordedRequest, parts: list[str]) -> FakeResponse:
        if len(parts) != 3 or parts[1] != "attachments":
            return FakeResponse(json_body={"error": "not found"}, status_code=404)
        self.deleted_attachments.append(parts[2])
        return FakeResponse(status_code=204)

    def _create_package(self, request: RecordedRequest, parts: list[str]) -> FakeResponse:
        self.store_sent_package(request.data)
        return FakeResponse(json_body={"id": self.created_id}, status_code=201)

    def store_sent_package(self, data: Any) -> None:  # noqa: ANN401
        """Record a `POST /packages` body and add the package, newest first, to the Sent mailbox."""
        self.created.append(data)
        package_id = self.created_id
        item = {
            "id": package_id,
            "subject": data["subject"],
            "sendStamp": "2026-09-18T09:00:00",
            "isNew": False,
            "sender": {"fullName": "Me", "email": "me@example.com"},
            "toRecipients": [r["identifier"] for r in data["recipients"] if r["role"] == "To"],
        }
        self.package_items.setdefault(SENT_ID, []).insert(0, item)
        self.package_details[(SENT_ID, package_id)] = {
            "id": package_id,
            "subject": data["subject"],
            "attachments": [{"id": a["id"], "name": a["id"].removeprefix("att-")} for a in data["attachments"]],
        }

    def _files(self, request: RecordedRequest, parts: list[str]) -> FakeResponse:
        content = self.contents.get(parts[1])
        if content is None:
            return FakeResponse(json_body={"error": "not found"}, status_code=404)
        if len(parts) == 3 and parts[2] == "download":
            return FakeResponse(content)
        return FakeResponse(json_body={"id": parts[1], "hash": hashlib.sha1(content).hexdigest()})  # noqa: S324
