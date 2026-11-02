import argparse
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fakes import FakeApiClient

from moveit_py.api import ApiClient
from moveit_py.auth.store import TokenStore
from moveit_py.auth.token import Token
from moveit_py.cli import CliDeps, Settings, TokenProviderLike, build_parser, main
from moveit_py.errors import SignInExpiredError, TransferToolException

Command = Callable[[argparse.Namespace, ApiClient, CliDeps], int]


class FakeProvider:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def get_access_token(self) -> str:
        self.events.append("token")
        return "tok"

    def disable_login(self) -> None:
        self.events.append("disable_login")

    def start_keep_alive(self) -> None:
        self.events.append("start")

    def stop_keep_alive(self) -> None:
        self.events.append("stop")


@dataclass
class Run:
    code: int
    out: str
    err: str
    events: list[str]
    settings: list[Settings]
    commands: list[argparse.Namespace] = field(default_factory=list)
    client: FakeApiClient = field(default_factory=FakeApiClient)


def run(
    argv: list[str],
    tmp_path: Path,
    environ: dict[str, str] | None = None,
    command: Command | None = None,
    forget: Callable[[Path, str], bool] | None = None,
    client: FakeApiClient | None = None,
) -> Run:
    out, err = io.StringIO(), io.StringIO()
    events: list[str] = []
    settings: list[Settings] = []
    api = client or FakeApiClient()
    result = Run(0, "", "", events, settings, client=api)

    def connect(s: Settings) -> tuple[TokenProviderLike, ApiClient]:
        settings.append(s)
        events.append("connect")
        return FakeProvider(events), api

    def default_command(args: argparse.Namespace, _client: ApiClient, _deps: CliDeps) -> int:
        result.commands.append(args)
        events.append("command")
        return 0

    chosen = command or default_command
    deps = CliDeps(
        environ={"MOVEIT_ENV_FILE": str(tmp_path / "none.env"), "HOME": str(tmp_path), **(environ or {})},
        stdout=out,
        stderr=err,
        forget_token=forget or (lambda _path, _url: False),
        connect=connect,
        commands=dict.fromkeys(("upload", "download", "send", "packages", "receive"), chosen),
    )
    result.code = main(argv, deps)
    result.out, result.err = out.getvalue(), err.getvalue()
    return result


URL = "https://moveit.example.com"


def parse(argv: list[str], environ: dict[str, str] | None = None) -> argparse.Namespace:
    return build_parser(environ or {}, Path("tokens.json")).parse_args(argv)


def test_at1_defaults_without_environment() -> None:
    args = parse(["upload", "input", "/Home/user"])
    assert args.url is None
    assert args.auth == "sso"
    assert args.threads == 4
    assert args.quiet is False
    assert (args.input_dir, args.remote_dir) == ("input", "/Home/user")


def test_at2_environment_defaults_and_options() -> None:
    environ = {"MOVEIT_URL": URL, "MOVEIT_AUTH": "password", "MOVEIT_USERNAME": "bot"}
    args = parse(["download", "/Data", "out", "--filter", r"\.txt$", "--threads", "2"], environ)
    assert (args.url, args.auth, args.username) == (URL, "password", "bot")
    assert args.filter is not None
    assert args.filter.search("/Data/a.txt")
    assert args.threads == 2


def test_at3_env_file_and_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / "moveit.env"
    env_file.write_text(
        '# comment\n\nMOVEIT_URL="https://moveit.example.com"\nexport MOVEIT_AUTH=password\nMOVEIT_USERNAME=x\n',
        encoding="utf-8",
    )
    environ = {"MOVEIT_ENV_FILE": str(env_file), "MOVEIT_USERNAME": "from-environment"}
    result = run(["login"], tmp_path, environ)
    assert result.settings[0].url == URL
    assert result.settings[0].auth == "password"
    assert result.settings[0].username == "from-environment"
    overridden = run(["login", "--url", "https://other.example.com"], tmp_path, environ)
    assert overridden.settings[0].url == "https://other.example.com"


def test_at3_missing_env_file_is_ignored(tmp_path: Path) -> None:
    result = run(["upload", "in", "/H", "--url", URL], tmp_path)
    assert result.code == 0


def test_at4_no_url(tmp_path: Path) -> None:
    result = run(["upload", "in", "/Home"], tmp_path)
    assert result.code == 1
    assert "moveit.env" in result.err
    assert "connect" not in result.events


