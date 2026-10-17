extends Area2D
## Spikes that send the player back to the start (a checkpoint respawn).


func _on_body_entered(body: Node2D) -> void:
	if body.is_in_group("player"):
		body.set_deferred("global_position", Vector2(0, 250))
