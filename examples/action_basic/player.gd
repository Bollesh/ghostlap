extends CharacterBody2D
## Platformer player. Every feel-relevant number is exported so gamefeel can point at it.

@export var speed := 260.0
@export var acceleration := 3600.0
@export var friction := 3000.0
@export var jump_velocity := -663.0
@export var gravity := 1745.0
@export var fall_gravity_multiplier := 2.0
@export var coyote_time := 0.1
@export var jump_buffer_time := 0.1
## Seconds the game freezes when the player is hit (0 = no hitstop).
@export var hitstop_time := 0.08
## Seconds of invulnerability after being hit.
@export var invulnerable_time := 1.0
## Seconds after a hit during which movement input is ignored (lets knockback play out).
@export var hurt_stun_time := 0.2
## Multiplier applied to upward velocity when jump is released early (1.0 = no variable jump).
@export var jump_cut := 0.45

## Attack timings, in physics frames.
@export var attack_startup := 12
@export var attack_active := 4
@export var attack_recovery := 30
## Frames after the hitbox ends during which pressing attack chains the next hit (0 = no combo).
@export var combo_window := 0

var health := 3
var _attack_frame := -1
var _queued := false
@onready var _hitbox_shape: CollisionShape2D = $Hitbox/CollisionShape2D
@onready var _anim: AnimationPlayer = $AnimationPlayer
var _coyote_left := 0.0
var _buffer_left := 0.0
var _invulnerable_left := 0.0
var _stun_left := 0.0


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
	_update_attack()
	move_and_slide()


func _update_attack() -> void:
	if Input.is_action_just_pressed("attack"):
		if _attack_frame < 0:
			_start_attack()
		elif combo_window > 0 and _attack_frame > attack_startup + attack_active and _attack_frame <= attack_startup + attack_active + combo_window:
			_queued = true
	if _attack_frame < 0:
		return
	_attack_frame += 1
	var active := _attack_frame > attack_startup and _attack_frame <= attack_startup + attack_active
	_hitbox_shape.disabled = not active
	if _queued and _attack_frame > attack_startup + attack_active:
		_queued = false
		_start_attack()
	elif _attack_frame > attack_startup + attack_active + attack_recovery:
		_attack_frame = -1
		_anim.play("idle")


func _start_attack() -> void:
	_attack_frame = 0
	_anim.play("attack")
	_anim.seek(0.0, true)


func take_damage(amount: int, source: Node2D) -> void:
	if _invulnerable_left > 0.0:
		return
	_invulnerable_left = invulnerable_time
	_stun_left = hurt_stun_time
	health -= amount
	velocity = Vector2(signf(global_position.x - source.global_position.x) * 300.0, -250.0)
	if hitstop_time > 0.0:
		Engine.time_scale = 0.05
		await get_tree().create_timer(hitstop_time, true, false, true).timeout
		Engine.time_scale = 1.0
