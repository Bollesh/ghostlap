"""Verdict rules, the input line and the artifacts (item 1.6): `pass` is refused without evidence
on the final input line, `fail` without a bug incident on it; `rewind` makes the abandoned
branch an attempt; `end` writes evidence.json, state.jsonl and report.md."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ghostlap.play.verdict import repro_steps, split_line
from ghostlap.project import find_godot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "incidents"
GOAL = 'goal: walk\nstart: {scene: res://clean.tscn}\nsuccess: "player.position.x > 300"\n'


@pytest.fixture
def proj(tmp_path):
    return Path(shutil.copytree(FIXTURE, tmp_path / "v", ignore=shutil.ignore_patterns(".ghostlap", ".godot")))


@pytest.fixture
def gl(proj):
    def _gl(*args, ok=True):
        r = subprocess.run([sys.executable, "-m", "ghostlap", "play", *args], cwd=proj, capture_output=True, text=True,
                           timeout=180, env={**os.environ, "GHOSTLAP_GODOT": find_godot()})
        if ok is True:
            assert r.returncode == 0, f"{args}:\n{r.stdout}\n{r.stderr}"
        elif ok is False:
            assert r.returncode != 0, f"{args} should be refused:\n{r.stdout}"
        return r
    yield _gl
    subprocess.run([sys.executable, "-m", "ghostlap", "play", "kill", "--all"], cwd=proj, capture_output=True)


def _session(proj: Path) -> Path:
    return next(d for d in (proj / ".ghostlap/play").iterdir() if d.is_dir())


def _events(proj: Path, kind: str) -> list[dict]:
    lines = (_session(proj) / "events.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []


pytest_godot = pytest.mark.godot


@pytest_godot
def test_pass_needs_the_success_check_on_the_final_line(proj, gl):
    (proj / "goal.yaml").write_text(GOAL)
    gl("start", "goal.yaml", "--headless")
    gl("step", "-n", "20", "--hold", "move_right", "--quiet")
    out = gl("until", "goal", "--max", "300", "--keep", "--quiet").stdout
    met = int(out.split("goal MET at f=", 1)[1].split(" ", 1)[0])
    # the success frame is on the abandoned branch now
    gl("rewind", "--to", "20")
    r = gl("end", "--verdict", "pass", "--why", "walked", ok=False)
    assert "success check true on the final input line, and it never was" in r.stderr
    assert "isn't on the final input line" in gl("end", "--verdict", "pass", "--why", "w", "--evidence", "f50", ok=False).stderr
    gl("until", "goal", "--max", "300", "--keep", "--quiet")
    out = gl("end", "--verdict", "pass", "--why", "walked").stdout
    assert "ended: pass (machine-checked)" in out
    d = _session(proj)
    ev = json.loads((d / "evidence.json").read_text())
    assert ev["verdict"] == "pass" and ev["basis"] == "machine" and ev["final_line"]["ends_at"] == met
    assert ev["evidence"][0]["what"] == "success" and ev["evidence"][0]["f"] == met
    assert ev["evidence"][0]["state"]["pos"][0] > 300
    assert [e["f"] for e in _jsonl(d / "state.jsonl")] == list(range(met + 1))
    assert [r["kind"] for r in _events(proj, "refused")] == ["refused", "refused"]
    report = (d / "report.md").read_text()
    assert report.startswith("# Playtest: walk — PASS") and "hold move_right from 0.02 s to the end" in report
    assert f"attempt 1: played on to f={met}" in report and "rewound to f=20" in report


@pytest_godot
def test_rewind_replays_inputs_and_eval_writes(proj, gl):
    gl("start", "--scene", "res://clean.tscn", "--headless")
    gl("step", "-n", "30", "--hold", "move_right", "--quiet")
    gl("step", "-n", "10", "--keep", "--press", "jump@2:8", "--quiet")
    h40 = json.loads(gl("look", "--json").stdout)["hash"]
    gl("step", "-n", "10", "--keep", "--quiet")
    gl("eval", "player.set('position', player.position + Vector2(40, 0))", "--write")   # at f=50
    gl("step", "-n", "20", "--release", "move_right", "--quiet")
    d = _session(proj)
    full = _jsonl(d / "inputs.jsonl")
    out = gl("rewind", "--to", "40").stdout
    assert "replay matched the first run frame for frame" in out and "held: move_right" in out
    assert json.loads(gl("look", "--json").stdout)["hash"] == h40
    assert _jsonl(d / "inputs.jsonl") == [e for e in full if e["f"] <= 40]
    assert _jsonl(d / "attempts" / "1.jsonl") == [e for e in full if e["f"] > 40]
    meta = json.loads((d / "session.json").read_text())
    assert meta["cheats"] == [] and meta["attempts"][0]["cheats"] == 1
    # play on, write again, and rewind past the write: the replay applies it at its frame
    gl("step", "-n", "10", "--keep", "--quiet")
    gl("eval", "player.set('position', player.position + Vector2(40, 0))", "--write")   # at f=50
    gl("step", "-n", "10", "--keep", "--quiet")
    x60 = float(gl("eval", "player.position.x").stdout)
    assert "replay matched" in gl("rewind", "--to", "55").stdout
    gl("step", "-n", "5", "--keep", "--quiet")
    assert float(gl("eval", "player.position.x").stdout) == x60
    assert len(json.loads((d / "session.json").read_text())["cheats"]) == 1
    assert "ended: blocked" in gl("end", "--verdict", "blocked", "--why", "a replay check").stdout


@pytest_godot
def test_fail_needs_a_bug_incident_on_the_final_line(proj, gl):
    gl("start", "--scene", "res://error.tscn", "--headless")
    gl("step", "-n", "40", "--quiet")
    gl("rewind", "--to", "10")   # the error at f=29 is on the abandoned branch
    assert "there are none" in gl("end", "--verdict", "fail", "--why", "error", ok=False).stderr
    gl("step", "-n", "30", "--quiet")   # and now on the final line
    assert "ended: fail (unconfirmed" in gl("end", "--verdict", "fail", "--why", "null instance").stdout
    ev = json.loads((_session(proj) / "evidence.json").read_text())
    assert ev["basis"] == "incident" and ev["confirmed"] is None
    assert [(i["what"], i["type"], i["f"]) for i in ev["evidence"]] == [("incident", "error", 29)]
    report = (_session(proj) / "report.md").read_text()
    assert "f=29 **error** (bug)" in report and "attempt 1" in report and "incidents: error" in report


@pytest_godot
def test_signals_alone_are_not_a_fail_unless_the_goal_says_so(proj, gl):
    gl("start", "--scene", "res://wall.tscn", "--headless")
    gl("step", "-n", "200", "--hold", "move_right", "--quiet")
    r = gl("end", "--verdict", "fail", "--why", "stuck", ok=False)
    assert "only signals (stuck)" in r.stderr
    assert "ended: blocked" in gl("end", "--verdict", "blocked", "--why", "a wall").stdout
    shutil.rmtree(proj / ".ghostlap/play")
    (proj / "goal.yaml").write_text("goal: g\nstart: {scene: res://wall.tscn}\nfail_if: [incident.stuck]\n")
    gl("start", "goal.yaml", "--headless")
    gl("step", "-n", "200", "--hold", "move_right", "--quiet")
    assert "ended: fail" in gl("end", "--verdict", "fail", "--why", "stuck at the wall").stdout


@pytest_godot
def test_visual_pass_and_the_pre_verdict_hint(proj, gl):
    gl("start", "--scene", "res://clean.tscn")
    gl("step", "-n", "30", "--hold", "move_right", "--quiet")
    gl("step", "-n", "5", "--quiet")   # the release at f=31 is the last input
    r = gl("end", "--verdict", "pass", "--why", "walked", ok=False)
    assert "SHOT HINT pre_verdict: read " in r.stderr
    hinted = Path(r.stderr.split("SHOT HINT pre_verdict: read ", 1)[1].split(". If", 1)[0])
    assert hinted.name == "f35.png" and hinted.is_file()
    assert _events(proj, "shot_hint")[-1]["reason"] == "pre_verdict"
    gl("note", "--saw", "shots/f0.png", "the start")
    r = gl("end", "--verdict", "pass", "--why", "walked", "--evidence", "shots/f0.png", ok=False)
    assert "shots/f0.png is from before the last input" in r.stderr
    r = gl("end", "--verdict", "pass", "--why", "walked", "--evidence", "shots/f35.png", ok=False)
    assert "shots/f35.png isn't cited" in r.stderr
    gl("note", "--saw", "shots/f35.png", "the player stands right of the start")
    out = gl("end", "--verdict", "pass", "--why", "walked", "--evidence", "shots/f35.png").stdout
    assert "ended: pass (visual evidence)" in out
    d = _session(proj)
    ev = json.loads((d / "evidence.json").read_text())
    assert ev["basis"] == "visual" and ev["evidence"][0]["note"] == "the player stands right of the start"
    assert "![f35](shots/f35.png)" in (d / "report.md").read_text()
    assert "f35.png" in (d / "shots" / "keep.txt").read_text()


@pytest_godot
def test_headless_pass_without_a_success_check_is_refused(proj, gl):
    gl("start", "--scene", "res://clean.tscn", "--headless")
    r = gl("end", "--verdict", "pass", "--why", "x", ok=False)
    assert "headless session has no screenshots" in r.stderr and "SHOT HINT" not in r.stderr


def test_repro_steps_compress_the_input_line():
    line = [{"f": 1, "action": "move_right", "pressed": True}, {"f": 30, "action": "jump", "pressed": True},
            {"f": 32, "action": "jump", "pressed": False}, {"f": 60, "action": "jump", "pressed": True},
            {"f": 90, "action": "jump", "pressed": False}, {"f": 120, "action": "move_right", "pressed": False},
            {"f": 130, "mouse": [10, 20], "button": 1, "pressed": True}, {"f": 131, "mouse": [10, 20], "button": 1, "pressed": False},
            {"f": 140, "key": "A", "pressed": True}]
    assert repro_steps(line, 60) == ["hold move_right 0.02–2.00 s", "tap jump at 0.50 s", "hold jump 1.00–1.50 s",
                                     "click at (10, 20) at 2.17 s", "hold key A from 2.33 s to the end"]


def test_a_rewind_abandons_later_events():
    # a rewind event is logged at the frame it went back to
    ev = [{"f": 5, "kind": "note"}, {"f": 30, "kind": "incident"}, {"f": 20, "kind": "rewind", "to": 20},
          {"f": 25, "kind": "note"}, {"f": 10, "kind": "rewind", "to": 10}, {"f": 12, "kind": "note"}]
    final, attempts = split_line(ev)
    assert [(e["f"], e["kind"]) for e in final] == [(5, "note"), (10, "rewind"), (12, "note")]
    assert [[e["f"] for e in a] for a in attempts] == [[30], [20, 25]]
