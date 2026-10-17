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


def _can_render() -> bool:
    import os
    import sys

    if not shutil.which("ffmpeg"):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or shutil.which("xvfb-run"))
    return True


def pytest_collection_modifyitems(config, items):
    no_godot = find_godot() is None
    no_render = no_godot or not _can_render()
    for item in items:
        if "godot" in item.keywords and no_godot:
            item.add_marker(pytest.mark.skip(reason="needs Godot 4 on PATH (or GAMEFEEL_GODOT)"))
        if "render" in item.keywords and no_render:
            item.add_marker(pytest.mark.skip(reason="needs Godot, ffmpeg and a display (or xvfb-run)"))
