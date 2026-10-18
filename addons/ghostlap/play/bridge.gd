# ghostlap play bridge: runs the user's game and freezes it between commands.
#
#   godot [--headless] --path <project> --fixed-fps <physics_tps> --script <abs>/bridge.gd -- \
#       --session=<abs dir> --scene=res://level.tscn [--seed=N] [--profile=<abs profile.json>] \
#       [--shots={"every":600,"keep":5,"on":[...],"width":960}] [--hud=<abs hud.json>]
#
# --hud draws the session video's captions (hud.gd); it's only given for Movie Maker replays.
#
# profile.json (written by the CLI from .ghostlap/profile.yaml): player {scene, node}, dims,
# track_props, hazard, views (default look views), state_nodes. See observe.gd.
#
# The start scene becomes current_scene (so reload_current_scene / change_scene_to_file in game
# code work as usual). When the scene is current the bridge writes <session>/bridge.json
# ({port, token, pid}) and blocks: it serves JSON-lines requests on 127.0.0.1:<port> until a
# `step` arrives, lets exactly that many frames run, then blocks again. While it is blocked
# nothing else in the game runs, and with --fixed-fps every frame advances the same game time,
# so time spent thinking between commands changes nothing.
#
# Clock: frame = engine iteration, counted from the iteration in which the start scene became
# current (frame 0). Iterations keep counting through hitstop (Engine.time_scale = 0, no physics
# ticks) and get_tree().paused, so a step always returns.
#
# Input contract: an input scheduled for frame f is queued at the end of frame f-1 with
# Input.parse_input_event(). Godot flushes buffered input at the start of each iteration, so the
# event arrives exactly like a real key press between frames: on frame f, _input/_unhandled_input
# get it, and is_action_just_pressed() is true in both _physics_process and _process.
# (Spike 1.1: the v1 runner's Input.action_press from a callback is seen by _physics_process on
# frame f but never by _process; flushing immediately, or also push_input, delivers it a frame
# early or twice.)
extends SceneTree

const Observe = preload("observe.gd")
const Incidents = preload("incidents.gd")
const Shots = preload("shots.gd")
const Hud = preload("hud.gd")

var bridge: Node


func _init() -> void:
	var args := {}
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--") and a.contains("="):
			var kv := a.substr(2).split("=", true, 1)
			args[kv[0]] = kv[1]
	if not args.has("session") or not args.has("scene"):
		printerr("ghostlap play bridge: --session=<dir> and --scene=<res://...> are required")
		quit(2)
		return
	bridge = Bridge.new(self, args)
	bridge.name = "GhostlapBridge"
	bridge.process_mode = Node.PROCESS_MODE_ALWAYS
	# run after every game node, so a frame's game logic is complete when we block
	bridge.process_priority = 1 << 30
	bridge.process_physics_priority = 1 << 30
	root.add_child.call_deferred(bridge)


