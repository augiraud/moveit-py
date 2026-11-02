"""Check a package before uploading its attachments and sending it.

The checks raise PackageNotSentError before anything has been uploaded. Uploading and sending are handled in packages.create.
"""

import argparse
import os
import shutil
import tempfile
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from moveit_py.api import ApiClient, get_all_pages
from moveit_py.config import MAX_ATTACHMENT_SIZE
from moveit_py.errors import PackageNotSentError
from moveit_py.packages.create import PackageSender

if TYPE_CHECKING:  # cli.py imports the package commands
    from moveit_py.cli import CliDeps

NO_LIMIT = -1
HOURS_PER_DAY = 24
GIGABYTE = 1024**3
MEGABYTE = 1024**2


@dataclass(frozen=True)
class ClassificationType:
    """An entry of `classificationTypes`."""

    id: Any
    value: str


@dataclass(frozen=True)
class Requirements:
    """Limits and permissions returned by the server.

    A missing number is -1. Missing permissions and classification requirements are treated as false.
    """

    allow_send_packages: bool = False
    allow_attach_files: bool = False
    max_files_per_package: int = NO_LIMIT
    default_expiration_hours: int = NO_LIMIT
    max_expiration_hours: int = NO_LIMIT
    default_downloads: int = NO_LIMIT
    max_downloads: int = NO_LIMIT
    default_secure_note: bool = False
    allow_change_secure_note: bool = False
    classification_required: bool = False
    classification_types: tuple[ClassificationType, ...] = ()


