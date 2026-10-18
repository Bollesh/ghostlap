extends Node
## A planted problem: on physics frame `at`, does `kind`.

@export var kind := ""
@export var at := 30
@export var target := ""   # show: the node to make visible; change: the scene to go to
var _n := 0


func _ready() -> void:
	if kind == "load_error":
		var missing: Node = null
		missing.queue_free()   # SCRIPT ERROR while the scene loads (frame 0)


func _physics_process(_delta: float) -> void:
	_n += 1
	if kind == "flood" and _n >= at:
		push_error("value %d out of range" % _n)   # a new message every frame
	if _n != at:
		return
	var player: Node = get_tree().get_first_node_in_group("player")
	match kind:
		"warn":
			push_warning("planted warning")   # a warning is not an incident
		"die_reload":
			player.health = 0
			get_tree().reload_current_scene.call_deferred()
		"error":
			var missing: Node = null
			missing.queue_free()   # SCRIPT ERROR: null instance
		"crash":
			OS.crash("planted crash")
		"hang":
			while true:
				pass
		"die":
			player.health = 0
		"free":
			player.queue_free()
		"show":
			get_node(target).show()
		"change":
			get_tree().change_scene_to_file(target)
		"chaos":
			# an unseeded generator: the replay check can't reproduce this frame
			var rng := RandomNumberGenerator.new()
			rng.randomize()
			(get_node(target) as Node2D).position.x += rng.randf_range(1.0, 100.0)
