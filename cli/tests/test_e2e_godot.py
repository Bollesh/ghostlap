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
    "platformer_floaty": {"jump.time_to_apex", "jump.coyote_frames", "jump.buffer_frames", "jump.variable",
                          "jump.fall_rise_ratio", "move.accel_time", "move.decel_time", "impact.hitstop_frames"},
    "platformer_tuned": set(),
    "topdown_sluggish": {"move.accel_time", "move.decel_time", "move.diagonal_ratio", "dash.duration_frames",
                         "dash.cooldown_frames", "impact.hitstop_frames"},
    "topdown_tuned": set(),
    "action_basic": {"attack.startup_frames", "attack.combo_window_frames"},
    "third_person_basic": {"move.turn_time", "camera.follow_lag", "jump.coyote_frames"},
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
    for f in rep["findings"]:
        if f["severity"] in ("warn", "error"):
            assert f["suggestions"], f"{f['metric']} has no suggestion"


def test_fix_loop_converges(example):
    """Apply the report's own property suggestions to the floaty platformer twice: it ends clean."""
    import re

    root = example("platformer_floaty")
    for _ in range(3):
        rep = _pipeline(root)
        props = {}
        for f in rep["findings"]:
            for s in f.get("suggestions", []):
                if s["kind"] == "property" and isinstance(s.get("proposed"), (int, float)):
                    props[(s["file"], s["property"])] = s["proposed"]
        if not props:
            break
        for (res, prop), val in props.items():
            path = root / res.removeprefix("res://")
            path.write_text(re.sub(rf"(@export var {prop} := )[-0-9.]+", lambda m: m.group(1) + f"{float(val):.4f}", path.read_text()))
    flagged = [f["metric"] for f in rep["findings"] if f["severity"] in ("warn", "error")]
    assert flagged == []
