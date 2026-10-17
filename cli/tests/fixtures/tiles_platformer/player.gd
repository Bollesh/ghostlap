extends CharacterBody2D

func _physics_process(delta: float) -> void:
	velocity.x = Input.get_axis("move_left", "move_right") * 160.0
	velocity.y += 900.0 * delta
	if is_on_floor() and Input.is_action_just_pressed("jump"):
		velocity.y = -380.0
	move_and_slide()
