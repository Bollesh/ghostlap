extends CharacterBody2D
## Top-down player (Zelda-like). Feel-relevant numbers are exported.

@export var speed := 200.0
@export var acceleration := 1400.0
@export var friction := 1600.0
## Clamp diagonal input to length 1 (otherwise diagonals are ~41% faster).
@export var normalize_diagonal := true
@export var dash_speed := 600.0
@export var dash_time := 0.15
## Seconds the game freezes when the player is hit (0 = no hitstop).
@export var hitstop_time := 0.08
## Seconds after a hit during which movement input is ignored.
@export var hurt_stun_time := 0.15
## Seconds of invulnerability after a hit.
@export var invulnerable_time := 0.75
@export var dash_cooldown := 0.5

var health := 5
var _dash_left := 0.0
var _cooldown_left := 0.0
var _dash_dir := Vector2.RIGHT
var _facing := Vector2.RIGHT
var _stun_left := 0.0
var _invulnerable_left := 0.0
@onready var _body: CanvasItem = $Body
@onready var _camera: Camera2D = $Camera2D
var _shake := 0.0
var _shake_time := 0.0


func _physics_process(delta: float) -> void:
	_cooldown_left = maxf(_cooldown_left - delta, 0.0)
	_stun_left = maxf(_stun_left - delta, 0.0)
	_invulnerable_left = maxf(_invulnerable_left - delta, 0.0)
	var input := Vector2(Input.get_axis("move_left", "move_right"), Input.get_axis("move_up", "move_down"))
	if normalize_diagonal:
		input = input.limit_length(1.0)
	if input != Vector2.ZERO:
		_facing = input.normalized()
	if _stun_left > 0.0:
		pass  # knockback plays out
	elif _dash_left > 0.0:
		_dash_left -= delta
		velocity = _dash_dir * dash_speed
	elif Input.is_action_just_pressed("dash") and _cooldown_left <= 0.0:
		_dash_left = dash_time
		_cooldown_left = dash_cooldown
		_dash_dir = _facing
		velocity = _dash_dir * dash_speed
	elif input != Vector2.ZERO:
		velocity = velocity.move_toward(input * speed, acceleration * delta)
	else:
		velocity = velocity.move_toward(Vector2.ZERO, friction * delta)
	move_and_slide()


func _process(delta: float) -> void:
	# hit feedback: blink while invulnerable, decaying camera shake (deterministic, no randomness)
	_body.visible = _invulnerable_left <= 0.0 or int(_invulnerable_left * 20.0) % 2 == 0
	_shake = move_toward(_shake, 0.0, 40.0 * delta)
	_shake_time += delta
	_camera.offset = Vector2(sin(_shake_time * 97.0), cos(_shake_time * 71.0)) * _shake


func _flash() -> void:
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
	velocity = (global_position - source.global_position).normalized() * 250.0
	_flash()
	_shake = 6.0
	if hitstop_time > 0.0:
		Engine.time_scale = 0.05
		await get_tree().create_timer(hitstop_time, true, false, true).timeout
		Engine.time_scale = 1.0
