import pytest

from gamefeel.detect.detect import detect, draft_profile
from gamefeel.graph.store import Graph
from gamefeel.packs.loader import load_packs
from gamefeel.project import Project
from gamefeel.scan.run import scan

CASES = [
    ("platformer_floaty", ["platformer"], "2d", {"left": "move_left", "right": "move_right", "jump": "jump"}),
    ("topdown_sluggish", ["topdown"], "2d", {"up": "move_up", "down": "move_down", "dash": "dash"}),
    ("action_basic", ["action_sidescroller"], "2d", {"attack": "attack", "jump": "jump"}),
    ("third_person_basic", ["third_person_3d"], "3d", {"up": "move_forward", "down": "move_back"}),
]


@pytest.mark.parametrize("name,genres,dims,roles", CASES)
def test_detect_examples_text_mode(example, name, genres, dims, roles):
    root = example(name)
    p = Project(root)
    scan(p, text_only=True)
    det = detect(Graph(p.graph_db), root)
    assert det["player"]["scene"] == "res://player.tscn"
    assert det["dims"] == dims
    for k, v in roles.items():
        assert det["roles"][k] == v
    prof = draft_profile(det, load_packs(root))
    assert prof["genres"] == genres
    assert prof["confirmed"] is False


def test_platformer_has_evidence(example):
    root = example("platformer_floaty")
    p = Project(root)
    scan(p, text_only=True)
    det = detect(Graph(p.graph_db), root)
    plat = next(g for g in det["genres"] if g["pack"] == "platformer")
    assert any("player.gd:" in m for m in plat["matched"]), plat["matched"]
    assert det["hazards"][0]["scene"] == "res://spikes.tscn"
