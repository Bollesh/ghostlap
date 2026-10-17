extends CharacterBody2D
## Platformer player. Every feel-relevant number is exported so gamefeel can point at it.

@export var speed := 220.0
@export var acceleration := 500.0
@export var friction := 400.0
@export var jump_velocity := -420.0
@export var gravity := 700.0
@export var fall_gravity_multiplier := 1.0
@export var coyote_time := 0.1
@export var jump_buffer_time := 0.1
## Seconds the game freezes when the player is hit (0 = no hitstop).
@export var hitstop_time := 0.07
## Seconds of invulnerability after being hit.
@export var invulnerable_time := 1.0
## Seconds after a hit during which movement input is ignored (lets knockback play out).
@export var hurt_stun_time := 0.2
## Multiplier applied to upward velocity when jump is released early (1.0 = no variable jump).
@export var jump_cut := 0.5

var health := 3
var _coyote_left := 0.0
var _buffer_left := 0.0
var _invulnerable_left := 0.0
var _stun_left := 0.0

@onready var _body: CanvasItem = $Body
@onready var _camera: Camera2D = $Camera2D
var _shake := 0.0
var _shake_time := 0.0


func _physics_process(delta: float) -> void:
	_invulnerable_left = maxf(_invulnerable_left - delta, 0.0)
	_stun_left = maxf(_stun_left - delta, 0.0)
	var on_floor := is_on_floor()
	if on_floor:
		_coyote_left = coyote_time
	else:
		_coyote_left = maxf(_coyote_left - delta, 0.0)
		var g := gravity * (fall_gravity_multiplier if velocity.y > 0.0 else 1.0)
		velocity.y += g * delta

	if Input.is_action_just_pressed("jump"):
		_buffer_left = jump_buffer_time + 0.0001
	var wants_jump := _buffer_left > 0.0
	_buffer_left = maxf(_buffer_left - delta, 0.0)
	if wants_jump and (on_floor or _coyote_left > 0.0):
		velocity.y = jump_velocity
		_coyote_left = 0.0
		_buffer_left = 0.0
	if Input.is_action_just_released("jump") and velocity.y < 0.0:
		velocity.y *= jump_cut

	var dir := Input.get_axis("move_left", "move_right")
	if _stun_left > 0.0:
		pass  # knockback keeps its velocity
	elif dir != 0.0:
		velocity.x = move_toward(velocity.x, dir * speed, acceleration * delta)
	else:
		velocity.x = move_toward(velocity.x, 0.0, friction * delta)
	move_and_slide()


func _process(delta: float) -> void:
	# hit feedback: blink while invulnerable, decaying camera shake (deterministic, no randomness)
	_body.visible = _invulnerable_left <= 0.0 or int(_invulnerable_left * 20.0) % 2 == 0
	_shake = move_toward(_shake, 0.0, 40.0 * delta)
	_shake_time += delta
	_camera.offset = Vector2(sin(_shake_time * 97.0), cos(_shake_time * 71.0)) * _shake


func _flash() -> void:
	# white flash, then red, then back to normal (tweens slow down with hitstop, which reads well)
	var tween := create_tween()
	_body.modulate = Color(3.0, 3.0, 3.0)
	tween.tween_property(_body, "modulate", Color(1.0, 0.35, 0.35), 0.06)
	tween.tween_property(_body, "modulate", Color.WHITE, 0.25)


func take_damage(amount: int, source: Node2D) -> void:
	if _invulnerable_left > 0.0:
		return
	_invulnerable_left = invulnerable_time
	_stun_left = hurt_stun_time
	health -= amount
	velocity = Vector2(signf(global_position.x - source.global_position.x) * 300.0, -250.0)
	_flash()
	_shake = 8.0
	if hitstop_time > 0.0:
		Engine.time_scale = 0.05
		await get_tree().create_timer(hitstop_time, true, false, true).timeout
		Engine.time_scale = 1.0
