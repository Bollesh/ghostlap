# Pattern: trauma-based camera shake

Shake by moving `Camera2D.offset` (or `Camera3D.h_offset`/`v_offset`), driven by a "trauma"
value that decays. ghostlap measures shake through these offsets.

```gdscript
extends Camera2D

@export var max_offset := 12.0
@export var decay := 2.5
var trauma := 0.0

func add_trauma(amount: float) -> void:
	trauma = minf(trauma + amount, 1.0)

func _process(delta: float) -> void:
	trauma = maxf(trauma - decay * delta, 0.0)
	var s := trauma * trauma
	offset = Vector2(randf_range(-1, 1), randf_range(-1, 1)) * max_offset * s
```

Call `add_trauma(0.4)` from `take_damage`. Verify: `impact.shake` > 0 after hits.