class Bridge extends Node:
	const IDLE_LIMIT_MS := 30 * 60 * 1000
	## While stepping, <session>/progress gets the frame at most this often: the CLI calls a step
	## hung when it stops changing (a `while true` in game code never returns to us). Time-based,
	## so a slow game (2 fps) still shows progress.
	const PROGRESS_MS := 250
	const _SNAP2 := Vector2(0.001, 0.001)
	const _SNAP3 := Vector3(0.001, 0.001, 0.001)

	var tree: SceneTree
	var args: Dictionary
	var session_dir: String
	var server := TCPServer.new()
	var peer: StreamPeerTCP = null
	var inbuf := PackedByteArray()
	var token := ""
	var frame := -1                 # -1 until the start scene is current
	var it0 := 0                    # Engine.get_process_frames() at frame 0
	var frames_left := 0
	var pending_peer: StreamPeerTCP = null
	var pending_id = null
	var schedule := {}              # absolute frame -> [input dicts]
	var hashes := PackedStringArray()   # state hash per frame (index = frame)
	var samples: Array = []             # player {pos, vel, floor} per frame (index = frame), {} without one
	var quitting := false
	var observe: RefCounted
	var headless := false
	var ctx: ExprContext
	var goal_expr: Expression = null    # `config goal=`: checked every frame
	var goal_met_at := -1
	var goal_error := ""                # the last check's error ("" when it evaluated fine)
	var until_expr: Expression = null   # set while an `until` runs
	var until_result := {}
	var incidents: RefCounted
	var cmd_incidents: Array = []       # incidents during the running step/until
	var shots: RefCounted
	var hud: CanvasLayer = null
	var _down := {}                     # injected presses not released yet: "action:x"/"key:x" -> input
	var _input_lost: Array = []         # re-pressed at the start of the running step (see _restore_held)
	var _progress_ms := 0

	func _init(t: SceneTree, a: Dictionary) -> void:
		tree = t
		args = a
		session_dir = a["session"]

	func _ready() -> void:
		headless = DisplayServer.get_name() == "headless"
		var prof := {}
		if args.has("profile"):
			var pf := FileAccess.open(args["profile"], FileAccess.READ)
			if pf == null:
				_fatal("cannot read --profile=" + str(args["profile"]))
				return
			var parsed = JSON.parse_string(pf.get_as_text())
			prof = parsed if parsed is Dictionary else {}
		observe = Observe.new(tree, prof)
		ctx = ExprContext.new(tree, observe)
		incidents = Incidents.new(tree, observe, prof, session_dir.path_join("godot.log"))
		shots = Shots.new(tree, session_dir, headless)
		if args.has("shots"):
			var sc = JSON.parse_string(str(args["shots"]))
			var bad: String = shots.set_config(sc) if sc is Dictionary else "not a JSON object"
			if bad != "":
				_fatal("--shots: " + bad)
				return
		if args.has("hud"):
			var hf := FileAccess.open(args["hud"], FileAccess.READ)
			var hd = JSON.parse_string(hf.get_as_text()) if hf != null else null
			if not hd is Dictionary:
				_fatal("cannot read --hud=" + str(args["hud"]))
				return
			hud = Hud.new()
			var hw := int(ProjectSettings.get_setting("display/window/size/viewport_width", 1152))
			var hh := int(ProjectSettings.get_setting("display/window/size/viewport_height", 648))
			hud.setup(hd, Vector2(hw, hh))
			add_child(hud)
		if not headless:
			_detach_window_from_desktop_input()
		if server.listen(0, "127.0.0.1") != OK:
			_fatal("cannot listen on 127.0.0.1")
			return
		# token from the OS RNG: the global RNG must stay untouched for determinism
		token = Crypto.new().generate_random_bytes(16).hex_encode()
		seed(int(args.get("seed", "0")))
		if headless:
			# the headless root viewport is 64x64; give it the project's size so GUI layout and
			# clicks match a rendered run
			var w := int(ProjectSettings.get_setting("display/window/size/viewport_width", 1152))
			var h := int(ProjectSettings.get_setting("display/window/size/viewport_height", 648))
			tree.root.size = Vector2i(w, h)
		var err := tree.change_scene_to_file(args["scene"])
		if err != OK:
			_fatal("cannot load scene %s (error %d)" % [args["scene"], err])

	# _process, not _physics_process: it runs once per iteration whatever the physics tick rate.
	# (Physics callbacks also run every iteration during hitstop and pause, with delta 0, but a
	# game whose tick rate differs from --fixed-fps would get 0 or 2 ticks per iteration.)
	func _process(_d: float) -> void:
		_end_of_frame()

	# ------------------------------------------------------------ clock

	func _end_of_frame() -> void:
		if quitting:
			return
		if frame < 0:
			if tree.current_scene == null:
				return
			frame = 0
			it0 = Engine.get_process_frames()
			_write_bridge_json()
		else:
			frame += 1
		hashes.append(_hash())
		samples.append(_sample())
		var new_incs := _check_incidents()
		var met_before := goal_met_at
		_check_goal()
		shots.tick(frame, new_incs, goal_met_at >= 0 and met_before < 0)
		if hud != null:
			hud.show_frame(frame)
		if frames_left > 0:
			frames_left -= 1
			if Time.get_ticks_msec() - _progress_ms >= PROGRESS_MS:
				_write_progress()
			if until_expr != null:
				_check_until()
		if frames_left == 0:
			if pending_peer != null:
				shots.flush(frame)
				var res := _look()
				res.merge(_news(), true)
				res["incidents"] = cmd_incidents
				cmd_incidents = []
				if not _input_lost.is_empty():
					res["input_lost"] = _input_lost
					_input_lost = []
				if until_expr != null:
					if until_result.is_empty():
						until_result = {"met": false, "reason": "max"}
					res["until"] = until_result
					until_expr = null
					# inputs scheduled past the stop never happen
					for f in schedule.keys():
						if f > frame:
							schedule.erase(f)
				_reply(pending_peer, pending_id, {"ok": true, "result": res})
				pending_peer = null
				pending_id = null
			var held := _held_now()
			_serve_until_step()
			_restore_held(held, frame + 1)
		if not quitting:
			_apply_inputs(frame + 1)

	# ------------------------------------------------------------ socket

	func _serve_until_step() -> void:
		var idle_start := Time.get_ticks_msec()
		while true:
			if server.is_connection_available():
				peer = server.take_connection()
				inbuf = PackedByteArray()
			var line := _read_line()
			if line == "":
				if Time.get_ticks_msec() - idle_start > IDLE_LIMIT_MS:
					printerr("ghostlap play bridge: idle limit reached, quitting")
					_quit()
					return
				if not headless:
					# keep answering the compositor ("not responding" otherwise); desktop input
					# can't reach the game (see _detach_window_from_desktop_input)
					DisplayServer.process_events()
				OS.delay_msec(2)
				continue
			idle_start = Time.get_ticks_msec()
			if _handle(line):
				return

	func _read_line() -> String:
		if peer == null:
			return ""
		peer.poll()
		if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			peer = null
			return ""
		var n := peer.get_available_bytes()
		if n > 0:
			var got: Array = peer.get_partial_data(n)
			if got[0] == OK:
				inbuf.append_array(got[1])
		var nl := inbuf.find(10)
		if nl < 0:
			return ""
		var line := inbuf.slice(0, nl).get_string_from_utf8()
		inbuf = inbuf.slice(nl + 1)
		return line

	func _reply(p: StreamPeerTCP, id, body: Dictionary) -> void:
		if p == null:
			return
		body["id"] = id
		body["frame"] = frame
		# keep insertion order: views put what matters (changed state, nearest objects) first
		p.put_data((JSON.stringify(body, "", false) + "\n").to_utf8_buffer())

	## Returns true when the game should run again (a step started, or quit).
	func _handle(line: String) -> bool:
		var req = JSON.parse_string(line)
		if typeof(req) != TYPE_DICTIONARY:
			_reply(peer, null, {"ok": false, "error": "bad request"})
			return false
		var id = req.get("id")
		if req.get("token", "") != token:
			_reply(peer, id, {"ok": false, "error": "bad token"})
			return false
		var a: Dictionary = req.get("args", {})
		match str(req.get("cmd", "")):
			"look":
				var res := _look()
				var views: Array = a.get("views", []) if a.get("views") is Array else []
				if views.is_empty():
					views = observe.default_views()
				res["views"] = observe.look(views, a)
				if a.get("shot", false):
					res["shot"] = shots.capture(frame, ["requested"], int(a.get("shot_width", -1)))
				res.merge(_news(), true)
				_reply(peer, id, {"ok": true, "result": res})
			"shot":
				# `why`: requested (look --shot) or hint (a shot_hint the CLI attaches)
				var sh: Dictionary = shots.capture(frame, [str(a.get("why", "requested"))], int(a.get("width", -1)))
				sh.merge(_news(), true)
				_reply(peer, id, {"ok": true, "result": sh})
			"eval":
				_reply(peer, id, _eval(str(a.get("expr", "")), bool(a.get("write", false))))
			"config":
				_reply(peer, id, _config(a))
			"incidents":
				_reply(peer, id, {"ok": true, "result": incidents.all})
			"hashes":
				var lo := int(a.get("from", 0))
				_reply(peer, id, {"ok": true, "result": Array(hashes.slice(lo))})
			"samples":
				_reply(peer, id, {"ok": true, "result": samples.slice(int(a.get("from", 0)))})
			"step":
				var n := int(a.get("frames", 1))
				if n < 1:
					_reply(peer, id, {"ok": false, "error": "frames must be >= 1"})
					return false
				var bad := _schedule_inputs(a.get("inputs", []), n)
				if bad != "":
					_reply(peer, id, {"ok": false, "error": bad})
					return false
				frames_left = n
				pending_peer = peer
				pending_id = id
				return true
			"until":
				var mx := int(a.get("max", 600))
				if mx < 1:
					_reply(peer, id, {"ok": false, "error": "max must be >= 1"})
					return false
				var c = _compile(str(a.get("expr", "")), false)
				if c is String:
					_reply(peer, id, {"ok": false, "error": c})
					return false
				var bad_u := _schedule_inputs(a.get("inputs", []), mx)
				if bad_u != "":
					_reply(peer, id, {"ok": false, "error": bad_u})
					return false
				until_expr = c
				until_result = {}
				frames_left = mx
				pending_peer = peer
				pending_id = id
				return true
			"quit":
				_reply(peer, id, {"ok": true, "result": {"quit": true}})
				_quit()
				return true
			var other:
				_reply(peer, id, {"ok": false, "error": "unknown command " + str(other)})
		return false

	func _quit() -> void:
		quitting = true
		if incidents != null:
			incidents.close()
		tree.quit()

	# ------------------------------------------------------------ inputs

	## inputs: [{at: k (1..n, relative to now), action|key|mouse: ..., pressed: bool, ...}]
	func _schedule_inputs(inputs: Array, n: int) -> String:
		for inp in inputs:
			if typeof(inp) != TYPE_DICTIONARY:
				return "input must be an object"
			var k := int(inp.get("at", 1))
			if k < 1 or k > n:
				return "input at %d outside this step (1..%d)" % [k, n]
			if inp.has("action") and not InputMap.has_action(str(inp["action"])):
				return "unknown action " + str(inp["action"])
			if inp.has("key") and OS.find_keycode_from_string(str(inp["key"])) == KEY_NONE:
				return "unknown key " + str(inp["key"])
			var f := frame + k
			if not schedule.has(f):
				schedule[f] = []
			schedule[f].append(inp)
		return ""

	## Injected holds that are down as the game freezes.
	func _held_now() -> Array:
		return _down.keys().filter(func(k): return _is_down(_down[k]))

	## A window that gets or loses focus while frozen makes Godot release every pressed input
	## (X11 does it on focus changes), behind the game's back. Press those holds again for the
	## next frame and report them: the CLI records a release+press on that frame, which replays
	## the same (just_pressed true, just_released false).
	func _restore_held(held: Array, f: int) -> void:
		var releasing := {}
		for inp in schedule.get(f, []):
			if not bool(inp.get("pressed", true)):
				releasing[_down_key(inp)] = true
		for k in held:
			if _down.has(k) and not _is_down(_down[k]) and not releasing.has(k):
				_inject(_down[k], true)
				_input_lost.append({"f": f, "input": k})

	static func _down_key(inp: Dictionary) -> String:
		if inp.has("action"):
			return "action:" + str(inp["action"])
		if inp.has("key"):
			return "key:" + str(inp["key"])
		return ""

	static func _is_down(inp: Dictionary) -> bool:
		if inp.has("action"):
			return Input.is_action_pressed(str(inp["action"]))
		return Input.is_physical_key_pressed(OS.find_keycode_from_string(str(inp["key"])))

	func _apply_inputs(f: int) -> void:
		for inp in schedule.get(f, []):
			var pressed := bool(inp.get("pressed", true))
			var dk := _down_key(inp)
			if dk != "":
				if pressed:
					_down[dk] = inp
				else:
					_down.erase(dk)
			_inject(inp, pressed)
		schedule.erase(f)

	func _inject(inp: Dictionary, pressed: bool) -> void:
		if inp.has("action"):
			var action := str(inp["action"])
			var mapped := _mapped_event(action, pressed) if _action_events_reset_others else null
			if mapped != null:
				Input.parse_input_event(mapped)
				return
			var ev := InputEventAction.new()
			ev.action = action
			ev.pressed = pressed
			if pressed:
				ev.strength = float(inp.get("strength", 1.0))
			Input.parse_input_event(ev)
		elif inp.has("key"):
			var kev := InputEventKey.new()
			kev.keycode = OS.find_keycode_from_string(str(inp["key"]))
			kev.physical_keycode = kev.keycode
			kev.pressed = pressed
			Input.parse_input_event(kev)
		elif inp.has("mouse"):
			var pos: Array = inp["mouse"]
			var mev := InputEventMouseButton.new()
			mev.position = Vector2(pos[0], pos[1])
			mev.global_position = mev.position
			mev.button_index = int(inp.get("button", MOUSE_BUTTON_LEFT))
			mev.pressed = pressed
			if pressed:
				mev.button_mask = 1 << (mev.button_index - 1)
			Input.parse_input_event(mev)

	## Godot < 4.4: InputMap.event_get_index() ignores whether an InputEventAction matches, so
	## parsing one updates every action, releasing all the others (fixed in 4.4). There, an action
	## is injected as its first mapped key/button event instead, which matches only its own actions.
	var _action_events_reset_others: bool = Engine.get_version_info()["hex"] < 0x040400

	func _mapped_event(action: String, pressed: bool) -> InputEvent:
		for e in InputMap.action_get_events(action):
			if e is InputEventKey or e is InputEventJoypadButton or e is InputEventMouseButton:
				var d: InputEvent = e.duplicate()
				d.pressed = pressed
				if d is InputEventKey:
					if d.keycode == KEY_NONE and d.physical_keycode != KEY_NONE:
						d.keycode = d.physical_keycode
				elif d is InputEventMouseButton and pressed:
					d.button_mask = 1 << (d.button_index - 1)
				return d
			if e is InputEventJoypadMotion:
				var m: InputEventJoypadMotion = e.duplicate()
				m.axis_value = signf(e.axis_value) if pressed else 0.0
				return m
		return null

	# ------------------------------------------------------------ state

	func _look() -> Dictionary:
		var cs := tree.current_scene
		return {
			"f": frame,
			"iteration": Engine.get_process_frames() - it0,
			"it0": it0,
			"physics_frames": Engine.get_physics_frames(),
			"time_scale": Engine.time_scale,
			"paused": tree.paused,
			"scene": cs.scene_file_path if cs != null else "",
			"hash": hashes[frame] if frame >= 0 and frame < hashes.size() else "",
			"goal_met_at": goal_met_at,
			"goal_error": goal_error,
			"incidents": incidents.all.size() if incidents != null else 0,
			"suppressed_errors": incidents.suppressed_errors if incidents != null else 0,
		}

	## The player at the end of this frame, for state.jsonl and the state at evidence frames.
	func _sample() -> Dictionary:
		var p: Node = observe.find_player()
		if not (p is Node2D or p is Node3D):
			return {}
		var d := {"pos": observe._vec(p.global_position)}
		if p is CharacterBody2D or p is CharacterBody3D:
			d["vel"] = observe._vec(p.velocity)
			d["floor"] = p.is_on_floor()
		elif p is RigidBody2D or p is RigidBody3D:
			d["vel"] = observe._vec(p.linear_velocity)
		return d

	## Shots captured and UI that appeared since the last response that carried them.
	func _news() -> Dictionary:
		var n: Dictionary = shots.take_news()
		return {"new_shots": n["shots"], "new_ui": n["ui"]}

	## Positions and velocities of every 2D/3D node in the current scene, rounded so float
	## noise below 1e-3 doesn't count.
	func _hash() -> String:
		var cs := tree.current_scene
		if cs == null:
			return ""
		var parts := PackedStringArray([cs.scene_file_path, str(Engine.time_scale), str(tree.paused)])
		var stack: Array[Node] = [cs]
		while not stack.is_empty():
			var n: Node = stack.pop_back()
			if n is Node2D:
				parts.append("%s:%s" % [n.get_path(), n.global_position.snapped(_SNAP2 if n is Node2D else _SNAP3)])
			elif n is Node3D:
				parts.append("%s:%s" % [n.get_path(), n.global_position.snapped(_SNAP2 if n is Node2D else _SNAP3)])
			if n is CharacterBody2D or n is CharacterBody3D or n is RigidBody2D or n is RigidBody3D:
				var v = n.velocity if (n is CharacterBody2D or n is CharacterBody3D) else n.linear_velocity
				parts.append("v:%s" % [v.snapped(_SNAP2 if v is Vector2 else _SNAP3)])
			for c in n.get_children():
				stack.append(c)
		return "|".join(parts).md5_text()

	# ------------------------------------------------------------ expressions

	const INPUT_NAMES := ["player", "scene", "tree", "incident", "Input", "InputMap", "Engine", "ProjectSettings", "DisplayServer"]
	## Calls that change the game, its input or its RNG. A name match, not a sandbox: it stops
	## accidents, not intent (a game's own `take_damage()` still gets through).
	static var _writes := RegEx.create_from_string(
			"(^|[^\\w])(set\\w*|queue_free|free|call\\w*|emit\\w*|add_child|remove_child|change_scene\\w*|reload\\w*|quit"
			+ "|rand\\w*|seed|randomize|action_press|action_release|parse_input_event|warp_mouse"
			+ "|connect|disconnect|play\\w*|stop|start|apply_\\w*|append\\w*|push_\\w*|pop_\\w*|erase|clear|insert|remove_at|resize|fill|sort\\w*|shuffle|reverse|merge)\\s*\\(")

	## Returns an Expression, or an error string.
	func _compile(src: String, allow_write: bool):
		if src.strip_edges() == "":
			return "empty expression"
		if not allow_write and _writes.search(src) != null:
			return "expression calls a method that changes the game (%s); eval --write allows it" % _writes.search(src).get_string(2)
		var e := Expression.new()
		if e.parse(src, PackedStringArray(INPUT_NAMES)) != OK:
			return "parse: " + e.get_error_text()
		return e

	func _run(e: Expression) -> Array:
		var last = incidents.all.back() if not incidents.all.is_empty() else null
		var v = e.execute([observe.find_player(), tree.current_scene, tree, last, Input, InputMap, Engine, ProjectSettings, DisplayServer], ctx, false)
		if e.has_execute_failed():
			return [false, e.get_error_text()]
		return [true, v]

	func _eval(src: String, allow_write: bool) -> Dictionary:
		var c = _compile(src, allow_write)
		if c is String:
			return {"ok": false, "error": c}
		var r := _run(c)
		if not r[0]:
			return {"ok": false, "error": "execute: " + str(r[1])}
		var out := {"ok": true, "result": _jsonable(r[1])}
		if allow_write and _writes.search(src) != null:
			out["write"] = true
		return out

	func _config(a: Dictionary) -> Dictionary:
		if a.get("shots") is Dictionary:
			var bad: String = shots.set_config(a["shots"])
			if bad != "":
				return {"ok": false, "error": "shots: " + bad}
		if a.has("goal"):
			var g := str(a["goal"])
			if g == "":
				goal_expr = null
			else:
				var c = _compile(g, false)
				if c is String:
					return {"ok": false, "error": "goal: " + c}
				goal_expr = c
			goal_met_at = -1
			goal_error = ""
			_check_goal()
		return {"ok": true, "result": {"goal_met_at": goal_met_at, "goal_error": goal_error, "shots": shots.config()}}

	## Errors don't switch the check off: they are reported in every look until it evaluates again
	## (a freed or not-yet-spawned node is often temporary).
	func _check_goal() -> void:
		# no scene for a frame while one reloads or changes
		if goal_expr == null or goal_met_at >= 0 or tree.current_scene == null:
			return
		var r := _run(goal_expr)
		goal_error = "" if r[0] else str(r[1])
		if r[0] and _truthy(r[1]):
			goal_met_at = frame

	## Every incident ends an `until` right away; a step runs its frames and reports them. Their
	## shots are attached by shots.tick() later in the frame.
	func _check_incidents() -> Array:
		var new: Array = incidents.tick(frame)
		for inc in new:
			# frame 0 (load errors): `start` reports them via the `incidents` command
			if frame > 0:
				cmd_incidents.append(inc)
		if not new.is_empty() and until_expr != null and frames_left > 0:
			until_result = {"met": false, "reason": "incident", "incident": new[0]["type"]}
			frames_left = 1   # the decrement below makes it 0: reply on this frame
		return new

	func _check_until() -> void:
		if tree.current_scene == null:
			return
		var r := _run(until_expr)
		if not r[0]:
			until_result = {"met": false, "reason": "error", "error": str(r[1])}
			frames_left = 0
		elif _truthy(r[1]):
			until_result = {"met": true, "reason": "true"}
			frames_left = 0

	static func _truthy(v) -> bool:
		return bool(v) if typeof(v) in [TYPE_BOOL, TYPE_INT, TYPE_FLOAT] else v != null

	func _jsonable(v):
		match typeof(v):
			TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
				return v
			TYPE_STRING_NAME, TYPE_NODE_PATH:
				return str(v)
			TYPE_VECTOR2, TYPE_VECTOR2I:
				return [v.x, v.y]
			TYPE_VECTOR3, TYPE_VECTOR3I:
				return [v.x, v.y, v.z]
			TYPE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_FLOAT32_ARRAY, \
					TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_STRING_ARRAY, TYPE_PACKED_VECTOR2_ARRAY:
				var out := []
				for x in v:
					out.append(_jsonable(x))
				return out
			TYPE_DICTIONARY:
				var d := {}
				for k in v:
					d[str(k)] = _jsonable(v[k])
				return d
			TYPE_OBJECT:
				return str(v) if v != null else null
		return var_to_str(v)

	# ------------------------------------------------------------ window

	## A visible window must not take real keyboard/mouse input: it would reach the game and break
	## determinism. No focus = no keys; a mouse-passthrough region outside the window = no mouse.
	func _detach_window_from_desktop_input() -> void:
		DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS, true)
		DisplayServer.window_set_mouse_passthrough(PackedVector2Array([Vector2(-2, -2), Vector2(-1, -2), Vector2(-1, -1)]))

	# ------------------------------------------------------------ misc

	func _write_progress() -> void:
		var f := FileAccess.open(session_dir.path_join("progress"), FileAccess.WRITE)
		if f != null:
			f.store_string(str(frame))
			f.close()
		_progress_ms = Time.get_ticks_msec()

	func _write_bridge_json() -> void:
		DirAccess.make_dir_recursive_absolute(session_dir)
		var tmp := session_dir.path_join("bridge.json.tmp")
		var f := FileAccess.open(tmp, FileAccess.WRITE)
		f.store_string(JSON.stringify({
			"port": server.get_local_port(), "token": token, "pid": OS.get_process_id(),
			"godot": Engine.get_version_info()["string"],
			"physics_tps": Engine.physics_ticks_per_second,
			"headless": headless,
		}))
		f.close()
		DirAccess.rename_absolute(tmp, session_dir.path_join("bridge.json"))

	func _fatal(msg: String) -> void:
		printerr("ghostlap play bridge: ", msg)
		DirAccess.make_dir_recursive_absolute(session_dir)
		var f := FileAccess.open(session_dir.path_join("bridge_error.txt"), FileAccess.WRITE)
		if f:
			f.store_string(msg)
			f.close()
		_quit()


## The base object of every expression: helpers, plus the current scene's own members
## (`score` reads current_scene.score).
class ExprContext extends RefCounted:
	var tree: SceneTree
	var observe: RefCounted

	func _init(t: SceneTree, o: RefCounted) -> void:
		tree = t
		observe = o

	func node(path: String) -> Node:
		if path.begins_with("/root"):
			return tree.root.get_node_or_null(path)
		return tree.current_scene.get_node_or_null(path) if tree.current_scene != null else null

	func group(name: String) -> Array:
		return tree.get_nodes_in_group(name)

	## Distance between two nodes (or a node and the player) in player heights.
	func dist_h(a, b = null) -> float:
		var na: Node = node(a) if a is String else a
		var nb: Node = observe.find_player() if b == null else (node(b) if b is String else b)
		if na == null or nb == null or not ("global_position" in na) or not ("global_position" in nb):
			return INF
		var p: Node = observe.find_player()
		var h: float = observe.measure_size(p).y if p != null else 1.0
		return na.global_position.distance_to(nb.global_position) / maxf(h, 0.001)

	func _get(property: StringName):
		var cs := tree.current_scene
		if cs != null and property in cs:
			return cs.get(property)
		return null
