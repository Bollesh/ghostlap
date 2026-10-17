from ghostlap.edits import Edit, apply_edit, format_value, read_value


def _proj(tmp_path):
    (tmp_path / "player.gd").write_text(
        "extends CharacterBody2D\n"
        "@export var speed := 220.0\n"
        "@export_range(0, 1) var coyote_time: float = 0.0  # seconds\n"
        "@export var jumps := 2\n"
        "var computed := SPEED * 2\n")
    (tmp_path / "player.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="Player" type="CharacterBody2D"]\nspeed = 300.0\n\n'
        '[node name="Camera2D" type="Camera2D" parent="."]\nzoom = Vector2(2, 2)\n')
    (tmp_path / "project.godot").write_text('config_version=5\n\n[application]\n\nconfig/name="x"\n')
    return tmp_path


def test_script_defaults(tmp_path):
    root = _proj(tmp_path)
    for prop, val, expect in (("speed", 260.5, "@export var speed := 260.5"),
                              ("coyote_time", 0.1, "var coyote_time: float = 0.1  # seconds"),
                              ("jumps", 3.0, "@export var jumps := 3")):
        ok, _ = apply_edit(root, Edit("property", "res://player.gd", prop, val))
        assert ok and expect in (root / "player.gd").read_text()
    ok, msg = apply_edit(root, Edit("property", "res://player.gd", "computed", 1.0))
    assert not ok and "by hand" in msg
    assert read_value(root, Edit("property", "res://player.gd", "speed", 0)) == 260.5


def test_scene_override_and_insert(tmp_path):
    root = _proj(tmp_path)
    assert apply_edit(root, Edit("node_property", "res://player.tscn", "speed", 280.0, node="."))[0]
    assert apply_edit(root, Edit("node_property", "res://player.tscn", "position_smoothing_speed", 8.0, node="Camera2D"))[0]
    text = (root / "player.tscn").read_text()
    assert "speed = 280.0" in text and "speed = 300.0" not in text
    cam = text.split('[node name="Camera2D"')[1]
    assert "position_smoothing_speed = 8.0" in cam and "zoom = Vector2(2, 2)" in cam


def test_project_setting(tmp_path):
    root = _proj(tmp_path)
    assert apply_edit(root, Edit("setting", "res://project.godot", "physics/2d/default_gravity", 1500.0))[0]
    assert apply_edit(root, Edit("setting", "res://project.godot", "physics/2d/default_gravity", 1600.0))[0]
    text = (root / "project.godot").read_text()
    assert text.count("2d/default_gravity=") == 1 and "2d/default_gravity=1600.0" in text


def test_format():
    assert format_value(0.0870, 0.0) == "0.087" and format_value(2.0, 1.0) == "2.0"
    assert format_value(3.0, 2) == "3" and format_value(True) == "true"
