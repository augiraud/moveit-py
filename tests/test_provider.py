import threading
import time
from pathlib import Path

import pytest
from fakes import FakeHttp, FakeResponse
from test_signin import make_deps

from moveit_py.auth.provider import TokenProvider, build_provider
from moveit_py.auth.store import TokenStore
from moveit_py.auth.token import Token
from moveit_py.cli import Settings
from moveit_py.errors import AuthenticationError, SignInExpiredError, TransientAuthenticationError

URL = "https://moveit.example.com"
NOW = 1_000_000.0
TIMEOUT = 5.0
EXPIRED = Token("old-access", "old-refresh", NOW - 10)
IN_MARGIN = Token("margin-access", "margin-refresh", NOW + 30)  # valid, but expired with the 60 s margin
KEEP_ALIVE_DUE = Token("due-access", "due-refresh", NOW + 200)  # valid, but due for the keep-alive (300 s margin)
VALID = Token("valid-access", "valid-refresh", NOW + 1000)
NEW = Token("new-access", "new-refresh", NOW + 1200)
SIGNED_IN = Token("login-access", "login-refresh", NOW + 1200)
SIGN_IN_EXPIRED = "The sign-in expired"

type Outcome = Token | Exception


class Fake:
    """A recording `refresh`/`login` callable answering from a list of outcomes (the last one repeats)."""

    def __init__(self, *outcomes: Outcome) -> None:
        self.outcomes = outcomes
        self.calls: list[Token | None] = []
        self.reached: dict[int, threading.Event] = {n: threading.Event() for n in range(1, 10)}

    def __call__(self, token: Token | None = None) -> Token:
        self.calls.append(token)
        count = len(self.calls)
        if not self.outcomes:
            pytest.fail("unexpected call")
        outcome = self.outcomes[min(count, len(self.outcomes)) - 1]
        self.reached[count].set()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_provider(
    tmp_path: Path,
    token: Token | None = None,
    *,
    refresh: Fake | None = None,
    login: Fake | None = None,
    interval: float = 3600,
) -> tuple[TokenProvider, TokenStore]:
    """A provider whose initial token is `token` (read from a store in `tmp_path`)."""
    store = TokenStore(tmp_path / "tokens.json")
    if token is not None:
        store.save(URL, token)
    provider = TokenProvider(
        refresh=refresh or Fake(),
        login=login or Fake(),
        store=store,
        url=URL,
        clock=lambda: NOW,
        keep_alive_interval=interval,
    )
    return provider, store


def test_at19_valid_cached_token_is_used_without_signing_in(tmp_path: Path) -> None:
    refresh, login = Fake(), Fake()
    provider, _ = make_provider(tmp_path, VALID, refresh=refresh, login=login)
    assert provider.get_access_token() == "valid-access"
    assert provider.get_access_token() == "valid-access"
    assert refresh.calls == [] and login.calls == []


def test_at20_expired_cached_token_is_refreshed_and_persisted(tmp_path: Path) -> None:
    refresh, login = Fake(NEW), Fake()
    provider, store = make_provider(tmp_path, EXPIRED, refresh=refresh, login=login)
    assert provider.get_access_token() == "new-access"
    assert refresh.calls == [EXPIRED]
    assert login.calls == []
    assert store.load(URL) == NEW
    assert TokenStore(store.path).load(URL) == NEW


def test_token_within_the_expiry_margin_is_refreshed(tmp_path: Path) -> None:
    refresh = Fake(NEW)
    provider, _ = make_provider(tmp_path, IN_MARGIN, refresh=refresh)
    assert provider.get_access_token() == "new-access"
    assert refresh.calls == [IN_MARGIN]


def test_at21_signs_in_when_the_refresh_fails(tmp_path: Path) -> None:
    refresh, login = Fake(AuthenticationError("invalid_grant")), Fake(SIGNED_IN)
    provider, store = make_provider(tmp_path, EXPIRED, refresh=refresh, login=login)
    assert provider.get_access_token() == "login-access"
    assert len(refresh.calls) == 1 and len(login.calls) == 1
    assert store.load(URL) == SIGNED_IN


def test_no_token_and_sign_in_allowed_signs_in_and_persists(tmp_path: Path) -> None:
    refresh, login = Fake(), Fake(SIGNED_IN)
    provider, store = make_provider(tmp_path, refresh=refresh, login=login)
    assert provider.get_access_token() == "login-access"
    assert provider.get_access_token() == "login-access"
    assert refresh.calls == []
    assert len(login.calls) == 1
    assert store.load(URL) == SIGNED_IN


