extends Node3D
## Follows the parent body with a lag (top_level so it doesn't inherit the body's motion).

@export var follow_speed := 2.5
@export var offset := Vector3(0, 3, 6)

@onready var _target: Node3D = get_parent()


func _ready() -> void:
	global_position = _target.global_position + offset


func _physics_process(delta: float) -> void:
	global_position = global_position.lerp(_target.global_position + offset, minf(follow_speed * delta, 1.0))
