import hashlib
import re
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import FakeHttp, FakeResponse

from moveit_py.auth.signin import (
    SignInDeps,
    make_login,
    make_pkce_pair,
    make_refresh,
    parse_authorization_code,
    password_sign_in,
    pick_identity_provider,
    refresh,
    request_token,
    sso_sign_in,
)
from moveit_py.auth.token import Token
from moveit_py.errors import AuthenticationError, TransientAuthenticationError

SERVER = "https://moveit.example.com"
WAF_PAGE = "<html><head><title>Request Rejected</title></head><body>The requested URL was rejected.</body></html>"
TOKEN_BODY = {"access_token": "acc", "refresh_token": "ref", "expires_in": 1199}
NOW = 1000.0


class Console:
    """Records output and answers prompts from a list of answers (an exception in the list is raised)."""

    def __init__(self, *answers: str | BaseException) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []
        self.opened: list[str] = []

    def prompt(self, text: str) -> str:
        self.prompts.append(text)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def open_browser(self, url: str) -> bool:
        self.opened.append(url)
        return True


def make_deps(
    http: FakeHttp,
    console: Console | None = None,
    *,
    environ: dict[str, str] | None = None,
    getpass: Callable[[str], str] | None = None,
    is_terminal: bool = True,
    with_prompt: bool = True,
) -> SignInDeps:
    console = console or Console()
    return SignInDeps(
        http_post=http.post,
        http_get=http.get,
        open_browser=console.open_browser,
        prompt=console.prompt if with_prompt else None,
        getpass=getpass or (lambda _text: pytest.fail("getpass called")),
        output=console.lines.append,
        is_terminal=lambda: is_terminal,
        environ=environ or {},
        now=lambda: NOW,
    )


def token_response(body: object = TOKEN_BODY, status: int = 200) -> FakeResponse:
    return FakeResponse(json_body=body, status_code=status)


def idp_response(*items: tuple[str, str, str]) -> FakeResponse:
    return FakeResponse(json_body={"items": [{"id": i, "name": n, "link": link} for i, n, link in items]})


def test_token_request_success() -> None:
    http = FakeHttp(post_responses=[token_response()])
    token = request_token(SERVER, {"grant_type": "x"}, make_deps(http))
    assert token == Token("acc", "ref", NOW + 1199)
    [call] = http.calls
    assert (call.url, call.data, call.timeout) == (f"{SERVER}/api/v1/token", {"grant_type": "x"}, 60)


def test_refresh_grant_fields() -> None:
    http = FakeHttp(post_responses=[token_response(), token_response()])
    deps = make_deps(http)
    assert refresh(SERVER, Token("a", "old-refresh", 0), deps).access_token == "acc"
    assert make_refresh(SERVER, deps)(Token("a", "older-refresh", 0)).refresh_token == "ref"
    assert [call.data for call in http.calls] == [
        {"grant_type": "refresh_token", "refresh_token": "old-refresh"},
        {"grant_type": "refresh_token", "refresh_token": "older-refresh"},
    ]


def test_password_from_environment_does_not_prompt() -> None:
    http = FakeHttp(post_responses=[token_response()])
    deps = make_deps(http, environ={"MOVEIT_PASSWORD": "s3cret"})
    password_sign_in(SERVER, "alice", deps)
    assert http.calls[0].data == {"grant_type": "password", "username": "alice", "password": "s3cret"}


@pytest.mark.parametrize("environ", [{}, {"MOVEIT_PASSWORD": ""}])
def test_password_from_getpass(environ: dict[str, str]) -> None:
    http = FakeHttp(post_responses=[token_response()])
    asked: list[str] = []

    def getpass(text: str) -> str:
        asked.append(text)
        return "typed"

    password_sign_in(SERVER, "alice", make_deps(http, environ=environ, getpass=getpass))
    assert len(asked) == 1
    assert re.fullmatch(r"(?i)moveit password for alice: ", asked[0])
    assert http.calls[0].data == {"grant_type": "password", "username": "alice", "password": "typed"}


def test_make_login_password_and_missing_username() -> None:
    http = FakeHttp(post_responses=[token_response()])
    deps = make_deps(http, environ={"MOVEIT_PASSWORD": "p"})
    assert make_login(SERVER, "password", "alice", None, deps)().access_token == "acc"
    assert not http.get_responses
    with pytest.raises(AuthenticationError, match="username"):
        make_login(SERVER, "password", None, None, deps)()