def test_no_token_and_sign_in_disabled_is_sign_in_expired(tmp_path: Path) -> None:
    refresh, login = Fake(), Fake()
    provider, _ = make_provider(tmp_path, refresh=refresh, login=login)
    provider.disable_login()
    with pytest.raises(SignInExpiredError, match=r"^The sign-in expired\.$"):
        provider.get_access_token()
    assert provider.sign_in_expired
    assert refresh.calls == [] and login.calls == []


def test_login_error_is_propagated_and_is_not_sign_in_expired(tmp_path: Path) -> None:
    provider, _ = make_provider(tmp_path, login=Fake(AuthenticationError("denied")))
    with pytest.raises(AuthenticationError, match="denied"):
        provider.get_access_token()
    assert not provider.sign_in_expired


def test_unwritable_cache_does_not_fail(tmp_path: Path) -> None:
    (tmp_path / "blocker").write_text("a file, not a directory")
    store = TokenStore(tmp_path / "blocker" / "tokens.json")
    warnings: list[str] = []
    provider = TokenProvider(
        refresh=Fake(), login=Fake(SIGNED_IN), store=store, url=URL, clock=lambda: NOW, warn=warnings.append
    )
    assert provider.get_access_token() == "login-access"
    [warning] = warnings
    assert warning.startswith(f"Warning: could not save the token cache {store.path}: ")
    assert "login-access" not in warning
    assert "login-refresh" not in warning


def test_at22_invalidate_with_an_outdated_access_token_does_nothing(tmp_path: Path) -> None:
    refresh = Fake(NEW)
    provider, store = make_provider(tmp_path, VALID, refresh=refresh)
    provider.invalidate("some-older-access-token")
    assert provider.get_access_token() == "valid-access"
    assert refresh.calls == []
    assert store.load(URL) == VALID


def test_at22_invalidate_with_the_current_access_token_forces_a_refresh(tmp_path: Path) -> None:
    refresh = Fake(NEW)
    provider, store = make_provider(tmp_path, VALID, refresh=refresh)
    invalidated = Token("valid-access", "valid-refresh", 0.0)
    assert provider.get_access_token() == "valid-access"
    provider.invalidate("valid-access")
    assert store.load(URL) == invalidated  # the cache follows the memory
    assert provider.get_access_token() == "new-access"
    assert refresh.calls == [invalidated]
    assert store.load(URL) == NEW


def test_invalidate_without_a_token_does_nothing(tmp_path: Path) -> None:
    provider, store = make_provider(tmp_path)
    provider.invalidate("anything")
    assert store.load(URL) is None


def wait_until_stopped(provider: TokenProvider) -> None:
    deadline = time.monotonic() + TIMEOUT
    while provider.keep_alive_running and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    assert not provider.keep_alive_running


def test_at23_keep_alive_retries_transient_failures_with_the_same_refresh_token(tmp_path: Path) -> None:
    refresh = Fake(TransientAuthenticationError("network"), TransientAuthenticationError("network"), NEW)
    provider, store = make_provider(tmp_path, KEEP_ALIVE_DUE, refresh=refresh, interval=0.01)
    provider.start_keep_alive()
    try:
        assert refresh.reached[3].wait(TIMEOUT)
        # Takes the lock, which the keep-alive holds until the new token is set.
        assert provider.get_access_token() == "new-access"
    finally:
        provider.stop_keep_alive()
    assert refresh.calls == [KEEP_ALIVE_DUE] * 3
    assert store.load(URL) == NEW
    assert not provider.keep_alive_running


def test_keep_alive_leaves_a_token_that_is_not_due_alone(tmp_path: Path) -> None:
    refresh = Fake(NEW)
    provider, _ = make_provider(tmp_path, VALID, refresh=refresh, interval=0.01)
    provider.start_keep_alive()
    try:
        assert not refresh.reached[1].wait(0.2)
        assert provider.keep_alive_running
    finally:
        provider.stop_keep_alive()
    assert refresh.calls == []


def test_at24_keep_alive_non_transient_failure_with_sign_in_disabled(tmp_path: Path) -> None:
    refresh, login = Fake(AuthenticationError("invalid_grant")), Fake()
    provider, _ = make_provider(tmp_path, KEEP_ALIVE_DUE, refresh=refresh, login=login, interval=0.01)
    provider.disable_login()
    provider.start_keep_alive()
    try:
        assert refresh.reached[1].wait(TIMEOUT)
        wait_until_stopped(provider)
    finally:
        provider.stop_keep_alive()
    assert provider.sign_in_expired
    with pytest.raises(SignInExpiredError, match=SIGN_IN_EXPIRED):
        provider.get_access_token()
    assert len(refresh.calls) == 1 and login.calls == []


