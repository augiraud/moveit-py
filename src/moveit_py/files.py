"""Local file paths, hashes and downloads."""

import contextlib
import hashlib
import os
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from moveit_py.config import DOWNLOAD_CHUNK_SIZE, HASH_CHUNK_SIZE
from moveit_py.engine import Outcome, TransferSession
from moveit_py.errors import ResponseException, SignInExpiredError, TransferToolException

if TYPE_CHECKING:  # api.py imports this module (sha256_file)
    from moveit_py.api import ApiClient, Response


def sha256_file(path: Path) -> str:
    """Lowercase hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sha1_file(path: Path) -> str | None:
    """Get the SHA-1 hash of a file, or None if it does not exist.

    The server uses SHA-1 to identify stored files. This is not a security check.
    """
    digest = hashlib.sha1(usedforsecurity=False)
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(DOWNLOAD_CHUNK_SIZE):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def remote_sha1(client: ApiClient, file_id: str) -> str:
    """The SHA-1 of a remote file (`GET /files/{id}`, field `hash`); ResponseException if the field is missing."""
    info = client.rest_call_json(f"/files/{file_id}")
    value = info.get("hash") if isinstance(info, Mapping) else None
    if not value or not isinstance(value, str):
        raise ResponseException("Hash not found in response.")
    return value


# --- Safe local paths ---


def _is_unsafe_part(part: str) -> bool:
    """Check path components against both POSIX and Windows rules."""
    if part == ".." or "\\" in part or ":" in part or "\x00" in part:
        return True
    return any(
        flavour(part).is_absolute() or flavour(part).drive or flavour(part).root
        for flavour in (PurePosixPath, PureWindowsPath)
    )


def local_path_for(remote_path: str, remote_dir: str, output_dir: Path) -> Path | None:
    """Map a remote path to a local path under output_dir, or return None if it is unsafe.

    Reject paths outside remote_dir, unsafe path components and paths that resolve outside output_dir, including through symlinks. Ignore empty and dot components.
    """
    prefix = remote_dir.rstrip("/") + "/"
    if not remote_path.startswith(prefix):
        return None
    parts = [part for part in remote_path[len(prefix) :].split("/") if part not in {"", "."}]
    if not parts or any(_is_unsafe_part(part) for part in parts):
        return None
    result = output_dir.joinpath(*parts)
    try:
        inside = result.resolve().is_relative_to(output_dir.resolve())
    except ValueError, OSError:
        return None
    return result if inside else None


def checked_local_path(remote_path: str, remote_dir: str, output_dir: Path) -> Path:
    """Map a remote path to a local path, raising an error if it is unsafe."""
    result = local_path_for(remote_path, remote_dir, output_dir)
    if result is None:
        raise TransferToolException(f"Error: refusing to download {remote_path} outside of {output_dir}")
    return result


def is_safe_attachment_name(name: str) -> bool:
    """Check that an attachment name is a plain file name.

    Reject empty names, . and .., separators, NUL, absolute paths and Windows drive forms. A colon elsewhere in the name is allowed.
    """
    if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        return False
    return not any(
        flavour(name).drive or flavour(name).is_absolute() or flavour(name).root
        for flavour in (PurePosixPath, PureWindowsPath)
    )


# --- Download if not present ---


def _remote_size(remote: Mapping[str, Any]) -> int | None:
    try:
        return int(remote["size"])
    except KeyError, TypeError, ValueError:
        return None


def _same_as_remote(
    client: ApiClient, session: TransferSession, remote: Mapping[str, Any], local_path: Path
) -> bool | None:
    """True if the local file is identical to the remote one, None if the remote hash can't be obtained."""
    local_hash = sha1_file(local_path)
    try:
        remote_hash = remote_sha1(client, str(remote["id"]))
    except SignInExpiredError:
        raise
    except Exception:  # noqa: BLE001 - any failure is reported the same way
        session.message(f"Error: could not get SHA1 of remote file {remote['path']}")
        return None
    return local_hash is not None and local_hash == remote_hash.lower()


def download_if_not_present(
    client: ApiClient, session: TransferSession, remote: Mapping[str, Any], local_path: Path
) -> Outcome:
    """Download a remote file unless an identical local file exists."""
    size = _remote_size(remote)
    if size is not None and local_path.is_file() and local_path.stat().st_size == size:
        same = _same_as_remote(client, session, remote, local_path)
        if same is None:
            return Outcome.ERROR
        if same:
            return Outcome.SKIPPED
    return _download(client, session, str(remote["id"]), size, local_path)


HTTP_ERROR_STATUS = 400


class _StreamError(Exception):
    """A network error while reading a response, as distinct from a local I/O error."""

    def __init__(self, error: Exception) -> None:
        super().__init__(error)
        self.error = error


def _chunks(response: Response) -> Iterator[bytes]:
    """Read the response in chunks, wrapping network errors to distinguish them from local I/O errors."""
    try:
        yield from response.iter_content(DOWNLOAD_CHUNK_SIZE)
    except OSError as error:
        raise _StreamError(error) from error


def _download(client: ApiClient, session: TransferSession, file_id: str, size: int | None, local_path: Path) -> Outcome:
    response = client.rest_call(f"/files/{file_id}/download", stream=True)
    part = local_path.with_name(local_path.name + ".part")
    try:
        if response.status_code >= HTTP_ERROR_STATUS:
            session.message(f"Error: could not download {local_path} (HTTP {response.status_code})")
            return Outcome.ERROR
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with part.open("wb") as stream:
                for chunk in _chunks(response):
                    stream.write(chunk)
            actual = part.stat().st_size
            if size is not None and actual != size:
                session.message(f"Error: downloaded {actual} bytes instead of {size} for {local_path}")
                return Outcome.ERROR
            os.replace(part, local_path)
        except _StreamError as error:
            raise error.error from None
        except OSError as error:
            session.message(f"Error: could not write to {local_path}: {error}")
            return Outcome.ERROR
        return Outcome.TRANSFERRED
    finally:
        response.close()
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)  # remove a partial download after failure
