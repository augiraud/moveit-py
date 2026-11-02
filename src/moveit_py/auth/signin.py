"""Sign in to the MoveIt API with a password or through single sign-on.

The SSO flow is undocumented and was reverse-engineered.
"""

import base64
import contextlib
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import requests

from moveit_py.api import Response
from moveit_py.auth.token import Token
from moveit_py.config import AUTH_TIMEOUT, SSO_CLIENT
from moveit_py.errors import AuthenticationError, TransientAuthenticationError

HTTP_BAD_REQUEST = 400
HTTP_SERVER_ERROR = 500
PASSWORD_VARIABLE = "MOVEIT_PASSWORD"  # noqa: S105
SECURITY_NOTICE_ERROR = "accept_security_notice_required"
SECURITY_NOTICE_FIELD = "accept_security_notice"
DEFAULT_SECURITY_NOTICE = "You must accept the security notice of the MOVEit server."
SECURITY_NOTICE_PROMPT = "Accept the security notice? [y/N] "
NOT_SIGNED_IN_MESSAGE = "Not signed in, or the sign-in expired: run ./scripts/login.sh first."
CANCELLED_MESSAGE = "Sign-in cancelled, no URL was pasted."
NO_CODE_MESSAGE = "No authorization code found in the URL."
BROWSER_MESSAGE = "Sign in with your browser. If it doesn't open, open this URL manually:\n\n    {url}\n"
REDIRECT_MESSAGE = "Once signed in, you will be redirected to a page whose URL contains 'authorization_code='."
PASTE_PROMPT = "Copy that full URL from the address bar and paste it here: "

type HttpPost = Callable[[str, dict[str, str], float], Response]
type HttpGet = Callable[[str, float], Response]
type Prompt = Callable[[str], str]
type AuthMethod = Literal["sso", "password"]


def default_http_post(url: str, data: dict[str, str], timeout: float) -> Response:
    """POST a form with `requests`."""
    return requests.post(url, data=data, timeout=timeout)


def default_http_get(url: str, timeout: float) -> Response:
    """GET with `requests`."""
    return requests.get(url, timeout=timeout)


def stdin_is_terminal() -> bool:
    """Whether standard input is an interactive terminal."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except ValueError:  # closed stdin
        return False


@dataclass(frozen=True)
class SignInDeps:
    """HTTP, browser and prompt functions used when signing in. They can be replaced in tests."""

    http_post: HttpPost = default_http_post
    http_get: HttpGet = default_http_get
    open_browser: Callable[[str], object] = webbrowser.open
    prompt: Prompt | None = None
    getpass: Prompt = getpass.getpass
    output: Callable[[str], object] = print
    is_terminal: Callable[[], bool] = stdin_is_terminal
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    now: Callable[[], float] = time.time

    @property
    def ask(self) -> Prompt:
        """The prompt function: the injected one, else `input`."""
        return self.prompt or input


def request_token(server: str, fields: Mapping[str, str], deps: SignInDeps) -> Token:
    """POST the form `fields` to the token endpoint and build the Token, handling the security notice."""
    data = dict(fields)
    body, status, text = _post_token(server, data, deps)
    if status < HTTP_BAD_REQUEST and body.get("access_token"):
        return _token_from_body(body, status, deps)
    if body.get("error") == SECURITY_NOTICE_ERROR and SECURITY_NOTICE_FIELD not in data:
        description = body.get("error_description") or DEFAULT_SECURITY_NOTICE
        deps.output(str(description))
        if _answered_yes(deps):
            return request_token(server, {**data, SECURITY_NOTICE_FIELD: "true"}, deps)
    detail = body.get("error_description") or body.get("error") or text
    raise AuthenticationError(f"Authentication failed (HTTP {status}): {detail}")


def _answered_yes(deps: SignInDeps) -> bool:
    try:
        return deps.ask(SECURITY_NOTICE_PROMPT).strip().lower() == "y"
    except EOFError:
        return False


def _post_token(server: str, data: dict[str, str], deps: SignInDeps) -> tuple[dict[str, Any], int, str]:
    """Send the request and return the body, status and raw response."""
    try:
        response = deps.http_post(f"{server}/api/v1/token", data, AUTH_TIMEOUT)
    except OSError as error:  # includes requests.RequestException
        raise TransientAuthenticationError(f"Could not reach {server}: {error}") from error
    status = response.status_code
    try:
        parsed = response.json()
    except ValueError as error:
        raise TransientAuthenticationError(f"Unexpected response from the token endpoint (HTTP {status})") from error
    if status >= HTTP_SERVER_ERROR:
        raise TransientAuthenticationError(f"The token endpoint failed (HTTP {status}): {json.dumps(parsed)}")
    return (parsed if isinstance(parsed, dict) else {}), status, response.text


def _token_from_body(body: Mapping[str, Any], status: int, deps: SignInDeps) -> Token:
    """Build the token without including the response body in errors: it contains credentials."""
    try:
        return Token.from_response(body, deps.now())
    except (KeyError, TypeError, ValueError) as error:
        raise TransientAuthenticationError(f"Unexpected response from the token endpoint (HTTP {status})") from error


def refresh(server: str, token: Token, deps: SignInDeps) -> Token:
    """Get a new token with the refresh token (which is single-use)."""
    return request_token(server, {"grant_type": "refresh_token", "refresh_token": token.refresh_token}, deps)


def password_sign_in(server: str, username: str, deps: SignInDeps) -> Token:
    """Sign in with a password: `MOVEIT_PASSWORD` if non-empty, else prompted without echo. Never stored."""
    password = deps.environ.get(PASSWORD_VARIABLE) or deps.getpass(f"MOVEit password for {username}: ")
    return request_token(server, {"grant_type": "password", "username": username, "password": password}, deps)


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code verifier and its S256 challenge."""
    verifier = _base64url(secrets.token_bytes(64))
    return verifier, _base64url(hashlib.sha256(verifier.encode("ascii")).digest())


