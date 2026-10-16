# gamefeel probe runner.
#
#   godot --headless --path <project> --fixed-fps <physics_tps> --script <abs>/runner.gd -- --plan=<abs.json>
#
# The plan (compiled by `gamefeel probe run` from YAML probes) lists probes; each probe has an
# optional calibration run and one or more runs. For every run we build a test arena (or load a
# real scene), instance the player, let it settle, then inject inputs on exact physics frames
# and record one sample per physics frame.
#
# Input timing contract: an input scheduled `at: f` is applied at the END of tick f-1, so on
# tick f both Input.is_action_pressed() and is_action_just_pressed() are true, exactly like a
# real key press arriving between frames.
extends SceneTree

const Arena = preload("arena.gd")
const Sampler = preload("sampler.gd")

var plan: Dictionary
var runner: Node


func _init() -> void:
	var args := {}
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--") and a.contains("="):
			var kv := a.substr(2).split("=", true, 1)
			args[kv[0]] = kv[1]
	var f := FileAccess.open(args.get("plan", ""), FileAccess.READ)
	if f == null:
		printerr("gamefeel probe: cannot read --plan=", args.get("plan", ""))
		quit(2)
		return
	plan = JSON.parse_string(f.get_as_text())
	runner = ProbeRunner.new(plan, self, get_script().resource_path.get_base_dir())
	runner.name = "GamefeelProbeRunner"
	runner.process_mode = Node.PROCESS_MODE_ALWAYS
	runner.process_physics_priority = 100000
	root.add_child.call_deferred(runner)


