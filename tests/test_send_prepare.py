import zipfile
from pathlib import Path
from typing import Any

import pytest
from fakes import FakePackageServer

from moveit_py.errors import PackageNotSentError
from moveit_py.packages.send import (
    Requirements,
    format_size,
    get_requirements,
    package_fields,
    prepare_attachments,
    resolve_recipients,
)

Option = int | str | bool | None


def make_tree(base: Path) -> None:
    (base / "report.pdf").write_bytes(b"pdf")
    (base / "results" / "sub").mkdir(parents=True)
    (base / "results" / "sub" / "a.txt").write_bytes(b"a")


def fields(client: FakePackageServer | None = None, **options: Option) -> dict[str, Any]:
    server = client or FakePackageServer()
    values: dict[str, Option] = {
        "subject": "S",
        "note": None,
        "secure_note": None,
        "expires_days": None,
        "max_downloads": None,
        "classification": None,
        "attachment_count": 1,
        **options,
    }
    return package_fields(requirements=get_requirements(server), **values)


def with_requirements(**changes: Option) -> FakePackageServer:
    server = FakePackageServer()
    server.requirements.update(changes)
    return server


def not_sent(match: str, **options: Option) -> None:
    with pytest.raises(PackageNotSentError, match=match):
        fields(None, **options)


def test_at47_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_tree(tmp_path)
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.chdir(tmp_path)
    result = prepare_attachments(["report.pdf", "results/"], temp)
    assert result == [Path("report.pdf"), temp / "results.zip"]
    with zipfile.ZipFile(temp / "results.zip") as archive:
        assert archive.read("results/sub/a.txt") == b"a"
        assert archive.getinfo("results/sub/a.txt").compress_type == zipfile.ZIP_DEFLATED
    assert prepare_attachments(["./results"], temp) == [temp / "results.zip"]


def test_zip_includes_file_links_but_not_directory_links(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / "results" / "link.txt").symlink_to(tmp_path / "report.pdf")
    (tmp_path / "results" / "dirlink").symlink_to(tmp_path / "results" / "sub")
    (tmp_path / "results" / "broken").symlink_to(tmp_path / "missing")
    temp = tmp_path / "tmp"
    temp.mkdir()
    prepare_attachments([str(tmp_path / "results")], temp)
    with zipfile.ZipFile(temp / "results.zip") as archive:
        assert archive.namelist() == ["results/link.txt", "results/sub/a.txt"]
        assert archive.read("results/link.txt") == b"pdf"


def test_at48_attachment_errors(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "results.zip").write_bytes(b"z")
    temp = tmp_path / "tmp"
    temp.mkdir()
    with pytest.raises(PackageNotSentError, match=r"missing is neither a file nor a directory, nothing was sent\."):
        prepare_attachments([str(tmp_path / "report.pdf"), "missing"], temp)
    with pytest.raises(PackageNotSentError, match=r"named results\.zip, nothing was sent\."):
        prepare_attachments([str(tmp_path / "results") + "/", str(tmp_path / "other" / "results.zip")], temp)
    with pytest.raises(PackageNotSentError, match=r"report\.pdf is 0\.0 MB, more than the maximum of 0\.0 MB, nothing"):
        prepare_attachments([str(tmp_path / "report.pdf")], temp, max_size=2)


def test_duplicate_names_are_sorted_and_joined(tmp_path: Path) -> None:
    for folder in ("x", "y"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "b.txt").write_bytes(b"1")
        (tmp_path / folder / "a.txt").write_bytes(b"1")
    paths = [str(tmp_path / f / n) for n in ("b.txt", "a.txt") for f in ("x", "y")]
    with pytest.raises(PackageNotSentError, match=r"named a\.txt, b\.txt, nothing was sent\."):
        prepare_attachments(paths, tmp_path)


def test_size_limit_default_message(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    with big.open("wb") as file:
        file.truncate(1024**3 + 1)  # sparse
    with pytest.raises(PackageNotSentError, match=r"big\.bin is 1\.00 GB, more than the maximum of 1\.00 GB, nothing"):
        prepare_attachments([str(big)], tmp_path)
    assert prepare_attachments([str(big)], tmp_path, max_size=1024**3 + 1) == [big]


def test_format_size() -> None:
    assert format_size(1024**3) == "1.00 GB"
    assert format_size(0) == "0.0 MB"
    assert format_size(int(56.3 * 1024**2)) == "56.3 MB"
    assert format_size(int(0.1 * 1024**3) + 1) == "0.10 GB"
    assert format_size(int(0.099 * 1024**3)) == "101.4 MB"


def test_get_requirements_missing_fields() -> None:
    server = FakePackageServer()
    server.requirements = {"allowSendPackages": True}
    requirements = get_requirements(server)
    assert requirements == Requirements(allow_send_packages=True)
    assert requirements.max_expiration_hours == -1


def test_at49_options() -> None:
    result = fields(
        note="hi", expires_days=10, max_downloads=2, classification="protected", secure_note=True, subject="Sub"
    )
    assert result == {
        "subject": "Sub",
        "body": "hi",
        "expirationHours": 240,
        "maxAttachDownloads": 2,
        "packageClassificationTypeId": 6,
        "isSecureBody": True,
    }
    assert fields(subject="Sub") == {"subject": "Sub", "body": "", "expirationHours": 168, "maxAttachDownloads": 10}


def test_secure_note_false_is_sent() -> None:
    assert fields(secure_note=False)["isSecureBody"] is False


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"expires_days": 11}, "packages must expire after 1 to 10 days"),
        ({"expires_days": 0}, "packages must expire after 1 to 10 days"),
        ({"max_downloads": 11}, "files must be downloadable 1 to 10 times"),
        ({"max_downloads": 0}, "files must be downloadable 1 to 10 times"),
        ({"classification": "Top secret"}, r"unknown classification Top secret \(available: Unclassified, Protected\)"),
        ({"attachment_count": 4}, "at most 3 files can be sent in a package, not 4"),
    ],
)
def test_at50_option_errors(options: dict[str, Option], message: str) -> None:
    not_sent(message, **options)


