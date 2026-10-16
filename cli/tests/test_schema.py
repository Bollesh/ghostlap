from gamefeel.metrics import REGISTRY
from gamefeel.probes.run import substitute_vars
from gamefeel.probes.schema import compile_probe, requirements_met, validate

ROLES = {"left": "move_left", "right": "move_right", "jump": "jump"}
ACTIONS = {"move_left", "move_right", "jump", "ui_accept"}


def _probe(**kw):
    base = {"probe": "p", "arena": "flat", "inputs": [{"at": 0, "action": "right", "hold": 10}], "measure": ["move.top_speed"]}
    base.update(kw)
    return base


def test_valid_probe():
    assert validate(_probe(), set(REGISTRY), ACTIONS, ROLES) == []


def test_errors_are_specific():
    issues = [str(i) for i in validate(_probe(arena="moon", foo=1, measure=["nope"],
                                              inputs=[{"at": "cal.left_floor + j", "action": "dash"}]),
                                       set(REGISTRY), ACTIONS, ROLES)]
    text = "\n".join(issues)
    assert "arena" in text and "unknown key" in text and "unknown metric 'nope'" in text
    assert "role 'dash' has no action" in text and "unknown name 'j'" in text and "calibrate" in text


def test_compile_hold_sweep_and_roles():
    spec = _probe(sweep={"var": "k", "from": 0, "to": 2},
                  inputs=[{"at": 0, "action": "right"}, {"at": "5 + k", "action": "jump", "hold": 1}])
    c = compile_probe(spec, ROLES, "x")
    assert [r["name"] for r in c["runs"]] == ["k=0", "k=1", "k=2"]
    ins = c["runs"][0]["inputs"]
    assert ins[0] == {"at": 0, "action": "move_right", "pressed": True, "strength": 1.0}
    assert ins[2] == {"at": "(5 + k) + 1", "action": "jump", "pressed": False}


def test_sweep_values_skip_missing_roles_and_pack_vars():
    spec = substitute_vars(_probe(sweep={"var": "action", "values": ["{move}", "jump", "dash"]},
                                  inputs=[{"at": 10, "action": "{action}", "hold": 5}]), {"move": "right"})
    c = compile_probe(spec, ROLES, "x")
    assert [r["inputs"][0]["action"] for r in c["runs"]] == ["move_right", "jump"]


def test_requirements():
    ok, why = requirements_met({"needs": ["hazard"]}, {"actions": ROLES}, "2d")
    assert not ok and "hazard" in why
    ok, _ = requirements_met({"requires": {"actions": ["jump"]}, "dims": ["2d"]}, {"actions": ROLES}, "2d")
    assert ok
