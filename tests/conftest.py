"""Fixtures shared by all the tests."""

import tempfile

import pytest


@pytest.fixture(autouse=True)
def _temp_files_under_pytest(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the temporary files of the code under test (e.g. `send`'s zip directory) out of the system's temp dir."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tempfile")))