def test_keep_alive_exits_on_a_non_transient_error_with_sign_in_allowed(tmp_path: Path) -> None:
    # With sign-in allowed, a refresh failure falls through to the sign-in, which here fails too.
    refresh, login = Fake(AuthenticationError("invalid_grant")), Fake(AuthenticationError("denied"))
    provider, _ = make_provider(tmp_path, KEEP_ALIVE_DUE, refresh=refresh, login=login, interval=0.01)
    provider.start_keep_alive()
    try:
        assert login.reached[1].wait(TIMEOUT)
        wait_until_stopped(provider)
    finally:
        provider.stop_keep_alive()
    assert len(refresh.calls) == 1 and len(login.calls) == 1
    assert not provider.sign_in_expired


def test_keep_alive_exits_when_there_is_no_token(tmp_path: Path) -> None:
    refresh = Fake()
    provider, _ = make_provider(tmp_path, refresh=refresh, interval=0.01)
    provider.start_keep_alive()
    try:
        wait_until_stopped(provider)
    finally:
        provider.stop_keep_alive()
    assert refresh.calls == []


def test_keep_alive_stops_promptly_without_waiting_for_the_interval(tmp_path: Path) -> None:
    provider, _ = make_provider(tmp_path, VALID, interval=3600)
    provider.start_keep_alive()
    assert provider.keep_alive_running
    started = time.monotonic()
    provider.stop_keep_alive()
    assert time.monotonic() - started < TIMEOUT
    assert not provider.keep_alive_running


def test_stop_keep_alive_without_start_is_safe(tmp_path: Path) -> None:
    provider, _ = make_provider(tmp_path)
    provider.stop_keep_alive()
    provider.stop_keep_alive()
    assert not provider.keep_alive_running


def test_keep_alive_can_be_restarted(tmp_path: Path) -> None:
    refresh = Fake(NEW)
    provider, _ = make_provider(tmp_path, KEEP_ALIVE_DUE, refresh=refresh, interval=0.01)
    provider.start_keep_alive()
    provider.stop_keep_alive()
    provider.start_keep_alive()
    try:
        assert refresh.reached[1].wait(TIMEOUT)
        assert provider.get_access_token() == "new-access"
    finally:
        provider.stop_keep_alive()


def test_start_keep_alive_stops_a_running_one_first(tmp_path: Path) -> None:
    provider, _ = make_provider(tmp_path, VALID, interval=3600)
    before = {t for t in threading.enumerate() if t.name == "token-keep-alive"}
    provider.start_keep_alive()
    provider.start_keep_alive()
    try:
        alive = {t for t in threading.enumerate() if t.name == "token-keep-alive"} - before
        assert len(alive) == 1
    finally:
        provider.stop_keep_alive()
    assert not provider.keep_alive_running


def test_at25_sign_in_disabled_expired_token_non_transient_failure_is_tried_once(tmp_path: Path) -> None:
    refresh, login = Fake(AuthenticationError("invalid_grant")), Fake()
    provider, _ = make_provider(tmp_path, EXPIRED, refresh=refresh, login=login)
    provider.disable_login()
    for _ in range(3):
        with pytest.raises(SignInExpiredError, match=SIGN_IN_EXPIRED):
            provider.get_access_token()
    assert len(refresh.calls) == 1
    assert login.calls == []


@pytest.mark.parametrize("error", [AuthenticationError("invalid_grant"), TransientAuthenticationError("network")])
def test_at45_many_threads_on_an_expired_token_make_one_refresh_attempt(tmp_path: Path, error: Exception) -> None:
    refresh, login = Fake(error), Fake()
    provider, _ = make_provider(tmp_path, EXPIRED, refresh=refresh, login=login)
    provider.disable_login()
    count = 16
    barrier = threading.Barrier(count)
    outcomes: list[BaseException | None] = [None] * count

    def worker(index: int) -> None:
        barrier.wait(TIMEOUT)
        try:
            provider.get_access_token()
        except SignInExpiredError as caught:
            outcomes[index] = caught

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
    assert all(isinstance(outcome, SignInExpiredError) for outcome in outcomes)
    assert len(refresh.calls) == 1
    assert login.calls == []


