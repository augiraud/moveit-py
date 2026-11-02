import argparse
import io
from pathlib import Path

import pytest
from fakes import FIRST_CONTENT, SECOND_CONTENT, FakePackageServer

from moveit_py.api import ApiClient
from moveit_py.cli import CliDeps, Settings, TokenProviderLike, main
from moveit_py.errors import SignInExpiredError, TransferToolException
from moveit_py.packages.receive import receive_command


class Result:
    def __init__(self, code: int, out: str) -> None:
        self.code, self.out = code, out


def receive(client: FakePackageServer, package_id: str, output_dir: Path, **options: object) -> Result:
    out = io.StringIO()
    values: dict[str, object] = {"threads": 2, "quiet": False, **options}
    args = argparse.Namespace(package_id=package_id, output_dir=str(output_dir), **values)
    code = receive_command(args, client, CliDeps(stdout=out, stderr=io.StringIO()))
    return Result(code, out.getvalue())


def downloads(client: FakePackageServer) -> list[str]:
    return [r.endpoint for r in client.requests if r.endpoint.endswith("/download")]


def set_attachments(client: FakePackageServer, *names: str) -> None:
    client.package_details[(1, 100)]["attachments"] = [
        {"id": "att-1", "name": name, "size": len(FIRST_CONTENT)} for name in names
    ]


def test_at61_receive_then_again(tmp_path: Path) -> None:
    client = FakePackageServer()
    result = receive(client, "100", tmp_path / "out")
    assert result.code == 0
    assert (tmp_path / "out" / "a b.txt").read_bytes() == FIRST_CONTENT
    assert (tmp_path / "out" / "results.zip").read_bytes() == SECOND_CONTENT
    assert "Package 100: Results, 2 files." in result.out
    assert "Downloaded 2 files, skipped 0 files, 0 errors." in result.out
    client.requests.clear()
    again = receive(client, "100", tmp_path / "out")
    assert again.code == 0
    assert downloads(client) == []
    assert "Downloaded 0 files, skipped 2 files, 0 errors." in again.out


def test_at62_package_not_found(tmp_path: Path) -> None:
    with pytest.raises(TransferToolException, match="not found"):
        receive(FakePackageServer(), "999", tmp_path / "out")


@pytest.mark.parametrize("name", ["../evil.txt", "..", "sub\\evil.txt"])
def test_at62_unsafe_attachment_names(tmp_path: Path, name: str) -> None:
    client = FakePackageServer()
    set_attachments(client, "fine.txt", name)
    with pytest.raises(TransferToolException, match="refusing") as error:
        receive(client, "100", tmp_path / "out")
    assert repr(name) in str(error.value)
    assert downloads(client) == []
    assert not (tmp_path / "out").exists()


def test_ki6_duplicate_attachment_names(tmp_path: Path) -> None:
    client = FakePackageServer()
    set_attachments(client, "b.txt", "a.txt", "b.txt", "a.txt", "c.txt")
    with pytest.raises(TransferToolException) as error:
        receive(client, "100", tmp_path / "out")
    assert str(error.value) == "Error: several attachments are named a.txt, b.txt, nothing was downloaded."
    assert downloads(client) == []


def test_falls_back_to_sent_mailbox(tmp_path: Path) -> None:
    client = FakePackageServer()
    client.package_details[(3, 101)]["attachments"] = [{"id": "att-2", "name": "mine.bin", "size": 11}]
    result = receive(client, "101", tmp_path / "out")
    assert result.code == 0
    assert (tmp_path / "out" / "mine.bin").read_bytes() == SECOND_CONTENT
    assert [r.params for r in client.requests if r.endpoint == "/packages/101"] == [{"mailboxId": 1}, {"mailboxId": 3}]


def test_non_404_failure_of_inbox_also_falls_back(tmp_path: Path) -> None:
    client = FakePackageServer()
    client.failures["/packages/101"] = [ConnectionError("boom")]
    result = receive(client, "101", tmp_path / "out")
    assert result.code == 0


def test_sign_in_expired_propagates(tmp_path: Path) -> None:
    client = FakePackageServer()
    client.failures["/packages/100"] = SignInExpiredError("expired")
    with pytest.raises(SignInExpiredError):
        receive(client, "100", tmp_path / "out")


def test_quiet_prints_only_the_summary(tmp_path: Path) -> None:
    result = receive(FakePackageServer(), "100", tmp_path / "out", quiet=True)
    assert result.out == "Downloaded 2 files, skipped 0 files, 0 errors.\n"


def test_new_files_are_downloaded_first(tmp_path: Path) -> None:
    client = FakePackageServer()
    out = tmp_path / "out"
    receive(client, "100", out)
    (out / "a b.txt").unlink()
    client.requests.clear()
    result = receive(client, "100", out, threads=1)
    assert result.code == 0
    assert downloads(client) == ["/files/att-1/download"]
    assert "skipped 1 files" in result.out


def test_download_error_exits_1(tmp_path: Path) -> None:
    client = FakePackageServer()
    del client.contents["att-2"]
    result = receive(client, "100", tmp_path / "out")
    assert result.code == 1
    assert "1 errors" in result.out


class Provider:
    def get_access_token(self) -> str:
        return "tok"

    def disable_login(self) -> None:
        pass

    def start_keep_alive(self) -> None:
        pass

    def stop_keep_alive(self) -> None:
        pass


def run_main(argv: list[str], client: FakePackageServer) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()

    def connect(_settings: Settings) -> tuple[TokenProviderLike, ApiClient]:
        return Provider(), client

    deps = CliDeps(
        environ={"MOVEIT_ENV_FILE": "/nonexistent/none.env", "HOME": "/nonexistent"},
        stdout=out,
        stderr=err,
        connect=connect,
    )
    code = main([*argv, "--url", "https://moveit.example.com"], deps)
    return code, out.getvalue(), err.getvalue()


def test_receive_through_the_command_line(tmp_path: Path) -> None:
    code, out, _ = run_main(["receive", "100", str(tmp_path / "out"), "--threads", "2"], FakePackageServer())
    assert code == 0
    assert "Downloaded 2 files" in out


def test_not_found_through_the_command_line(tmp_path: Path) -> None:
    code, _, err = run_main(["receive", "999", str(tmp_path / "out")], FakePackageServer())
    assert (code, err.strip()) == (1, "Error: package 999 not found in your Inbox or Sent mailboxes.")


def test_sign_in_expired_exits_3(tmp_path: Path) -> None:
    client = FakePackageServer()
    client.failures["/packages/100"] = SignInExpiredError("expired")
    code, _, err = run_main(["receive", "100", str(tmp_path / "out")], client)
    assert code == 3
    assert "./scripts/login.sh" in err
