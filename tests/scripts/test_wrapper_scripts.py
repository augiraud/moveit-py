"""Test the wrapper scripts with a stub uv that records each command."""

from pathlib import Path

import pytest
from script_runner import PROJECT_ARGS, SCRIPTS, Runner

ALL = [
    "login.sh",
    "logout.sh",
    "folder-upload.sh",
    "folder-download.sh",
    "package-send.sh",
    "package-list.sh",
    "package-receive.sh",
    "package-send-large.sh",
]
# A valid command line for each script (relative to the runner's work directory)
VALID: dict[str, list[str]] = {
    "login.sh": [],
    "logout.sh": [],
    "folder-upload.sh": ["up", "/Home/x/up"],
    "folder-download.sh": ["/Data/x", "down"],
    "package-send.sh": ["Subj", "a@b.c", "file.txt"],
    "package-list.sh": [],
    "package-receive.sh": ["123", "recv"],
    "package-send-large.sh": ["Subj", "a@b.c", "file.txt"],
}
WRONG_COUNT: dict[str, list[str]] = {
    "login.sh": ["x"],
    "logout.sh": ["x"],
    "folder-upload.sh": ["a"],
    "folder-download.sh": ["a"],
    "package-send.sh": ["a", "b"],
    "package-list.sh": ["x"],
    "package-receive.sh": ["1"],
    "package-send-large.sh": ["a", "b"],
}


@pytest.fixture
def ready(runner: Runner) -> Runner:
    (runner.work / "up").mkdir()
    (runner.work / "file.txt").write_text("x")
    (runner.work / "other.txt").write_text("x")
    return runner


@pytest.mark.parametrize("script", ALL)
def test_help(ready: Runner, script: str) -> None:
    for flag in ("-h", "--help"):
        r = ready.run(script, flag, "ignored")
        assert r.code == 0
        assert "Usage:" in r.out
        assert script in r.out
        assert "moveit" in r.out.lower()
        assert r.err == ""
        assert r.calls == []


@pytest.mark.parametrize("script", ALL)
def test_wrong_count(ready: Runner, script: str) -> None:
    r = ready.run(script, *WRONG_COUNT[script])
    assert r.code == 2
    assert "Usage:" in r.err
    assert r.out == ""
    assert r.calls == []


@pytest.mark.parametrize("script", ALL)
def test_help_not_first_is_not_help(ready: Runner, script: str) -> None:
    r = ready.run(script, "x", "--help", "y", "z", "w", "v")
    assert r.code == 2
    assert r.out == ""


@pytest.mark.parametrize("script", ALL)
def test_url_option(ready: Runner, script: str) -> None:
    ready.run("login.sh", url=None)  # no-op: start from a clean log below
    (ready.stub_dir / "uv.log").unlink(missing_ok=True)
    if script == "package-send-large.sh":
        pytest.skip("covered in test_package_send_large")
    r = ready.run(script, "--url", "https://other.test", *VALID[script], url=None)
    assert r.code == 0, r.err
    assert r.calls[0][0] == "https://other.test"


@pytest.mark.parametrize("script", ALL)
def test_url_overrides_environment(ready: Runner, script: str) -> None:
    if script == "package-send-large.sh":
        pytest.skip("covered in test_package_send_large")
    r = ready.run(script, "--url", "https://other.test", *VALID[script])
    assert r.calls[0][0] == "https://other.test"


@pytest.mark.parametrize("script", ALL)
@pytest.mark.parametrize("extra", [[], [""]])
def test_url_needs_value(ready: Runner, script: str, extra: list[str]) -> None:
    r = ready.run(script, "--url", *extra)
    assert r.code == 2
    assert r.err == "Error: --url needs a URL\n"
    assert r.calls == []


@pytest.mark.parametrize("script", [s for s in ALL if s != "package-send-large.sh"])
def test_url_only_first(ready: Runner, script: str) -> None:
    r = ready.run(script, "x", "--url", "https://other.test")
    assert r.code == 2
    assert r.calls == []


def test_uv_missing(ready: Runner) -> None:
    r = ready.run("login.sh", path=str(ready.minimal))
    assert r.code == 1
    assert r.err == "Error: uv is not installed, see README.md\n"


def test_no_server(ready: Runner) -> None:
    r = ready.run("login.sh", url=None)
    assert r.code == 2
    assert r.err == (
        "Error: no MOVEit server: create moveit.env from moveit.env.example, or use --url https://moveit.example.com\n"
    )
    assert r.calls == []


def test_empty_url_counts_as_none(ready: Runner) -> None:
    assert ready.run("login.sh", url="").code == 2


@pytest.mark.parametrize(
    "line", ["MOVEIT_URL=https://x", "export MOVEIT_URL=https://x", '  export   MOVEIT_URL="https://x"']
)
def test_env_file_satisfies_check(ready: Runner, line: str) -> None:
    ready.env_file.write_text(f"# comment\n{line}\n")
    r = ready.run("login.sh", url=None)
    assert r.code == 0, r.err
    assert r.calls[0][0] == "<unset>"  # the tool reads the file itself


