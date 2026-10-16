from gamefeel.scan.gdscript import parse_gdscript

SRC = '''class_name Hero
extends CharacterBody2D

signal died(cause)
@export var jump_velocity := -400.0
@export_range(0, 1) var coyote_time: float = 0.1
const SPEED = 200.0
var _hidden := 1

func _physics_process(delta: float) -> void:
	var dir := Input.get_axis(&"move_left", "move_right")
	if Input.is_action_just_pressed("jump") and is_on_floor():
		velocity.y = jump_velocity
	velocity.y += gravity * delta
	Engine.time_scale = 0.1
	died.emit("fall")
	move_and_slide()

func _unhandled_input(event):
	if event.is_action_pressed("attack"):
		emit_signal("died", "x")
'''


def test_structure():
    r = parse_gdscript(SRC)
    assert r["class_name"] == "Hero" and r["extends"] == "CharacterBody2D"
    props = {p["name"]: p for p in r["properties"]}
    assert props["jump_velocity"]["exported"] and props["coyote_time"]["exported"]
    assert not props["_hidden"]["exported"]
    assert "SPEED" in r["constants"]
    assert [m["name"] for m in r["methods"]] == ["_physics_process", "_unhandled_input"]
    assert r["signals"][0]["name"] == "died"


def test_facts():
    f = parse_gdscript(SRC)["facts"]
    reads = {(x["call"], tuple(x["actions"])) for x in f["reads_input"]}
    assert ("get_axis", ("move_left", "move_right")) in reads
    assert ("is_action_just_pressed", ("jump",)) in reads
    assert ("is_action_pressed", ("attack",)) in reads
    targets = {(a["target"], a["op"]) for a in f["assigns"]}
    assert ("velocity.y", "+=") in targets and ("Engine.time_scale", "=") in targets
    assert {e["signal"] for e in f["emits"]} == {"died"}


def test_broken_function_still_yields_input_reads():
    broken = SRC.replace("move_and_slide()", "move_and_slide(")
    r = parse_gdscript(broken)
    assert r["errors"], "parse error should be reported"
    reads = {a for x in r["facts"]["reads_input"] for a in x["actions"]}
    assert {"move_left", "move_right", "jump"} <= reads
