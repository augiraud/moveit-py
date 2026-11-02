"""Provide and refresh tokens for the MoveIt API."""

import dataclasses
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from moveit_py.auth.signin import SignInDeps, make_login, make_refresh
from moveit_py.auth.store import TokenStore
from moveit_py.auth.token import Token
from moveit_py.config import EXPIRY_MARGIN, KEEP_ALIVE_INTERVAL, KEEP_ALIVE_MARGIN
from moveit_py.errors import AuthenticationError, SignInExpiredError, TransientAuthenticationError

if TYPE_CHECKING:
    from moveit_py.cli import Settings

SIGN_IN_EXPIRED_MESSAGE = "The sign-in expired."
STOP_JOIN_TIMEOUT = 1.0


def warn_on_stderr(message: str) -> None:
    """Print a warning to stderr."""
    print(message, file=sys.stderr)


class TokenProvider:
    """Provide a valid token from the cache, by refreshing it, or by signing in.

    A lock prevents concurrent refreshes and sign-ins. Token changes are saved to the cache when enabled; a write failure is reported as a warning.
    """

    def __init__(
        self,
        *,
        refresh: Callable[[Token], Token],
        login: Callable[[], Token],
        store: TokenStore | None,
        url: str,
        clock: Callable[[], float] = time.time,
        keep_alive_interval: float = KEEP_ALIVE_INTERVAL,
        load_cache: bool = True,
        warn: Callable[[str], object] = warn_on_stderr,
    ) -> None:
        self._refresh = refresh
        self._login = login
        self._store = store
        self._url = url
        self._clock = clock
        self._warn = warn
        self._keep_alive_interval = keep_alive_interval
        self._lock = threading.Lock()
        self._token: Token | None = store.load(url) if store is not None and load_cache else None
        self._login_allowed = True
        self._sign_in_expired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def sign_in_expired(self) -> bool:
        """Whether the sign-in has expired."""
        with self._lock:
            return self._sign_in_expired

    @property
    def keep_alive_running(self) -> bool:
        """Whether the keep-alive thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def get_access_token(self) -> str:
        """A valid access token, refreshing or signing in if needed. Raises SignInExpiredError once the sign-in is lost."""
        with self._lock:
            if self._sign_in_expired:
                raise SignInExpiredError(SIGN_IN_EXPIRED_MESSAGE)
            if self._token is None or self._token.is_expired(self._clock(), EXPIRY_MARGIN):
                self._refresh_or_login()
            token = self._token
            if token is None:  # unreachable: _refresh_or_login sets a token or raises
                raise SignInExpiredError(SIGN_IN_EXPIRED_MESSAGE)
            return token.access_token

    def invalidate(self, access_token: str) -> None:
        """Mark a rejected token as expired, unless it has already been replaced."""
        with self._lock:
            if self._token is not None and self._token.access_token == access_token:
                self._set_token(dataclasses.replace(self._token, expires_at=0.0))

    def disable_login(self) -> None:
        """Do not sign in again, so transfer threads cannot prompt the user."""
        with self._lock:
            self._login_allowed = False

    def start_keep_alive(self) -> None:
        """Refresh the token in the background before it expires."""
        self.stop_keep_alive()  # never two refreshers
        stop = threading.Event()
        thread = threading.Thread(target=self._keep_alive, args=(stop,), name="token-keep-alive", daemon=True)
        self._stop = stop
        self._thread = thread
        thread.start()

    def stop_keep_alive(self) -> None:
        """Stop the background refresh, if running."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_TIMEOUT)

    def _keep_alive(self, stop: threading.Event) -> None:
        while not stop.wait(self._keep_alive_interval):
            if not self._keep_alive_step():
                return

    def _keep_alive_step(self) -> bool:
        """Check the token. Return False if the thread should stop."""
        with self._lock:
            if self._sign_in_expired or self._token is None:
                return False
            if not self._token.is_expired(self._clock(), KEEP_ALIVE_MARGIN):
                return True
            try:
                self._refresh_or_login()
            except AuthenticationError:
                return False
            return True

    def _refresh_or_login(self) -> None:
        """Refresh the token or sign in again. Called with the lock held."""
        token = self._token
        if token is not None:
            try:
                self._set_token(self._refresh(token))
            except TransientAuthenticationError:
                if self._clock() < token.expires_at:
                    return  # still valid, ignoring the margin: keep it and retry later
                if self._login_allowed:
                    raise  # before transfers: report the network problem
            except AuthenticationError:
                pass  # the refresh token expired too
            else:
                return
        if not self._login_allowed:
            self._sign_in_expired = True
            raise SignInExpiredError(SIGN_IN_EXPIRED_MESSAGE)
        self._set_token(self._login())

    def _set_token(self, token: Token) -> None:
        self._token = token
        if self._store is not None:
            try:
                self._store.save(self._url, token)
            except OSError as error:
                self._warn(f"Warning: could not save the token cache {self._store.path}: {error}")


def build_provider(
    settings: Settings, signin_deps: SignInDeps, clock: Callable[[], float] = time.time
) -> TokenProvider:
    """Build a token provider. Login ignores the cached token but still updates it."""
    return TokenProvider(
        refresh=make_refresh(settings.url, signin_deps),
        login=make_login(settings.url, settings.auth, settings.username, settings.idp, signin_deps),
        store=None if settings.no_token_cache else TokenStore(settings.token_file),
        url=settings.url,
        clock=clock,
        load_cache=settings.command != "login",
    )