def test_at10_pkce() -> None:
    verifier, challenge = make_pkce_pair()
    expected = urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert challenge == expected
    assert len(verifier) == 86
    assert "=" not in verifier + challenge
    assert re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
    assert make_pkce_pair() != (verifier, challenge)
    assert make_pkce_pair()[0] != verifier


def test_at11_authorization_code_parsing() -> None:
    url = "  https://addins.example.com/transfer/outlook/samlAuth.html?authorization_code=abc%2B%3D&orgid=1\n"
    assert parse_authorization_code(url) == "abc+="
    with pytest.raises(AuthenticationError, match="denied"):
        parse_authorization_code("https://x.example.com/?error=denied")
    with pytest.raises(AuthenticationError, match="No authorization code"):
        parse_authorization_code("https://x.example.com/?orgid=1")
    with pytest.raises(AuthenticationError, match="No authorization code"):
        parse_authorization_code("http://[bad")


def test_at13_error_description_in_message() -> None:
    body = {"error": "invalid_grant", "error_description": "Bad password"}
    http = FakeHttp(post_responses=[token_response(body, 400)])
    with pytest.raises(AuthenticationError, match=r"HTTP 400.*Bad password") as info:
        request_token(SERVER, {"grant_type": "password"}, make_deps(http))
    assert not isinstance(info.value, TransientAuthenticationError)


@pytest.mark.parametrize(
    ("body", "status", "expected"),
    [
        ({"error": "invalid_grant"}, 400, "Authentication failed (HTTP 400): invalid_grant"),
        ({"other": 1}, 401, 'Authentication failed (HTTP 401): {"other": 1}'),
        ({"foo": "bar"}, 200, 'Authentication failed (HTTP 200): {"foo": "bar"}'),
        (["a"], 403, 'Authentication failed (HTTP 403): ["a"]'),
    ],
)
def test_token_request_error_fallbacks(body: object, status: int, expected: str) -> None:
    http = FakeHttp(post_responses=[token_response(body, status)])
    with pytest.raises(AuthenticationError) as info:
        request_token(SERVER, {}, make_deps(http))
    assert str(info.value) == expected
    assert not isinstance(info.value, TransientAuthenticationError)


def test_at14_security_notice_accepted() -> None:
    notice = {"error": "accept_security_notice_required", "error_description": "Please accept the notice."}
    http = FakeHttp(post_responses=[token_response(notice, 400), token_response()])
    console = Console(" Y \n")
    fields = {"grant_type": "password", "username": "alice", "password": "pw"}
    token = request_token(SERVER, fields, make_deps(http, console))
    assert token.access_token == "acc"
    first, second = (call.data for call in http.calls)
    assert first == fields
    assert second == {**fields, "accept_security_notice": "true"}
    assert console.lines == ["Please accept the notice."]
    assert console.prompts == ["Accept the security notice? [y/N] "]


def test_security_notice_default_text() -> None:
    http = FakeHttp(
        post_responses=[token_response({"error": "accept_security_notice_required"}, 400), token_response()]
    )
    console = Console("y")
    request_token(SERVER, {}, make_deps(http, console))
    assert len(console.lines) == 1
    assert console.lines[0].startswith("You must accept the security notice of the ")


@pytest.mark.parametrize("answer", ["n", "", EOFError()])
def test_security_notice_declined(answer: str | BaseException) -> None:
    notice = {"error": "accept_security_notice_required", "error_description": "Accept it."}
    http = FakeHttp(post_responses=[token_response(notice, 400)])
    with pytest.raises(AuthenticationError, match=r"HTTP 400.*Accept it\."):
        request_token(SERVER, {}, make_deps(http, Console(answer)))
    assert len(http.calls) == 1


def test_security_notice_retried_only_once() -> None:
    notice = token_response({"error": "accept_security_notice_required", "error_description": "Accept."}, 400)
    http = FakeHttp(post_responses=[notice, notice])
    console = Console("y", "y")
    with pytest.raises(AuthenticationError, match="Accept"):
        request_token(SERVER, {}, make_deps(http, console))
    assert len(http.calls) == 2
    assert len(console.prompts) == 1


@pytest.mark.parametrize("missing", ["refresh_token", "expires_in"])
def test_incomplete_success_response_is_a_clean_error(missing: str) -> None:
    body = {key: value for key, value in TOKEN_BODY.items() if key != missing}
    http = FakeHttp(post_responses=[token_response(body)])
    with pytest.raises(TransientAuthenticationError, match=r"Unexpected response.*HTTP 200") as info:
        request_token(SERVER, {}, make_deps(http))
    assert "acc" not in str(info.value)