@pytest.mark.parametrize("line", ["MOVEIT_URL=", "# MOVEIT_URL=x", "OTHER_MOVEIT_URL=x"])
def test_env_file_without_url(ready: Runner, line: str) -> None:
    ready.env_file.write_text(line + "\n")
    assert ready.run("login.sh", url=None).code == 2


@pytest.mark.parametrize("use_default", [False, True])
def test_env_file_exported_to_tool(ready: Runner, use_default: bool) -> None:
    uv = ready.bin / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s" "$MOVEIT_ENV_FILE" > "$STUB_DIR/env-file"\n')
    env_file = "" if use_default else str(ready.env_file)
    r = ready.run("login.sh", env={"MOVEIT_ENV_FILE": env_file})
    assert r.code == 0, r.err
    expected = SCRIPTS.parent / "moveit.env" if use_default else ready.env_file
    assert (ready.stub_dir / "env-file").read_text() == str(expected)


def test_forwarded_commands(ready: Runner) -> None:
    cases = [
        ("login.sh", [], ["login"]),
        ("logout.sh", [], ["logout"]),
        ("folder-upload.sh", ["up", "/Home/x/up"], ["upload", "up", "/Home/x/up"]),
        ("folder-download.sh", ["/Data/x", "down"], ["download", "/Data/x", "down"]),
        (
            "folder-download.sh",
            ["/Data/x", "down", "\\.zip$"],
            ["download", "/Data/x", "down", "--filter", "\\.zip$"],
        ),
        ("folder-download.sh", ["/Data/x", "down", ""], ["download", "/Data/x", "down"]),
        (
            "package-send.sh",
            ["-weird subject", "a@b.c,d@e.f", "file.txt", "up"],
            ["send", "--to", "a@b.c,d@e.f", "--", "-weird subject", "file.txt", "up"],
        ),
        ("package-list.sh", [], ["packages"]),
        ("package-list.sh", ["--sent"], ["packages", "--sent"]),
        ("package-receive.sh", ["379341516", "recv dir"], ["receive", "379341516", "recv dir"]),
    ]
    for script, args, expected in cases:
        (ready.stub_dir / "uv.log").unlink(missing_ok=True)
        r = ready.run(script, *args)
        assert r.code == 0, (script, r.err)
        assert r.calls == [["https://moveit.test", *PROJECT_ARGS, *expected]], script


@pytest.mark.parametrize("code", [1, 2, 3, 130])
def test_exit_code_propagated(ready: Runner, code: int) -> None:
    for script in ALL[:-1]:
        r = ready.run(script, *VALID[script], env={"STUB_UV_CODES": str(code)})
        assert r.code == code, script


def test_package_list_rejects_other_args(ready: Runner) -> None:
    for args in (["--received"], ["--sent", "x"]):
        r = ready.run("package-list.sh", *args)
        assert r.code == 2
        assert "Usage:" in r.err


@pytest.mark.parametrize(
    ("script", "args", "message"),
    [
        ("folder-upload.sh", ["nodir", "/Home/x"], "LOCAL_DIR 'nodir' is not a directory"),
        ("folder-upload.sh", ["file.txt", "/Home/x"], "LOCAL_DIR 'file.txt' is not a directory"),
        (
            "folder-upload.sh",
            ["up", "Home/x"],
            "MOVEIT_FOLDER must start with /, e.g. /Home/abcdef123456/results",
        ),
        (
            "folder-download.sh",
            ["Data/x", "down"],
            "MOVEIT_FOLDER must start with /, e.g. /Data/project/results",
        ),
        (
            "package-send.sh",
            ["S", "nobody", "file.txt"],
            "EMAILS must be comma-separated emails, e.g. alice@example.com,bob@example.com",
        ),
        ("package-send.sh", ["S", "a@b.c", "file.txt", "missing"], "'missing' does not exist"),
        (
            "package-receive.sh",
            ["12a", "recv"],
            "PACKAGE_ID must be a number, e.g. 379341516 (see package-list.sh)",
        ),
        (
            "package-receive.sh",
            ["", "recv"],
            "PACKAGE_ID must be a number, e.g. 379341516 (see package-list.sh)",
        ),
    ],
)
def test_validations(ready: Runner, script: str, args: list[str], message: str) -> None:
    r = ready.run(script, *args)
    assert r.code == 2
    assert r.err == f"Error: {message}\n"
    assert r.calls == []


def test_validation_before_setup_check(ready: Runner) -> None:
    r = ready.run("package-send.sh", "S", "nobody", "file.txt", path=str(ready.minimal))
    assert r.code == 2


def test_usage_texts_key_lines(ready: Runner) -> None:
    out = ready.run("folder-download.sh", "-h").out
    assert "          ./scripts/folder-download.sh /Data/project/results ./results '\\.zip$'" in out
    assert out.endswith("matched against the MOVEit file paths.\n")
    assert Path(ready.work).exists()
