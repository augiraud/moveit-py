"""List packages in the Inbox or Sent mailbox."""

import argparse
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from moveit_py.api import ApiClient, get_all_pages
from moveit_py.packages.mailboxes import find_mailbox

if TYPE_CHECKING:  # cli.py imports the package commands
    from moveit_py.cli import CliDeps

PEOPLE_WIDTH = 30
DATE_WIDTH = 16


def people_of(item: Mapping[str, Any], mailbox_name: str) -> str:
    """The recipients (Sent) or the sender (Inbox) of a package list item."""
    if mailbox_name == "Sent":
        return ", ".join(str(name) for name in item.get("toRecipients") or [])
    sender = item.get("sender") or {}
    return str(sender.get("fullName") or sender.get("email") or "")


def format_package_line(item: Mapping[str, Any], mailbox_name: str) -> str:
    """`{new} {id:>10}  {date}  {people:<30}  {subject}`."""
    new = "*" if item.get("isNew") and mailbox_name == "Inbox" else " "
    date = str(item.get("sendStamp") or "").replace("T", " ")[:DATE_WIDTH]
    people = people_of(item, mailbox_name)[:PEOPLE_WIDTH]
    return f"{new} {item['id']!s:>10}  {date}  {people:<{PEOPLE_WIDTH}}  {item.get('subject') or ''}"


def packages_command(args: argparse.Namespace, client: ApiClient, deps: CliDeps) -> int:
    """`packages [--sent] [--new]`; returns the exit code."""
    mailbox_name = "Sent" if args.sent else "Inbox"
    params: dict[str, Any] = {
        "mailboxId": find_mailbox(client, mailbox_name),
        "sortField": "sendStamp",
        "sortDirection": "desc",
    }
    if args.new:
        params["newOnly"] = "true"
    items = get_all_pages(client, "/packages", params)
    if not items:
        print(f"No packages in {mailbox_name}.", file=deps.stdout)
    for item in items:
        print(format_package_line(item, mailbox_name), file=deps.stdout)
    return 0
