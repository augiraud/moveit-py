from pathlib import Path

import pytest

from moveit_py.config import default_token_file, load_env_file, parse_env_line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", None),
        ("   ", None),
        ("# comment", None),
        ("no equals", None),
        ("A=b", ("A", "b")),
        ("  export A = b  ", ("A", "b")),
        ('A="quoted"', ("A", "quoted")),
        ("A='x'", ("A", "x")),
        ("A=b=c", ("A", "b=c")),
        ("A=", ("A", "")),
        ("=x", None),
    ],
)
def test_parse_env_line(line: str, expected: tuple[str, str] | None) -> None:
    assert parse_env_line(line) == expected


def test_env_file_does_not_override_set_variables(tmp_path: Path) -> None:
    env_file = tmp_path / "e.env"
    env_file.write_text("A=file\nB=file\nC=file\n", encoding="utf-8")
    environ = {"A": "env", "B": "", "MOVEIT_ENV_FILE": str(env_file)}
    load_env_file(environ)
    assert environ["A"] == "env"
    assert environ["B"] == ""
    assert environ["C"] == "file"


def test_env_file_default_is_moveit_env_in_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "moveit.env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    environ: dict[str, str] = {}
    load_env_file(environ)
    assert environ == {"X": "1"}


def test_missing_env_file_is_ignored(tmp_path: Path) -> None:
    environ = {"MOVEIT_ENV_FILE": str(tmp_path / "nope.env")}
    load_env_file(environ)
    assert environ == {"MOVEIT_ENV_FILE": str(tmp_path / "nope.env")}


def test_token_file_xdg(tmp_path: Path) -> None:
    assert default_token_file({"XDG_CONFIG_HOME": "/x"}, tmp_path) == Path("/x/moveit-py/tokens.json")
    assert default_token_file({}, tmp_path) == tmp_path / ".config" / "moveit-py" / "tokens.json"


def test_empty_xdg_counts_as_unset(tmp_path: Path) -> None:
    assert default_token_file({"XDG_CONFIG_HOME": ""}, tmp_path) == (tmp_path / ".config" / "moveit-py" / "tokens.json")
