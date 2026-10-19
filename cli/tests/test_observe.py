"""Look views (item 1.2): ui, player, near, map, grid, state, game; screenshots; default views."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ghostlap.play.bridge_client import Bridge, BridgeError
from ghostlap.play.profile import bridge_profile
from ghostlap.project import Project, find_godot

pytestmark = pytest.mark.godot

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.fixture
def session(tmp_path):
    opened = []

    def _open(src: Path, scene: str = "res://main.tscn", views=None, display="headless", profile=None):
        proj = tmp_path / src.name
        if not proj.exists():
            # never the source's .ghostlap/: it's local state (gitignored), absent on a fresh clone
            shutil.copytree(src, proj, ignore=shutil.ignore_patterns(".godot", ".ghostlap"))
            if profile is None and (src / ".gitignore").is_file() and ".ghostlap/" in (src / ".gitignore").read_text():
                # an example: detect its profile as a user would
                from ghostlap.cli import main
                assert main(["scan", "--project", str(proj)]) == 0
                assert main(["detect", "--project", str(proj), "--write-profile"]) == 0
        py = proj / ".ghostlap/profile.yaml"
        prof = profile if profile is not None else (yaml.safe_load(py.read_text()) if py.is_file() else {})
        bp = bridge_profile(Project(proj), prof)
        if views:
            bp["views"] = views
        b = Bridge.launch(find_godot(), proj, scene, proj / ".ghostlap/play" / display, profile=bp, display=display)
        opened.append(b)
        return b

    yield _open
    for b in opened:
        b.quit()


def _tap(at: int, action: str) -> list[dict]:
    return [{"at": at, "action": action}, {"at": at + 1, "action": action, "pressed": False}]


def _click(at: int, xy) -> list[dict]:
    return [{"at": at, "mouse": list(xy)}, {"at": at + 1, "mouse": list(xy), "pressed": False}]


def test_platformer_look_is_compact_and_shows_the_level(session):
    b = session(EXAMPLES / "platformer_floaty", "res://level.tscn")
    b.step(30)
    look = b.look()  # default views from the profile: player, near, map, ui
    assert set(look["views"]) == {"player", "near", "map", "ui"}
    assert len(json.dumps(look, separators=(",", ":"))) / 4 <= 600  # ~tokens
    v = look["views"]
    assert v["player"]["floor"] is True and v["player"]["health"] == 3 and v["player"]["size"] == [24, 40]
    near = {n["name"]: n for n in v["near"]}
    assert near["Spikes"]["kind"] == "hazard" and near["Ledge"]["kind"] == "solid"
    rows = v["map"]["rows"]
    prow = next(i for i, r in enumerate(rows) if "P" in r)
    assert set(rows[prow + 1]) == {"#"}           # standing on the ground
    assert "^" in rows[prow] and "#" in "".join(rows[:prow])  # spikes ahead, the ledge above


def test_tile_platformer_map_and_grid(session):
    b = session(FIXTURES / "tiles_platformer", views=["map", "grid"])
    b.step(1)
    v = b.look()["views"]
    assert v["grid"]["rows"] == [
        "                  o       G   ",
        "               =====      #   ",
        "          =====           #   ",
        "  @                       #   ",
        "##########    ^^^^   #########",
        "##############################",
    ]
    assert v["grid"]["legend"]["="] == "one-way platform" and v["grid"]["legend"]["o"] == "coin"
    m = "\n".join(v["map"]["rows"])
    assert "=====" in m and "^^^^" in m and "P" in m
    assert v["map"]["legend"]["="] == "oneway"


def test_sokoban_is_solvable_from_the_grid_view(session):
    b = session(FIXTURES / "sokoban", views=["grid", "ui", "game"])
    b.step(1)
    v = b.look()["views"]
    assert v["grid"]["rows"] == ["#######", "#.....#", "#.@B*.#", "#.....#", "#.B*..#", "#######"]
    assert {v["grid"]["legend"][k] for k in "@B*#"} == {"player", "box", "goal (Goals)", "solid"}
    assert v["ui"] == [{"path": "UI/Status", "text": "Boxes on goals: 0/2"}]
    assert v["game"] == {"boxes_on_goals": 0, "goals": 2}
    # the solution, read off the board: push right; go round to the left of the second box; push right
    moves = ["move_right", "move_down", "move_left", "move_left", "move_down", "move_right"]
    inputs = [i for k, m in enumerate(moves) for i in _tap(1 + 3 * k, m)]
    b.step(3 * len(moves) + 1, inputs)
    v = b.look()["views"]
    assert v["grid"]["rows"] == ["#######", "#.....#", "#...B.#", "#.....#", "#.@B..#", "#######"]
    assert v["ui"][0]["text"] == "Solved!" and v["game"]["boxes_on_goals"] == 2


def test_card_battle_is_playable_from_the_ui_view(session):
    b = session(FIXTURES / "cards", views=["ui", "state"])
    b.step(1)
    v = b.look()["views"]
    ui = {e["path"]: e for e in v["ui"]}
    assert ui["EnemyHP"]["text"] == "Enemy HP 20/20" and ui["Energy"]["text"] == "Energy 3/3"
    cards = {e["text"]: e for e in v["ui"] if e["path"].startswith("Hand/")}
    assert set(cards) == {"Strike (1) / deal 6", "Block (1) / block 5", "Heavy Blow (3) / deal 14"}
    assert v["state"]["."]["enemy_hp"] == 20
    b.step(4, _click(1, cards["Heavy Blow (3) / deal 14"]["at"]))
    v = b.look()["views"]
    ui = {e["path"]: e for e in v["ui"]}
    assert ui["EnemyHP"]["text"] == "Enemy HP 6/20" and ui["Hand/Card1"].get("disabled") is True
    st = v["state"]["."]
    assert set(st["changed"]) == {"enemy_hp", "energy"} and set(list(st)[:2]) == {"enemy_hp", "energy"}  # changed first
    b.step(4, _click(1, ui["EndTurn"]["at"]))
    b.step(4, _click(1, cards["Strike (1) / deal 6"]["at"]))
    ui = {e["path"]: e for e in b.look(views=["ui"])["views"]["ui"]}
    assert ui["Result"]["text"] == "Victory!"


def test_view_arguments_and_errors(session):
    b = session(FIXTURES / "sokoban", views=["grid"])
    v = b.look(views=["state:player", "grid:Walls", "nope"])["views"]
    assert list(v["state:player"]) == ["Pieces/Player"]
    assert v["grid:Walls"]["rows"][2] == "# @B  #"  # walls layer only (no floor or goals), pieces still on top
    assert "unknown view" in v["nope"]["error"]
    assert b.look(views=["player"])["views"]["player"]["node"] == "Pieces/Player"


def test_headless_has_no_screenshots(session):
    b = session(FIXTURES / "sokoban", views=["grid"])
    assert "headless" in b.shot()["error"]


@pytest.mark.render
def test_screenshot_of_the_frozen_frame_and_window_determinism(session, tmp_path):
    script = _tap(5, "jump") + [{"at": 1, "action": "move_right"}]
    hashes = {}
    for display in ("headless", "offscreen"):
        b = session(EXAMPLES / "platformer_floaty", "res://level.tscn", display=display)
        b.step(40, script)
        if display == "offscreen":
            s = b.look(views=["player"], shot=True, shot_width=640)["shot"]
            png = Path(s["path"])
            assert png.is_file() and s["size"][0] == 640 and s["frame"] == 40
            # not a blank frame: the player (orange) is in it
            out = subprocess.run([sys.executable, "-c", "import sys;d=open(sys.argv[1],'rb').read();print(len(d))", str(png)],
                                 capture_output=True, text=True)
            assert int(out.stdout) > 2000
        hashes[display] = b.hashes()
    assert hashes["headless"] == hashes["offscreen"]  # rendering doesn't change the simulation


@pytest.mark.parametrize("fixture,kind,views", [
    ("sokoban", "grid", ["grid", "ui", "state"]),
    ("cards", "turn_based", ["ui", "state"]),
    ("tiles_platformer", "spatial", ["player", "near", "map", "ui"]),
])
def test_detect_picks_default_views(tmp_path, fixture, kind, views):
    from ghostlap.cli import main

    proj = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, proj, ignore=shutil.ignore_patterns(".godot"))
    assert main(["scan", "--project", str(proj)]) == 0
    assert main(["detect", "--project", str(proj), "--write-profile"]) == 0
    prof = yaml.safe_load((proj / ".ghostlap/profile.yaml").read_text())
    assert prof["views"] == views
    from ghostlap.detect.detect import detect
    from ghostlap.graph.store import Graph

    assert detect(Graph(Project(proj).graph_db), proj)["game_type"]["type"] == kind


def test_the_map_marks_pickups_physics_cant_see(session):
    """A coin Area2D on collision layer 0 is invisible to point queries; the map marks it by node.
    One that stopped monitoring (collected, still animating) isn't a pickup any more."""
    b = session(Path(__file__).resolve().parent / "fixtures" / "incidents", "res://coins.tscn", profile={})
    m = b.look(views=["map"])["views"]["map"]
    prow = next(r for r in m["rows"] if "P" in r)
    assert prow.count("o") == 1 and prow.index("o") > prow.index("P"), m


def test_near_gives_the_top_edge_of_slopes(session):
    """The map can't draw a slope: near lists a sloped solid's top edge, left to right."""
    b = session(Path(__file__).resolve().parent / "fixtures" / "incidents", "res://slopes.tscn", profile={})
    near = {e["name"]: e for e in b.look(views=["near"], radius=20)["views"]["near"]}
    assert near["Ramp"]["surface"] == [[-100, 280], [100, 200]], near["Ramp"]
    tilted = near["Tilted"]["surface"]
    assert len(tilted) == 2 and tilted[0][0] < tilted[1][0] and tilted[0][1] < tilted[1][1], tilted   # slopes down to the right
