"""Find mailboxes and retrieve packages."""

from typing import Any

from moveit_py.api import ApiClient
from moveit_py.errors import TransferToolException


def find_mailbox(client: ApiClient, name: str) -> int:
    """Find a mailbox by its exact name and return its ID."""
    for mailbox in client.rest_call_json("/mailboxes"):
        if mailbox.get("name") == name:
            return int(mailbox["id"])
    raise TransferToolException(f"Mailbox {name} not found")


def get_package(client: ApiClient, package_id: str | int, mailbox_id: int) -> dict[str, Any]:
    """Get a package from a mailbox. This marks it as read."""
    package: dict[str, Any] = client.rest_call_json(f"/packages/{package_id}", params={"mailboxId": mailbox_id})
    return package
