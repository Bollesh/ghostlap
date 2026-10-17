# Pattern: variable jump height

Cut upward velocity when jump is released early, so taps make short hops and holds make full
jumps. Players read this as "control".

```gdscript
@export var jump_cut := 0.45  # fraction of upward speed kept on early release

func _physics_process(delta: float) -> void:
	# 2D (y down): moving up means velocity.y < 0
	if Input.is_action_just_released("jump") and velocity.y < 0.0:
		velocity.y *= jump_cut
	# 3D (y up): if Input.is_action_just_released("jump") and velocity.y > 0.0: velocity.y *= jump_cut
```

Verify: `jump.variable` (hold height / tap height) should be ~1.5–4.
