import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import DUMMY, DUMMY_SHA1, WAF_PAGE, FakeFolderServer, FakeResponse

from moveit_py.engine import Outcome, TransferSession
from moveit_py.errors import ResponseException, SignInExpiredError, TransferToolException
from moveit_py.files import (
    checked_local_path,
    download_if_not_present,
    is_safe_attachment_name,
    local_path_for,
    remote_sha1,
    sha1_file,
)

OUT = Path("out")


def make() -> tuple[FakeFolderServer, TransferSession, io.StringIO]:
    out = io.StringIO()
    session = TransferSession(2, True, out, io.StringIO())
    return FakeFolderServer(), session, out


def remote(file_id: str = "good_file_1", size: object = 13) -> dict[str, object]:
    return {"id": file_id, "path": f"/remote/{file_id}", "size": size}


def downloads(server: FakeFolderServer, file_id: str = "good_file_1") -> int:
    return server.count("GET", f"/files/{file_id}/download")


@pytest.mark.parametrize(
    ("remote_path", "remote_dir", "expected"),
    [
        ("/remote/a b/c.txt", "/remote/", OUT / "a b" / "c.txt"),
        ("/remote/a b/c.txt", "/remote", OUT / "a b" / "c.txt"),
        ("/remote/c.txt", "/", OUT / "remote" / "c.txt"),
        ("/remote/c.txt", "", OUT / "remote" / "c.txt"),
        ("/remote/../c.txt", "/remote", None),
        ("/remote/../c.txt", "/", None),
        ("/remote/a//c.txt", "/remote/", OUT / "a" / "c.txt"),
        ("/remote/./c.txt", "/remote/", OUT / "c.txt"),
        ("/remote/", "/remote/", None),
        ("/remote//./", "/remote/", None),
        ("/remote/a\x00b", "/remote/", None),
        ("/remote/a\\b", "/remote/", None),
        ("/remote/C:x", "/remote/", None),
        ("/remote/c:\\x", "/remote/", None),
        ("/remote/\\\\server\\share", "/remote/", None),
        ("/other/c.txt", "/remote/", None),
    ],
)
def test_at31_remote_to_local(remote_path: str, remote_dir: str, expected: Path | None) -> None:
    assert local_path_for(remote_path, remote_dir, OUT) == expected


def test_at31_checked_path_raises() -> None:
    with pytest.raises(TransferToolException, match=r"Error: refusing to download /remote/../c.txt outside of out"):
        checked_local_path("/remote/../c.txt", "/remote/", OUT)


@pytest.mark.parametrize("name", ["../evil.txt", "..", "sub\\evil.txt", "a/b", "", ".", "C:evil", "/abs", "a\x00b"])
def test_unsafe_attachment_names(name: str) -> None:
    assert not is_safe_attachment_name(name)


@pytest.mark.parametrize("name", ["a b.txt", "results.zip", "Meeting 10:30.pdf"])
def test_safe_attachment_names(name: str) -> None:
    assert is_safe_attachment_name(name)


