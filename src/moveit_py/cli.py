"""Command line interface of moveit-py."""

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import IO, Any, Protocol

from moveit_py.api import ApiClient, MoveitClient
from moveit_py.auth.provider import build_provider
from moveit_py.auth.signin import AuthMethod, SignInDeps
from moveit_py.auth.store import TokenStore
from moveit_py.config import DEFAULT_THREADS, default_token_file, load_env_file
from moveit_py.errors import AuthenticationError, ResponseException, SignInExpiredError, TransferToolException
from moveit_py.folders import download_command, upload_command
from moveit_py.packages.listing import packages_command
from moveit_py.packages.receive import receive_command
from moveit_py.packages.send import send_command

DESCRIPTION = "Upload/download directories to/from a MOVEit Transfer server, send and receive packages."
AUTH_METHODS = ("sso", "password")
TRANSFER_COMMANDS = ("upload", "download", "send", "receive")
EXIT_INTERRUPTED = 130
EXIT_SIGN_IN_EXPIRED = 3

NO_SERVER_MESSAGE = "Error: no MOVEit server: set MOVEIT_URL in moveit.env (see moveit.env.example), or use --url."
USERNAME_REQUIRED_MESSAGE = "--username (or MOVEIT_USERNAME) is required for password authentication"
LOGIN_AGAIN = "Run ./scripts/login.sh, then run the same command again"
EXPIRED_SEND = f"The sign-in expired, the package was not sent. {LOGIN_AGAIN}."
EXPIRED_TRANSFER = f"The sign-in expired during the transfer. {LOGIN_AGAIN}: files already transferred are skipped."
EXPIRED_OTHER = f"The sign-in expired. {LOGIN_AGAIN}."


class SupportsWrite[T](Protocol):
    """A text stream argparse can write to."""

    def write(self, data: T, /) -> object:
        """Write text."""
        ...


class TokenProviderLike(Protocol):
    """What the CLI needs from the token provider."""

    def get_access_token(self) -> str:
        """Return a valid access token, signing in if needed."""
        ...

    def disable_login(self) -> None:
        """Forbid interactive sign-in from now on."""
        ...

    def start_keep_alive(self) -> None:
        """Start refreshing the token in the background."""
        ...

    def stop_keep_alive(self) -> None:
        """Stop the background refresh."""
        ...


@dataclass(frozen=True)
class Settings:
    """Resolved settings of one run."""

    url: str
    auth: AuthMethod
    username: str | None
    idp: str | None
    token_file: Path
    no_token_cache: bool
    command: str


def _forget_token(path: Path, url: str) -> bool:
    return TokenStore(path).delete(url)


def _connect(settings: Settings) -> tuple[TokenProviderLike, ApiClient]:
    """Create the token provider and client, signing in if needed."""
    provider = build_provider(settings, SignInDeps())
    return provider, MoveitClient(settings.url, provider)


@dataclass
class CliDeps:
    """Everything the CLI depends on, injectable for tests."""

    environ: MutableMapping[str, str] = field(default_factory=lambda: os.environ)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    stderr: IO[str] = field(default_factory=lambda: sys.stderr)
    forget_token: Callable[[Path, str], bool] = _forget_token
    connect: Callable[[Settings], tuple[TokenProviderLike, ApiClient]] = _connect
    commands: Mapping[str, Callable[[argparse.Namespace, ApiClient, CliDeps], int]] = field(
        default_factory=lambda: {
            "send": send_command,
            "upload": upload_command,
            "download": download_command,
            "packages": packages_command,
            "receive": receive_command,
        }
    )


