extends CharacterBody2D

var health := 3


func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity.y += 900.0 * delta
	elif Input.is_action_just_pressed("jump"):
		velocity.y = -400.0
	velocity.x = Input.get_axis("move_left", "move_right") * 200.0
	move_and_slide()
