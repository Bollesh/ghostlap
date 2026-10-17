"""End-to-end: scan (engine) -> detect -> confirm -> probes -> analyze, on every example."""
import pytest

from gamefeel.analyze import analyze
from gamefeel.detect.detect import detect, draft_profile
from gamefeel.graph.store import Graph
from gamefeel.packs.loader import load_packs
from gamefeel.probes import run as prun
from gamefeel.project import Project
from gamefeel.scan.run import scan

pytestmark = pytest.mark.godot

# example -> metrics that must be flagged (warn/error); "tuned" examples must have none
EXPECTED = {
    "platformer_floaty": {"jump.coyote_frames", "jump.buffer_frames", "jump.variable", "impact.hitstop_frames"},
    "platformer_tuned": set(),
    "topdown_sluggish": {"move.diagonal_ratio", "dash.cooldown_frames", "impact.hitstop_frames", "impact.feedback"},
    "topdown_tuned": set(),
    "action_basic": {"attack.combo_window_frames"},
    "third_person_basic": {"jump.coyote_frames", "jump.buffer_frames"},
}
# motion character is described as "style", never flagged, by default
STYLE = {
    "platformer_floaty": {"jump.time_to_apex", "move.accel_time", "move.decel_time"},
    "topdown_sluggish": {"move.accel_time", "move.decel_time"},
    "third_person_basic": {"move.turn_time", "camera.follow_lag"},
}


def _pipeline(root):
    p = Project(root)
    r = scan(p)
    assert r.mode == "engine", r.warnings
    det = detect(Graph(p.graph_db), root)
    prof = draft_profile(det, load_packs(root))
    prof["confirmed"] = True
    res = prun.run(p, prof)
    assert not res.summary.get("errors"), res.summary["errors"]
    return analyze(p, res.run_dir, prof)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_example(example, name):
    rep = _pipeline(example(name))
    flagged = {f["metric"] for f in rep["findings"] if f["severity"] in ("warn", "error", "assert_failed")}
    assert EXPECTED[name] <= flagged, f"missing: {EXPECTED[name] - flagged}"
    if not EXPECTED[name]:
        assert flagged == set(), f"tuned example should be clean, got {flagged}"
    styled = {f["metric"] for f in rep["findings"] if f["severity"] == "style"}
    assert STYLE.get(name, set()) <= styled, f"expected as style: {STYLE.get(name, set()) - styled}"
    for f in rep["findings"]:
        if f["severity"] in ("warn", "error"):
            assert f["suggestions"], f"{f['metric']} has no suggestion"


def _setup(root):
    p = Project(root)
    scan(p)
    prof = draft_profile(detect(Graph(p.graph_db), root), load_packs(root))
    prof["confirmed"] = True
    return p, prof


def test_propose_is_verified_and_apply_writes_it(example):
    """propose fixes the floaty platformer in a scratch copy (real project untouched), without
    regressions or jump-height drift; apply then writes exactly those values."""
    from gamefeel import propose as prop
    from gamefeel.edits import Edit, read_value

    root = example("platformer_floaty")
    original = (root / "player.gd").read_text()
    p, prof = _setup(root)
    res = prop.propose(p, prof, scope="all")
    assert (root / "player.gd").read_text() == original, "propose must not touch the real project"
    assert res["clean"] and not res["regressions"], res["remaining"]
    m = {x["metric"]: x for x in res["metrics"]}
    assert abs(m["jump.height_max"]["after"] - m["jump.height_max"]["before"]) / m["jump.height_max"]["before"] < 0.1
    assert m["jump.fall_time"]["severity_after"] == "ok"
    out = prop.apply(p)
    assert out["applied"] and not out["skipped"]
    for c in res["changes"]:
        got = read_value(root, Edit(c["kind"], c["file"], c["property"], c["value"], node=c.get("node")))
        assert abs(float(got) - float(c["value"])) < 1e-3, c
    rep = _pipeline(root)
    assert not [f for f in rep["findings"] if f["severity"] in ("warn", "error")]


def test_default_scope_keeps_motion_character(example):
    """By default only forgiveness/feedback is fixed: the floaty game's motion stays floaty."""
    from gamefeel import propose as prop

    root = example("platformer_floaty")
    p, prof = _setup(root)
    res = prop.propose(p, prof)
    changed = {c["property"] for c in res["changes"]}
    assert {"coyote_time", "jump_buffer_time", "hitstop_time", "jump_cut"} <= changed
    assert not changed & {"acceleration", "friction", "gravity", "jump_velocity", "fall_gravity_multiplier"}
    m = {x["metric"]: x for x in res["metrics"]}
    assert m["jump.time_to_apex"]["after"] == m["jump.time_to_apex"]["before"]
    assert {t["metric"] for t in res["taste_left_alone"]} >= {"jump.time_to_apex", "move.accel_time"}
    assert res["clean"]


def test_apply_refuses_stale_files(example):
    from gamefeel import propose as prop

    root = example("platformer_floaty")
    p, prof = _setup(root)
    prop.propose(p, prof, passes=1)
    gd = root / "player.gd"
    gd.write_text(gd.read_text().replace("@export var coyote_time := 0.0", "@export var coyote_time := 0.05"))
    out = prop.apply(p)
    assert any(s["property"] == "coyote_time" and "changed since" in s["why"] for s in out["skipped"])


def test_feedback_steers_the_next_proposal(example):
    from gamefeel import propose as prop
    from gamefeel.detect.detect import read_profile
    from gamefeel.feedback import feedback

    root = example("platformer_floaty")
    p, prof = _setup(root)
    p.profile_path.write_text(__import__("yaml").safe_dump({**prof, "tune": "all"}))
    prof = {**prof, "tune": "all"}
    first = prop.propose(p, prof)
    fall1 = {x["metric"]: x for x in first["metrics"]}["jump.fall_time"]["after"]
    feedback(p, "the player falls too fast")
    prof2 = read_profile(p.profile_path)
    second = prop.propose(p, prof2)
    fall2 = {x["metric"]: x for x in second["metrics"]}["jump.fall_time"]["after"]
    assert fall2 > fall1, (fall1, fall2)


@pytest.mark.render
def test_clip_renders_now_vs_proposal(example):
    from pathlib import Path

    from gamefeel import propose as prop
    from gamefeel.clip import clip

    root = example("platformer_floaty")
    p, prof = _setup(root)
    prop.propose(p, prof, passes=2)
    out = clip(p, prof, probes=["jump", "hit_react"])
    assert out["right"] == "proposed" and "jump" in out["probes"]
    for k in ("gif", "sheet"):
        assert Path(out[k]).is_file() and Path(out[k]).stat().st_size > 10_000
