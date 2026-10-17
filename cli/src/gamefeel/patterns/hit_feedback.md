# Pattern: hit feedback (several channels at once)

A hit should register through more than one sense. gamefeel's `impact.feedback` counts the
channels it sees within 0.75 s of a hit: flash, blink, camera shake, hitstop, sound,
particles, hurt animation. Two or more reads clearly; one is easy to miss.

```gdscript
@onready var _sprite: CanvasItem = $Sprite2D        # or your visual node
@onready var _camera: Camera2D = $Camera2D
@onready var _hurt_sfx: AudioStreamPlayer2D = $HurtSfx
var _shake := 0.0

func take_damage(amount: int, source: Node) -> void:
	# ... damage, knockback, hitstop
	_flash()
	_shake = 8.0
	_hurt_sfx.play()

func _process(delta: float) -> void:
	_shake = move_toward(_shake, 0.0, 40.0 * delta)
	_camera.offset = Vector2(randf_range(-1, 1), randf_range(-1, 1)) * _shake

func _flash() -> void:
	var tween := create_tween()
	_sprite.modulate = Color(3, 3, 3)                   # white flash (values > 1 brighten)
	tween.tween_property(_sprite, "modulate", Color(1, 0.35, 0.35), 0.06)
	tween.tween_property(_sprite, "modulate", Color.WHITE, 0.25)
```

Blinking while invulnerable (toggle `visible` every few frames) adds a channel and tells the
player *why* the next hit didn't hurt. See also `gamefeel pattern hitstop`, `camera_shake`,
`invulnerability`.