def test_non_numeric_expires_in_is_a_clean_error() -> None:
    http = FakeHttp(post_responses=[token_response({**TOKEN_BODY, "expires_in": "soon"})])
    with pytest.raises(TransientAuthenticationError):
        request_token(SERVER, {}, make_deps(http))


@pytest.mark.parametrize(
    "response",
    [
        ConnectionError("connection refused"),
        FakeResponse(WAF_PAGE, 200),
        FakeResponse(json_body={"error": "bad gateway"}, status_code=502),
        FakeResponse("<html>Bad Gateway</html>", 502),
    ],
)
def test_at18_transient_errors(response: FakeResponse | Exception) -> None:
    http = FakeHttp(post_responses=[response])
    with pytest.raises(TransientAuthenticationError) as info:
        request_token(SERVER, {}, make_deps(http))
    if isinstance(response, Exception):
        assert str(info.value) == f"Could not reach {SERVER}: connection refused"
    elif response.status_code == 200:
        assert "Unexpected response from the token endpoint (HTTP 200)" in str(info.value)
    elif response.content.startswith(b"{"):
        assert "The token endpoint failed (HTTP 502)" in str(info.value)


def test_at18_invalid_grant_is_not_transient() -> None:
    http = FakeHttp(post_responses=[token_response({"error": "invalid_grant"}, 400)])
    with pytest.raises(AuthenticationError) as info:
        request_token(SERVER, {}, make_deps(http))
    assert not isinstance(info.value, TransientAuthenticationError)


def test_pick_identity_provider() -> None:
    items = [{"name": "A", "link": "a"}, {"name": "B", "link": "b"}]
    assert pick_identity_provider(items, "B") == items[1]
    assert pick_identity_provider(items[:1], None) == items[0]
    with pytest.raises(AuthenticationError, match=r"--idp \(available: none\)"):
        pick_identity_provider([], None)
    with pytest.raises(AuthenticationError, match=r"\(available: A, B\)"):
        pick_identity_provider(items, "C")
    with pytest.raises(AuthenticationError, match=r"\(available: none\)"):
        pick_identity_provider([], "A")
    with pytest.raises(AuthenticationError, match=r"\(available: A\)$"):
        pick_identity_provider([{"name": "A"}, {"id": "x"}, {"name": None}], "C")


def sso_http(*items: tuple[str, str, str], code_url: bool = True) -> FakeHttp:
    return FakeHttp(get_responses=[idp_response(*items)], post_responses=[token_response()] if code_url else [])


def test_at15_sso_sign_in() -> None:
    link = f"{SERVER}/samlsignon.aspx?orgid=5&idpid=1"
    http = sso_http(("1", "Corp", link))
    console = Console("https://addins.example.com/samlAuth.html?authorization_code=the-code&orgid=5\n")
    token = sso_sign_in(SERVER, None, make_deps(http, console))
    assert token == Token("acc", "ref", NOW + 1199)

    [opened] = console.opened
    query = parse_qs(urlsplit(opened).query)
    assert urlsplit(opened).path == "/samlsignon.aspx"
    assert query["orgid"] == ["5"]
    assert query["idpid"] == ["1"]
    assert query["client"] == ["outlook"]
    [post] = http.posts()
    assert post.url == f"{SERVER}/api/v1/token"
    assert post.data is not None
    assert post.data["grant_type"] == "code"
    assert post.data["code"] == "the-code"
    verifier = post.data["code_verifier"]
    digest = urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [digest]

    assert http.calls[0].method == "GET"
    assert http.calls[0].url == f"{SERVER}/api/v1/auth/identityproviders"
    assert http.calls[0].timeout == 60
    assert console.prompts == ["Copy that full URL from the address bar and paste it here: "]
    assert console.lines == [
        f"Sign in with your browser. If it doesn't open, open this URL manually:\n\n    {opened}\n",
        "Once signed in, you will be redirected to a page whose URL contains 'authorization_code='.",
    ]