def test_at50_not_allowed() -> None:
    for key in ("allowSendPackages", "allowAttachFiles"):
        with pytest.raises(PackageNotSentError, match=r"your account isn't allowed to send packages with files\."):
            fields(with_requirements(**{key: False}))


def test_at50_classification_required() -> None:
    with pytest.raises(
        PackageNotSentError, match=r"a classification is required \(available: Unclassified, Protected\)"
    ):
        fields(with_requirements(isClassificationFieldRequired=True))
    assert (
        fields(with_requirements(isClassificationFieldRequired=True), classification="UNCLASSIFIED")[
            "packageClassificationTypeId"
        ]
        == 5
    )


def test_at50_secure_note_not_changeable() -> None:
    server = with_requirements(allowChangeSecureNote=False)
    with pytest.raises(PackageNotSentError, match=r"isn't allowed to change whether the note is secure\."):
        fields(server, secure_note=True)
    assert fields(server, secure_note=False)["isSecureBody"] is False  # equal to the default
    assert "isSecureBody" not in fields(server)


def test_at53_too_many_files_for_the_account() -> None:
    server = with_requirements(maxFilesPerPackageLimit=1)
    with pytest.raises(PackageNotSentError, match="at most 1 files"):
        fields(server, attachment_count=2)
    assert fields(server, attachment_count=1)


def test_no_file_limit() -> None:
    server = with_requirements()
    del server.requirements["maxFilesPerPackageLimit"]
    assert fields(server, attachment_count=500)


def test_ki9_no_maximum() -> None:
    server = with_requirements()
    for key in ("maxUserPackageExpirationHours", "maxAttachmentDownloads"):
        del server.requirements[key]
    with pytest.raises(PackageNotSentError, match=r"^Error: packages must expire after at least 1 day\.$"):
        fields(server, expires_days=0)
    with pytest.raises(PackageNotSentError, match=r"^Error: files must be downloadable at least once\.$"):
        fields(server, max_downloads=0)
    assert fields(server, expires_days=1000, max_downloads=1000)["maxAttachDownloads"] == 1000


def test_limit_check_omits_fields_without_default_or_value() -> None:
    server = with_requirements()
    for key in ("defaultUserPackageExpirationHours", "defaultAttachmentDownloads"):
        del server.requirements[key]
    assert fields(server) == {"subject": "S", "body": ""}
    assert fields(server, expires_days=2)["expirationHours"] == 48


def test_order_of_checks() -> None:
    server = with_requirements(allowSendPackages=False, maxFilesPerPackageLimit=1)
    with pytest.raises(PackageNotSentError, match="isn't allowed to send"):
        fields(server, attachment_count=5, expires_days=99)
    server = with_requirements(maxFilesPerPackageLimit=1, isClassificationFieldRequired=True)
    with pytest.raises(PackageNotSentError, match="at most 1"):
        fields(server, attachment_count=5, expires_days=99)
    with pytest.raises(PackageNotSentError, match="must expire"):
        fields(server, expires_days=99, max_downloads=99)
    with pytest.raises(PackageNotSentError, match="downloadable"):
        fields(server, max_downloads=99)
    with pytest.raises(PackageNotSentError, match="classification is required"):
        fields(server)


def test_at51_recipients() -> None:
    client = FakePackageServer()
    recipients = resolve_recipients(
        client, ["alice@example.com", "ext@example.com"], ["bob@example.com"], ["ALICE@example.com"]
    )
    assert recipients == [
        {"type": "User", "role": "To", "identifier": "user-alice"},
        {"type": "Unreg", "role": "To", "identifier": "ext@example.com"},
        {"type": "User", "role": "CC", "identifier": "user-bob"},
        {"type": "User", "role": "BCC", "identifier": "user-alice"},
    ]
    searches = [(r.params or {})["searchString"] for r in client.requests if r.endpoint == "/contacts"]
    assert searches == ["alice@example.com", "ext@example.com", "bob@example.com", "ALICE@example.com"]


def test_user_preferred_over_other_types() -> None:
    client = FakePackageServer()
    client.contacts = [
        {"name": "G", "email": "x@example.com", "identifier": "group-x", "type": "Group"},
        {"name": "U", "email": "x@example.com", "identifier": "user-x", "type": "User"},
        {"name": "U2", "email": "x@example.com", "identifier": "user-x2", "type": "User"},
    ]
    assert resolve_recipients(client, ["x@example.com"], [], []) == [
        {"type": "User", "role": "To", "identifier": "user-x"}
    ]
    client.contacts = client.contacts[:1]
    assert resolve_recipients(client, ["x@example.com"], [], []) == [
        {"type": "Group", "role": "To", "identifier": "group-x"}
    ]


def test_no_to_recipient() -> None:
    client = FakePackageServer()
    with pytest.raises(PackageNotSentError, match=r"needs at least one recipient, nothing was sent\."):
        resolve_recipients(client, [], ["bob@example.com"], [])
    assert client.requests == []
