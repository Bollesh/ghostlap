"""The replay check, `play replay` and `play video` (item 1.7): `end` replays the final line and
compares every frame (exact, or diverged with the field that differs); a fail is confirmed when
the replay hits its incident again; the video shows the goal, held inputs, notes and verdict."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ghostlap.play.replay import confirms, windows
from ghostlap.play.verdict import confirm
from ghostlap.project import find_godot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "incidents"
GOAL = 'goal: walk\ndescribe: "Walk right"\nstart: {scene: res://clean.tscn}\nsuccess: "player.position.x > 300"\n'


@pytest.fixture
def proj(tmp_path):
    return Path(shutil.copytree(FIXTURE, tmp_path / "r", ignore=shutil.ignore_patterns(".ghostlap", ".godot")))


@pytest.fixture
def gl(proj):
    def _gl(*args, ok=True, env=None):
        r = subprocess.run([sys.executable, "-m", "ghostlap", "play", *args], cwd=proj, capture_output=True, text=True,
                           timeout=300, env={**os.environ, "GHOSTLAP_GODOT": find_godot(), **(env or {})})
        if ok:
            assert r.returncode == 0, f"{args}:\n{r.stdout}\n{r.stderr}"
        return r
    yield _gl
    subprocess.run([sys.executable, "-m", "ghostlap", "play", "kill", "--all"], cwd=proj, capture_output=True)


def _session(proj: Path) -> Path:
    return sorted(d for d in (proj / ".ghostlap/play").iterdir() if d.is_dir())[-1]


@pytest.mark.godot
def test_a_pass_replays_exactly(proj, gl):
    (proj / "goal.yaml").write_text(GOAL)
    gl("start", "goal.yaml", "--headless")
    gl("step", "-n", "20", "--hold", "move_right", "--press", "jump@5:10", "--quiet")
    gl("until", "goal", "--max", "300", "--keep", "--quiet")
    out = gl("end", "--verdict", "pass", "--why", "walked").stdout
    assert "replay check: exact (" in out and "headless" in out
    d = _session(proj)
    rep = json.loads((d / "replay.json").read_text())
    assert rep["result"] == "exact" and rep["mode"] == "headless" and rep["incidents"] == []
    assert json.loads((d / "evidence.json").read_text())["replay"]["result"] == "exact"
    report = (d / "report.md").read_text()
    assert "## Replay check" in report and "**exact**" in report and f"ghostlap play replay {d.name}" in report
    out = gl("replay", "--headless", "--fast").stdout
    assert "matched the session at every recorded frame" in out


@pytest.mark.godot
def test_nondeterminism_is_reported_where_it_starts(proj, gl):
    gl("start", "--scene", "res://chaos.tscn", "--headless")
    gl("step", "-n", "60", "--quiet")
    out = gl("end", "--verdict", "blocked", "--why", "checking replay").stdout
    # the plant moves the crate by an unseeded random amount on physics frame 30
    assert "replay check: diverged at f=29" in out and "a node other than the player" in out
    report = (_session(proj) / "report.md").read_text()
    assert "**diverged**" in report and "no test can be made" in report


@pytest.mark.godot
def test_fails_are_confirmed_by_the_replay(proj, gl):
    gl("start", "--scene", "res://error.tscn", "--headless")
    gl("step", "-n", "40", "--quiet")
    assert "ended: fail (confirmed by the replay)" in gl("end", "--verdict", "fail", "--why", "error").stdout
    ev = json.loads((_session(proj) / "evidence.json").read_text())
    assert ev["verdict"] == "fail" and ev["confirmed"] is True
    # a crash: the inputs up to it stay on the line, and the replay runs on until it crashes too
    gl("start", "--scene", "res://crash.tscn", "--headless")
    r = gl("step", "-n", "120", "--hold", "move_right", "--quiet", ok=False)
    assert "CRASH" in r.stderr
    d = _session(proj)
    assert [json.loads(x)["f"] for x in (d / "inputs.jsonl").read_text().splitlines()] == [1]
    out = gl("end", "--verdict", "fail", "--why", "crash").stdout
    assert "fail (confirmed by the replay)" in out and "not compared" in out
    assert "crashed_at" in json.loads((d / "replay.json").read_text())


def test_confirmation_rules():
    rep = {"incidents": [{"f": 31, "type": "error"}], "result": "exact"}
    assert confirms(rep, {"f": 29, "type": "error"}) and not confirms(rep, {"f": 25, "type": "error"})
    assert not confirms(rep, {"f": 31, "type": "out_of_bounds"})
    assert confirms({"incidents": [], "hung_at": 400}, {"f": 380, "type": "hang"})
    res = {"verdict": "fail", "basis": "incident", "items": [{"f": 10, "what": "incident", "type": "error"}]}
    confirm(res, {"incidents": [], "result": "diverged"})
    assert res["verdict"] == "fail (unconfirmed)" and res["confirmed"] is False
    res = {"verdict": "fail", "basis": "incident", "items": [{"f": 10, "what": "incident", "type": "error"}]}
    confirm(res, None)
    assert res["verdict"] == "fail (unconfirmed)"


def test_highlight_windows():
    assert windows([500], 1000, 60, False) == [(0, 1000)]              # short: everything
    assert windows([900], 6000, 60, True) == [(0, 6000)]               # --full
    assert windows([900, 1000, 3000], 6000, 60, False) == [(0, 180), (720, 1180), (2820, 3180), (5820, 6000)]


@pytest.mark.godot
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_video_has_the_hud_and_holds_the_verdict(proj, gl):
    (proj / "goal.yaml").write_text(GOAL)
    gl("start", "goal.yaml")
    gl("note", "walking to x=300")
    gl("until", "goal", "--max", "300", "--hold", "move_right", "--press", "jump@10:6", "--quiet")
    out = gl("end", "--verdict", "pass", "--why", "walked past 300", "--video").stdout
    assert "replay check: exact" in out and "rendered" in out and "video: " in out and "highlights" not in out
    d = _session(proj)
    for f in ("playtest.mp4", "playtest.gif", "playtest-sheet.png"):
        assert (d / f).stat().st_size > 0, f
    assert not (d / "video" / "movie.avi").exists()
    frames = int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames",
                                 "-of", "csv=p=0", str(d / "playtest.mp4")], capture_output=True, text=True).stdout.strip())
    final = json.loads((d / "session.json").read_text())["frame"]
    assert abs(frames - (final + 120) / 2) <= 3   # 30 fps of the line plus the 2 s verdict hold
    hud = json.loads((d / "video" / "hud.json").read_text())
    assert hud["verdict"] == "pass" and hud["notes"][0]["text"] == "walking to x=300" and hud["final"] == final
    assert "![playtest](playtest.gif)" in (d / "report.md").read_text()


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.godot
@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.iterdir() if (p / "project.godot").is_file()))
def test_example_sessions_replay_exactly(tmp_path, name):
    """A session of random holds and taps on each example's main scene replays exactly."""
    import random
    import re

    proj = Path(shutil.copytree(EXAMPLES / name, tmp_path / name, ignore=shutil.ignore_patterns(".ghostlap", ".godot")))
    scene = re.search(r'run/main_scene="([^"]+)"', (proj / "project.godot").read_text()).group(1)
    env = {**os.environ, "GHOSTLAP_GODOT": find_godot()}

    def gl(*args):
        r = subprocess.run([sys.executable, "-m", "ghostlap", "play", *args], cwd=proj, capture_output=True, text=True,
                           timeout=300, env=env)
        assert r.returncode == 0, f"{args}:\n{r.stdout}\n{r.stderr}"
        return r.stdout

    try:
        gl("start", "--scene", scene, "--headless")
        actions = [a for a in json.loads(gl("eval", "InputMap.get_actions()")) if not a.startswith("ui_")]
        rng = random.Random(11)
        for _ in range(5):
            hold = rng.sample(actions, k=min(2, len(actions)))
            tap = rng.choice([a for a in actions if a not in hold] or actions)
            gl("step", "-n", "60", "--hold", ",".join(hold), "--press", f"{tap}@{rng.randint(1, 50)}:{rng.randint(1, 8)}", "--quiet")
        out = gl("end", "--verdict", "blocked", "--why", "replay check")
        assert "replay check: exact (300 frames" in out, out
    finally:
        subprocess.run([sys.executable, "-m", "ghostlap", "play", "kill", "--all"], cwd=proj, capture_output=True, env=env)


