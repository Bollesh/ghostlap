extends Area2D
## Below the level: falling in restarts it.


func _on_body_entered(body: Node2D) -> void:
	if body.is_in_group("player"):
		get_tree().reload_current_scene.call_deferred()