def test_at34_sha1_file(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(DUMMY)
    assert sha1_file(tmp_path / "f") == DUMMY_SHA1
    assert sha1_file(tmp_path / "missing") is None


def test_at36_remote_sha1() -> None:
    server, _, _ = make()
    assert remote_sha1(server, "good_file_1") == DUMMY_SHA1
    with pytest.raises(ResponseException, match=r"Hash not found in response\."):
        remote_sha1(server, "bad_file")


def test_at37_download_creates_directories(tmp_path: Path) -> None:
    server, session, _ = make()
    target = tmp_path / "sub" / "deeper" / "f.txt"
    assert download_if_not_present(server, session, remote(), target) == Outcome.TRANSFERRED
    assert target.read_bytes() == DUMMY
    assert not target.with_name("f.txt.part").exists()


def test_at37_download_404(tmp_path: Path) -> None:
    server, session, out = make()
    target = tmp_path / "f.txt"
    assert download_if_not_present(server, session, remote("missing_file"), target) == Outcome.ERROR
    assert f"Error: could not download {target} (HTTP 404)" in out.getvalue()
    assert not target.exists()


def test_at38_waf_page(tmp_path: Path) -> None:
    server, session, out = make()
    target = tmp_path / "f.txt"
    assert download_if_not_present(server, session, remote("waf_file"), target) == Outcome.ERROR
    assert f"Error: downloaded {len(WAF_PAGE)} bytes instead of 13 for {target}" in out.getvalue()
    assert not target.exists()
    assert not target.with_name("f.txt.part").exists()


def test_at40_multithread(tmp_path: Path) -> None:
    server, session, _ = make()
    items = ["good_file_1", "missing_file", "good_file_2"]
    session.total = 3
    session.transfer_multithread(items, lambda i: download_if_not_present(server, session, remote(i), tmp_path / i))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["good_file_1", "good_file_2"]
    assert (session.transferred, session.skipped, session.errors) == (2, 0, 1)


def test_identical_file_skipped(tmp_path: Path) -> None:
    server, session, _ = make()
    target = tmp_path / "f"
    target.write_bytes(DUMMY)
    assert download_if_not_present(server, session, remote(), target) == Outcome.SKIPPED
    assert downloads(server) == 0
    assert server.count("GET", "/files/good_file_1") == 1


def test_uppercase_remote_hash_skipped(tmp_path: Path) -> None:
    server, session, _ = make()
    server.add_json("GET", "/files/good_file_1", {"hash": DUMMY_SHA1.upper()})
    target = tmp_path / "f"
    target.write_bytes(DUMMY)
    assert download_if_not_present(server, session, remote(), target) == Outcome.SKIPPED


def test_same_size_different_content_redownloaded(tmp_path: Path) -> None:
    server, session, _ = make()
    target = tmp_path / "f"
    target.write_bytes(b"x" * len(DUMMY))
    assert download_if_not_present(server, session, remote(), target) == Outcome.TRANSFERRED
    assert target.read_bytes() == DUMMY


def test_different_size_downloads_without_hash_request(tmp_path: Path) -> None:
    server, session, _ = make()
    target = tmp_path / "f"
    target.write_bytes(b"short")
    assert download_if_not_present(server, session, remote(), target) == Outcome.TRANSFERRED
    assert server.count("GET", "/files/good_file_1") == 0


def test_hash_lookup_failure(tmp_path: Path) -> None:
    server, session, out = make()
    server.failures["/files/good_file_1"] = ConnectionError("boom")
    target = tmp_path / "f"
    target.write_bytes(DUMMY)
    assert download_if_not_present(server, session, remote(), target) == Outcome.ERROR
    assert "Error: could not get SHA1 of remote file /remote/good_file_1" in out.getvalue()
    assert downloads(server) == 0


def test_size_as_string(tmp_path: Path) -> None:
    server, session, _ = make()
    target = tmp_path / "f"
    target.write_bytes(DUMMY)
    assert download_if_not_present(server, session, remote(size="13"), target) == Outcome.SKIPPED


def test_missing_size_always_downloads(tmp_path: Path) -> None:
    server, session, _ = make()
    target = tmp_path / "f"
    target.write_bytes(DUMMY)
    info = {"id": "good_file_1", "path": "/remote/good_file_1"}
    assert download_if_not_present(server, session, info, target) == Outcome.TRANSFERRED
    assert downloads(server) == 1


class BrokenResponse(FakeResponse):
    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        yield b"abc"
        raise ConnectionError("reset")


def test_mid_stream_error_removes_part(tmp_path: Path) -> None:
    server, session, _ = make()
    broken = BrokenResponse(DUMMY)
    server.failures["/files/good_file_1/download"] = broken
    target = tmp_path / "f"
    with pytest.raises(ConnectionError):
        download_if_not_present(server, session, remote(), target)
    assert list(tmp_path.iterdir()) == []
    assert broken.closed


def test_write_error_directory_in_the_way(tmp_path: Path) -> None:
    server, session, out = make()
    target = tmp_path / "f"
    target.mkdir()
    assert download_if_not_present(server, session, remote(), target) == Outcome.ERROR
    assert f"Error: could not write to {target}: " in out.getvalue()
    assert not (tmp_path / "f.part").exists()


def test_write_error_parent_is_file(tmp_path: Path) -> None:
    server, session, out = make()
    (tmp_path / "p").write_text("x")
    target = tmp_path / "p" / "f"
    assert download_if_not_present(server, session, remote(), target) == Outcome.ERROR
    assert f"Error: could not write to {target}: " in out.getvalue()


def test_checked_local_path_success() -> None:
    assert checked_local_path("/remote/a/b.txt", "/remote", OUT) == OUT / "a" / "b.txt"


def test_symlink_escape_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    try:
        (output / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported")
    assert local_path_for("/remote/link/f.txt", "/remote", output) is None


def test_sign_in_expired_propagates(tmp_path: Path) -> None:
    server, session, _ = make()
    server.failures["/files/good_file_1"] = SignInExpiredError("expired")
    target = tmp_path / "f"
    target.write_bytes(DUMMY)
    with pytest.raises(SignInExpiredError):
        download_if_not_present(server, session, remote(), target)
