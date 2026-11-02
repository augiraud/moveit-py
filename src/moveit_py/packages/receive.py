"""Download the files of a package."""

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from moveit_py.api import ApiClient
from moveit_py.engine import TransferSession
from moveit_py.errors import SignInExpiredError, TransferToolException
from moveit_py.files import download_if_not_present, is_safe_attachment_name
from moveit_py.folders import order_new_first
from moveit_py.packages.mailboxes import find_mailbox, get_package

if TYPE_CHECKING:  # cli.py imports the package commands
    from moveit_py.cli import CliDeps


def find_package(client: ApiClient, package_id: str) -> dict[str, Any]:
    """Find a package in the Inbox or Sent mailbox.

    If the Inbox lookup fails, try Sent. Do not treat an expired sign-in as a missing package.
    """
    for mailbox in ("Inbox", "Sent"):
        try:
            return get_package(client, package_id, find_mailbox(client, mailbox))
        except SignInExpiredError:
            raise
        except Exception:  # noqa: BLE001, S112 - try the next mailbox
            continue
    raise TransferToolException(f"Error: package {package_id} not found in your Inbox or Sent mailboxes.")


def check_attachments(attachments: Sequence[Mapping[str, Any]]) -> None:
    """Reject unsafe or duplicate attachment names before downloading anything."""
    for attachment in attachments:
        name = attachment.get("name")
        if not isinstance(name, str) or not is_safe_attachment_name(name):
            raise TransferToolException(f"Error: refusing to download an attachment named {name!r}")
    counts = Counter(str(attachment["name"]) for attachment in attachments)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise TransferToolException(
            f"Error: several attachments are named {', '.join(duplicates)}, nothing was downloaded."
        )


def receive_command(args: argparse.Namespace, client: ApiClient, deps: CliDeps) -> int:
    """`receive PACKAGE_ID OUTPUT_DIR`; returns the exit code."""
    package = find_package(client, args.package_id)
    attachments = package.get("attachments") or []
    check_attachments(attachments)
    output_dir = Path(args.output_dir)
    pairs = order_new_first(
        (
            {"id": attachment["id"], "path": attachment["name"], "size": attachment.get("size")},
            output_dir / attachment["name"],
        )
        for attachment in attachments
    )
    if not args.quiet:
        print(f"Package {args.package_id}: {package.get('subject') or ''}, {len(pairs)} files.", file=deps.stdout)
    output_dir.mkdir(parents=True, exist_ok=True)
    session = TransferSession(args.threads, args.quiet, out=deps.stdout, err=deps.stderr)
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
