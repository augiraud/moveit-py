import pytest
from fakes import FakeResponse

from moveit_py.api import json_from_response
from moveit_py.errors import (
    AuthenticationError,
    PackageNotSentError,
    ResponseException,
    SignInExpiredError,
    TransferToolException,
    TransientAuthenticationError,
)


def test_hierarchy() -> None:
    assert issubclass(TransientAuthenticationError, AuthenticationError)
    assert issubclass(SignInExpiredError, AuthenticationError)
    assert issubclass(PackageNotSentError, TransferToolException)
    assert not issubclass(ResponseException, TransferToolException)


def test_json_from_response_ok() -> None:
    assert json_from_response(FakeResponse(json_body={"a": 1}), "GET", "/x") == {"a": 1}


def test_json_from_response_waf_page() -> None:
    with pytest.raises(ResponseException) as info:
        json_from_response(FakeResponse("<html>Request Rejected</html>" + "x" * 400), "GET", "/x")
    assert str(info.value) == "Unexpected response to GET /x (HTTP 200): <html>Request Rejected</html>" + "x" * 271


def test_json_from_response_error_status() -> None:
    with pytest.raises(ResponseException) as info:
        json_from_response(FakeResponse(json_body={"error": "x"}, status_code=404), "POST", "/y")
    assert str(info.value) == 'Error response to POST /y (HTTP 404): {"error": "x"}'
