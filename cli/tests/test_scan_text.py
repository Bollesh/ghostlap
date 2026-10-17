import os
import time

from ghostlap.graph.store import Graph
from ghostlap.project import Project
from ghostlap.scan.run import scan
from ghostlap.scan.text import parse_project_text, parse_scene_text


def test_scene_and_project_text(example):
    root = example("platformer_floaty")
    level = parse_scene_text((root / "level.tscn").read_text())
    player = next(n for n in level["nodes"] if n["name"] == "Player")
    assert player["instance"] == "res://player.tscn" and player["path"] == "Player"
    spikes = parse_scene_text((root / "spikes.tscn").read_text())
    assert spikes["connections"][0]["method"] == "_on_body_entered"
    proj = parse_project_text((root / "project.godot").read_text())
    assert proj["main_scene"] == "res://level.tscn"
    assert proj["inputs"]["jump"][0] == {"type": "key", "code": 32}


def test_incremental_scan(example):
    root = example("platformer_floaty")
    p = Project(root)
    r = scan(p, text_only=True)
    assert r.counts["changed"] == 9 and r.mode == "text"
    assert scan(p, text_only=True).counts["unchanged"] == 8

    time.sleep(0.01)
    os.utime(root / "player.gd")
    r = scan(p, text_only=True)
    assert r.counts["rehashed"] == 1 and r.counts["re_extracted"] == 0

    (root / "player.gd").write_text((root / "player.gd").read_text().replace("speed := 220.0", "speed := 230.0"))
    r = scan(p, text_only=True)
    assert r.counts["changed"] == 1 and r.dependents == 1  # player.tscn attaches it
    assert Graph(p.graph_db).node("prop:res://player.gd::speed")["props"]["default"] == 230.0

    (root / "spikes.gd").rename(root / "hazard.gd")
    for f in root.glob("spikes.gd.uid"):
        f.rename(root / "hazard.gd.uid")
    r = scan(p, text_only=True)
    assert r.counts["renamed"] == 1
    g = Graph(p.graph_db)
    assert g.node("script:res://hazard.gd") and not g.node("script:res://spikes.gd")

    (root / "level.tscn").unlink()
    r = scan(p, text_only=True)
    assert r.counts["deleted"] == 1
    assert not Graph(p.graph_db).node("scene:res://level.tscn")