class ProbeRunner extends Node:
	enum State { NEXT, SETUP, SETTLE, RECORD, DONE }

	var plan: Dictionary
	var tree: SceneTree
	var state := State.NEXT
	var queue: Array = []          # [{probe, run, is_cal}]
	var cur: Dictionary
	var cal_vars := {}             # probe id -> {cal_*: value}
	var arena: Node
	var level: Node
	var player: Node
	var sampler: RefCounted
	var schedule := {}             # frame -> [input dicts]
	var frame := 0
	var settle_ticks := 0
	var settle_still := 0
	var settle_floor := 0
	var process_frames := 0
	var run_process_start := 0
	var logger: Object = null
	var summary := {"runs": [], "errors": []}
	var player_ps: PackedScene
	var base_dir: String

	func _init(p: Dictionary, t: SceneTree, dir: String) -> void:
		plan = p
		tree = t
		base_dir = dir

	func _ready() -> void:
		if ClassDB.class_exists("Logger"):
			logger = load(base_dir.path_join("../scan/error_logger.gd").simplify_path()).new()
			OS.call("add_logger", logger)
		player_ps = load(plan["player"]["scene"])
		if player_ps == null:
			_fatal("cannot load player scene " + str(plan["player"]["scene"]))
			return
		DirAccess.make_dir_recursive_absolute(plan["out_dir"])
		for probe in plan["probes"]:
			if probe.get("calibrate") != null:
				queue.append({"probe": probe, "run": probe["calibrate"], "is_cal": true})
			for r in probe["runs"]:
				queue.append({"probe": probe, "run": r, "is_cal": false})

	func _fatal(msg: String) -> void:
		summary["errors"].append({"kind": "fatal", "message": msg})
		_finish()

	func _process(_d: float) -> void:
		process_frames += 1
		if state != State.DONE and process_frames > int(plan.get("max_process_frames", 200000)):
			summary["errors"].append({"kind": "fatal", "message": "global frame budget exceeded"})
			_finish()
			return
		if state == State.RECORD and sampler != null:
			sampler.process_sample(process_frames - run_process_start, tree)
			if process_frames - run_process_start > int(cur["run"].get("frames", 180)) * 8 + 600:
				_end_run("timeout: game clock stalled (time_scale 0 or paused?)")

	func _physics_process(_d: float) -> void:
		match state:
			State.NEXT:
				# free the previous run's nodes this tick; build the next run on the following tick
				_teardown()
				state = State.SETUP
			State.SETUP:
				_start_next()
			State.SETTLE:
				_settle_tick()
			State.RECORD:
				_record_tick()

	# ------------------------------------------------------------ run lifecycle

	func _start_next() -> void:
		if queue.is_empty():
			_finish()
			return
		cur = queue.pop_front()
		var probe: Dictionary = cur["probe"]
		var run: Dictionary = cur["run"]
		if logger:
			logger.take()
		var err := _setup(probe, run)
		if err != "":
			summary["runs"].append({"probe": probe["id"], "run": run.get("name", "?"), "is_cal": cur["is_cal"], "error": err})
			state = State.NEXT
			return
		var sched_err := _build_schedule(probe, run)
		if sched_err != "":
			summary["runs"].append({"probe": probe["id"], "run": run.get("name", "?"), "is_cal": cur["is_cal"], "error": sched_err})
			state = State.NEXT
			return
		settle_ticks = 0
		settle_still = 0
		settle_floor = 0
		state = State.SETTLE

	func _setup(probe: Dictionary, run: Dictionary) -> String:
		var dims: String = plan["dims"]
		var arena_name: String = run.get("arena", probe.get("arena", "flat"))
		var scene_spec = probe.get("scene")
		var holder: Node
		if scene_spec != null:
			var ps: PackedScene = load(scene_spec["path"])
			if ps == null:
				return "cannot load scene " + str(scene_spec["path"])
			level = ps.instantiate()
			add_child(level)
			player = _find_player_in(level)
			if player == null:
				player = _instance_player()
				if player == null:
					return "cannot instance player"
				level.add_child(player)
			holder = level
		else:
			arena = Arena.new()
			add_child(arena)
			player = _instance_player()
			if player == null:
				return "cannot instance player"
			arena.add_child(player)
			holder = arena
		sampler = Sampler.new(player, dims, plan.get("hitbox_pattern", ""), plan.get("hitbox_paths", []), plan.get("track_props", []))
		if arena != null:
			var vars: Dictionary = cal_vars.get(probe["id"], {})
			arena.build(arena_name, dims, sampler.size, player, vars)
			if arena_name == "hazard":
				var hz := _spawn_hazard()
				if hz != "":
					return hz
		var spawn = probe.get("spawn")
		if scene_spec != null and scene_spec.get("spawn"):
			var marker := level.get_node_or_null(NodePath(scene_spec["spawn"]))
			if marker == null:
				return "spawn node not found: " + str(scene_spec["spawn"])
			player.global_position = marker.global_position
		elif arena != null:
			player.global_position = arena.spawn_position(arena_name, dims, sampler.size)
		if arena != null and arena.hazard != null:
			arena.place_hazard(player.global_position, dims, sampler.size, cal_vars.get(probe["id"], {}))
		if spawn is Dictionary and spawn.has("offset"):
			var o: Array = spawn["offset"]
			if dims == "3d":
				player.global_position += Vector3(o[0], o[1], o[2] if o.size() > 2 else 0.0) * sampler.size.y
			else:
				player.global_position += Vector2(o[0], o[1]) * sampler.size.y
		return ""

	func _instance_player() -> Node:
		var inst := player_ps.instantiate()
		var path: String = plan["player"].get("node", ".")
		if path == "." or path == "":
			return inst
		var p := inst.get_node_or_null(NodePath(path))
		if p == null:
			inst.free()
			return null
		p.get_parent().remove_child(p)
		inst.free()
		return p

	func _find_player_in(n: Node) -> Node:
		if n.scene_file_path == plan["player"]["scene"]:
			return n
		for c in n.get_children():
			var r := _find_player_in(c)
			if r != null:
				return r
		return null

	func _spawn_hazard() -> String:
		var hz_path = plan.get("hazard")
		if hz_path == null or hz_path == "":
			return "this probe needs a hazard scene: set `hazard:` in .gamefeel/profile.yaml"
		var ps: PackedScene = load(hz_path)
		if ps == null:
			return "cannot load hazard scene " + str(hz_path)
		var hz := ps.instantiate()
		arena.add_child(hz)
		arena.hazard = hz
		return ""

	func _build_schedule(probe: Dictionary, run: Dictionary) -> String:
		schedule.clear()
		var names := ["k"]
		var values := [run.get("vars", {}).get("k", 0)]
		var cv: Dictionary = cal_vars.get(probe["id"], {})
		for key in cv:
			names.append(key)
			values.append(cv[key])
		for inp in run.get("inputs", []):
			var at = inp["at"]
			var f: int
			if typeof(at) == TYPE_STRING:
				var e := Expression.new()
				if e.parse(at, names) != OK:
					return "bad `at` expression '%s': %s" % [at, e.get_error_text()]
				var v = e.execute(values, null, false)
				if e.has_execute_failed() or v == null:
					var missing := []
					for word in at.replace("(", " ").replace(")", " ").replace("+", " ").replace("-", " ").split(" ", false):
						if word.begins_with("cal_") and not cv.has(word):
							missing.append(word)
					if not missing.is_empty():
						return "calibration run produced no %s (e.g. the player never left/landed on the floor; players without is_on_floor() can't be measured this way)" % ", ".join(missing)
					return "cannot evaluate `at` '%s'" % at
				f = int(round(float(v)))
			else:
				f = int(at)
			if f < 0:
				continue
			if not schedule.has(f):
				schedule[f] = []
			schedule[f].append(inp)
		return ""

	func _broken() -> bool:
		# a script error inside _setup aborts that function and can leave us half-built
		if sampler == null:
			_end_run("run setup failed (see script_errors)")
			return true
		if not is_instance_valid(player) or not player.is_inside_tree():
			_end_run("the player was freed or removed during the run (did game code kill/despawn it?)")
			return true
		return false

	func _settle_tick() -> void:
		if _broken():
			return
		settle_ticks += 1
		var run: Dictionary = cur["run"]
		var mode: String = run.get("settle", cur["probe"].get("settle", "auto"))
		var done := false
		if mode == "none":
			done = true
		else:
			var on_floor = sampler.on_floor()
			if on_floor == true:
				settle_floor += 1
			else:
				settle_floor = 0
			if sampler.speed() < 0.001 * sampler.size.y:
				settle_still += 1
			else:
				settle_still = 0
			done = settle_floor >= 3 or (settle_still >= 10 and on_floor != false) or (settle_still >= 20) or settle_ticks >= 240
		if done:
			frame = 0
			_apply_inputs(0)
			sampler.begin()
			run_process_start = process_frames
			state = State.RECORD

	func _record_tick() -> void:
		if _broken():
			return
		sampler.sample(frame)
		frame += 1
		if frame >= int(cur["run"].get("frames", 180)):
			_end_run("")
			return
		_apply_inputs(frame)

	func _apply_inputs(f: int) -> void:
		for inp in schedule.get(f, []):
			var action: String = inp["action"]
			if not InputMap.has_action(action):
				summary["errors"].append({"kind": "input", "message": "unknown action " + action})
				continue
			var ev := InputEventAction.new()
			ev.action = action
			if inp.get("pressed", true):
				var s := float(inp.get("strength", 1.0))
				Input.action_press(action, s)
				ev.pressed = true
				ev.strength = s
			else:
				Input.action_release(action)
				ev.pressed = false
			# deliver as an event too, for code that uses _input/_unhandled_input
			get_viewport().push_input(ev, true)
			sampler.note_input(f, action, inp.get("pressed", true))

	func _end_run(error: String) -> void:
		var probe: Dictionary = cur["probe"]
		var run: Dictionary = cur["run"]
		var run_name: String = run.get("name", "run")
		var dir: String = plan["out_dir"].path_join(probe["id"])
		DirAccess.make_dir_recursive_absolute(dir)
		var file_name := ("_calibrate" if cur["is_cal"] else run_name.validate_filename()) + ".jsonl"
		var errors := []
		if logger:
			errors = logger.take()
		var header := {
			"probe": probe["id"], "run": run_name, "is_cal": cur["is_cal"], "vars": run.get("vars", {}),
			"cal": cal_vars.get(probe["id"], {}), "dims": plan["dims"], "physics_fps": plan["physics_fps"],
			"size": [sampler.size.x, sampler.size.y] if sampler else [0, 0], "arena": run.get("arena", probe.get("arena", "flat")),
			"error": error, "script_errors": errors, "frames": frame, "settle_ticks": settle_ticks,
			"player_type": player.get_class() if player else "",
		}
		var f := FileAccess.open(dir.path_join(file_name), FileAccess.WRITE)
		f.store_line(JSON.stringify(header))
		if sampler != null:
			for s in sampler.samples:
				f.store_line(JSON.stringify(s))
			f.store_line(JSON.stringify({"process": sampler.process_log}))
		f.close()
		if cur["is_cal"] and sampler != null:
			cal_vars[probe["id"]] = sampler.calibration_vars()
		summary["runs"].append({"probe": probe["id"], "run": run_name, "is_cal": cur["is_cal"],
				"file": dir.path_join(file_name), "error": error, "script_errors": errors.size()})
		state = State.NEXT

	func _teardown() -> void:
		for action in InputMap.get_actions():
			if Input.is_action_pressed(action):
				Input.action_release(action)
		Engine.time_scale = 1.0
		tree.paused = false
		if arena != null:
			arena.queue_free()
			arena = null
		if level != null:
			level.queue_free()
			level = null
		player = null
		sampler = null

	func _finish() -> void:
		state = State.DONE
		_teardown()
		var f := FileAccess.open(plan["out_dir"].path_join("summary.json"), FileAccess.WRITE)
		if f:
			f.store_string(JSON.stringify(summary))
			f.close()
		if logger:
			OS.call("remove_logger", logger)
		tree.quit(0)
