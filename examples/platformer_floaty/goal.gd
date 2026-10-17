extends Area2D
## End of the level: `reached` turns true when the player touches it.

var reached := false


func _on_body_entered(body: Node2D) -> void:
	if body.is_in_group("player"):
		reached = true
