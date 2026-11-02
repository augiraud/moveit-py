import argparse
import io
from typing import Any

import pytest
from fakes import SENT_ID, FakePackageServer, FakeResponse

from moveit_py.api import ApiClient
from moveit_py.cli import CliDeps, Settings, TokenProviderLike, main
from moveit_py.errors import SignInExpiredError, TransferToolException
from moveit_py.packages.listing import format_package_line, packages_command
from moveit_py.packages.mailboxes import find_mailbox, get_package


def run(client: FakePackageServer, *, sent: bool = False, new: bool = False) -> str:
    out = io.StringIO()
    args = argparse.Namespace(sent=sent, new=new)
    assert packages_command(args, client, CliDeps(stdout=out, stderr=io.StringIO())) == 0
    return out.getvalue()


def test_at60_listing_inbox_sent_and_empty() -> None:
    client = FakePackageServer()
    inbox = run(client)
    sent = run(client, sent=True)
    client.package_items[1] = []
    empty = run(client)
    assert inbox == "*        100  2026-09-17 10:52  Alice                           Results\n"
    assert sent == "         101  2026-09-16 08:00  Bob, Carol                      Report\n"
    assert empty == "No packages in Inbox.\n"
    assert inbox + sent + empty == (
        "*        100  2026-09-17 10:52  Alice                           Results\n"
        "         101  2026-09-16 08:00  Bob, Carol                      Report\n"
        "No packages in Inbox.\n"
    )


def test_listing_request_parameters() -> None:
    client = FakePackageServer()
    run(client, new=True)
    request = next(r for r in client.requests if r.endpoint == "/packages")
    assert request.params is not None
    assert request.params["mailboxId"] == 1
    assert (request.params["sortField"], request.params["sortDirection"]) == ("sendStamp", "desc")
    assert request.params["newOnly"] == "true"
    run(client)
    last = [r for r in client.requests if r.endpoint == "/packages"][-1]
    assert last.params is not None
    assert "newOnly" not in last.params


def test_new_only_filters_new_packages() -> None:
    client = FakePackageServer()
    client.package_items[1].append({"id": 99, "subject": "Old", "isNew": False})
    assert run(client).count("\n") == 2
    assert run(client, new=True).count("\n") == 1


def test_all_pages_are_listed() -> None:
    client = FakePackageServer()
    client.package_items[1] = [{"id": i, "subject": f"s{i}"} for i in range(450)]
    assert run(client).count("\n") == 450


def test_sent_mailbox_uses_sent_id() -> None:
    client = FakePackageServer()
    run(client, sent=True)
    request = next(r for r in client.requests if r.endpoint == "/packages")
    assert request.params is not None
    assert request.params["mailboxId"] == SENT_ID


def test_star_only_for_new_inbox_packages() -> None:
    item: dict[str, Any] = {"id": 5, "isNew": True, "subject": "x"}
    assert format_package_line(item, "Inbox").startswith("* ")
    assert format_package_line(item, "Sent").startswith("  ")
    assert format_package_line({**item, "isNew": False}, "Inbox").startswith("  ")


def test_people_truncated_then_padded() -> None:
    long_name = "A" * 40
    line = format_package_line({"id": 1, "sender": {"fullName": long_name}, "subject": "S"}, "Inbox")
    assert line == f"  {1:>10}    {'A' * 30}  S"
    short = format_package_line({"id": 1, "sender": {"fullName": "Al"}, "subject": "S"}, "Inbox")
    assert short == f"  {1:>10}    {'Al':<30}  S"


def test_sender_falls_back_to_email_then_empty() -> None:
    assert "bob@x.org" in format_package_line({"id": 1, "sender": {"email": "bob@x.org"}}, "Inbox")
    assert format_package_line({"id": 1}, "Inbox") == f"  {1:>10}    {'':<30}  "
    assert format_package_line({"id": 1, "toRecipients": []}, "Sent") == f"  {1:>10}    {'':<30}  "


def test_missing_send_stamp_gives_empty_date() -> None:
    assert format_package_line({"id": 7, "subject": "S"}, "Sent") == f"  {7:>10}    {'':<30}  S"


def test_find_mailbox_and_get_package() -> None:
    client = FakePackageServer()
    assert find_mailbox(client, "Sent") == SENT_ID
    with pytest.raises(TransferToolException, match=r"^Mailbox Drafts not found$"):
        find_mailbox(client, "Drafts")
    assert get_package(client, "101", SENT_ID)["subject"] == "Report"
    assert client.requests[-1].params == {"mailboxId": SENT_ID}


def test_mailbox_not_found_for_sent() -> None:
    client = FakePackageServer()
    client.mailboxes = [{"id": 1, "name": "Inbox"}]
    with pytest.raises(TransferToolException, match="Mailbox Sent not found"):
        run(client, sent=True)


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


def test_packages_through_the_command_line() -> None:
    code, out, _ = run_main(["packages", "--sent"], FakePackageServer())
    assert code == 0
    assert "Bob, Carol" in out


def test_mailbox_not_found_through_the_command_line() -> None:
    client = FakePackageServer()
    client.mailboxes = []
    code, _, err = run_main(["packages", "--sent"], client)
    assert (code, err.strip()) == (1, "Mailbox Sent not found")


def test_sign_in_expired_while_listing() -> None:
    client = FakePackageServer()
    client.failures["/packages"] = SignInExpiredError("expired")
    code, _, err = run_main(["packages"], client)
    assert code == 3
    assert "./scripts/login.sh" in err


def test_network_error_while_listing() -> None:
    client = FakePackageServer()
    client.routes[("GET", "/mailboxes")] = FakeResponse("<html>", status_code=502)
    code, _, err = run_main(["packages"], client)
    assert code == 1
    assert "HTTP 502" in err
