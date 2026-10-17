# Pattern: hitstop (hit freeze)

Freeze or slow the game for a few frames when a hit lands. It sells impact more than any
particle effect.

```gdscript
@export var hitstop_time := 0.08  # seconds of real time

func take_damage(amount: int, source: Node) -> void:
	# ... apply damage / knockback first
	if hitstop_time > 0.0:
		Engine.time_scale = 0.05
		# ignore_time_scale = true so the timer runs on real time
		await get_tree().create_timer(hitstop_time, true, false, true).timeout
		Engine.time_scale = 1.0
```

Notes: use one hitstop owner (an autoload) if several things can trigger it, so two hits
don't fight over `Engine.time_scale`. Verify: `impact.hitstop_frames` 2–8.