@pytest.mark.godot
def test_holds_released_while_frozen_are_pressed_again(proj, gl):
    """A window focus change makes Godot release every pressed input while the game is frozen.
    The bridge presses the holds again, warns, and records it so the line still replays."""
    gl("start", "--scene", "res://clean.tscn", "--headless")
    gl("step", "-n", "10", "--hold", "move_right", "--quiet")
    # what a focus change does to every hold, and the notification it sends
    gl("eval", "[Input.action_release('move_right'), tree.root.propagate_notification(1005)]", "--write")
    x10 = float(gl("eval", "player.position.x").stdout)
    out = gl("step", "-n", "20", "--keep", "--quiet").stdout
    assert "WARNING: Godot released move_right (its window gained or lost focus)" in out and "f=11" in out
    assert float(gl("eval", "player.position.x").stdout) > x10 + 50   # still walking
    line = [json.loads(x) for x in (_session(proj) / "inputs.jsonl").read_text().splitlines()]
    assert line == [{"f": 1, "action": "move_right", "pressed": True}, {"f": 11, "action": "move_right", "pressed": False},
                    {"f": 11, "action": "move_right", "pressed": True}]
    out = gl("end", "--verdict", "blocked", "--why", "x").stdout
    assert "replay check: exact" in out


@pytest.mark.godot
def test_holds_released_mid_step_are_pressed_again(proj, gl):
    """A focus change in the middle of a step (a replay runs the whole line as one): the bridge
    checks every frame, so the hold comes back on the next frame and the run still replays."""
    gl("start", "--scene", "res://focus_out.tscn", "--headless")
    out = gl("step", "-n", "60", "--hold", "move_right", "--quiet").stdout
    assert "WARNING: Godot released move_right" in out and "f=30" in out, out
    line = [json.loads(x) for x in (_session(proj) / "inputs.jsonl").read_text().splitlines()]
    # the game ran frame 29 without the hold (the release came before its physics), then it's back on 30
    assert {"f": 29, "action": "move_right", "pressed": False} in line and {"f": 30, "action": "move_right", "pressed": True} in line, line
    assert float(gl("eval", "player.position.x").stdout) > 150   # walked on after it, not stopped at 30
    assert "replay check: exact" in gl("end", "--verdict", "blocked", "--why", "x").stdout


