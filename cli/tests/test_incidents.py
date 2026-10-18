"""Incidents (item 1.4): each planted fixture fires its incident, and a clean run fires none."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ghostlap.play.bridge_client import Bridge
from ghostlap.project import find_godot

pytestmark = pytest.mark.godot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "incidents"
PROFILE = {"player": {}, "dims": "2d", "genres": ["platformer"], "actions": {"left": "move_left", "right": "move_right", "jump": "jump"},
           "views": ["player"], "track_props": ["health"]}


@pytest.fixture
def proj(tmp_path):
    return Path(shutil.copytree(FIXTURE, tmp_path / "incidents", ignore=shutil.ignore_patterns(".ghostlap", ".godot")))


@pytest.fixture
def run(proj):
    opened = []

    def _run(scene: str, frames: int = 240, hold: str | None = None) -> tuple[Bridge, list[dict]]:
        b = Bridge.launch(find_godot(), proj, f"res://{scene}.tscn", proj / ".ghostlap/play" / scene, profile=PROFILE)
        opened.append(b)
        inputs = [{"at": 1, "action": hold}] if hold else []
        res = b.step(frames, inputs)
        return b, res["incidents"]

    yield _run
    for b in opened:
        b.quit()


def _types(incs: list[dict]) -> list[str]:
    return [i["type"] for i in incs]


def test_clean_run_fires_nothing(run):
    b, incs = run("clean", 300, hold="move_right")   # walks 1000 px on flat ground, stops short of the wall
    assert incs == [] and b.request("incidents")["result"] == []


def test_script_error(run):
    _, incs = run("error")
    assert _types(incs) == ["error"]
    e = incs[0]
    assert e["class"] == "bug" and e["file"].endswith("plant.gd") and e["line"] > 0
    assert 28 <= e["f"] <= 31   # physics frame 30, give or take the boot tick


def test_out_of_bounds_once(run):
    _, incs = run("pit", 400, hold="move_right")     # the ground ends at x=200; nothing below
    assert _types(incs) == ["out_of_bounds"]
    assert "fell below" in incs[0]["why"] and incs[0]["class"] == "bug"


def test_player_lost(run):
    _, incs = run("free", 200)
    assert _types(incs) == ["player_lost"]
    assert incs[0]["f"] - incs[0]["since"] == 120


def test_stuck_against_a_wall_is_a_signal(run):
    _, incs = run("wall", 200, hold="move_right")
    assert _types(incs) == ["stuck"] and incs[0]["class"] == "signal"


@pytest.mark.parametrize("scene,why", [("die", "health fell to 0"), ("spikes", "right after touching Spikes"),
                                       ("reload", "re-instanced")])
def test_death_signals(run, scene, why):
    _, incs = run(scene, 120, hold=None if scene == "die" else "move_right")
    deaths = [i for i in incs if i["type"] == "death"]
    assert deaths and why in deaths[0]["why"] and deaths[0]["class"] == "signal", incs
    assert all(i["type"] in ("death", "stuck") for i in incs)


def test_warnings_are_not_incidents(run):
    _, incs = run("warn", 60)
    assert incs == []


def test_one_death_for_health_zero_then_reload(run):
    # health 0 and a reload every 30 frames: one death, then the gap keeps the repeats quiet
    _, incs = run("die_reload", 100)
    assert _types(incs) == ["death"]
    _, incs = run("die_reload", 300)   # deaths keep coming: about one per 120 frames
    assert _types(incs) in (["death"] * 2, ["death"] * 3)


def test_an_error_flood_is_capped(run):
    b, incs = run("flood", 120)
    errs = [i for i in incs if i["type"] == "error"]
    assert len(errs) == 20 and "further new errors are counted" in errs[-1]["why"]
    assert b.look()["suppressed_errors"] > 50


def test_a_game_without_a_player_never_loses_it(tmp_path):
    proj = Path(shutil.copytree(FIXTURE.parent / "cards", tmp_path / "cards", ignore=shutil.ignore_patterns(".ghostlap", ".godot")))
    scene = re.search(r'run/main_scene="([^"]+)"', (proj / "project.godot").read_text()).group(1)
    b = Bridge.launch(find_godot(), proj, scene, proj / ".ghostlap/play/cards", profile={"views": ["ui"]})
    try:
        assert b.step(300)["incidents"] == []
    finally:
        b.quit()


def test_an_incident_ends_until(proj):
    b = Bridge.launch(find_godot(), proj, "res://wall.tscn", proj / ".ghostlap/play/until", profile=PROFILE)
    try:
        r = b.until("false", 600, [{"at": 1, "action": "move_right"}])
        assert r["until"] == {"met": False, "reason": "incident", "incident": "stuck"}
        assert r["f"] < 200 and _types(r["incidents"]) == ["stuck"]
        assert b.eval("incident.type") == "stuck"   # the latest incident, for expressions
    finally:
        b.quit()


def _cli(proj: Path, *args, ok=True, env=None):
    r = subprocess.run([sys.executable, "-m", "ghostlap", *args], cwd=proj, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "GHOSTLAP_GODOT": find_godot(), **(env or {})})
    if ok:
        assert r.returncode == 0, f"{args}:\n{r.stdout}\n{r.stderr}"
    return r


def _events(proj: Path) -> list[dict]:
    d = sorted(p for p in (proj / ".ghostlap/play").iterdir() if p.is_dir())[-1]
    return [json.loads(ln) for ln in (d / "events.jsonl").read_text().splitlines()]


def test_crash_is_an_incident(proj):
    _cli(proj, "play", "start", "--scene", "res://crash.tscn", "--headless")
    r = _cli(proj, "play", "step", "-n", "120", ok=False)
    assert "CRASH: Godot is gone" in r.stderr and "planted crash" in r.stderr
    inc = [e for e in _events(proj) if e["kind"] == "incident"]
    assert [e["type"] for e in inc] == ["crash"] and "planted crash" in inc[0]["log_tail"]
    assert "incidents: crash ×1" in _cli(proj, "play", "status").stdout


def test_hang_is_detected_and_killed(proj):
    _cli(proj, "play", "start", "--scene", "res://hang.tscn", "--headless")
    r = _cli(proj, "play", "until", "false", "--max", "3000", ok=False, env={"GHOSTLAP_HANG_SECONDS": "3"})
    assert "HANG during until" in r.stderr and "Godot was killed" in r.stderr
    inc = [e for e in _events(proj) if e["kind"] == "incident"]
    assert [e["type"] for e in inc] == ["hang"]
    meta = json.loads(next((proj / ".ghostlap/play").glob("*/session.json")).read_text())
    assert meta["status"] == "hung"
    assert "killed 0" in _cli(proj, "play", "kill", "--all").stdout   # nothing left behind


def test_load_errors_are_reported_by_start(proj):
    out = _cli(proj, "play", "start", "--scene", "res://load_error.tscn", "--headless").stdout
    assert "INCIDENT f=0 error [bug]" in out and "plant.gd:" in out
    out = _cli(proj, "play", "step", "-n", "10", "--quiet").stdout
    assert "INCIDENT" not in out   # reported once, not again on the first step
    assert "incidents: error ×1" in _cli(proj, "play", "status").stdout


def test_cli_prints_incidents_and_stops_until(proj):
    _cli(proj, "play", "start", "--scene", "res://error.tscn", "--headless")
    out = _cli(proj, "play", "until", "false", "--max", "300", "--quiet").stdout
    assert "until: stopped by an incident (error)" in out
    assert "INCIDENT f=" in out and "error [bug]" in out and "plant.gd:" in out
    _cli(proj, "play", "end", "--verdict", "fail", "--why", "planted error")


def test_turning_back_is_not_stuck(run):
    """Right, then back left past the start: the 90-frame window must restart with the direction,
    or some frame compares two equal positions (found playing the Godot platformer demo)."""
    b, incs = run("clean", 60, hold="move_right")
    res = b.step(120, [{"at": 1, "action": "move_right", "pressed": False}, {"at": 1, "action": "move_left"}])
    assert incs == [] and res["incidents"] == []
