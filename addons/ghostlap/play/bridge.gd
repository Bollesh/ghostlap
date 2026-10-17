# ghostlap play bridge: runs the user's game and freezes it between commands.
#
#   godot [--headless] --path <project> --fixed-fps <physics_tps> --script <abs>/bridge.gd -- \
#       --session=<abs dir> --scene=res://level.tscn [--seed=N] [--profile=<abs profile.json>]
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
	var quitting := false
	var observe: RefCounted
	var headless := false

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
		if frames_left > 0:
			frames_left -= 1
		if frames_left == 0:
			if pending_peer != null:
				_reply(pending_peer, pending_id, {"ok": true, "result": _look()})
				pending_peer = null
				pending_id = null
			_serve_until_step()
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
					res["shot"] = _shot(int(a.get("shot_width", 960)))
				_reply(peer, id, {"ok": true, "result": res})
			"shot":
				_reply(peer, id, {"ok": true, "result": _shot(int(a.get("width", 960)))})
			"eval":
				_reply(peer, id, _eval(str(a.get("expr", ""))))
			"hashes":
				var lo := int(a.get("from", 0))
				_reply(peer, id, {"ok": true, "result": Array(hashes.slice(lo))})
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
			"quit":
				_reply(peer, id, {"ok": true, "result": {"quit": true}})
				_quit()
				return true
			var other:
				_reply(peer, id, {"ok": false, "error": "unknown command " + str(other)})
		return false

	func _quit() -> void:
		quitting = true
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

	func _apply_inputs(f: int) -> void:
		for inp in schedule.get(f, []):
			var pressed := bool(inp.get("pressed", true))
			if inp.has("action"):
				var action := str(inp["action"])
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
		schedule.erase(f)

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
		}

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

	func _eval(src: String) -> Dictionary:
		var e := Expression.new()
		if e.parse(src, PackedStringArray(["scene", "tree", "Input", "InputMap", "Engine", "ProjectSettings", "DisplayServer"])) != OK:
			return {"ok": false, "error": "parse: " + e.get_error_text()}
		var v = e.execute([tree.current_scene, tree, Input, InputMap, Engine, ProjectSettings, DisplayServer], tree.current_scene, false)
		if e.has_execute_failed():
			return {"ok": false, "error": "execute: " + e.get_error_text()}
		return {"ok": true, "result": _jsonable(v)}

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

	# ------------------------------------------------------------ screenshots / window

	## Render the frozen frame (no game logic runs) and save it under <session>/shots/.
	func _shot(width: int) -> Dictionary:
		if headless:
			return {"error": "headless: no renderer (start the session with a window or xvfb for screenshots)"}
		RenderingServer.force_draw(false)
		var img := tree.root.get_texture().get_image()
		if img == null or img.is_empty():
			return {"error": "viewport image is empty"}
		if width > 0 and img.get_width() > width:
			img.resize(width, maxi(1, roundi(img.get_height() * float(width) / img.get_width())), Image.INTERPOLATE_LANCZOS)
		var dir := session_dir.path_join("shots")
		DirAccess.make_dir_recursive_absolute(dir)
		var path := dir.path_join("f%d.png" % frame)
		if img.save_png(path) != OK:
			return {"error": "cannot write " + path}
		return {"path": path, "size": [img.get_width(), img.get_height()], "frame": frame}

	## A visible window must not take real keyboard/mouse input: it would reach the game and break
	## determinism. No focus = no keys; a mouse-passthrough region outside the window = no mouse.
	func _detach_window_from_desktop_input() -> void:
		DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS, true)
		DisplayServer.window_set_mouse_passthrough(PackedVector2Array([Vector2(-2, -2), Vector2(-1, -2), Vector2(-1, -1)]))

	# ------------------------------------------------------------ misc

	func _write_bridge_json() -> void:
		DirAccess.make_dir_recursive_absolute(session_dir)
		var tmp := session_dir.path_join("bridge.json.tmp")
		var f := FileAccess.open(tmp, FileAccess.WRITE)
		f.store_string(JSON.stringify({
			"port": server.get_local_port(), "token": token, "pid": OS.get_process_id(),
			"godot": Engine.get_version_info()["string"],
			"physics_tps": Engine.physics_ticks_per_second,
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
