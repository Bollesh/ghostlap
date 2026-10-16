# Pattern: stronger gravity while falling

Most jumps feel better when the fall is faster than the rise. With the same gravity up and
down, the arc feels floaty.

```gdscript
@export var gravity := 1745.0
@export var fall_gravity_multiplier := 2.0  # rise/fall time ratio ≈ sqrt(multiplier)

func _physics_process(delta: float) -> void:
	if not is_on_floor():
		var falling := velocity.y > 0.0  # 2D; in 3D use velocity.y < 0.0
		velocity.y += gravity * (fall_gravity_multiplier if falling else 1.0) * delta
```

`jump.fall_rise_ratio` ≈ sqrt(fall_gravity_multiplier); aim for ~1.3–2.2.
