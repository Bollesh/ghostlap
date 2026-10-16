# Pattern: jump buffer

Remember a jump press for a few frames so pressing jump just *before* landing still jumps.

```gdscript
@export var jump_buffer_time := 0.1  # seconds
var _buffer_left := 0.0

func _physics_process(delta: float) -> void:
	if Input.is_action_just_pressed("jump"):
		_buffer_left = jump_buffer_time
	if _buffer_left > 0.0 and is_on_floor():
		velocity.y = jump_velocity
		_buffer_left = 0.0
	_buffer_left = maxf(_buffer_left - delta, 0.0)
```

Combine with coyote time by checking `(is_on_floor() or _coyote_left > 0.0)`.
Verify: `gamefeel check --only jump_buffer` → `jump.buffer_frames` 4–8.
