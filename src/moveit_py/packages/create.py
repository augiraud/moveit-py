"""Upload attachments and send a package.

If the server response is unclear, check the Sent mailbox before telling the user whether it is safe to send again. Delete attachments when the package was not sent, as far as possible.
"""

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from moveit_py.api import ApiClient, Response
from moveit_py.config import RECENT_SENT_PACKAGES
from moveit_py.engine import Outcome, TransferSession
from moveit_py.errors import PackageNotSentError, SignInExpiredError, TransferToolException
from moveit_py.packages.mailboxes import find_mailbox, get_package

HTTP_CREATED = 201
HTTP_ERROR = 400
ERROR_EXCERPT = 300
CHECK_YOUR_SENT_PACKAGES = "check your sent packages (./scripts/package-list.sh --sent) before sending it again."


def could_not_tell(problem: str) -> str:
    """The message when it can't be known whether the package was sent."""
    return f"Error: could not tell whether the package was sent ({problem}): {CHECK_YOUR_SENT_PACKAGES}"


def describe_problem(response: Response) -> str:
    """What went wrong according to a response to `POST /packages`."""
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        if "Request Rejected" in response.text:
            return f"HTTP {status}, rejected by the firewall in front of the server"
        return f"HTTP {status}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return f"HTTP {status}: {json.dumps(body) if detail is None else detail}"


def _response_id(response: Response, status: int) -> str | None:
    """The `id` of a JSON response with the expected status, as a string; None if there is no usable one."""
    if response.status_code != status:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("id") if isinstance(body, dict) else None
    return None if value is None or value == "" else str(value)


def find_sent_package(client: ApiClient, subject: str, attachment_ids: set[str]) -> str | None:
    """The ID of the recent Sent package with this subject and one of these attachments, if any."""
    sent = find_mailbox(client, "Sent")
    page = client.rest_call_json(
        "/packages",
        params={"mailboxId": sent, "sortField": "sendStamp", "sortDirection": "desc", "perPage": RECENT_SENT_PACKAGES},
    )
    for item in page.get("items") or []:
        if item.get("subject") != subject:
            continue
        package = get_package(client, item["id"], sent)
        found = {str(attachment.get("id")) for attachment in package.get("attachments") or []}
        if found & attachment_ids:
            return str(item["id"])
    return None


class PackageSender:
    """Upload attachments and send a package."""

    def __init__(self, client: ApiClient, out: IO[str], err: IO[str]) -> None:
        self._client = client
        self._out = out
        self._err = err

    def delete_attachments(self, attachment_ids: Sequence[str]) -> None:
        """Delete attachments as far as possible, warning about any failures."""
        for attachment_id in attachment_ids:
            try:
                response = self._client.rest_call(f"/packages/attachments/{attachment_id}", method="DELETE")
                status = response.status_code
                response.close()
                problem = f"HTTP {status}" if status >= HTTP_ERROR else ""
            except Exception as error:  # noqa: BLE001 - best effort, whatever the failure
                problem = str(error) or type(error).__name__
            if problem:
                print(f"Warning: could not delete the uploaded attachment {attachment_id}: {problem}", file=self._err)

    def upload_attachments(self, attachments: Sequence[Path], threads: int, quiet: bool) -> list[str]:
        """Upload attachments and return their IDs in order.

        If an upload fails or is interrupted, delete the attachments already uploaded.
        """
        session = TransferSession(threads, quiet, out=self._out, err=self._err)
        session.total = len(attachments)
        uploaded: dict[Path, str] = {}
        lock = threading.Lock()

        def upload(attachment: Path) -> Outcome:
            attachment_id = self._upload_one(session, attachment)
            if attachment_id is None:
                return Outcome.ERROR
            with lock:
                uploaded[attachment] = attachment_id
            return Outcome.TRANSFERRED

        try:
            session.transfer_multithread(attachments, upload, lambda attachment: attachment.name)
            if len(uploaded) < len(attachments):
                raise PackageNotSentError("Error: some files could not be uploaded, nothing was sent.")
        except BaseException:
            self.delete_attachments([uploaded[attachment] for attachment in attachments if attachment in uploaded])
            raise
        return [uploaded[attachment] for attachment in attachments]

    def _upload_one(self, session: TransferSession, attachment: Path) -> str | None:
        """The ID of the uploaded attachment; None (after the error message) if the upload was refused."""
        response = self._client.rest_call("/packages/attachments", method="POST", file=attachment)
        try:
            attachment_id = _response_id(response, HTTP_CREATED)
            if attachment_id is None:
                excerpt = response.text[:ERROR_EXCERPT]
                session.message(f"Error: could not upload {attachment.name} (HTTP {response.status_code}): {excerpt}")
            return attachment_id
        finally:
            response.close()

    def create_package(self, payload: dict[str, Any], attachment_ids: Sequence[str]) -> int:
        """Send the package. Raise an error if it was not sent or its status is unknown."""
        try:
            response = self._client.rest_call("/packages", data=payload, method="POST", app_json=True)
        except SignInExpiredError:
            self.delete_attachments(attachment_ids)  # the package was not created: the request wasn't (fully) sent
            raise
        except KeyboardInterrupt:
            print(could_not_tell("interrupted"), file=self._err)  # the user wants to stop: don't check, keep files
            raise
        except Exception as error:  # noqa: BLE001 - no response, whatever the reason
            return self._check_sent(payload["subject"], attachment_ids, f"no response: {error}")
        try:
            package_id = _response_id(response, HTTP_CREATED)
            problem = "" if package_id is not None else describe_problem(response)
        finally:
            response.close()
        if package_id is not None:
            print(f"Sent package {package_id}.", file=self._out)
            return 0
        return self._check_sent(payload["subject"], attachment_ids, problem)

    def _check_sent(self, subject: str, attachment_ids: Sequence[str], problem: str) -> int:
        """Check the Sent mailbox when the response does not confirm that the package was created."""
        try:
            package_id = find_sent_package(self._client, subject, set(attachment_ids))
        except KeyboardInterrupt:
            print(could_not_tell(problem), file=self._err)  # the package may exist: say so, and keep the attachments
            raise
        except Exception as error:  # whatever the failure of the check, the attachments are kept
            raise TransferToolException(could_not_tell(problem)) from error
        if package_id is not None:
            print(f"Sent package {package_id}.", file=self._out)
            return 0
        self.delete_attachments(attachment_ids)
        raise PackageNotSentError(f"Error: the package was not sent ({problem}).")
