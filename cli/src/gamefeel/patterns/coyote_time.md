# Pattern: coyote time

Let the player jump for a few frames after walking off a ledge. Without it, jumps pressed a
frame "late" are silently eaten, which feels unfair even though the player was technically
in the air.

```gdscript
@export var coyote_time := 0.1  # seconds (~6 frames at 60 fps)
var _coyote_left := 0.0

func _physics_process(delta: float) -> void:
	if is_on_floor():
		_coyote_left = coyote_time
	else:
		_coyote_left = maxf(_coyote_left - delta, 0.0)
	if Input.is_action_just_pressed("jump") and (is_on_floor() or _coyote_left > 0.0):
		velocity.y = jump_velocity
		_coyote_left = 0.0  # no double use
	# ... gravity, movement, move_and_slide()
```

Verify: `gamefeel check --only coyote` → `jump.coyote_frames` should be 4–8.
