import shutil
from pathlib import Path

import pytest

from gamefeel.project import find_godot

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.fixture
def example(tmp_path):
    """Copy an example project into tmp so tests never touch the repo's examples."""
    def _copy(name: str) -> Path:
        dst = tmp_path / name
        shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns(".gamefeel", ".godot"))
        return dst
    return _copy


def pytest_collection_modifyitems(config, items):
    if find_godot() is None:
        skip = pytest.mark.skip(reason="needs Godot 4 on PATH (or GAMEFEEL_GODOT)")
        for item in items:
            if "godot" in item.keywords:
                item.add_marker(skip)
