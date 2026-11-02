import argparse
import io
import re
from pathlib import Path

import pytest
from fakes import DUMMY, FakeApiClient, FakeFolderServer, FakeResponse

from moveit_py.api import ApiClient
from moveit_py.cli import CliDeps, Settings, TokenProviderLike, main
from moveit_py.engine import TransferSession
from moveit_py.errors import SignInExpiredError, TransferToolException
from moveit_py.folders import (
    FolderCache,
    download_command,
    list_local_files,
    order_new_first,
    remote_path_for,
    select_files,
    upload_command,
)


def make_args(**values: object) -> argparse.Namespace:
    defaults: dict[str, object] = {"threads": 1, "quiet": False, "filter": None}
    return argparse.Namespace(**{**defaults, **values})


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def call(command, args: argparse.Namespace, client: FakeApiClient) -> Result:  # noqa: ANN001
    out, err = io.StringIO(), io.StringIO()
    code = command(args, client, CliDeps(stdout=out, stderr=err))
    return Result(code, out.getvalue(), err.getvalue())


def upload(client: FakeApiClient, input_dir: Path, remote_dir: str, **options: object) -> Result:
    return call(upload_command, make_args(input_dir=str(input_dir), remote_dir=remote_dir, **options), client)


def download(client: FakeApiClient, remote_dir: str, output_dir: Path, **options: object) -> Result:
    return call(download_command, make_args(remote_dir=remote_dir, output_dir=str(output_dir), **options), client)


