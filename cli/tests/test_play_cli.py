"""`ghostlap play …` from the shell (item 1.3): a full session on a real level, the input line,
holds and taps across commands, the eval write guard, and budget enforcement."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ghostlap.play.bridge_client import Bridge
from ghostlap.project import find_godot

pytestmark = pytest.mark.godot

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

GOAL = """goal: finish_long
describe: "From the start of the long level, the player can reach the goal at the far right"
start: {scene: res://level_long.tscn}
success: "node('Goal').reached"
budget: {frames: 3600, commands: 60, shots: 5}
"""

# the route found by playing the level from the shell: (until expression, extra flags)
ROUTE = [
    ("player.global_position.x > 380", []),                                    # up to the first spikes
    ("player.is_on_floor() and player.global_position.x > 520", ["--press", "jump@1:20"]),
    ("player.global_position.x > 770", []),                                    # first pit
    ("player.is_on_floor() and player.global_position.x > 960", ["--press", "jump@1:20"]),
    ("player.global_position.x > 1400", []),                                   # the step
    ("player.is_on_floor() and player.global_position.x > 1500", ["--press", "jump@1:20"]),
    ("player.global_position.x > 1680", []),                                   # step -> ledge
    ("player.is_on_floor() and player.global_position.x > 1800", ["--press", "jump@1:30"]),
    ("player.global_position.x > 2030", []),                                   # ledge -> ground 3
    ("player.is_on_floor() and player.global_position.y > 200", ["--press", "jump@1:30"]),
    ("player.global_position.x > 2530", []),                                   # second spikes
    ("player.is_on_floor() and player.global_position.x > 2650", ["--press", "jump@1:20"]),
    ("player.global_position.x > 2970", []),                                   # last pit
    ("player.is_on_floor() and player.global_position.x > 3140", ["--press", "jump@1:20"]),
    ("goal", []),
]


@pytest.fixture
def game(tmp_path):
    proj = tmp_path / "pf"
    shutil.copytree(EXAMPLES / "platformer_floaty", proj, ignore=shutil.ignore_patterns(".ghostlap", ".godot"))
    (proj / ".ghostlap/goals").mkdir(parents=True)
    (proj / ".ghostlap/goals/finish_long.yaml").write_text(GOAL)

    def gl(*args, ok=True):
        env = {**os.environ, "GHOSTLAP_GODOT": find_godot()}
        r = subprocess.run([sys.executable, "-m", "ghostlap", *args], cwd=proj, capture_output=True, text=True,
                           env=env, timeout=120)
        if ok:
            assert r.returncode == 0, f"ghostlap {' '.join(args)}:\n{r.stdout}\n{r.stderr}"
        return r

    gl.proj = proj
    yield gl
    gl("play", "kill", "--all", ok=False)


def _session_dir(proj: Path) -> Path:
    return sorted(p for p in (proj / ".ghostlap/play").iterdir() if p.is_dir())[-1]


def test_shell_session_finishes_the_long_level(game):
    game("scan")
    game("detect")
    out = game("play", "start", ".ghostlap/goals/finish_long.yaml", "--headless").stdout
    assert "frozen at frame 0" in out and "success: node('Goal').reached" in out
    assert "map:" in out and "P" in out  # player-aware views from the detected profile

    for expr, flags in ROUTE:
        out = game("play", "until", expr, "--hold", "move_right", "--max", "300", *flags, "--quiet").stdout
        assert "until: true" in out, f"{expr}:\n{out}"
    assert "goal MET" in out
    assert "health" in game("play", "look", "--view", "player").stdout
    assert game("play", "eval", "player.health").stdout.strip() == "3"  # no spike was touched
    game("play", "note", "reached the goal")
    status = game("play", "status").stdout
    assert "MET at f=" in status and "note: reached the goal" in status
    out = game("play", "end", "--verdict", "pass", "--why", "node('Goal').reached").stdout
    assert "ended: pass" in out

    d = _session_dir(game.proj)
    meta = json.loads((d / "session.json").read_text())
    assert meta["status"] == "ended" and meta["used"]["commands"] < 30
    assert not meta.get("incidents")   # a clean run fires no incident (item 1.4)
    assert not (game.proj / ".ghostlap/play/active").exists()
    assert (d / "hashes.jsonl").read_text().count("\n") >= meta["final"]["f"] // 60

    # the input line replays to the same frame and state in a single step
    ev = [json.loads(ln) for ln in (d / "inputs.jsonl").read_text().splitlines()]
    b = Bridge.launch(find_godot(), game.proj, meta["scene"], game.proj / ".ghostlap/play/replay",
                      profile=json.loads((d / "profile.json").read_text()))
    try:
        b.request("config", goal=meta["goal"]["success"])
        r = b.step(meta["final"]["f"], [dict({k: v for k, v in e.items() if k != "f"}, at=e["f"]) for e in ev])
        assert r["goal_met_at"] == meta["goal_met_at"] and r["hash"] == meta["final"]["hash"]
    finally:
        b.quit()


def test_holds_taps_and_writes(game):
    game("play", "start", "--scene", "res://level_long.tscn", "--headless")
    # a tap longer than its command is released on the next command's first frame
    game("play", "step", "-n", "5", "--press", "jump@2:20", "--quiet")
    assert game("play", "eval", "Input.is_action_pressed('jump')").stdout.strip() == "true"
    game("play", "step", "-n", "3", "--hold", "move_right", "--quiet")
    assert game("play", "eval", "[Input.is_action_pressed('jump'), Input.is_action_pressed('move_right')]").stdout.strip() == "[false,true]"
    # holds are declarative: not listing move_right releases it
    out = game("play", "step", "-n", "2", "--quiet").stdout
    assert "held: nothing" in out
    assert game("play", "eval", "Input.is_action_pressed('move_right')").stdout.strip() == "false"
    lines = [json.loads(ln) for ln in (_session_dir(game.proj) / "inputs.jsonl").read_text().splitlines()]
    assert lines == [{"f": 2, "action": "jump", "pressed": True}, {"f": 6, "action": "jump", "pressed": False},
                     {"f": 6, "action": "move_right", "pressed": True}, {"f": 9, "action": "move_right", "pressed": False}]
    assert "both held and pressed" in game("play", "step", "--hold", "jump", "--press", "jump", ok=False).stderr

    # an until that stops early drops inputs the game never reached
    game("play", "until", "true", "--press", "jump@50", "--quiet")
    assert len((_session_dir(game.proj) / "inputs.jsonl").read_text().splitlines()) == 4

    # eval can't change the game unless asked, and a write is logged as a cheat
    assert "changes the game" in game("play", "eval", "player.set('health', 9)", ok=False).stderr
    game("play", "eval", "--write", "player.set('health', 9)")
    assert game("play", "eval", "node('Player').health").stdout.strip() == "9"
    meta = json.loads((_session_dir(game.proj) / "session.json").read_text())
    assert meta["cheats"] and "setup cheats: 1" in game("play", "status").stdout

    assert "still running" in game("play", "start", "--scene", "res://level.tscn", ok=False).stderr
    assert "killed 1" in game("play", "kill").stdout
    assert json.loads((_session_dir(game.proj) / "session.json").read_text())["status"] == "killed"
    assert "no active play session" in game("play", "step", ok=False).stderr


def test_budget_is_enforced(game):
    (game.proj / ".ghostlap/goals/tiny.yaml").write_text(
        "goal: tiny\nstart: {scene: res://level_long.tscn}\nsuccess: \"node('Goal').reached\"\n"
        "budget: {frames: 100, commands: 8, shots: 1}\n")
    game("play", "start", ".ghostlap/goals/tiny.yaml", "--headless")
    game("play", "step", "-n", "60", "--quiet")
    assert "only 40 frames" in game("play", "step", "-n", "60", ok=False).stderr
    out = game("play", "until", "goal", "--hold", "move_right", "--max", "600", "--quiet").stdout
    assert "clamped to the budget" in out and "100/100 frames" in out
    err = game("play", "step", "-n", "1", ok=False).stderr
    assert "frame budget is used up" in err and "end --verdict blocked" in err
    game("play", "look")           # observing still works
    game("play", "note", "out of frames")
    out = game("play", "end", "--verdict", "blocked", "--why", "budget").stdout
    assert "ended: blocked" in out
    events = (_session_dir(game.proj) / "events.jsonl").read_text()
    assert events.count('"refused"') == 2

    # the command budget: start, look and one step fit in 3; the next step doesn't
    (game.proj / ".ghostlap/goals/few.yaml").write_text(
        "goal: few\nstart: {scene: res://level_long.tscn}\nbudget: {commands: 3}\n")
    game("play", "start", ".ghostlap/goals/few.yaml", "--headless")
    game("play", "look", "--view", "player")
    game("play", "step", "-n", "5", "--quiet")
    assert "command budget is used up (3" in game("play", "step", "-n", "5", ok=False).stderr
    game("play", "end", "--verdict", "blocked", "--why", "commands")


def test_a_scene_reload_doesnt_break_until_or_the_goal_check(game):
    game("play", "start", ".ghostlap/goals/finish_long.yaml", "--headless")
    game("play", "until", "player.global_position.x > 380", "--hold", "move_right", "--quiet")
    game("play", "until", "player.is_on_floor() and player.global_position.x > 520", "--hold", "move_right",
         "--press", "jump@1:20", "--quiet")
    # walk into the first pit: pit.gd reloads the level, leaving one frame with no current scene
    out = game("play", "until", "player.global_position.x < 100", "--hold", "move_right", "--max", "300", "--quiet").stdout
    assert "until: true" in out, out
    out = game("play", "step", "-n", "2", "--quiet").stdout
    assert "level_long.tscn" in out and "check failing" not in out
    # the goal is still checked after the reload (here: never met, and no error left behind)
    status = game("play", "status").stdout
    assert "goal not met" in status and "MET" not in status
    assert "incidents: death ×1" in status   # the reload re-instanced the player


def test_bad_goal_expression_fails_cleanly(game):
    (game.proj / ".ghostlap/goals/typo.yaml").write_text(
        "goal: typo\nstart: {scene: res://level_long.tscn}\nsuccess: \"node('Goal').reached ==\"\n")
    err = game("play", "start", ".ghostlap/goals/typo.yaml", "--headless", ok=False).stderr
    assert "success expression doesn't compile" in err
    assert not (game.proj / ".ghostlap/play/active").exists()
    assert "killed 0" in game("play", "kill", "--all").stdout  # nothing left running
    game("play", "start", "--scene", "res://level_long.tscn", "--headless")  # and a new session starts


def test_eval_refuses_rng_and_input_calls(game):
    game("play", "start", "--scene", "res://level_long.tscn", "--headless")
    for expr in ("randf()", "Input.action_press('jump')", "node('Goal').emit_signal('body_entered', player)"):
        assert "changes the game" in game("play", "eval", expr, ok=False).stderr, expr
    assert game("play", "eval", "player.is_on_floor()").stdout.strip() in ("true", "false")
