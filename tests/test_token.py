import pytest

from moveit_py.auth.token import Token


def test_at9_expiry_with_margin() -> None:
    token = Token.from_response({"access_token": "a", "refresh_token": "r", "expires_in": 1199}, now=1000.0)
    assert token.expires_at == 2199.0
    assert not token.is_expired(1000.0)
    assert not token.is_expired(2138.9)
    assert token.is_expired(2139.0)  # now >= expires_at - 60
    assert token.is_expired(3000.0)
    assert not token.is_expired(2198.0, margin=0)
    assert token.is_expired(2199.0, margin=0)


def test_json_round_trip() -> None:
    token = Token("a", "r", 12.5)
    assert token.to_json() == {"access_token": "a", "refresh_token": "r", "expires_at": 12.5}
    assert Token.from_json(token.to_json()) == token
    assert Token.from_json({"access_token": "a", "refresh_token": "r", "expires_at": 7}) == Token("a", "r", 7.0)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "x",
        {},
        {"access_token": "a", "refresh_token": "r"},
        {"access_token": "a", "refresh_token": "r", "expires_at": 1, "extra": 1},
        {"access_token": 1, "refresh_token": "r", "expires_at": 1},
        {"access_token": "a", "refresh_token": None, "expires_at": 1},
        {"access_token": "a", "refresh_token": "r", "expires_at": "1"},
        {"access_token": "a", "refresh_token": "r", "expires_at": True},
        {"access_token": "a", "refresh_token": "r", "expires_at": float("nan")},
        {"access_token": "a", "refresh_token": "r", "expires_at": float("inf")},
        {"access_token": "a", "refresh_token": "r", "expires_at": float("-inf")},
    ],
)
def test_from_json_rejects_malformed(data: object) -> None:
    assert Token.from_json(data) is None


def test_tokens_are_not_in_repr() -> None:
    token = Token("secret-access", "secret-refresh", 5.0)
    assert "secret" not in repr(token)
    assert "secret" not in str(token)
