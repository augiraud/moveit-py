import json
import os
import threading
from pathlib import Path

import pytest

from moveit_py.auth.store import TokenStore
from moveit_py.auth.token import Token

A, B = "https://a.example.com", "https://b.example.com"
TA, TB = Token("a1", "r1", 100.5), Token("a2", "r2", 200.0)
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


def test_at12_two_servers_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.save(A, TA)
    store.save(B, TB)
    other = TokenStore(path)
    assert other.load(A) == TA
    assert other.load(B) == TB
    assert other.load("https://c.example.com") is None
    assert other.load_all() == {A: TA, B: TB}
    assert json.loads(path.read_text())[A]["access_token"] == "a1"
    assert '\n  "' in path.read_text()


@posix_only
def test_at12_file_mode_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    TokenStore(path).save(A, TA)
    assert path.stat().st_mode & 0o777 == 0o600


@posix_only
def test_at12_wider_existing_file_becomes_0600(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("{}")
    path.chmod(0o644)
    TokenStore(path).save(A, TA)
    assert path.stat().st_mode & 0o777 == 0o600


def test_at12_delete(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.save(A, TA)
    store.save(B, TB)
    assert store.delete(A) is True
    assert store.delete(A) is False
    assert store.load(A) is None
    assert store.load(B) == TB


def test_at12_corrupted_file_is_empty_and_can_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("{not json")
    store = TokenStore(path)
    assert store.load(A) is None
    store.save(A, TA)
    assert TokenStore(path).load_all() == {A: TA}


def test_ki10_json_list_is_empty_and_can_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("[1, 2]")
    store = TokenStore(path)
    assert store.load_all() == {}
    assert store.delete(A) is False
    store.save(A, TA)
    assert store.load(A) == TA


def test_missing_and_unreadable_file_is_empty(tmp_path: Path) -> None:
    assert TokenStore(tmp_path / "nope.json").load_all() == {}
    assert TokenStore(tmp_path).load_all() == {}  # a directory: unreadable


def test_malformed_entries_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    good = TA.to_json()
    path.write_text(json.dumps({A: good, B: {"access_token": "x"}, "c": "str", "d": {**good, "more": 1}}))
    store = TokenStore(path)
    assert store.load_all() == {A: TA}
    store.save(B, TB)
    assert set(json.loads(path.read_text())) == {A, B}


def test_delete_of_missing_entry_does_not_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    content = json.dumps({A: TA.to_json()})  # compact: a rewrite would pretty-print it
    path.write_text(content)
    inode = path.stat().st_ino
    assert TokenStore(path).delete(B) is False
    assert path.read_text() == content
    assert path.stat().st_ino == inode


def test_parent_directories_created_and_no_tmp_left(tmp_path: Path) -> None:
    path = tmp_path / "x" / "y" / "tokens.json"
    store = TokenStore(path)
    store.save(A, TA)
    store.save(B, TB)
    store.delete(A)
    assert [p.name for p in path.parent.iterdir()] == ["tokens.json"]


def test_stale_tmp_is_replaced_and_failure_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    stale = tmp_path / f"tokens.json.{os.getpid()}.{threading.get_ident()}.tmp"
    stale.write_text("stale")
    store.save(A, TA)
    assert not stale.exists()

    def boom(self: Path, target: object) -> Path:
        raise OSError("boom")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="boom"):
        store.save(B, TB)
    monkeypatch.undo()
    assert [p.name for p in tmp_path.iterdir()] == ["tokens.json"]
    assert TokenStore(path).load_all() == {A: TA}
