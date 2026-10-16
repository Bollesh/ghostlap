extends CharacterBody3D
## Third-person 3D player. Movement is world-relative to keep the example small.

@export var speed := 5.0
@export var acceleration := 40.0
@export var friction := 40.0
@export var jump_velocity := 5.0
@export var gravity := 14.0
@export var fall_gravity_multiplier := 1.6
## How fast the model turns to face movement (higher = snappier).
@export var turn_speed := 3.0

var health := 3
@onready var _model: Node3D = $Model


func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity.y -= gravity * (fall_gravity_multiplier if velocity.y < 0.0 else 1.0) * delta
	if Input.is_action_just_pressed("jump") and is_on_floor():
		velocity.y = jump_velocity
	var input := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	var dir := Vector3(input.x, 0.0, input.y)
	var planar := Vector2(velocity.x, velocity.z)
	if dir != Vector3.ZERO:
		planar = planar.move_toward(Vector2(dir.x, dir.z) * speed, acceleration * delta)
		var target_yaw := atan2(-dir.x, -dir.z)
		_model.rotation.y = lerp_angle(_model.rotation.y, target_yaw, minf(turn_speed * delta, 1.0))
	else:
		planar = planar.move_toward(Vector2.ZERO, friction * delta)
	velocity.x = planar.x
	velocity.z = planar.y
	move_and_slide()


func take_damage(amount: int, source: Node3D) -> void:
	health -= amount
	var away := (global_position - source.global_position)
	away.y = 0.0
	velocity = away.normalized() * 6.0 + Vector3.UP * 3.0
