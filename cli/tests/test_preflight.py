"""`ghostlap play preflight` (item 1.8): load check, determinism smoke, code findings, the cache."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ghostlap.play.preflight import code_findings
from ghostlap.project import find_godot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "incidents"


@pytest.fixture
def proj(tmp_path):
    return Path(shutil.copytree(FIXTURE, tmp_path / "p", ignore=shutil.ignore_patterns(".ghostlap", ".godot")))


def _gl(proj, *args):
    r = subprocess.run([sys.executable, "-m", "ghostlap", "play", *args], cwd=proj, capture_output=True, text=True,
                       timeout=300, env={**os.environ, "GHOSTLAP_GODOT": find_godot()})
    assert r.returncode == 0, f"{args}:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def test_code_findings(tmp_path):
    (tmp_path / "project.godot").write_text("[physics]\ncommon/run_on_separate_thread=true\n")
    (tmp_path / "a.gd").write_text("func _ready():\n\trandomize()  # chaos\n\tif Input.is_key_pressed(KEY_A): pass\n"
                                   "\tvar r := RandomNumberGenerator.new()\n\t# Time.get_ticks_msec() in a comment\n")
    (tmp_path / "b.gd").write_text("var rng := RandomNumberGenerator.new()\nfunc _init():\n\trng.seed = 4\n")
    (tmp_path / "s.tscn").write_text('[node name="N" type="VisibleOnScreenNotifier2D" parent="."]\n')
    (tmp_path / "addons" / "x").mkdir(parents=True)
    (tmp_path / "addons" / "x" / "c.gd").write_text("func f():\n\trandomize()\n")   # addons aren't the game
    f = code_findings(tmp_path)
    assert [(x["what"], x["at"]) for x in f["nondeterminism"]] == [
        ("randomize()", "res://a.gd:2"), ("RandomNumberGenerator.new() without a seed", "res://a.gd:4"),
        ("physics/common/run_on_separate_thread", "project.godot")]
    assert [x["at"] for x in f["input_style"]] == ["res://a.gd:3"]
    assert [x["at"] for x in f["render_dependent"]] == ["res://s.tscn:1"]


@pytest.mark.godot
def test_preflight_runs_caches_and_warns(proj):
    out = _gl(proj, "preflight")
    assert "✅ loads" in out and "✅ determinism" in out and "✅ input_style" in out
    # the chaos plant's unseeded generator is the one warning
    assert "1 warning(s)" in out and "RandomNumberGenerator.new() without a seed at res://plant.gd:" in out
    assert "(cached" in _gl(proj, "preflight")
    # chaos.tscn moves a node by an unseeded random amount on frame 30: the boots differ there
    out = _gl(proj, "preflight", "--scene", "res://chaos.tscn")
    assert "⚠️ determinism: two boots differ from f=29" in out and "RandomNumberGenerator" in out
    pf = json.loads((proj / ".ghostlap" / "preflight.json").read_text())
    assert pf["determinism"] == {"ok": False, "frames": 300, "diverged_at": 29}
    # start reads the cache while it's current
    out = _gl(proj, "start", "--scene", "res://chaos.tscn", "--headless")
    assert "preflight: two identical boots differ from f=29" in out
    _gl(proj, "kill")
    (proj / "plant.gd").write_text((proj / "plant.gd").read_text() + "\n")   # a change makes it stale
    shutil.rmtree(proj / ".ghostlap" / "play")
    assert "preflight: not run for this version" in _gl(proj, "start", "--scene", "res://clean.tscn", "--headless")
    _gl(proj, "kill")
