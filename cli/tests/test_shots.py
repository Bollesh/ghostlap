"""Screenshot checkpoints (item 1.5): each capture trigger and shot hint fires on a fixture built to
cause it and none on a plain flat run; settings come from cfg < goal < start < live config, a live
change applies on the next frame; reads of shots (the plugin's Read hook) land in events.jsonl."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ghostlap.project import find_godot

pytestmark = pytest.mark.godot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "incidents"
HOOKS = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "hooks.json"


@pytest.fixture
def proj(tmp_path):
    return Path(shutil.copytree(FIXTURE, tmp_path / "shots", ignore=shutil.ignore_patterns(".ghostlap", ".godot")))


@pytest.fixture
def gl(proj):
    def _gl(*args, ok=True, stdin=None):
        r = subprocess.run([sys.executable, "-m", "ghostlap", "play", *args], cwd=proj, capture_output=True, text=True,
                           timeout=120, input=stdin, env={**os.environ, "GHOSTLAP_GODOT": find_godot()})
        if ok:
            assert r.returncode == 0, f"{args}:\n{r.stdout}\n{r.stderr}"
        return r.stdout
    yield _gl
    subprocess.run([sys.executable, "-m", "ghostlap", "play", "kill", "--all"], cwd=proj, capture_output=True)


def _session(proj: Path) -> Path:
    return next(d for d in (proj / ".ghostlap/play").iterdir() if d.is_dir())


def _events(proj: Path, kind: str) -> list[dict]:
    lines = (_session(proj) / "events.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


def _triggers(proj: Path) -> dict[str, list[str]]:
    """shot file -> the triggers that captured it"""
    out = {}
    for e in _events(proj, "shot"):
        out[e["path"]] = e["why"]
    return out


def _on_disk(proj: Path) -> set[str]:
    return {p.name for p in (_session(proj) / "shots").glob("*.png")}


def test_a_plain_flat_run_captures_only_the_start(proj, gl):
    out = gl("start", "--scene", "res://clean.tscn")
    assert "SHOT HINT first_look" in out and "shots/f0.png" in out
    out = gl("step", "-n", "300", "--hold", "move_right")
    assert "SHOT HINT" not in out and "UI f=" not in out
    assert _triggers(proj) == {"shots/f0.png": ["start"]} and _on_disk(proj) == {"f0.png"}
    assert [h["reason"] for h in _events(proj, "shot_hint")] == ["first_look"]


@pytest.mark.parametrize("scene,frames,hold,trigger,hint", [
    ("popup", 60, None, "ui", None),                       # a Game Over panel: its text is in `ui`
    ("icons", 60, None, "ui", "ui_without_text"),          # icon-only buttons: only a picture says what they are
    ("change", 60, None, "scene_change", "scene_changed"),
    ("error", 60, None, "incident", None),
    ("die", 60, None, "death", None),
    ("wall", 200, "move_right", "incident", "expectation_missed"),   # stuck
    ("die_reload", 300, None, "death", "repeat_death"),    # dies at the same spot again
])
def test_each_trigger_and_hint(proj, gl, scene, frames, hold, trigger, hint):
    gl("start", "--scene", f"res://{scene}.tscn")
    out = gl("step", "-n", str(frames), *(["--hold", hold] if hold else []), "--quiet")
    shots = {p: why for p, why in _triggers(proj).items() if p != "shots/f0.png"}
    assert any(trigger in why for why in shots.values()), (shots, out)
    hints = [h["reason"] for h in _events(proj, "shot_hint")][1:]   # after first_look
    if hint:
        assert hint in hints and f"SHOT HINT {hint}" in out, out
        path = out.split(f"SHOT HINT {hint}: read ", 1)[1].split(" before", 1)[0]
        assert Path(path).is_file()
    else:
        assert hints == [], out


def test_a_toast_and_a_screen_flash_are_not_new_ui(proj, gl):
    gl("start", "--scene", "res://toast.tscn")
    gl("step", "-n", "60", "--quiet")
    assert _on_disk(proj) == {"f0.png"} and _events(proj, "ui_shown") == []


def test_the_ui_shot_waits_for_the_popup_to_settle(proj, gl):
    gl("start", "--scene", "res://popup.tscn")
    out = gl("step", "-n", "60", "--quiet")
    ui = _events(proj, "ui_shown")
    assert len(ui) == 1 and ui[0]["text"] == "GAME OVER | Retry" and not ui[0]["textless"]
    assert ui[0]["shot"] == f"shots/f{ui[0]['f'] + 15}.png" and "UI f=" in out and "GAME OVER" in out
    # a step that ends before the popup settles still gets its shot, at the last frame
    gl("kill")
    shutil.rmtree(proj / ".ghostlap/play")
    gl("start", "--scene", "res://popup.tscn")
    gl("step", "-n", "35", "--quiet")
    assert _events(proj, "ui_shown")[0]["shot"] == "shots/f35.png"


def test_success_is_captured(proj, gl):
    (proj / "goal.yaml").write_text('goal: walk\nstart: {scene: res://clean.tscn}\nsuccess: "player.position.x > 100"\n')
    gl("start", "goal.yaml")
    gl("until", "goal", "--max", "200", "--hold", "move_right", "--quiet")
    assert any("success" in why for why in _triggers(proj).values())


def test_unclassified_nearby_is_hinted_once(proj, gl):
    gl("start", "--scene", "res://unknown.tscn")
    out = gl("look", "--view", "near,map")
    assert "SHOT HINT unclassified_nearby" in out and "near:Thing" in out
    assert "SHOT HINT" not in gl("look", "--view", "near,map")   # the same unknown thing isn't new


def test_until_max_is_an_expectation_missed(proj, gl):
    gl("start", "--scene", "res://clean.tscn")
    out = gl("until", "false", "--max", "20", "--quiet")
    assert "SHOT HINT expectation_missed" in out and "shots/f20.png" in out


def test_rolling_rotation_live_config_and_pins(proj, gl):
    gl("start", "--scene", "res://clean.tscn", "--rolling-shots", "50:2")
    gl("step", "-n", "100", "--quiet")
    assert _on_disk(proj) == {"f0.png", "f50.png", "f100.png"}
    gl("note", "--saw", "shots/f50.png", "the player on flat ground")   # cited: rotation keeps it
    # every=101 at frame 100 means the very next frame is a rolling frame
    out = gl("config", "shots.rolling.every=101")
    assert "from f=101 on" in out
    gl("step", "-n", "1", "--quiet")
    assert _on_disk(proj) == {"f0.png", "f50.png", "f100.png", "f101.png"}
    gl("step", "-n", "101", "--quiet")   # f202: f100 rotates out (keep 2), the cited f50 stays
    assert _on_disk(proj) == {"f0.png", "f50.png", "f101.png", "f202.png"}
    shown = gl("config")
    assert "shots.rolling.every = 101  (play config at f=100)" in shown
    assert "shots.rolling.keep = 2  (play start)" in shown and "shots.width = 960  (default)" in shown
    assert [e["changes"] for e in _events(proj, "config")] == [{"shots.rolling.every": 101}]


def test_settings_layers_and_errors(proj, gl):
    (proj / "ghostlap.cfg").write_text("[shots]\nrolling_every = 40\nrolling_keep = 3\n[look]\nradius = 6\n")
    (proj / "goal.yaml").write_text("goal: g\nstart: {scene: res://clean.tscn}\nshots: {rolling: {keep: 1}, on: [incident, death]}\n")
    gl("start", "goal.yaml", "--headless")
    shown = gl("config")
    assert "shots.rolling.every = 40  (ghostlap.cfg)" in shown and "shots.rolling.keep = 1  (goal)" in shown
    assert "shots.on = incident, death  (goal)" in shown and "look.radius = 6.0  (ghostlap.cfg)" in shown
    r = subprocess.run([sys.executable, "-m", "ghostlap", "play", "config", "shots.rolling.every=-1"], cwd=proj,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "shots.rolling.every must be" in r.stderr
    r = subprocess.run([sys.executable, "-m", "ghostlap", "play", "config", "shots.nope=1"], cwd=proj,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "unknown setting" in r.stderr
    assert "warning: incident shots are off" in gl("config", "shots.on=death")
    (proj / "bad.yaml").write_text("goal: g\nstart: {scene: res://clean.tscn}\nshots: {width: 10}\n")
    gl("kill")
    r = subprocess.run([sys.executable, "-m", "ghostlap", "play", "start", "bad.yaml"], cwd=proj, capture_output=True, text=True)
    assert r.returncode != 0 and "shots.width must be" in r.stderr


def test_headless_has_no_shots_or_hints(proj, gl):
    out = gl("start", "--scene", "res://unknown.tscn", "--headless")
    assert "screenshots: off (headless" in out and "SHOT HINT" not in out
    assert "SHOT HINT" not in gl("look", "--view", "near,map")
    assert not (_session(proj) / "shots").exists()


def test_the_read_hook_logs_reads(proj, gl, tmp_path):
    gl("start", "--scene", "res://unknown.tscn")
    gl("look", "--view", "near,map", "--shot")
    shot = _session(proj) / "shots" / "f0.png"
    # the plugin's hook command, as Claude Code runs it, with `ghostlap` on PATH
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ghostlap").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m ghostlap "$@"\n')
    (bin_dir / "ghostlap").chmod(0o755)
    hook = json.loads(HOOKS.read_text())["hooks"]["PostToolUse"][0]
    assert hook["matcher"] == "Read"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def read(path: Path) -> subprocess.CompletedProcess:
        payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read", "cwd": str(proj),
                              "tool_input": {"file_path": str(path)}})
        return subprocess.run(["bash", "-c", hook["hooks"][0]["command"]], input=payload, text=True,
                              capture_output=True, env=env, timeout=60)

    assert read(shot).returncode == 0
    assert read(proj / "player.gd").returncode == 0                 # other reads: nothing happens
    assert read(proj / ".ghostlap/play/nope/shots/f1.png").returncode == 0   # unknown session: ignored
    reads = _events(proj, "shot_read")
    assert reads == [{"f": 0, "kind": "shot_read", "path": "shots/f0.png", "reason": "unclassified_nearby"}]
    assert "f0.png" in (shot.parent / "keep.txt").read_text()
    assert "1 read (unclassified_nearby ×1)" in gl("status")
    # without the hook, by hand
    assert "logged: read shots/f0.png (f=0, unclassified_nearby)" in gl("log-read", str(shot))
    r = subprocess.run([sys.executable, "-m", "ghostlap", "play", "log-read", str(proj / "player.gd")], cwd=proj,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "not a play session screenshot" in r.stderr
