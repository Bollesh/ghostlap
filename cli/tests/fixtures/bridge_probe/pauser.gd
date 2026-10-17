extends Node
## Runs only while the tree is paused; unpauses after 30 frames (a stand-in for a pause menu).

var frames := 0


func _process(_d: float) -> void:
	frames += 1
	if frames >= 30:
		frames = 0
		get_parent().events.append([Engine.get_process_frames(), "pauser", "unpause"])
		get_tree().paused = false
