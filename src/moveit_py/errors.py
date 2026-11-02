"""Errors raised by moveit-py."""


class AuthenticationError(Exception):
    """Signing in failed."""


class TransientAuthenticationError(AuthenticationError):
    """Signing in failed for a reason that may go away: network error, 5xx, non-JSON reply."""


class SignInExpiredError(AuthenticationError):
    """The token can't be refreshed and signing in again isn't allowed."""


class ResponseException(Exception):
    """Unexpected or error response from the API."""


class TransferToolException(Exception):
    """The command can't proceed, e.g. invalid input or an unsafe path."""


class PackageNotSentError(TransferToolException):
    """Sending a package failed, and it's certain nothing was sent."""