def _number(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return NO_LIMIT if value is None else int(value)


def get_requirements(client: ApiClient) -> Requirements:
    """`GET /packages/requirements`."""
    data: dict[str, Any] = client.rest_call_json("/packages/requirements")
    return Requirements(
        allow_send_packages=bool(data.get("allowSendPackages", False)),
        allow_attach_files=bool(data.get("allowAttachFiles", False)),
        max_files_per_package=_number(data, "maxFilesPerPackageLimit"),
        default_expiration_hours=_number(data, "defaultUserPackageExpirationHours"),
        max_expiration_hours=_number(data, "maxUserPackageExpirationHours"),
        default_downloads=_number(data, "defaultAttachmentDownloads"),
        max_downloads=_number(data, "maxAttachmentDownloads"),
        default_secure_note=bool(data.get("defaultSecureNote", False)),
        allow_change_secure_note=bool(data.get("allowChangeSecureNote", False)),
        classification_required=bool(data.get("isClassificationFieldRequired", False)),
        classification_types=tuple(
            ClassificationType(item.get("id"), str(item.get("value", "")))
            for item in data.get("classificationTypes") or []
        ),
    )


def format_size(size: int) -> str:
    """`1.00 GB` from 0.1 GB on, else `56.3 MB`."""
    if size * 10 >= GIGABYTE:
        return f"{size / GIGABYTE:.2f} GB"
    return f"{size / MEGABYTE:.1f} MB"


def zip_directory(directory: Path, temp_dir: Path) -> Path:
    """Zip `directory` into `{temp_dir}/{name}.zip`, entries stored under the directory name (`results/sub/a.txt`)."""
    resolved = directory.resolve()
    name = resolved.name or "root"
    target = temp_dir / f"{name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, dirnames, filenames in os.walk(resolved, followlinks=False):
            dirnames.sort()
            for filename in sorted(filenames):
                file = Path(root) / filename
                if file.is_file():  # links to files are included by content; broken links are skipped
                    archive.write(file, Path(name) / file.relative_to(resolved))
    return target


def prepare_attachments(paths: Sequence[str], temp_dir: Path, max_size: int = MAX_ATTACHMENT_SIZE) -> list[Path]:
    """Get the files to upload, zipping directories and checking names and sizes."""
    attachments: list[Path] = []
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            attachments.append(candidate)
        elif candidate.is_dir():
            attachments.append(zip_directory(candidate, temp_dir))
        else:
            raise PackageNotSentError(f"Error: {path} is neither a file nor a directory, nothing was sent.")
    check_names(attachments)
    check_sizes(attachments, max_size)
    return attachments


def check_names(attachments: Sequence[Path]) -> None:
    """Refuse several attachments with the same file name."""
    counts = Counter(attachment.name for attachment in attachments)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise PackageNotSentError(
            f"Error: several attachments would be named {', '.join(duplicates)}, nothing was sent."
        )


def check_sizes(attachments: Sequence[Path], max_size: int) -> None:
    """Refuse an attachment larger than `max_size`."""
    for attachment in attachments:
        size = attachment.stat().st_size
        if size > max_size:
            raise PackageNotSentError(
                f"Error: {attachment.name} is {format_size(size)}, more than the maximum of {format_size(max_size)}, "
                "nothing was sent."
            )


def check_limit(value: int | None, default: int, maximum: int, message: str) -> int | None:
    """Check a value against the server limits, or return the default if it was not given."""
    if value is None:
        return default if default > 0 else None
    if value < 1 or (maximum > 0 and value > maximum):
        raise PackageNotSentError(message)
    return value


def check_permissions(attachment_count: int, requirements: Requirements) -> None:
    """The account can send packages with files, and not too many."""
    if not (requirements.allow_send_packages and requirements.allow_attach_files):
        raise PackageNotSentError("Error: your account isn't allowed to send packages with files.")
    maximum = requirements.max_files_per_package
    if maximum >= 0 and attachment_count > maximum:
        raise PackageNotSentError(f"Error: at most {maximum} files can be sent in a package, not {attachment_count}.")


def check_expiration(expires_days: int | None, requirements: Requirements) -> int | None:
    """The `expirationHours` to send, if any."""
    maximum = requirements.max_expiration_hours
    if maximum > 0:
        message = f"Error: packages must expire after 1 to {maximum // HOURS_PER_DAY} days."
    else:
        message = "Error: packages must expire after at least 1 day."
    hours = None if expires_days is None else expires_days * HOURS_PER_DAY
    return check_limit(hours, requirements.default_expiration_hours, maximum, message)


def check_downloads(max_downloads: int | None, requirements: Requirements) -> int | None:
    """The `maxAttachDownloads` to send, if any."""
    maximum = requirements.max_downloads
    if maximum > 0:
        message = f"Error: files must be downloadable 1 to {maximum} times."
    else:
        message = "Error: files must be downloadable at least once."
    return check_limit(max_downloads, requirements.default_downloads, maximum, message)


def find_classification(given: str | None, requirements: Requirements) -> ClassificationType | None:
    """The classification type called `given` (case-insensitive); None if none was given and none is required."""
    names = ", ".join(item.value for item in requirements.classification_types)
    if given is None:
        if requirements.classification_required:
            raise PackageNotSentError(f"Error: a classification is required (available: {names}).")
        return None
    for item in requirements.classification_types:
        if item.value.casefold() == given.casefold():
            return item
    raise PackageNotSentError(f"Error: unknown classification {given} (available: {names}).")


def check_secure_note(secure_note: bool | None, requirements: Requirements) -> None:
    """Changing whether the note is secure needs the permission."""
    if (
        secure_note is not None
        and secure_note != requirements.default_secure_note
        and not requirements.allow_change_secure_note
    ):
        raise PackageNotSentError("Error: your account isn't allowed to change whether the note is secure.")


def package_fields(
    subject: str,
    note: str | None,
    secure_note: bool | None,
    expires_days: int | None,
    max_downloads: int | None,
    classification: str | None,
    attachment_count: int,
    requirements: Requirements,
) -> dict[str, Any]:
    """Check the options against the server requirements and return the package fields.

    The caller replaces an empty body with the file names.
    """
    check_permissions(attachment_count, requirements)
    hours = check_expiration(expires_days, requirements)
    downloads = check_downloads(max_downloads, requirements)
    classification_type = find_classification(classification, requirements)
    check_secure_note(secure_note, requirements)
    fields: dict[str, Any] = {"subject": subject, "body": note or ""}
    if hours is not None:
        fields["expirationHours"] = hours
    if downloads is not None:
        fields["maxAttachDownloads"] = downloads
    if classification_type is not None:
        fields["packageClassificationTypeId"] = classification_type.id
    if secure_note is not None:
        fields["isSecureBody"] = secure_note
    return fields


def resolve_recipient(client: ApiClient, email: str, role: str) -> dict[str, Any]:
    """Find a user with this exact email, or use an external recipient."""
    contacts = get_all_pages(client, "/contacts", {"searchString": email})
    matches = [c for c in contacts if str(c.get("email") or "").casefold() == email.casefold()]
    matches.sort(key=lambda contact: contact.get("type") != "User")  # stable: users first
    if matches:
        return {"type": matches[0]["type"], "role": role, "identifier": matches[0]["identifier"]}
    return {"type": "Unreg", "role": role, "identifier": email}


def resolve_recipients(
    client: ApiClient, to: Sequence[str], cc: Sequence[str], bcc: Sequence[str]
) -> list[dict[str, Any]]:
    """Resolve To, CC and BCC recipients. At least one To recipient is required."""
    if not to:
        raise PackageNotSentError("Error: the package needs at least one recipient, nothing was sent.")
    return [
        resolve_recipient(client, email, role)
        for role, emails in (("To", to), ("CC", cc), ("BCC", bcc))
        for email in emails
    ]


def build_payload(
    fields: dict[str, Any], recipients: list[dict[str, Any]], attachments: Sequence[Path], attachment_ids: Sequence[str]
) -> dict[str, Any]:
    """Build the package request, using the file names if the body is empty."""
    body = fields["body"] or f"Files: {', '.join(attachment.name for attachment in attachments)}"
    return {
        "type": "General",
        **fields,
        "body": body,
        "recipients": recipients,
        "attachments": [{"id": attachment_id} for attachment_id in attachment_ids],
    }


def send_command(args: argparse.Namespace, client: ApiClient, deps: CliDeps) -> int:
    """Send a package and return the exit code.

    Remove the temporary directory after uploading the attachments or on failure.
    """
    requirements = get_requirements(client)
    temp_dir = Path(tempfile.mkdtemp(prefix="moveit-package-"))
    sender = PackageSender(client, deps.stdout, deps.stderr)
    attachment_ids: list[str] = []
    try:
        try:
            attachments = prepare_attachments(args.paths, temp_dir)
            fields = package_fields(
                args.subject,
                args.note,
                args.secure_note,
                args.expires_days,
                args.max_downloads,
                args.classification,
                len(attachments),
                requirements,
            )
            recipients = resolve_recipients(client, args.to, args.cc, args.bcc)
            attachment_ids = sender.upload_attachments(attachments, args.threads, args.quiet)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        payload = build_payload(fields, recipients, attachments, attachment_ids)
    except BaseException:
        sender.delete_attachments(attachment_ids)  # nothing when the uploads themselves failed: they cleaned up
        raise
    return sender.create_package(payload, attachment_ids)
