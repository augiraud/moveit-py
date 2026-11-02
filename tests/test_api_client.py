import hashlib
from pathlib import Path

import pytest
from fakes import WAF_PAGE, FakeFolderServer, FakeResponse, FakeSession, FakeTokens

from moveit_py import cli
from moveit_py.api import MoveitClient, get_all_pages
from moveit_py.cli import Settings
from moveit_py.errors import ResponseException, SignInExpiredError

SERVER = "https://moveit.example.com"


def make(
    *responses: FakeResponse | Exception, tokens: FakeTokens | None = None
) -> tuple[MoveitClient, FakeSession, FakeTokens]:
    session = FakeSession(*responses)
    provider = tokens or FakeTokens()
    return MoveitClient(SERVER, provider, session), session, provider


def test_constructor_gets_a_token() -> None:
    _, session, provider = make(FakeResponse())
    assert provider.issued == 1
    assert session.requests == []


def test_request_parameters() -> None:
    client, session, _ = make(FakeResponse(json_body={}))
    client.rest_call("/files", params={"page": 2}, timeout=5, stream=True)
    request = session.requests[0]
    assert (request.method, request.url) == ("GET", f"{SERVER}/api/v1/files")
    assert request.params == {"page": 2}
    assert request.authorization == "Bearer first"
    assert request.timeout == 5
    assert request.stream


def test_json_body() -> None:
    client, session, _ = make(FakeResponse(json_body={}))
    client.rest_call("/folders/1/subfolders", data={"name": "x"}, method="POST", app_json=True)
    assert session.requests[0].json == {"name": "x"}
    assert session.requests[0].method == "POST"


def test_at27_retry_on_401() -> None:
    client, session, provider = make(FakeResponse(status_code=401), FakeResponse(b"ok"))
    first = session.responses[0]
    response = client.rest_call("/users/self")
    assert response.status_code == 200
    assert [r.authorization for r in session.requests] == ["Bearer first", "Bearer second"]
    assert provider.invalidated == ["first"]
    assert isinstance(first, FakeResponse)
    assert first.closed


def test_at28_always_401() -> None:
    client, session, _ = make(FakeResponse(status_code=401))
    assert client.rest_call("/users/self").status_code == 401
    assert len(session.requests) == 2


def test_sign_in_expired_propagates() -> None:
    tokens = FakeTokens(error_after=1, error=SignInExpiredError("expired"))
    client, session, _ = make(FakeResponse(status_code=401), tokens=tokens)
    with pytest.raises(SignInExpiredError):
        client.rest_call("/users/self")
    assert len(session.requests) == 1


def test_network_error_propagates() -> None:
    client, _, _ = make(ConnectionError("down"))
    with pytest.raises(ConnectionError):
        client.rest_call("/users/self")


def test_at29_upload(tmp_path: Path) -> None:
    path = tmp_path / "a b.txt"
    path.write_bytes(b"some content")
    client, session, _ = make(FakeResponse(status_code=201))
    client.rest_call("/folders/1/files", method="POST", file=path)
    request = session.requests[0]
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    assert request.body is not None
    body = request.body
    digest = hashlib.sha256(b"some content").hexdigest().encode()
    positions = [body.index(x) for x in (b"sha-256", digest, b'filename="a b.txt"', b"some content")]
    assert positions == sorted(positions)
    assert body.index(b'name="hashtype"') < body.index(b'name="hash"') < body.index(b'name="file"')
    assert b"application/octet-stream" in body


def test_upload_retry_resends_content(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"payload")
    client, session, _ = make(FakeResponse(status_code=401), FakeResponse(status_code=201))
    assert client.rest_call("/folders/1/files", method="POST", file=path).status_code == 201
    assert len(session.requests) == 2
    for request in session.requests:
        assert request.body is not None
        assert b"payload" in request.body
    assert session.requests[1].authorization == "Bearer second"


def test_at30_json_calls() -> None:
    client, _, _ = make(FakeResponse(WAF_PAGE))
    with pytest.raises(ResponseException, match="Request Rejected"):
        client.rest_call_json("/folders/1/files", method="POST")
    client, _, _ = make(FakeResponse(json_body={"error": "x"}, status_code=404))
    with pytest.raises(ResponseException, match="404"):
        client.rest_call_json("/files/9")
    client, _, _ = make(FakeResponse(json_body={"a": 1}))
    assert client.rest_call_json("/users/self") == {"a": 1}


def test_at35_listing_reads_both_pages() -> None:
    server = FakeFolderServer()
    files = {f["path"]: f for f in get_all_pages(server, "/files")}
    assert len(files) == 6
    assert "/upload/already_uploaded" in files
    assert files["/remote/sub/good_file_3"]["id"] == "good_file_3"


def test_connect_builds_provider_and_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    built: dict[str, object] = {}

    def fake_build(settings: Settings, signin_deps: object) -> FakeTokens:
        built["provider"] = provider = FakeTokens()
        return provider

    class FakeClient:
        def __init__(self, server: str, provider: object) -> None:
            built["client"] = (server, provider)

    monkeypatch.setattr(cli, "build_provider", fake_build)
    monkeypatch.setattr(cli, "MoveitClient", FakeClient)
    settings = Settings(SERVER, "sso", None, None, tmp_path / "t.json", True, "login")
    provider, client = cli._connect(settings)
    assert provider is built["provider"]
    assert built["client"] == (SERVER, provider)
    assert isinstance(client, FakeClient)
