"""Release hygiene: every place that states the version must agree."""
import json
import re
from pathlib import Path

import ghostlap

ROOT = Path(__file__).resolve().parents[2]


def test_versions_in_sync():
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    plugin = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())["version"]
    addon = re.search(r'^version="([^"]+)"', (ROOT / "addons/ghostlap/plugin.cfg").read_text(), re.M).group(1)
    assert pyproject == ghostlap.__version__ == plugin == addon, (
        f"pyproject {pyproject}, ghostlap.__version__ {ghostlap.__version__}, plugin.json {plugin}, plugin.cfg {addon}"
    )


def test_marketplace_points_at_plugin():
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    plugin = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    assert (ROOT / entry["source"] / ".claude-plugin/plugin.json").is_file()
    assert (ROOT / entry["source"] / "skills/feel/SKILL.md").is_file()