@pytest.mark.godot
def test_a_release_by_the_game_itself_stays_released(proj, gl):
    """Without a focus change, a released hold is the game's own doing: it isn't pressed again."""
    gl("start", "--scene", "res://clean.tscn", "--headless")
    gl("step", "-n", "10", "--hold", "move_right", "--quiet")
    gl("eval", "Input.action_release('move_right')", "--write")
    out = gl("step", "-n", "20", "--keep", "--quiet").stdout
    assert "WARNING" not in out and gl("eval", "Input.is_action_pressed('move_right')").stdout.strip() == "false"


@pytest.mark.godot
def test_sessions_run_at_the_projects_tick_rate(proj, gl):
    pg = proj / "project.godot"
    pg.write_text(pg.read_text() + "\n[physics]\n\ncommon/physics_ticks_per_second=120\n")
    gl("start", "--scene", "res://clean.tscn", "--headless")
    assert json.loads((_session(proj) / "session.json").read_text())["fps"] == 120
    before = int(gl("eval", "Engine.get_physics_frames()").stdout)
    gl("step", "-n", "60", "--quiet")
    assert int(gl("eval", "Engine.get_physics_frames()").stdout) - before == 60   # one physics tick per frame
    gl("look")   # a look after a step must not trip over the incident count (a 1.5 bug)
    assert "replay check: exact" in gl("end", "--verdict", "blocked", "--why", "x").stdout
