extends Node2D
## Records [iteration, where, what] whenever an input is seen, so tests can check timing.
## iteration = Engine.get_process_frames().

var events: Array = []
var ready_iteration := -1
var physics_iterations: Array = []   # iteration of every _physics_process call
var noise := 0.0                     # from the global RNG: same seed -> same value
var _k_phys := false
var _k_proc := false
@onready var body: CharacterBody2D = $Body


func _ready() -> void:
	ready_iteration = Engine.get_process_frames()
	noise = randf()
	$UI/Button.pressed.connect(func(): events.append([Engine.get_process_frames(), "signal", "button"]))


func _physics_process(delta: float) -> void:
	var it := Engine.get_process_frames()
	physics_iterations.append(it)
	if Input.is_action_just_pressed("jump"):
		events.append([it, "physics", "jump"])
	if Input.is_action_just_pressed("hitstop"):
		events.append([it, "physics", "hitstop"])
		Engine.time_scale = 0.0
		get_tree().create_timer(0.1, true, false, true).timeout.connect(_end_hitstop)
	if Input.is_action_just_pressed("pause"):
		events.append([it, "physics", "pause"])
		get_tree().paused = true
	if Input.is_action_just_pressed("reload"):
		get_tree().reload_current_scene.call_deferred()
	var k := Input.is_key_pressed(KEY_K)
	if k and not _k_phys:
		events.append([it, "physics", "key_k"])
	_k_phys = k
	body.velocity.x = Input.get_axis("move_left", "move_right") * 200.0 + noise
	body.velocity.y += 980.0 * delta
	if body.is_on_floor() and Input.is_action_just_pressed("jump"):
		body.velocity.y = -400.0
	body.move_and_slide()


func _process(_d: float) -> void:
	var it := Engine.get_process_frames()
	if Input.is_action_just_pressed("jump"):
		events.append([it, "process", "jump"])
	var k := Input.is_key_pressed(KEY_K)
	if k and not _k_proc:
		events.append([it, "process", "key_k"])
	_k_proc = k


func _unhandled_input(e: InputEvent) -> void:
	var it := Engine.get_process_frames()
	if e is InputEventKey and e.keycode == KEY_K and e.pressed and not e.echo:
		events.append([it, "input", "key_k"])
	elif e is InputEventAction and e.action == "jump" and e.pressed:
		events.append([it, "input", "jump"])


func _end_hitstop() -> void:
	Engine.time_scale = 1.0
	events.append([Engine.get_process_frames(), "timer", "hitstop_end"])