class _Parser(argparse.ArgumentParser):
    """ArgumentParser writing to injected streams instead of sys.stdout/sys.stderr."""

    def __init__(self, *args: Any, out: IO[str], err: IO[str], **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self._out = out
        self._err = err

    def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
        if message:
            super()._print_message(message, self._err if file is sys.stderr else self._out)


def email_list(value: str) -> list[str]:
    """Parse a comma-separated list of emails."""
    items = [item.strip() for item in value.split(",")]
    items = [item for item in items if item]
    for item in items:
        if "@" not in item:
            raise argparse.ArgumentTypeError(f"not an email: {item}")
    return items


def thread_count(value: str) -> int:
    """Parse --threads: values below 1 mean 1."""
    return max(1, int(value))


def regex(value: str) -> re.Pattern[str]:
    """Compile --filter."""
    try:
        return re.compile(value)
    except re.error as error:
        raise argparse.ArgumentTypeError(f"invalid regex {value!r}: {error}") from error


def _add_common_options(parser: argparse.ArgumentParser, environ: Mapping[str, str], token_file: Path) -> None:
    group = parser.add_argument_group("common options")
    group.add_argument(
        "--url",
        default=environ.get("MOVEIT_URL"),
        help="URL of the MOVEit server, e.g. https://moveit.example.com (env: MOVEIT_URL)",
    )
    group.add_argument(
        "--auth",
        choices=AUTH_METHODS,
        default=environ.get("MOVEIT_AUTH") or "sso",
        help="authentication method (env: MOVEIT_AUTH, default: sso)",
    )
    group.add_argument(
        "--username",
        default=environ.get("MOVEIT_USERNAME"),
        help="username for password authentication (env: MOVEIT_USERNAME); "
        "the password is read from MOVEIT_PASSWORD or prompted",
    )
    group.add_argument("--idp", help="name of the SSO identity provider, if the server has several")
    group.add_argument(
        "--token-file",
        type=Path,
        default=token_file,
        help=f"file where the API token is cached (default: {str(token_file).replace('%', '%%')})",
    )
    group.add_argument("--no-token-cache", action="store_true", help="don't read or write the token cache")


def _add_transfer_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("transfer options")
    group.add_argument(
        "--threads",
        type=thread_count,
        default=DEFAULT_THREADS,
        help=f"number of parallel transfers (default: {DEFAULT_THREADS})",
    )
    group.add_argument("-q", "--quiet", action="store_true", help="don't show the progress")


def _add_send_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("subject", help="subject of the package")
    parser.add_argument("paths", nargs="+", metavar="PATH", help="file or directory to send")
    group = parser.add_argument_group("package options")
    for option, label, required in (("to", "", True), ("cc", "CC ", False), ("bcc", "BCC ", False)):
        group.add_argument(
            f"--{option}",
            type=email_list,
            action="extend",
            default=[],
            required=required,
            metavar="EMAILS",
            help=f"comma-separated emails of the {label}recipients, the option can be repeated",
        )
    group.add_argument("--note", metavar="TEXT", help="message sent with the files (default: the list of files)")
    group.add_argument(
        "--secure-note",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="only show the note in MOVEit, not in the notification email (default: MOVEit setting)",
    )
    group.add_argument(
        "--expires-days", type=int, metavar="N", help="days before the package expires (default: MOVEit setting)"
    )
    group.add_argument(
        "--max-downloads",
        type=int,
        metavar="N",
        help="how many times each file can be downloaded (default: MOVEit setting)",
    )
    group.add_argument("--classification", metavar="NAME", help="classification of the package, e.g. Unclassified")


def build_parser(
    environ: Mapping[str, str], token_file: Path | None = None, out: IO[str] | None = None, err: IO[str] | None = None
) -> argparse.ArgumentParser:
    """Build the argument parser; defaults come from `environ`."""
    token_file = token_file or default_token_file(environ)
    new_parser = partial(_Parser, out=out or sys.stdout, err=err or sys.stderr)
    parser = new_parser(prog="moveit", description=DESCRIPTION)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND", parser_class=new_parser)

    def add(name: str, help_text: str, transfer: bool = False) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        _add_common_options(sub, environ, token_file)
        if transfer:
            _add_transfer_options(sub)
        return sub

    add("login", "sign in, and cache the API token")
    add("logout", "forget the cached API token")
    upload = add("upload", "upload a local directory, skipping files already on the server", transfer=True)
    upload.add_argument("input_dir", metavar="INPUT_DIR", help="local directory to upload")
    upload.add_argument(
        "remote_dir",
        metavar="REMOTE_DIR",
        help="remote folder to upload to, e.g. /Home/abcdef123456/folder (see login)",
    )
    download = add("download", "download a remote folder, skipping files already downloaded", transfer=True)
    download.add_argument(
        "remote_dir", metavar="REMOTE_DIR", help="remote folder to download, e.g. /Data/project/results"
    )
    download.add_argument("output_dir", metavar="OUTPUT_DIR", help="local directory to download to")
    download.add_argument(
        "--filter", type=regex, metavar="REGEX", help="only download files whose remote path matches this regex"
    )
    send = add("send", "send files to other users, identified by their email; directories are zipped", transfer=True)
    _add_send_options(send)
    packages = add("packages", "list your packages, newest first")
    packages.add_argument("--sent", action="store_true", help="list the sent packages instead of the received ones")
    packages.add_argument("--new", action="store_true", help="only list the packages not opened yet")
    receive = add("receive", "download the files of a package, skipping those already downloaded", transfer=True)
    receive.add_argument(
        "package_id", metavar="PACKAGE_ID", help="ID of the package, as listed by the packages command"
    )
    receive.add_argument("output_dir", metavar="OUTPUT_DIR", help="local directory to download to")
    return parser


def parse_args(argv: Sequence[str], deps: CliDeps) -> argparse.Namespace:
    """Parse the command line. Invalid arguments exit with code 2."""
    parser = build_parser(deps.environ, out=deps.stdout, err=deps.stderr)
    args = parser.parse_args(argv)
    if args.auth not in AUTH_METHODS:
        choices = ", ".join(repr(method) for method in AUTH_METHODS)
        parser.error(f"invalid auth method: {args.auth!r} (choose from {choices})")
    return args


def make_settings(args: argparse.Namespace) -> Settings:
    """Check the URL and credentials options, and gather the settings."""
    if not args.url:
        raise TransferToolException(NO_SERVER_MESSAGE)
    settings = Settings(
        url=args.url.rstrip("/"),
        auth=args.auth,
        username=args.username or None,
        idp=args.idp,
        token_file=args.token_file,
        no_token_cache=args.no_token_cache,
        command=args.command,
    )
    if settings.command != "logout" and settings.auth == "password" and not settings.username:
        raise TransferToolException(USERNAME_REQUIRED_MESSAGE)
    return settings


def run_logout(settings: Settings, deps: CliDeps) -> int:
    """Forget the cached token without accessing the network."""
    existed = not settings.no_token_cache and deps.forget_token(settings.token_file, settings.url)
    if existed:
        print(f"Forgot the cached token for {settings.url}.", file=deps.stdout)
    else:
        print(f"No cached token for {settings.url}.", file=deps.stdout)
    return 0


def run_login(client: ApiClient, deps: CliDeps) -> int:
    """Print the signed-in user and home folder."""
    user = client.rest_call_json("/users/self")
    folder = client.rest_call_json(f"/folders/{user['homeFolderID']}")
    full_name, email = user.get("fullName"), user.get("email")
    if full_name and email:
        who = f" as {full_name} ({email})"
    elif full_name or email:
        who = f" as {full_name or email}"
    else:
        who = ""
    print(f"Signed in{who}.", file=deps.stdout)
    print(f"Your home folder is {folder['path']}", file=deps.stdout)
    return 0


def run_command(args: argparse.Namespace, provider: TokenProviderLike, client: ApiClient, deps: CliDeps) -> int:
    """Run a command with background token refresh and interactive sign-in disabled."""
    provider.disable_login()
    provider.start_keep_alive()
    try:
        return deps.commands[args.command](args, client, deps)
    finally:
        provider.stop_keep_alive()


def execute(args: argparse.Namespace, deps: CliDeps) -> int:
    """Run the command for the parsed arguments."""
    settings = make_settings(args)
    if settings.command == "logout":
        return run_logout(settings, deps)
    provider, client = deps.connect(settings)
    if settings.command == "login":
        return run_login(client, deps)
    return run_command(args, provider, client, deps)


def expired_message(command: str) -> str:
    """Message of a sign-in expiry for `command`."""
    if command == "send":
        return EXPIRED_SEND
    if command in ("upload", "download", "receive"):
        return EXPIRED_TRANSFER
    return EXPIRED_OTHER


def report_error(error: BaseException, command: str, deps: CliDeps) -> int:
    """Print an error to stderr and return the exit code."""
    if isinstance(error, KeyboardInterrupt):
        print("\nInterrupted.", file=deps.stderr)
        return EXIT_INTERRUPTED
    if isinstance(error, SignInExpiredError):
        print(expired_message(command), file=deps.stderr)
        return EXIT_SIGN_IN_EXPIRED
    if isinstance(error, AuthenticationError | ResponseException | TransferToolException):
        print(str(error), file=deps.stderr)
        return 1
    message = " ".join(str(error).split())
    print(f"Error: {type(error).__name__}: {message}", file=deps.stderr)
    return 1


def main(argv: list[str] | None = None, deps: CliDeps | None = None) -> int:
    """Run the moveit command line and return the exit code."""
    deps = deps or CliDeps()
    command = ""
    try:
        load_env_file(deps.environ)
        args = parse_args(sys.argv[1:] if argv is None else argv, deps)
        command = args.command
        return execute(args, deps)
    except SystemExit as exit_request:
        return exit_request.code or 0 if exit_request.code is None or isinstance(exit_request.code, int) else 1
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - report all errors at the top level
        return report_error(error, command, deps)
