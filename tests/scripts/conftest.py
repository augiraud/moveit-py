from pathlib import Path

import pytest
from script_runner import Runner


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)
