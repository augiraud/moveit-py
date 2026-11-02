"""Upload and download MoveIt folders."""

import argparse
import os
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from moveit_py.api import ApiClient, get_all_pages
from moveit_py.engine import Outcome, TransferSession
from moveit_py.errors import TransferToolException
from moveit_py.files import checked_local_path, download_if_not_present

if TYPE_CHECKING:  # cli.py imports this module
    from moveit_py.cli import CliDeps

HTTP_CREATED = 201

type RemoteFile = Mapping[str, Any]


# --- Paths and selection ---


def remote_path_for(local_file: Path, input_dir: Path, remote_dir: str) -> str:
    """`{remote_dir without trailing /}/{path relative to input_dir, with / separators}`."""
    relative = local_file.relative_to(input_dir).as_posix()
    return f"{remote_dir.rstrip('/')}/{relative}"


def list_local_files(input_dir: Path) -> list[Path]:
    """List regular files in a stable order. Include links to files, but do not follow directory links."""
    found: list[Path] = []
    for root, dirs, files in os.walk(input_dir, followlinks=False):
        dirs.sort()
        found.extend(Path(root, name) for name in sorted(files) if Path(root, name).is_file())
    return found


def select_files(files: Iterable[RemoteFile], remote_dir: str, pattern: re.Pattern[str] | None) -> list[RemoteFile]:
    """The files under `remote_dir` whose path matches `pattern` (searched in the full remote path)."""
    prefix = remote_dir.rstrip("/") + "/"
    return [
        file for file in files if file["path"].startswith(prefix) and (pattern is None or pattern.search(file["path"]))
    ]


def order_new_first[T](pairs: Iterable[tuple[T, Path]]) -> list[tuple[T, Path]]:
    """Pairs whose local file doesn't exist first, then the others (stable)."""
    return sorted(pairs, key=lambda pair: pair[1].exists())


# --- Upload ---


class FolderCache:
    """Keep track of remote folders and create missing ones."""

    def __init__(self, client: ApiClient, session: TransferSession, folders: Mapping[str, str]) -> None:
        self._client = client
        self._session = session
        self._ids = dict(folders)

    def get(self, path: str) -> str | None:
        """The ID of a known folder."""
        return self._ids.get(path)

    def create_folder(self, path: str) -> str | None:
        """The ID of the folder `path`, created with its missing parents; None if one could not be created."""
        return self._create(path, path)

    def _create(self, path: str, origin: str) -> str | None:
        if path in self._ids:
            return self._ids[path]
        parent = posixpath.dirname(path)
        if parent == path:
            raise TransferToolException(
                f"Error: remote folder {origin} doesn't exist, and its parent folder could not be found."
            )
        parent_id = self._create(parent, origin)
        if parent_id is None:
            return None
        response = self._client.rest_call(
            f"/folders/{parent_id}/subfolders",
            method="POST",
            data={"name": posixpath.basename(path), "inheritPermissions": "None"},
            app_json=True,
        )
        try:
            if response.status_code != HTTP_CREATED:
                self._session.message(
                    f"Error: could not create folder {path} (HTTP {response.status_code}): {response.text}"
                )
                return None
            self._ids[path] = str(response.json()["id"])
            return self._ids[path]
        finally:
            response.close()


def upload_file(client: ApiClient, session: TransferSession, folders: FolderCache, item: tuple[Path, str]) -> Outcome:
    """Upload a local file to an existing remote folder."""
    local_file, remote_path = item
    parent = posixpath.dirname(remote_path)
    folder_id = folders.get(parent)
    if folder_id is None:
        session.message(f"Error: remote folder {parent} could not be created, skipping {local_file}")
        return Outcome.ERROR
    response = client.rest_call(f"/folders/{folder_id}/files", method="POST", file=local_file)
    try:
        if response.status_code == HTTP_CREATED:
            return Outcome.TRANSFERRED
        session.message(f"Error: could not upload {local_file} (HTTP {response.status_code}): {response.text}")
        return Outcome.ERROR
    finally:
        response.close()


def plan_upload(
    local_files: Sequence[Path], input_dir: Path, remote_dir: str, existing: Iterable[str]
) -> tuple[list[tuple[Path, str]], int]:
    """Get the files to upload and the number already on the server."""
    known = set(existing)
    pairs = [(file, remote_path_for(file, input_dir, remote_dir)) for file in local_files]
    todo = [pair for pair in pairs if pair[1] not in known]
    return todo, len(pairs) - len(todo)


def upload_command(args: argparse.Namespace, client: ApiClient, deps: CliDeps) -> int:
    """`upload INPUT_DIR REMOTE_DIR`; returns the exit code."""
    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        raise TransferToolException(f"Error: {args.input_dir} is not a directory, terminating.")
    session = TransferSession(args.threads, args.quiet, out=deps.stdout, err=deps.stderr)
    remote_files = get_all_pages(client, "/files")
    remote_folders = get_all_pages(client, "/folders")
    local_files = list_local_files(input_dir)
    todo, already = plan_upload(local_files, input_dir, args.remote_dir, (f["path"] for f in remote_files))
    # Count files already on the server without showing a progress line for each one
    session.total = len(local_files)
    for _ in range(already):
        session.count(Outcome.SKIPPED, show_progress=False)
    if not args.quiet:
        print(f"Found {len(todo)} files to upload.", file=deps.stdout)
    try:
        folders = FolderCache(client, session, {f["path"]: str(f["id"]) for f in remote_folders})
        for parent in sorted({posixpath.dirname(remote) for _, remote in todo}):
            folders.create_folder(parent)
        session.transfer_multithread(
            todo, lambda item: upload_file(client, session, folders, item), lambda i: str(i[0])
        )
    finally:
        session.summary("Uploaded")
    return 1 if session.errors else 0


# --- Download ---


def download_command(args: argparse.Namespace, client: ApiClient, deps: CliDeps) -> int:
    """`download REMOTE_DIR OUTPUT_DIR [--filter REGEX]`; returns the exit code."""
    session = TransferSession(args.threads, args.quiet, out=deps.stdout, err=deps.stderr)
    output_dir = Path(args.output_dir)
    selected = select_files(get_all_pages(client, "/files"), args.remote_dir, args.filter)
    pairs = order_new_first((f, checked_local_path(f["path"], args.remote_dir, output_dir)) for f in selected)
    if not args.quiet:
        print(f"Found {len(pairs)} files to download.", file=deps.stdout)
    output_dir.mkdir(parents=True, exist_ok=True)
    session.total = len(pairs)
    try:
        session.transfer_multithread(
            pairs,
            lambda pair: download_if_not_present(client, session, pair[0], pair[1]),
            lambda pair: str(pair[0]["path"]),
        )
    finally:
        session.summary("Downloaded")
    return 1 if session.errors else 0
