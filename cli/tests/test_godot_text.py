from ghostlap.scan.godot_text import parse_godot_text, parse_variant


def test_scalars_and_containers():
    assert parse_variant("true") is True
    assert parse_variant("-420.5") == -420.5
    assert parse_variant('"a \\"b\\""') == 'a "b"'
    assert parse_variant('&"jump"') == "jump"
    assert parse_variant("[1, 2, [3]]") == [1, 2, [3]]
    assert parse_variant('{"a": 1, "b": [true]}') == {"a": 1, "b": [True]}


def test_constructors():
    assert parse_variant("Vector2(24, 40)") == {"$type": "Vector2", "args": [24, 40]}
    assert parse_variant('ExtResource("1_player")') == {"$ext": "1_player"}
    assert parse_variant('SubResource("Shape_1")') == {"$sub": "Shape_1"}
    assert parse_variant('PackedStringArray("4.3")') == {"$type": "PackedStringArray", "args": ["4.3"]}
    assert parse_variant('Array[int]([1, 2])') == [1, 2]
    obj = parse_variant('Object(InputEventKey,"keycode":0,"physical_keycode":32,"script":null)')
    assert obj["$object"] == "InputEventKey" and obj["physical_keycode"] == 32


def test_bad_value_is_kept_raw():
    v = parse_variant("Vector2(1, ")
    assert "$raw" in v


def test_sections_with_multiline_values():
    text = '''[gd_scene load_steps=2 format=3 uid="uid://abc"]

[node name="Player" type="CharacterBody2D" groups=["player", "hero"]]
script = ExtResource("1")

[input]
jump={
"deadzone": 0.5,
"events": []
}
'''
    secs = parse_godot_text(text)
    node = next(s for s in secs if s.tag == "node")
    assert node.attrs["name"] == "Player" and node.attrs["groups"] == ["player", "hero"]
    assert next(s for s in secs if s.tag == "gd_scene").attrs["uid"] == "uid://abc"
    assert next(s for s in secs if s.tag == "input").props["jump"]["deadzone"] == 0.5
