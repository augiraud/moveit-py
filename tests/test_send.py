import argparse
import io
import shutil
import tempfile
from concurrent.futures import Future, wait
from pathlib import Path
from typing import Any

import pytest
from fakes import SENT_ID, WAF_PAGE, FakePackageServer, FakeResponse, RecordedRequest

from moveit_py.api import ApiClient
from moveit_py.cli import CliDeps, Settings, TokenProviderLike, main
from moveit_py.engine import TransferSession
from moveit_py.errors import PackageNotSentError, SignInExpiredError, TransferToolException
from moveit_py.packages import create
from moveit_py.packages.create import describe_problem
from moveit_py.packages.send import send_command

RESULTS_ID, REPORT_ID = "att-results.zip", "att-report.pdf"
SENT_ADVICE = "check your sent packages (./scripts/package-list.sh --sent) before sending it again."


@pytest.fixture(autouse=True)
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`results/sub/a.txt` and `report.pdf` in the current directory."""
    (tmp_path / "report.pdf").write_bytes(b"pdf")
    (tmp_path / "results" / "sub").mkdir(parents=True)
    (tmp_path / "results" / "sub" / "a.txt").write_bytes(b"a")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def temp_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The temporary directories created by `tempfile.mkdtemp`."""
    created: list[Path] = []
    real = tempfile.mkdtemp

    def mkdtemp(*args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        path = real(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    return created


class Streams:
    def __init__(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()


def send(client: FakePackageServer, streams: Streams | None = None, **options: object) -> int:
    streams = streams or Streams()
    values: dict[str, object] = {
        "subject": "Results",
        "paths": ["results", "report.pdf"],
        "to": ["alice@example.com", "ext@example.com"],
        "cc": ["bob@example.com"],
        "bcc": [],
        "note": "hi",
        "secure_note": None,
        "expires_days": None,
        "max_downloads": None,
        "classification": None,
        "threads": 2,
        "quiet": False,
        **options,
    }
    deps = CliDeps(stdout=streams.out, stderr=streams.err)
    return send_command(argparse.Namespace(**values), client, deps)


def post_packages(client: FakePackageServer) -> list[RecordedRequest]:
    return [r for r in client.requests if (r.method, r.endpoint) == ("POST", "/packages")]


def answer_package(client: FakePackageServer, outcome: FakeResponse | BaseException, *, created: bool = False) -> None:
    """Make `POST /packages` fail with `outcome`, although the package was created if `created`."""

    def route(request: RecordedRequest) -> FakeResponse:
        if created:
            client.store_sent_package(request.data)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client.add("POST", "/packages", route)


def test_at52_send_files_and_directory() -> None:
    client = FakePackageServer()
    streams = Streams()
    assert send(client, streams) == 0
    assert streams.out.getvalue().endswith("Sent package 42.\n")
    assert streams.err.getvalue() == ""
    assert set(client.uploaded) == {RESULTS_ID, REPORT_ID}
    assert client.uploaded[REPORT_ID] == b"pdf"
    assert len(post_packages(client)) == 1
    assert post_packages(client)[0].app_json
    assert client.created == [
        {
            "type": "General",
            "subject": "Results",
            "body": "hi",
            "expirationHours": 168,
            "maxAttachDownloads": 10,
            "recipients": [
                {"type": "User", "role": "To", "identifier": "user-alice"},
                {"type": "Unreg", "role": "To", "identifier": "ext@example.com"},
                {"type": "User", "role": "CC", "identifier": "user-bob"},
            ],
            "attachments": [{"id": RESULTS_ID}, {"id": REPORT_ID}],
        }
    ]
    assert client.deleted_attachments == []


def test_at52_options_reach_the_package() -> None:
    client = FakePackageServer()
    send(client, expires_days=2, max_downloads=3, classification="protected", secure_note=True, bcc=["bob@example.com"])
    package = client.created[0]
    assert package["expirationHours"] == 48
    assert package["maxAttachDownloads"] == 3
    assert package["packageClassificationTypeId"] == 6
    assert package["isSecureBody"] is True
    assert package["recipients"][-1] == {"type": "User", "role": "BCC", "identifier": "user-bob"}


def test_at53_too_many_files_uploads_nothing() -> None:
    client = FakePackageServer()
    client.requirements["maxFilesPerPackageLimit"] = 1
    with pytest.raises(PackageNotSentError, match="at most 1 files"):
        send(client)
    assert client.uploaded == {}
    assert post_packages(client) == []
    assert not [r for r in client.requests if r.endpoint == "/packages/attachments"]


def test_at54_refused_upload_deletes_the_others() -> None:
    client = FakePackageServer()
    refused = FakeResponse(json_body={"detail": "refused"}, status_code=422)
    client.failures["/packages/attachments"] = [None, refused]
    streams = Streams()
    with pytest.raises(PackageNotSentError, match="some files could not be uploaded, nothing was sent"):
        send(client, streams, threads=1)
    assert "Error: could not upload report.pdf (HTTP 422): " in streams.out.getvalue()
    assert refused.closed
    assert post_packages(client) == []
    assert client.deleted_attachments == [RESULTS_ID]


def test_upload_error_body_is_cut_at_300_characters() -> None:
    client = FakePackageServer()
    client.failures["/packages/attachments"] = [FakeResponse("x" * 400, status_code=500)]
    streams = Streams()
    with pytest.raises(PackageNotSentError):
        send(client, streams, threads=1, paths=["report.pdf"])
    assert f"(HTTP 500): {'x' * 300}\n" in streams.out.getvalue()


@pytest.mark.parametrize(
    "answer", [FakeResponse(json_body={}, status_code=201), FakeResponse(WAF_PAGE, status_code=201)]
)
def test_a_201_without_id_is_a_failed_upload(answer: FakeResponse) -> None:
    client = FakePackageServer()
    client.failures["/packages/attachments"] = [answer]
    with pytest.raises(PackageNotSentError, match="could not be uploaded"):
        send(client, paths=["report.pdf"])
    assert client.deleted_attachments == []


def test_at55_sign_in_expires_during_the_uploads() -> None:
    client = FakePackageServer()
    client.failures["/packages/attachments"] = [None, SignInExpiredError("expired")]
    with pytest.raises(SignInExpiredError):
        send(client, threads=1)
    assert post_packages(client) == []
    assert client.deleted_attachments == [RESULTS_ID]


def test_at56_no_note_lists_the_files() -> None:
    client = FakePackageServer()
    send(client, note=None)
    assert client.created[0]["body"] == "Files: results.zip, report.pdf"
    send(client, note="")
    assert client.created[1]["body"] == "Files: results.zip, report.pdf"


@pytest.mark.parametrize(
    ("outcome", "problem"),
    [
        (
            FakeResponse(json_body={"detail": "No recipients were supplied."}, status_code=404),
            "HTTP 404: No recipients were supplied.",
        ),
        (
            FakeResponse(json_body={"detail": "Invalid download limit value."}, status_code=500),
            "HTTP 500: Invalid download limit value.",
        ),
        (FakeResponse(WAF_PAGE), "HTTP 200, rejected by the firewall in front of the server"),
        (ConnectionError("connection reset"), "no response: connection reset"),
    ],
)
def test_at57_package_not_created(outcome: FakeResponse | Exception, problem: str) -> None:
    client = FakePackageServer()
    answer_package(client, outcome)
    with pytest.raises(PackageNotSentError, match="not sent") as error:
        send(client, paths=["report.pdf"])
    assert str(error.value) == f"Error: the package was not sent ({problem})."
    assert client.deleted_attachments == [REPORT_ID]
    sent_lookups = [r for r in client.requests if r.endpoint == "/packages" and r.method == "GET"]
    assert [r.params for r in sent_lookups] == [
        {"mailboxId": SENT_ID, "sortField": "sendStamp", "sortDirection": "desc", "perPage": 20}
    ]


@pytest.mark.parametrize("outcome", [FakeResponse(WAF_PAGE), ConnectionError("connection reset")])
def test_at58_package_created_although_the_answer_says_otherwise(outcome: FakeResponse | Exception) -> None:
    client = FakePackageServer()
    older = {"id": 90, "subject": "Results", "sendStamp": "2026-09-01T09:00:00"}
    client.package_items[SENT_ID].append(older)
    client.package_details[(SENT_ID, 90)] = {"id": 90, "subject": "Results", "attachments": [{"id": "att-old"}]}
    answer_package(client, outcome, created=True)
    streams = Streams()
    assert send(client, streams) == 0
    assert streams.out.getvalue().endswith("Sent package 42.\n")
    assert client.deleted_attachments == []


def test_at58_skips_same_subject_packages_with_other_attachments() -> None:
    client = FakePackageServer()
    newer = {"id": 90, "subject": "Results", "sendStamp": "2026-09-18T10:00:00"}
    client.package_items[SENT_ID].insert(0, newer)
    client.package_details[(SENT_ID, 90)] = {"id": 90, "subject": "Results", "attachments": [{"id": "att-old"}]}

    def route(request: RecordedRequest) -> FakeResponse:
        client.store_sent_package(request.data)
        client.package_items[SENT_ID].append(client.package_items[SENT_ID].pop(0))  # listed after package 90
        return FakeResponse(WAF_PAGE)

    client.add("POST", "/packages", route)
    streams = Streams()
    assert send(client, streams) == 0
    assert streams.out.getvalue().endswith("Sent package 42.\n")
    looked_up = [r.endpoint for r in client.requests if r.endpoint.startswith("/packages/") and r.method == "GET"]
    assert looked_up[-2:] == ["/packages/90", "/packages/42"]
    assert "/packages/101" not in looked_up  # another subject
    assert client.deleted_attachments == []


def test_at58_a_201_without_id_is_checked_too() -> None:
    client = FakePackageServer()
    answer_package(client, FakeResponse(json_body={}, status_code=201), created=True)
    assert send(client) == 0
    assert client.deleted_attachments == []


def test_a_201_without_id_that_was_not_created() -> None:
    client = FakePackageServer()
    answer_package(client, FakeResponse(json_body={}, status_code=201))
    with pytest.raises(PackageNotSentError, match=r"not sent \(HTTP 201: \{\}\)"):
        send(client)
    assert sorted(client.deleted_attachments) == sorted([RESULTS_ID, REPORT_ID])


def test_at59_the_check_fails() -> None:
    client = FakePackageServer()
    answer_package(client, FakeResponse(WAF_PAGE), created=True)
    client.failures["/mailboxes"] = ConnectionError("network down")
    with pytest.raises(TransferToolException, match="--sent") as error:
        send(client)
    assert not isinstance(error.value, PackageNotSentError)
    assert str(error.value) == (
        "Error: could not tell whether the package was sent "
        f"(HTTP 200, rejected by the firewall in front of the server): {SENT_ADVICE}"
    )
    assert client.deleted_attachments == []


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("down"),
        SignInExpiredError("expired"),
        FakeResponse(json_body={"error": "server"}, status_code=500),
    ],
)
def test_any_failure_of_the_check_keeps_the_attachments(failure: Exception | FakeResponse) -> None:
    client = FakePackageServer()
    answer_package(client, ConnectionError("reset"))
    client.failures["/packages"] = [None, failure]  # the POST, then the listing of the Sent mailbox
    with pytest.raises(TransferToolException, match=r"no response: reset.*--sent") as error:
        send(client)
    assert not isinstance(error.value, PackageNotSentError)
    assert client.deleted_attachments == []


def test_sent_mailbox_not_found_during_the_check() -> None:
    client = FakePackageServer()
    client.mailboxes = [{"id": 1, "name": "Inbox"}]
    answer_package(client, FakeResponse(json_body={"detail": "oops"}, status_code=500))
    with pytest.raises(TransferToolException, match=r"could not tell.*HTTP 500: oops.*--sent") as error:
        send(client)
    assert not isinstance(error.value, PackageNotSentError)
    assert isinstance(error.value.__cause__, TransferToolException)
    assert str(error.value.__cause__) == "Mailbox Sent not found"
    assert client.deleted_attachments == []


def test_ki5_ctrl_c_while_creating_the_package() -> None:
    client = FakePackageServer()
    answer_package(client, KeyboardInterrupt())
    streams = Streams()
    with pytest.raises(KeyboardInterrupt):
        send(client, streams)
    assert (
        streams.err.getvalue() == f"Error: could not tell whether the package was sent (interrupted): {SENT_ADVICE}\n"
    )
    assert client.deleted_attachments == []
    assert not [r for r in client.requests if r.endpoint == "/mailboxes"]  # no check: the user wants to stop


def test_ctrl_c_during_the_sent_check_says_the_package_may_exist() -> None:
    client = FakePackageServer()
    answer_package(client, ConnectionError("reset"))

    def interrupted(_request: RecordedRequest) -> FakeResponse:
        raise KeyboardInterrupt

    client.add("GET", "/mailboxes", interrupted)
    streams = Streams()
    with pytest.raises(KeyboardInterrupt):
        send(client, streams)
    assert streams.err.getvalue() == (
        f"Error: could not tell whether the package was sent (no response: reset): {SENT_ADVICE}\n"
    )
    assert client.deleted_attachments == []


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("bug")])
def test_failure_between_the_uploads_and_the_package_deletes_the_attachments(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def rmtree(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    client = FakePackageServer()
    with pytest.raises(type(error)):
        send(client)
    assert sorted(client.deleted_attachments) == sorted([RESULTS_ID, REPORT_ID])
    assert post_packages(client) == []


def test_ctrl_c_during_the_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    def wait_for(pending: set[Future[Any]], _timeout: float, _when: str) -> tuple[set[Future[Any]], set[Future[Any]]]:
        wait(pending)  # let the uploads finish, then interrupt
        raise KeyboardInterrupt

    def session(*args: Any, **kwargs: Any) -> TransferSession:  # noqa: ANN401
        return TransferSession(*args, wait_for=wait_for, **kwargs)

    monkeypatch.setattr(create, "TransferSession", session)
    client = FakePackageServer()
    with pytest.raises(KeyboardInterrupt):
        send(client)
    assert set(client.uploaded) == {RESULTS_ID, REPORT_ID}
    assert sorted(client.deleted_attachments) == sorted([RESULTS_ID, REPORT_ID])
    assert post_packages(client) == []


def test_unexpected_error_during_the_uploads_deletes_the_uploaded() -> None:
    client = FakePackageServer()
    client.failures["/packages/attachments"] = [None, RuntimeError("bug")]
    streams = Streams()
    with pytest.raises(PackageNotSentError, match="some files could not be uploaded"):
        send(client, streams, threads=1)
    assert "Error: could not transfer report.pdf" in streams.out.getvalue()
    assert client.deleted_attachments == [RESULTS_ID]


@pytest.mark.parametrize(
    ("failure", "warning"),
    [(ConnectionError("down"), "down"), (FakeResponse(status_code=500), "HTTP 500")],
)
def test_a_failed_deletion_is_a_warning_and_the_others_go_on(failure: Exception | FakeResponse, warning: str) -> None:
    client = FakePackageServer()
    answer_package(client, FakeResponse(json_body={"detail": "no"}, status_code=404))
    client.failures[f"/packages/attachments/{RESULTS_ID}"] = failure
    streams = Streams()
    with pytest.raises(PackageNotSentError):
        send(client, streams)
    assert streams.err.getvalue() == f"Warning: could not delete the uploaded attachment {RESULTS_ID}: {warning}\n"
    assert client.deleted_attachments == [REPORT_ID]
    deletes = [r.endpoint for r in client.requests if r.method == "DELETE"]
    assert deletes == [f"/packages/attachments/{RESULTS_ID}", f"/packages/attachments/{REPORT_ID}"]


def test_sign_in_expired_while_creating_the_package() -> None:
    client = FakePackageServer()
    answer_package(client, SignInExpiredError("expired"))
    with pytest.raises(SignInExpiredError):
        send(client)
    assert sorted(client.deleted_attachments) == sorted([RESULTS_ID, REPORT_ID])
    assert not [r for r in client.requests if r.endpoint == "/mailboxes"]


def test_sign_in_expired_deletions_may_fail_too() -> None:
    client = FakePackageServer()
    answer_package(client, SignInExpiredError("expired"))
    client.failures[f"/packages/attachments/{REPORT_ID}"] = SignInExpiredError("expired")
    client.failures[f"/packages/attachments/{RESULTS_ID}"] = SignInExpiredError("expired")
    streams = Streams()
    with pytest.raises(SignInExpiredError):
        send(client, streams)
    assert streams.err.getvalue().count("Warning: could not delete the uploaded attachment") == 2


def test_temporary_directory_is_removed_on_success(temp_dirs: list[Path]) -> None:
    send(FakePackageServer())
    assert len(temp_dirs) == 1
    assert temp_dirs[0].name.startswith("moveit-package-")
    assert not temp_dirs[0].exists()


def test_temporary_directory_is_removed_when_the_checks_fail(temp_dirs: list[Path]) -> None:
    client = FakePackageServer()
    client.requirements["maxFilesPerPackageLimit"] = 1
    with pytest.raises(PackageNotSentError):
        send(client)
    assert len(temp_dirs) == 1
    assert not temp_dirs[0].exists()


def test_temporary_directory_is_removed_when_the_uploads_fail(temp_dirs: list[Path]) -> None:
    client = FakePackageServer()
    client.failures["/packages/attachments"] = [SignInExpiredError("expired")]
    with pytest.raises(SignInExpiredError):
        send(client)
    assert len(temp_dirs) == 1
    assert not temp_dirs[0].exists()


def test_temporary_directory_is_removed_when_the_package_fails(temp_dirs: list[Path]) -> None:
    client = FakePackageServer()
    answer_package(client, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        send(client)
    assert not temp_dirs[0].exists()


def test_temporary_directory_is_removed_when_a_path_is_invalid(temp_dirs: list[Path]) -> None:
    with pytest.raises(PackageNotSentError, match="neither a file nor a directory"):
        send(FakePackageServer(), paths=["missing"])
    assert not temp_dirs[0].exists()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            FakeResponse(json_body={"detail": "No recipients were supplied."}, status_code=404),
            "HTTP 404: No recipients were supplied.",
        ),
        (FakeResponse(json_body={"error": "x", "code": 3}, status_code=500), 'HTTP 500: {"error": "x", "code": 3}'),
        (FakeResponse(json_body=["a"], status_code=500), 'HTTP 500: ["a"]'),
        (FakeResponse(WAF_PAGE), "HTTP 200, rejected by the firewall in front of the server"),
        (FakeResponse("<html>Bad gateway</html>", status_code=502), "HTTP 502"),
        (FakeResponse(b"", status_code=204), "HTTP 204"),
    ],
)
def test_problem_description(response: FakeResponse, expected: str) -> None:
    assert describe_problem(response) == expected


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