def test_many_threads_share_one_refresh_that_succeeds(tmp_path: Path) -> None:
    refresh = Fake(NEW)
    provider, _ = make_provider(tmp_path, EXPIRED, refresh=refresh)
    provider.disable_login()
    count = 16
    barrier = threading.Barrier(count)
    results: list[str] = []

    def worker() -> None:
        barrier.wait(TIMEOUT)
        results.append(provider.get_access_token())

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
    assert results == ["new-access"] * count
    assert len(refresh.calls) == 1


def test_at26_transient_failure_sign_in_disabled_token_within_margin_keeps_being_served(tmp_path: Path) -> None:
    refresh, login = Fake(TransientAuthenticationError("network")), Fake()
    provider, _ = make_provider(tmp_path, IN_MARGIN, refresh=refresh, login=login)
    provider.disable_login()
    assert provider.get_access_token() == "margin-access"
    assert provider.get_access_token() == "margin-access"
    assert len(refresh.calls) == 2  # retried at each call
    assert login.calls == []
    assert not provider.sign_in_expired


def test_at26_transient_failure_sign_in_disabled_expired_token_is_sign_in_expired(tmp_path: Path) -> None:
    refresh, login = Fake(TransientAuthenticationError("network")), Fake()
    provider, _ = make_provider(tmp_path, EXPIRED, refresh=refresh, login=login)
    provider.disable_login()
    with pytest.raises(SignInExpiredError, match=SIGN_IN_EXPIRED):
        provider.get_access_token()
    assert provider.sign_in_expired
    assert login.calls == []


def test_at26_transient_failure_sign_in_allowed_expired_token_raises_the_transient_error(tmp_path: Path) -> None:
    refresh, login = Fake(TransientAuthenticationError("network down")), Fake()
    provider, _ = make_provider(tmp_path, EXPIRED, refresh=refresh, login=login)
    with pytest.raises(TransientAuthenticationError, match="network down"):
        provider.get_access_token()
    assert login.calls == []
    assert not provider.sign_in_expired


def settings(tmp_path: Path, *, command: str = "upload", no_token_cache: bool = False) -> Settings:
    return Settings(
        url=URL,
        auth="password",
        username="alice",
        idp=None,
        token_file=tmp_path / "tokens.json",
        no_token_cache=no_token_cache,
        command=command,
    )


def token_response(access: str, refresh: str) -> FakeResponse:
    return FakeResponse(json_body={"access_token": access, "refresh_token": refresh, "expires_in": 1199})


def test_factory_uses_and_updates_the_cache(tmp_path: Path) -> None:
    TokenStore(tmp_path / "tokens.json").save(URL, EXPIRED)
    http = FakeHttp(post_responses=[token_response("acc", "ref")])
    provider = build_provider(settings(tmp_path), make_deps(http), clock=lambda: NOW)
    assert provider.get_access_token() == "acc"
    assert [call.data for call in http.posts()] == [{"grant_type": "refresh_token", "refresh_token": "old-refresh"}]
    saved = TokenStore(tmp_path / "tokens.json").load(URL)
    assert saved is not None and saved.refresh_token == "ref"


def test_login_command_discards_the_cache_and_signs_in_again(tmp_path: Path) -> None:
    TokenStore(tmp_path / "tokens.json").save(URL, VALID)
    http = FakeHttp(post_responses=[token_response("fresh", "fresh-ref")])
    deps = make_deps(http, environ={"MOVEIT_PASSWORD": "s3cret"})
    provider = build_provider(settings(tmp_path, command="login"), deps, clock=lambda: NOW)
    assert provider.get_access_token() == "fresh"
    assert [(call.data or {}).get("grant_type") for call in http.posts()] == ["password"]
    saved = TokenStore(tmp_path / "tokens.json").load(URL)
    assert saved is not None and saved.access_token == "fresh"


def test_no_token_cache_reads_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    TokenStore(path).save(URL, VALID)
    before = path.read_text()
    http = FakeHttp(post_responses=[token_response("fresh", "fresh-ref")])
    deps = make_deps(http, environ={"MOVEIT_PASSWORD": "s3cret"})
    provider = build_provider(settings(tmp_path, no_token_cache=True), deps, clock=lambda: NOW)
    assert provider.get_access_token() == "fresh"  # the cached valid token was not read
    assert path.read_text() == before


def test_no_token_cache_creates_no_file(tmp_path: Path) -> None:
    http = FakeHttp(post_responses=[token_response("fresh", "fresh-ref")])
    deps = make_deps(http, environ={"MOVEIT_PASSWORD": "s3cret"})
    provider = build_provider(settings(tmp_path, no_token_cache=True), deps)
    assert provider.get_access_token() == "fresh"
    assert not (tmp_path / "tokens.json").exists()