def make_files(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {name}")


def posts(client: FakeApiClient, suffix: str) -> list[str]:
    return [r.endpoint for r in client.requests if r.method == "POST" and r.endpoint.endswith(suffix)]


# --- Path mapping, selection, ordering ---


def test_at31_local_to_remote_paths() -> None:
    assert remote_path_for(Path("input/a b/c.txt"), Path("input"), "/Home/user/") == "/Home/user/a b/c.txt"
    assert remote_path_for(Path("/data/in/c.txt"), Path("/data/in"), "") == "/c.txt"
    assert remote_path_for(Path("input/input/c.txt"), Path("input"), "/Data") == "/Data/input/c.txt"


def test_at32_filtering() -> None:
    files = FakeFolderServer.PAGE_1 + FakeFolderServer.PAGE_2
    items = [{"path": p} for p in files]
    assert [f["path"] for f in select_files(items, "/remote", None)] == [
        "/remote/good_file_1",
        "/remote/bad_file",
        "/remote/good_file_2",
        "/remote/sub/good_file_3",
    ]
    selected = select_files(items, "/", re.compile("good_file_[34]"))
    assert [f["path"] for f in selected] == ["/remote/sub/good_file_3", "/remote_other/good_file_4"]


def test_at33_ordering(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.write_text("x")
    pairs = [("a", existing), ("b", tmp_path / "missing"), ("c", tmp_path / "missing2")]
    assert [name for name, _ in order_new_first(pairs)] == ["b", "c", "a"]


# --- Upload ---


def test_at42_upload_skips_existing(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "already_uploaded", "new_file_1")
    client = FakeFolderServer()
    result = upload(client, tmp_path / "in", "/upload")
    assert posts(client, "/files") == ["/folders/12345/files"]
    assert client.uploads["/folders/12345/files"] == b"content of new_file_1"
    assert "Found 1 files to upload." in result.out
    assert "Uploaded 1 files, skipped 1 files, 0 errors." in result.out
    assert result.code == 0


def test_ki1_progress_total_includes_skipped(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "already_uploaded", "new_file_1")
    result = upload(FakeFolderServer(), tmp_path / "in", "/upload")
    assert "Progress: 2 / 2 (1 transferred, 1 skipped, 0 errors)" in result.out
    assert "Progress: 1 / 2" not in result.out.replace("Progress: 2 / 2", "")


def test_at43_nested_folders_created_once(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "sub dir/nested/x.txt", "sub dir/nested/y.txt", "sub dir/z.txt", "already_uploaded")
    client = FakeFolderServer()
    result = upload(client, tmp_path / "in", "/upload/", threads=3)
    created = [(r.endpoint, r.data["name"]) for r in client.requests if r.endpoint.endswith("/subfolders")]
    assert created == [("/folders/12345/subfolders", "sub dir"), ("/folders/id-sub dir/subfolders", "nested")]
    uploads = posts(client, "/files")
    assert uploads.count("/folders/id-nested/files") == 2
    assert uploads.count("/folders/id-sub dir/files") == 1
    assert len(uploads) == 3
    assert "Uploaded 3 files, skipped 1 files, 0 errors." in result.out
    assert result.code == 0


def test_at43_counters_and_targets(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "a.txt", "sub dir/nested/x.txt", "sub dir/nested/y.txt", "sub dir/z.txt")
    client = FakeFolderServer()
    result = upload(client, tmp_path / "in", "/upload/")
    uploads = posts(client, "/files")
    assert uploads.count("/folders/12345/files") == 1
    assert uploads.count("/folders/id-nested/files") == 2
    assert uploads.count("/folders/id-sub dir/files") == 1
    assert len(posts(client, "/subfolders")) == 2
    assert "Uploaded 4 files, skipped 0 files, 0 errors." in result.out


def test_at44_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(TransferToolException, match="is not a directory, terminating"):
        upload(FakeFolderServer(), tmp_path / "missing", "/upload")


def test_upload_error_status(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "new_file_1")
    client = FakeFolderServer()
    client.failures["/folders/12345/files"] = FakeResponse("denied", status_code=403)
    result = upload(client, tmp_path / "in", "/upload")
    assert f"Error: could not upload {tmp_path / 'in' / 'new_file_1'} (HTTP 403): denied" in result.out
    assert "Uploaded 0 files, skipped 0 files, 1 errors." in result.out
    assert result.code == 1


def test_failed_folder_is_retried_for_each_folder_below(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "bad/a.txt", "bad/deeper/b.txt")
    client = FakeFolderServer()
    client.failures["/folders/12345/subfolders"] = FakeResponse("no", status_code=500)
    result = upload(client, tmp_path / "in", "/upload")
    assert result.out.count("Error: could not create folder /upload/bad (HTTP 500): no") == 2
    assert "Error: remote folder /upload/bad could not be created, skipping" in result.out
    assert "Error: remote folder /upload/bad/deeper could not be created, skipping" in result.out
    assert "Uploaded 0 files, skipped 0 files, 2 errors." in result.out
    assert result.code == 1


def test_ki15_ancestors_not_found(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "a.txt")
    result_error = None
    try:
        upload(FakeFolderServer(), tmp_path / "in", "/nowhere/deep")
    except TransferToolException as error:
        result_error = str(error)
    assert result_error == "Error: remote folder /nowhere/deep doesn't exist, and its parent folder could not be found."


def test_ki15_summary_still_printed(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "a.txt")
    out = io.StringIO()
    args = make_args(input_dir=str(tmp_path / "in"), remote_dir="/nowhere")
    with pytest.raises(TransferToolException):
        upload_command(args, FakeFolderServer(), CliDeps(stdout=out, stderr=io.StringIO()))
    assert "Uploaded 0 files, skipped 0 files, 0 errors, 1 files not transferred." in out.getvalue()


def test_quiet_hides_found_and_progress_but_not_summary(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "new_file_1")
    result = upload(FakeFolderServer(), tmp_path / "in", "/upload", quiet=True)
    assert result.out == "Uploaded 1 files, skipped 0 files, 0 errors.\n"


def test_folder_cache_create_folder_uses_cache() -> None:
    client = FakeFolderServer()
    session = TransferSession(1, True, out=io.StringIO(), err=io.StringIO())
    cache = FolderCache(client, session, {"/upload": "12345"})
    assert cache.create_folder("/upload") == "12345"
    assert cache.create_folder("/upload/a/b") == "id-b"
    assert cache.get("/upload/a") == "id-a"
    assert cache.create_folder("/upload/a/b") == "id-b"
    assert len(posts(client, "/subfolders")) == 2


def test_list_local_files_symlinks(tmp_path: Path) -> None:
    make_files(tmp_path, "a.txt", "d/b.txt")
    try:
        (tmp_path / "link.txt").symlink_to(tmp_path / "a.txt")
        (tmp_path / "linkdir").symlink_to(tmp_path / "d", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available")
    names = [p.relative_to(tmp_path).as_posix() for p in list_local_files(tmp_path)]
    assert names == ["a.txt", "link.txt", "d/b.txt"]


# --- Download ---


def test_at39_download_remote_dir(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "good_file_1").write_bytes(DUMMY)
    (out / "good_file_2").write_bytes(b"x" * 13)
    client = FakeFolderServer()
    result = download(client, "/remote", out, filter=re.compile("good_file"), threads=2)
    for name in ("good_file_1", "good_file_2", "sub/good_file_3"):
        assert (out / name).read_bytes() == DUMMY
    assert not (out / "bad_file").exists()
    assert not list(out.rglob("good_file_4"))
    assert "Found 3 files to download." in result.out
    assert "Downloaded 2 files, skipped 1 files, 0 errors." in result.out
    assert result.code == 0


def test_download_unsafe_path_aborts_everything(tmp_path: Path) -> None:
    client = FakeFolderServer()
    client.add_json(
        "GET",
        "/files",
        {
            "items": [
                {"id": "good_file_1", "path": "/remote/a", "size": 13},
                {"id": "x", "path": "/remote/../x", "size": 1},
            ]
        },
    )
    out = tmp_path / "out"
    with pytest.raises(TransferToolException, match=r"refusing to download /remote/\.\./x"):
        download(client, "/remote", out)
    assert not out.exists()
    assert not [r for r in client.requests if r.endpoint.endswith("/download")]


def test_download_quiet_and_error_exit_code(tmp_path: Path) -> None:
    client = FakeFolderServer()
    client.failures["/files/good_file_1/download"] = FakeResponse(b"", status_code=500)
    result = download(client, "/remote", tmp_path / "out", filter=re.compile("good_file_1"), quiet=True)
    assert result.out.splitlines()[0].startswith("Error: could not download")
    assert result.out.splitlines()[-1] == "Downloaded 0 files, skipped 0 files, 1 errors."
    assert "Found" not in result.out
    assert result.code == 1


def test_at46_download_sign_in_expired(tmp_path: Path) -> None:
    client = FakeFolderServer()
    for name in ("good_file_1", "bad_file", "good_file_2", "good_file_3"):
        client.failures[f"/files/{name}/download"] = SignInExpiredError("expired")
    out = io.StringIO()
    args = make_args(remote_dir="/remote", output_dir=str(tmp_path / "out"))
    with pytest.raises(SignInExpiredError):
        download_command(args, client, CliDeps(stdout=out, stderr=io.StringIO()))
    assert "Downloaded 0 files, skipped 0 files, 0 errors, 4 files not transferred." in out.getvalue()


# --- Through the command line ---


class Provider:
    def get_access_token(self) -> str:
        return "tok"

    def disable_login(self) -> None:
        pass

    def start_keep_alive(self) -> None:
        pass

    def stop_keep_alive(self) -> None:
        pass


def run_main(argv: list[str], client: FakeApiClient, tmp_path: Path) -> Result:
    out, err = io.StringIO(), io.StringIO()

    def connect(_settings: Settings) -> tuple[TokenProviderLike, ApiClient]:
        return Provider(), client

    deps = CliDeps(
        environ={"MOVEIT_ENV_FILE": str(tmp_path / "none.env"), "HOME": str(tmp_path)},
        stdout=out,
        stderr=err,
        connect=connect,
    )
    code = main([*argv, "--url", "https://moveit.example.com"], deps)
    return Result(code, out.getvalue(), err.getvalue())


def test_at7_upload_sign_in_expired_end_to_end(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "new_file_1", "new_file_2")
    client = FakeFolderServer()
    client.failures["/folders/12345/files"] = SignInExpiredError("expired")
    result = run_main(["upload", str(tmp_path / "in"), "/upload"], client, tmp_path)
    assert result.code == 3
    assert "./scripts/login.sh" in result.err
    assert "Uploaded 0 files, skipped 0 files, 0 errors, 2 files not transferred." in result.out


def test_ki3_network_error_while_listing(tmp_path: Path) -> None:
    make_files(tmp_path / "in", "new_file_1")
    client = FakeFolderServer()
    client.failures["/files"] = ConnectionError("connection refused")
    result = run_main(["upload", str(tmp_path / "in"), "/upload"], client, tmp_path)
    assert result.code == 1
    assert result.err.strip() == "Error: ConnectionError: connection refused"


def test_default_commands_are_wired(tmp_path: Path) -> None:
    client = FakeFolderServer()
    result = run_main(["download", "/remote", str(tmp_path / "out"), "--filter", "good_file_1", "-q"], client, tmp_path)
    assert result.code == 0
    assert (tmp_path / "out" / "good_file_1").read_bytes() == DUMMY