SEND_ARGS = ["send", "Results", "results", "report.pdf", "--to", "alice@example.com,ext@example.com", "--note", "hi"]


def test_send_through_the_command_line() -> None:
    client = FakePackageServer()
    code, out, err = run_main([*SEND_ARGS, "--cc", "bob@example.com", "-q"], client)
    assert (code, out, err) == (0, "Sent package 42.\n", "")
    assert client.created[0]["attachments"] == [{"id": RESULTS_ID}, {"id": REPORT_ID}]


def test_sign_in_expired_exits_3_through_the_command_line() -> None:
    client = FakePackageServer()
    answer_package(client, SignInExpiredError("expired"))
    code, _, err = run_main(SEND_ARGS, client)
    assert code == 3
    assert "not sent" in err
    assert sorted(client.deleted_attachments) == sorted([RESULTS_ID, REPORT_ID])


def test_at55_sign_in_expires_during_the_uploads_through_the_command_line() -> None:
    client = FakePackageServer()
    client.failures["/packages/attachments"] = [None, SignInExpiredError("expired")]
    code, _, err = run_main([*SEND_ARGS, "--threads", "1"], client)
    assert code == 3
    assert "not sent" in err
    assert "login.sh" in err
    assert client.deleted_attachments == [RESULTS_ID]
    assert post_packages(client) == []


def test_ki5_exits_130_through_the_command_line() -> None:
    client = FakePackageServer()
    answer_package(client, KeyboardInterrupt())
    code, _, err = run_main(SEND_ARGS, client)
    assert code == 130
    assert f"(interrupted): {SENT_ADVICE}" in err
    assert err.endswith("\nInterrupted.\n")
    assert client.deleted_attachments == []


def test_not_sent_exits_1_through_the_command_line() -> None:
    client = FakePackageServer()
    code, _, err = run_main(
        [*SEND_ARGS[:-2], "--to", "x@example.com", "--secure-note", "--max-downloads", "99"], client
    )
    assert code == 1
    assert "files must be downloadable 1 to 10 times" in err
    assert client.uploaded == {}