def test_at5_logout_forgets_token(tmp_path: Path) -> None:
    calls: list[tuple[Path, str]] = []

    def forget(path: Path, url: str) -> bool:
        calls.append((path, url))
        return True

    result = run(["logout", "--url", f"{URL}/", "--token-file", str(tmp_path / "t.json")], tmp_path, forget=forget)
    assert result.code == 0
    assert "Forgot" in result.out
    assert result.events == []
    assert calls == [(tmp_path / "t.json", URL)]


def test_at5_logout_without_token_or_with_no_token_cache(tmp_path: Path) -> None:
    assert "No cached token for" in run(["logout", "--url", URL], tmp_path).out
    calls: list[str] = []

    def forget(_path: Path, url: str) -> bool:
        calls.append(url)
        return True

    result = run(["logout", "--url", URL, "--no-token-cache"], tmp_path, forget=forget)
    assert f"No cached token for {URL}." in result.out
    assert calls == []


def test_at6_password_without_username(tmp_path: Path) -> None:
    result = run(["login", "--url", URL, "--auth", "password"], tmp_path)
    assert result.code == 1
    assert "--username" in result.err
    assert "connect" not in result.events


def test_at7_upload_sign_in_expired(tmp_path: Path) -> None:
    def expired(_args: argparse.Namespace, _client: ApiClient, _deps: CliDeps) -> int:
        raise SignInExpiredError("expired")

    result = run(["upload", "in", "/Home", "--url", URL], tmp_path, command=expired)
    assert result.code == 3
    assert "./scripts/login.sh" in result.err
    assert "during the transfer" in result.err
    assert result.events[-1] == "stop"


def test_sign_in_expired_messages_per_command(tmp_path: Path) -> None:
    def expired(_args: argparse.Namespace, _client: ApiClient, _deps: CliDeps) -> int:
        raise SignInExpiredError("expired")

    sent = run(["send", "s", "f", "--to", "a@b.c", "--url", URL], tmp_path, command=expired)
    assert (sent.code, "not sent" in sent.err) == (3, True)
    listed = run(["packages", "--url", URL], tmp_path, command=expired)
    assert (listed.code, "during the transfer" in listed.err, "./scripts/login.sh" in listed.err) == (3, False, True)


def test_at8_send_arguments(tmp_path: Path) -> None:
    result = run(
        [
            "send",
            "--to",
            "a@example.com,b@example.com",
            "--to",
            "c@example.com",
            "--url",
            URL,
            "--",
            "-subject",
            "f1",
            "f2",
        ],
        tmp_path,
    )
    assert result.code == 0
    args = result.commands[0]
    assert args.to == ["a@example.com", "b@example.com", "c@example.com"]
    assert args.subject == "-subject"
    assert args.paths == ["f1", "f2"]
    assert args.cc == []
    assert args.secure_note is None
    assert args.expires_days is None
    assert args.max_downloads is None


def test_at8_send_usage_errors(tmp_path: Path) -> None:
    bad = run(["send", "--to", "alice", "s", "f", "--url", URL], tmp_path)
    assert bad.code == 2
    assert "not an email: alice" in bad.err
    missing = run(["send", "s", "f", "--url", URL], tmp_path)
    assert missing.code == 2
    assert "--to" in missing.err


def test_email_list_drops_empty_items() -> None:
    args = parse(["send", "s", "f", "--to", " a@b.c, ,d@e.f,"])
    assert args.to == ["a@b.c", "d@e.f"]


def test_ki2_invalid_filter_is_usage_error(tmp_path: Path) -> None:
    result = run(["download", "/d", "out", "--filter", "(", "--url", URL], tmp_path)
    assert result.code == 2
    assert "invalid regex" in result.err


def test_ki4_invalid_moveit_auth_from_environment(tmp_path: Path) -> None:
    result = run(["login", "--url", URL], tmp_path, {"MOVEIT_AUTH": "kerberos"})
    assert result.code == 2
    assert "kerberos" in result.err
    assert run(["login", "--url", URL, "--auth", "kerberos"], tmp_path).code == 2
    assert run(["upload", "a", "/H", "--url", URL, "--auth", "sso"], tmp_path, {"MOVEIT_AUTH": "kerberos"}).code == 0


def test_ki3_unexpected_error_is_one_line(tmp_path: Path) -> None:
    def boom(_args: argparse.Namespace, _client: ApiClient, _deps: CliDeps) -> int:
        raise ConnectionError("connection\nrefused")

    result = run(["packages", "--url", URL], tmp_path, command=boom)
    assert result.code == 1
    assert result.err == "Error: ConnectionError: connection refused\n"
    assert "Traceback" not in result.err