def parse_authorization_code(pasted: str) -> str:
    """Extract the authorization code from the pasted redirect URL."""
    try:
        query = parse_qs(urlsplit(pasted.strip()).query)
    except ValueError:
        query = {}
    if query.get("authorization_code"):
        return query["authorization_code"][0]
    if query.get("error"):
        raise AuthenticationError(f"Single sign-on failed: {query['error'][0]}")
    raise AuthenticationError(NO_CODE_MESSAGE)


def pick_identity_provider(items: list[dict[str, Any]], idp: str | None) -> dict[str, Any]:
    """Find the named identity provider, or the only one if no name was given."""
    candidates = [item for item in items if idp is None or item.get("name") == idp]
    if len(candidates) != 1:
        names = ", ".join(str(item["name"]) for item in items if isinstance(item.get("name"), str)) or "none"
        raise AuthenticationError(f"Could not pick an identity provider, choose one with --idp (available: {names})")
    return candidates[0]


def list_identity_providers(server: str, deps: SignInDeps) -> list[dict[str, Any]]:
    """Get the identity providers from the server."""
    try:
        response = deps.http_get(f"{server}/api/v1/auth/identityproviders", AUTH_TIMEOUT)
    except OSError as error:
        raise TransientAuthenticationError(f"Could not reach {server}: {error}") from error
    status = response.status_code
    if status >= HTTP_BAD_REQUEST:
        raise AuthenticationError(f"Could not list the identity providers (HTTP {status})")
    try:
        items = response.json()["items"]
    except (ValueError, KeyError, TypeError) as error:
        raise TransientAuthenticationError(
            f"Unexpected response from the identity providers (HTTP {status})"
        ) from error
    if not isinstance(items, list):
        raise TransientAuthenticationError(f"Unexpected response from the identity providers (HTTP {status})")
    return [item for item in items if isinstance(item, dict)]


def sso_sign_in(server: str, idp: str | None, deps: SignInDeps) -> Token:
    """Single sign-on through the browser: the user pastes back the final URL, which holds an authorization code."""
    if deps.prompt is None and not deps.is_terminal():
        raise AuthenticationError(NOT_SIGNED_IN_MESSAGE)
    verifier, challenge = make_pkce_pair()
    link = pick_identity_provider(list_identity_providers(server, deps), idp).get("link")
    if not isinstance(link, str) or not link:
        raise AuthenticationError("The identity provider has no sign-on link.")
    url = f"{link}&client={SSO_CLIENT}&code_challenge={challenge}"
    deps.output(BROWSER_MESSAGE.format(url=url))
    with contextlib.suppress(Exception):  # failing to open a browser is not an error: the URL is printed
        deps.open_browser(url)
    deps.output(REDIRECT_MESSAGE)
    try:
        pasted = deps.ask(PASTE_PROMPT)
    except EOFError as error:
        raise AuthenticationError(CANCELLED_MESSAGE) from error
    code = parse_authorization_code(pasted)
    return request_token(server, {"grant_type": "code", "code": code, "code_verifier": verifier}, deps)


def make_login(
    server: str, auth: AuthMethod, username: str | None, idp: str | None, deps: SignInDeps
) -> Callable[[], Token]:
    """Get the sign-in function for the configured authentication method."""

    def login() -> Token:
        if auth == "sso":
            return sso_sign_in(server, idp, deps)
        if not username:
            raise AuthenticationError("A username is required for password authentication.")
        return password_sign_in(server, username, deps)

    return login


def make_refresh(server: str, deps: SignInDeps) -> Callable[[Token], Token]:
    """Get the token refresh function."""

    def refresh_token(token: Token) -> Token:
        return refresh(server, token, deps)

    return refresh_token