def test_sso_browser_failures_are_ignored() -> None:
    def broken(_url: str) -> bool:
        raise RuntimeError("no browser")

    for opener in (broken, lambda _url: False):
        http = sso_http(("1", "Corp", f"{SERVER}/samlsignon.aspx?orgid=5&idpid=1"))
        deps = make_deps(http, Console("https://x/?authorization_code=c"))
        deps = replace(deps, open_browser=opener)
        assert sso_sign_in(SERVER, None, deps).access_token == "acc"


def test_at16_two_identity_providers() -> None:
    items = (("1", "A", f"{SERVER}/a?orgid=1&idpid=1"), ("2", "B", f"{SERVER}/b?orgid=1&idpid=2"))
    http = sso_http(*items, code_url=False)
    console = Console()
    with pytest.raises(AuthenticationError, match=r"available: A, B\)"):
        sso_sign_in(SERVER, None, make_deps(http, console))
    assert not console.opened
    assert not http.posts()

    http = sso_http(*items)
    console = Console("https://x/?authorization_code=c")
    sso_sign_in(SERVER, "B", make_deps(http, console))
    assert urlsplit(console.opened[0]).path == "/b"


def test_sso_idp_name_without_match() -> None:
    http = sso_http(("1", "A", "l"), ("2", "B", "l"), code_url=False)
    with pytest.raises(AuthenticationError, match=r"available: A, B\)"):
        sso_sign_in(SERVER, "C", make_deps(http))


def test_sso_without_identity_providers() -> None:
    http = FakeHttp(get_responses=[FakeResponse(json_body={"items": []})])
    with pytest.raises(AuthenticationError, match=r"\(available: none\)"):
        sso_sign_in(SERVER, None, make_deps(http))


def test_sso_identity_provider_listing_error() -> None:
    http = FakeHttp(get_responses=[FakeResponse(json_body={"error": "x"}, status_code=403)])
    with pytest.raises(AuthenticationError, match=r"^Could not list the identity providers \(HTTP 403\)$"):
        sso_sign_in(SERVER, None, make_deps(http))


@pytest.mark.parametrize(
    "response",
    [
        ConnectionError("down"),
        FakeResponse(WAF_PAGE, 200),
        FakeResponse(json_body={"items": "nope"}),
        FakeResponse(json_body=[1]),
    ],
)
def test_sso_identity_provider_listing_transient(response: FakeResponse | Exception) -> None:
    http = FakeHttp(get_responses=[response])
    console = Console()
    with pytest.raises(TransientAuthenticationError):
        sso_sign_in(SERVER, None, make_deps(http, console))
    assert not console.opened


def test_sso_identity_provider_without_link() -> None:
    http = FakeHttp(get_responses=[FakeResponse(json_body={"items": [{"id": "1", "name": "A"}]})])
    with pytest.raises(AuthenticationError, match="no sign-on link"):
        sso_sign_in(SERVER, None, make_deps(http))


def test_at17_no_terminal_and_no_prompt() -> None:
    http = FakeHttp()
    console = Console()
    deps = make_deps(http, console, is_terminal=False, with_prompt=False)
    with pytest.raises(AuthenticationError, match=r"login\.sh"):
        sso_sign_in(SERVER, None, deps)
    assert not console.opened
    assert not http.calls
    assert not console.lines


def test_no_terminal_with_injected_prompt_is_allowed() -> None:
    http = sso_http(("1", "A", f"{SERVER}/s?orgid=5&idpid=1"))
    console = Console("https://x/?authorization_code=c")
    assert sso_sign_in(SERVER, None, make_deps(http, console, is_terminal=False)).access_token == "acc"


def test_at17_end_of_input_at_the_prompt() -> None:
    http = sso_http(("1", "A", f"{SERVER}/s?orgid=5&idpid=1"), code_url=False)
    with pytest.raises(AuthenticationError, match="cancelled"):
        sso_sign_in(SERVER, None, make_deps(http, Console(EOFError())))
    assert not http.posts()


def test_sso_error_in_pasted_url() -> None:
    http = sso_http(("1", "A", f"{SERVER}/s?orgid=5&idpid=1"), code_url=False)
    with pytest.raises(AuthenticationError, match="Single sign-on failed: access_denied"):
        sso_sign_in(SERVER, None, make_deps(http, Console("https://x/?error=access_denied")))
    assert not http.posts()


def test_make_login_sso() -> None:
    http = sso_http(("1", "A", f"{SERVER}/s?orgid=5&idpid=1"))
    login = make_login(SERVER, "sso", None, "A", make_deps(http, Console("https://x/?authorization_code=c")))
    assert login().access_token == "acc"