def test_known_errors_print_their_message(tmp_path: Path) -> None:
    def fail(_args: argparse.Namespace, _client: ApiClient, _deps: CliDeps) -> int:
        raise TransferToolException("Mailbox X not found")

    result = run(["packages", "--url", URL], tmp_path, command=fail)
    assert (result.code, result.err) == (1, "Mailbox X not found\n")


def test_ctrl_c_exits_130(tmp_path: Path) -> None:
    def interrupt(_args: argparse.Namespace, _client: ApiClient, _deps: CliDeps) -> int:
        raise KeyboardInterrupt

    result = run(["upload", "in", "/H", "--url", URL], tmp_path, command=interrupt)
    assert result.code == 130
    assert result.err == "\nInterrupted.\n"
    assert result.events[-1] == "stop"


def test_command_exit_code_and_lifecycle_order(tmp_path: Path) -> None:
    result = run(["upload", "in", "/H", "--url", URL], tmp_path, command=lambda *_: 1)
    assert result.code == 1
    assert result.events == ["connect", "disable_login", "start", "stop"]


def test_threads_below_one_become_one() -> None:
    assert parse(["upload", "a", "b", "--threads", "0"]).threads == 1
    assert parse(["upload", "a", "b", "--threads", "-3"]).threads == 1


def test_secure_note_tristate() -> None:
    assert parse(["send", "s", "f", "--to", "a@b.c", "--no-secure-note"]).secure_note is False
    assert parse(["send", "s", "f", "--to", "a@b.c", "--secure-note"]).secure_note is True


def test_integer_options() -> None:
    args = parse(["send", "s", "f", "--to", "a@b.c", "--expires-days", "7", "--max-downloads", "2"])
    assert (args.expires_days, args.max_downloads) == (7, 2)


def test_packages_has_only_common_options(tmp_path: Path) -> None:
    assert run(["packages", "--threads", "2", "--url", URL], tmp_path).code == 2
    args = parse(["packages", "--sent", "--new"])
    assert (args.sent, args.new) == (True, True)


def test_login_output(tmp_path: Path) -> None:
    client = FakeApiClient()
    client.add_json("GET", "/users/self", {"fullName": "Ada L", "email": "ada@example.com", "homeFolderID": 7})
    client.add_json("GET", "/folders/7", {"path": "/Home/abc"})
    result = run(["login", "--url", URL], tmp_path, client=client)
    assert result.code == 0
    assert result.out == "Signed in as Ada L (ada@example.com).\nYour home folder is /Home/abc\n"
    assert result.events == ["connect"]


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"fullName": "Ada L"}, "Signed in as Ada L."),
        ({"email": "ada@example.com"}, "Signed in as ada@example.com."),
        ({}, "Signed in."),
    ],
)
def test_ki18_login_omits_missing_parts(tmp_path: Path, user: dict[str, str], expected: str) -> None:
    client = FakeApiClient()
    client.add_json("GET", "/users/self", {**user, "homeFolderID": 7})
    client.add_json("GET", "/folders/7", {"path": "/Home/abc"})
    result = run(["login", "--url", URL], tmp_path, client=client)
    assert result.out.splitlines()[0] == expected


def test_token_file_help_shows_default_path(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser({"XDG_CONFIG_HOME": "/cfg"})
    with pytest.raises(SystemExit):
        parser.parse_args(["login", "--help"])
    assert "/cfg/moveit-py/tokens.json" in re.sub(r"\s+", "", capsys.readouterr().out)


def test_every_command_is_wired_by_default() -> None:
    assert set(CliDeps().commands) == {"upload", "download", "send", "packages", "receive"}


def test_at5_logout_with_real_token_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "tokens.json"
    store = TokenStore(path)
    store.save(URL, Token("a", "r", 1.0))
    store.save("https://other.example.com", Token("b", "s", 2.0))
    out = io.StringIO()
    deps = CliDeps(environ={"MOVEIT_ENV_FILE": str(tmp_path / "none.env")}, stdout=out, stderr=io.StringIO())
    assert main(["logout", "--url", f"{URL}/", "--token-file", str(path)], deps) == 0
    assert "Forgot" in out.getvalue()
    assert store.load(URL) is None
    assert store.load("https://other.example.com") == Token("b", "s", 2.0)
