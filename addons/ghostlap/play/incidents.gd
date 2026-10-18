# Incident rules checked at the end of every frame (design §3.4).
#
# Bug incidents can support a `fail` verdict; signals only inform. Each fires once per occurrence
# and re-arms when its condition clears. `crash` and `hang` are detected by the CLI (the process
# is dead or silent), not here.
#
#   error          engine/script error: the Logger API on Godot 4.5+, else new lines in godot.log
#   out_of_bounds  player outside the level geometry, expanded by 10 player heights (side views
#                  and 3D: the top is open)
#   player_lost    no player for 120 frames without a scene change
#   stuck          (signal) movement held >= 90 frames, player moved < 0.25 h
#   death          (signal) health-like value fell to <= 0, the player was re-instanced in the same
#                  scene, or it teleported > 4 h right after touching a hazard. `at` is where it
#                  died; `repeat_of` names an earlier death within REPEAT_H of the same spot
extends RefCounted

const BUG := ["error", "crash", "hang", "out_of_bounds", "player_lost", "trapped", "softlock"]
const LOST_FRAMES := 120
const STUCK_FRAMES := 90
const STUCK_H := 0.25
const OOB_H := 10.0
const TELEPORT_H := 4.0
const MAX_ERRORS := 20               # distinct error incidents; more are counted, not reported
const DEATH_GAP := 120               # one death, not two, for "health hit 0, then the level reloaded"
const REPEAT_H := 3.0
const HP_RE := "^(health|hp|hit_?points|life|lives)$"
const MOVE_ROLES := ["left", "right", "up", "down", "forward", "back", "backward"]
const MOVE_RE := "(^|_)(move|walk|run|left|right|up|down|forward|back|backward|strafe)(_|$)"

var tree: SceneTree
var observe: RefCounted
var profile: Dictionary
var all: Array = []                  # every incident so far

var _logger: Object = null           # 4.5+
var _log_path := ""
var _log_pos := 0
var _log_partial := ""
var _log_last: Dictionary = {}       # error being read from the log, waiting for its "at:" line
var _errors := {}                    # key -> incident (repeats bump its count)
var suppressed_errors := 0

var _scene_id := 0
var _scene_changed_at := 0
var _bounds = null                   # Rect2 / AABB of the level geometry, or null
var _side_view := true
var _move_actions: Array = []
var _move_roles := {}                # role -> action, when the profile names them
var _stuck_pos = null                # where `stuck` fired; it re-arms half a height away
var _hp_re := RegEx.create_from_string(HP_RE)

var _player_id := 0
var _player_seen := false            # in this scene instance: `player_lost` needs a player to lose
var _last_death := -1000000
var _player_scene := ""
var _lost_since := -1
var _lost_fired := false
var _prev_pos = null
var _last_pos = null                 # the player's last position, kept across a reload
var _last_h := 1.0
var _deaths: Array = []              # [scene path, position, frame] of each reported death
var _hist: Array = []                # positions while movement is held in one direction
var _hist_dir := ""                  # that direction; a change starts a new window
var _stuck_fired := false
var _oob_fired := false
var _last_hp := {}
var _hp_cache: Array = []
var _hp_cache_id := 0


func _init(t: SceneTree, o: RefCounted, prof: Dictionary, log_path: String) -> void:
	tree = t
	observe = o
	profile = prof
	_log_path = log_path
	var genres: Array = prof.get("genres", [])
	_side_view = str(prof.get("dims", "2d")) == "3d" or genres.is_empty() \
			or genres.any(func(g): return "platform" in str(g) or "side" in str(g))
	_move_actions = _find_move_actions(prof.get("actions", {}))
	for r in prof.get("actions", {}):
		if str(r) in MOVE_ROLES and InputMap.has_action(str(prof["actions"][r])):
			_move_roles[str(r)] = str(prof["actions"][r])
	if ClassDB.class_exists("Logger"):
		_logger = load(get_script().resource_path.get_base_dir().path_join("../scan/error_logger.gd").simplify_path()).new()
		OS.call("add_logger", _logger)
	else:
		# skip whatever the log already holds (engine startup output)
		var f := FileAccess.open(_log_path, FileAccess.READ)
		if f != null:
			_log_pos = f.get_length()


func close() -> void:
	if _logger != null:
		OS.call("remove_logger", _logger)
		_logger = null


func is_bug(kind: String) -> bool:
	return kind in BUG


## Returns this frame's new incidents.
func tick(frame: int) -> Array:
	var out: Array = []
	_check_errors(frame, out)
	var cs := tree.current_scene
	if cs == null:
		return out
	if cs.get_instance_id() != _scene_id:
		_scene_id = cs.get_instance_id()
		_scene_changed_at = frame
		_bounds = _level_bounds(cs)
		_prev_pos = null
		_hist.clear()
		_oob_fired = false
		_lost_since = -1
		_player_seen = false
	elif _bounds == null and frame % 60 == 0:
		_bounds = _level_bounds(cs)   # geometry added after the scene started
	var p: Node = observe.find_player()
	if p == null:
		if not _player_seen:
			return out   # menus, card tables: no player to lose
		if _lost_since < 0:
			_lost_since = frame
		if not _lost_fired and frame - _lost_since >= LOST_FRAMES and _scene_changed_at <= _lost_since:
			_lost_fired = true
			out.append(_fire(frame, "player_lost", "no player for %d frames (freed without a respawn?)" % LOST_FRAMES, {"since": _lost_since}))
		_prev_pos = null   # _player_id stays, to spot a respawn
		return out
	_lost_since = -1
	_lost_fired = false
	_player_seen = true
	var h: float = maxf(observe.measure_size(p).y, 0.001)
	var pos = p.global_position
	var scene_path := cs.scene_file_path

	# death: re-instanced in the same scene (reload or respawn), not a move to another level
	if _player_id != 0 and p.get_instance_id() != _player_id:
		if scene_path == _player_scene:
			var died_at = _last_pos if _last_pos != null else pos
			_death(out, frame, "player re-instanced in the same scene (reload or respawn)", {"pos": _v(pos)}, died_at, _last_h)
		_last_hp.clear()
		_prev_pos = null
		_hist.clear()
	_player_id = p.get_instance_id()
	_player_scene = scene_path

	# death: a health-like value fell to <= 0
	for prop in _hp_props(p):
		var v = p.get(prop)
		if typeof(v) in [TYPE_INT, TYPE_FLOAT]:
			if _last_hp.has(prop) and _last_hp[prop] > 0 and v <= 0:
				_death(out, frame, "%s fell to %s" % [prop, v], {"prop": prop, "value": v}, pos, h)
			_last_hp[prop] = v

	# death: teleport right after touching a hazard (a respawn at a checkpoint)
	if _prev_pos != null:
		var jump: float = pos.distance_to(_prev_pos)
		if jump > TELEPORT_H * h:
			var hz := _hazard_near(_prev_pos, 1.5 * h)
			if hz != "":
				_death(out, frame, "teleported %.1f h right after touching %s (respawn?)" % [jump / h, hz], {"from": _v(_prev_pos), "to": _v(pos)}, _prev_pos, h)
	_prev_pos = pos
	_last_pos = pos
	_last_h = h

	# out_of_bounds
	if _bounds != null:
		var outside := _outside(pos, OOB_H * h)
		if outside != "" and not _oob_fired:
			_bounds = _level_bounds(cs)   # the level may have grown (streamed chunks, spawned geometry)
			outside = _outside(pos, OOB_H * h) if _bounds != null else ""
		if outside != "" and not _oob_fired:
			_oob_fired = true
			out.append(_fire(frame, "out_of_bounds", "player %s the level geometry by more than %d h" % [outside, OOB_H], {"pos": _v(pos)}))
		elif outside == "":
			_oob_fired = false

	# stuck (a signal: pushing into a wall can be on purpose)
	var dir := _move_dir()
	if dir != _hist_dir:
		# right then back left can end where it began: that's moving, not stuck
		_hist.clear()
		_hist_dir = dir
	if dir != "":
		_hist.append(pos)
		if _hist.size() > STUCK_FRAMES + 1:
			_hist.pop_front()
		if _stuck_fired and pos.distance_to(_stuck_pos) >= 2.0 * STUCK_H * h:
			_stuck_fired = false
		if _hist.size() == STUCK_FRAMES + 1 and not _stuck_fired:
			var moved: float = pos.distance_to(_hist[0])
			if moved < STUCK_H * h:
				_stuck_fired = true
				_stuck_pos = pos
				out.append(_fire(frame, "stuck", "movement held %d frames, moved %.2f h" % [STUCK_FRAMES, moved / h], {"pos": _v(pos)}))
	else:
		_hist.clear()
		_stuck_fired = false
	return out


## The held movement direction, "" for none: left+right held together cancel out (no intent to move).
func _move_dir() -> String:
	if _move_roles.is_empty():
		return ",".join(_move_actions.filter(func(a): return Input.is_action_pressed(a)))
	var held := func(role: String) -> int:
		return 1 if _move_roles.has(role) and Input.is_action_pressed(_move_roles[role]) else 0
	var x: int = held.call("right") - held.call("left")
	var y: int = held.call("up") + held.call("forward") - held.call("down") - held.call("back") - held.call("backward")
	return "" if x == 0 and y == 0 else "%d,%d" % [x, y]


func _death(out: Array, frame: int, why: String, data: Dictionary, at, h: float) -> void:
	# counted from the last death reported, so a game that kills the player every second still
	# reports one death per DEATH_GAP
	if frame - _last_death < DEATH_GAP:
		return
	var scene_path := tree.current_scene.scene_file_path
	data["at"] = _v(at)
	for d in _deaths:
		if d[0] == scene_path and at.distance_to(d[1]) <= REPEAT_H * h:
			data["repeat_of"] = d[2]
			break
	_deaths.append([scene_path, at, frame])
	out.append(_fire(frame, "death", why, data))
	_last_death = frame


func _fire(frame: int, kind: String, why: String, data: Dictionary = {}) -> Dictionary:
	var inc := {"f": frame, "type": kind, "class": "bug" if is_bug(kind) else "signal", "why": why}
	inc.merge(data)
	var p: Node = observe.find_player()
	if p != null:
		observe.player = p
		inc["player"] = observe.view_player()
	all.append(inc)
	return inc


# ---------------------------------------------------------------- errors

func _check_errors(frame: int, out: Array) -> void:
	var items: Array = _logger.take() if _logger != null else _read_log()
	for e in items:
		if e.get("kind", "") == "warning":
			continue
		var key := "%s|%s|%s" % [e.get("message", ""), e.get("file", ""), e.get("line", 0)]
		if _errors.has(key):
			_errors[key]["count"] += 1
			continue
		if _errors.size() >= MAX_ERRORS:
			suppressed_errors += 1
			continue
		var why := str(e.get("message", ""))
		if _errors.size() == MAX_ERRORS - 1:
			why += " (error %d: further new errors are counted, not reported)" % MAX_ERRORS
		var inc := _fire(frame, "error", why, {"count": 1})
		for k in ["kind", "file", "line", "function"]:
			if e.has(k):
				inc["error_kind" if k == "kind" else k] = e[k]   # `kind` names the event in events.jsonl
		_errors[key] = inc
		out.append(inc)


## Godot < 4.5: new "SCRIPT ERROR:" / "ERROR:" lines in godot.log (stderr is unbuffered), each
## followed by an "at: function (file:line)" line.
func _read_log() -> Array:
	var f := FileAccess.open(_log_path, FileAccess.READ)
	if f == null:
		return []
	var n := f.get_length()
	if n <= _log_pos:
		return []
	f.seek(_log_pos)
	var text := _log_partial + f.get_buffer(n - _log_pos).get_string_from_utf8()
	_log_pos = n
	var lines := text.split("\n")
	_log_partial = lines[lines.size() - 1]   # an unfinished last line waits for the next frame
	lines.remove_at(lines.size() - 1)
	var out: Array = []
	for raw in lines:
		var ln := raw.strip_edges()
		var kind := ""
		if ln.begins_with("SCRIPT ERROR:"):
			kind = "script"
		elif ln.begins_with("ERROR:") or ln.begins_with("USER ERROR:") or ln.begins_with("USER SCRIPT ERROR:"):
			kind = "engine"
		if kind != "":
			_log_last = {"kind": kind, "message": ln.substr(ln.find(":") + 1).strip_edges()}
			out.append(_log_last)
		elif ln.begins_with("at:") and not _log_last.is_empty():
			# at: _process (res://bug.gd:8)
			var m := RegEx.create_from_string("at:\\s*(\\S+)\\s*\\((.*):(\\d+)\\)").search(ln)
			if m != null:
				_log_last["function"] = m.get_string(1)
				_log_last["file"] = m.get_string(2)
				_log_last["line"] = int(m.get_string(3))
			_log_last = {}
	return out


# ---------------------------------------------------------------- geometry

## Union of static level geometry (static/animatable bodies, tile layers, grid maps, CSG). Areas
## (kill zones, triggers) and moving bodies don't count.
func _level_bounds(root: Node):
	var r = null
	var stack: Array[Node] = [root]
	while not stack.is_empty():
		var n: Node = stack.pop_back()
		var b = null
		if n is CollisionShape2D and n.shape != null and (n.get_parent() is StaticBody2D):
			b = n.global_transform * n.shape.get_rect()
		elif n is CollisionPolygon2D and n.get_parent() is StaticBody2D and n.polygon.size() > 0:
			var pr := Rect2(n.polygon[0], Vector2.ZERO)
			for q in n.polygon:
				pr = pr.expand(q)
			b = n.global_transform * pr
		elif n is TileMapLayer and n.tile_set != null and n.get_used_cells().size() > 0:
			var used: Rect2i = n.get_used_rect()
			var ts := Vector2(n.tile_set.tile_size)
			b = n.global_transform * Rect2(Vector2(used.position) * ts, Vector2(used.size) * ts)
		elif n is CollisionShape3D and n.shape != null and (n.get_parent() is StaticBody3D):
			b = n.global_transform * n.shape.get_debug_mesh().get_aabb()
		elif n is GridMap and n.get_used_cells().size() > 0:
			var cs3: Vector3 = n.cell_size
			var g := AABB(Vector3(n.get_used_cells()[0]) * cs3, cs3)
			for c in n.get_used_cells():
				g = g.merge(AABB(Vector3(c) * cs3, cs3))
			b = n.global_transform * g
		elif n.get_class().begins_with("CSG") and n.get("use_collision") == true and n.has_method("get_aabb"):
			b = n.global_transform * n.get_aabb()
		if b != null:
			r = b if r == null else r.merge(b)
		for c in n.get_children():
			stack.append(c)
	return r


## "" when inside the bounds grown by `margin`, else which side the player left by.
func _outside(pos, margin: float) -> String:
	if _bounds is Rect2:
		var b: Rect2 = _bounds
		if pos.y > b.end.y + margin:
			return "fell below"
		if pos.x < b.position.x - margin or pos.x > b.end.x + margin:
			return "left the sides of"
		if not _side_view and pos.y < b.position.y - margin:
			return "left the top of"
		return ""
	var a: AABB = _bounds
	if pos.y < a.position.y - margin:
		return "fell below"
	if pos.x < a.position.x - margin or pos.x > a.end.x + margin or pos.z < a.position.z - margin or pos.z > a.end.z + margin:
		return "left the sides of"
	return ""


## Name of a hazard-like body or area within `radius` of `at`, else "".
func _hazard_near(at, radius: float) -> String:
	var stack: Array[Node] = [tree.current_scene]
	while not stack.is_empty():
		var n: Node = stack.pop_back()
		if (n is CollisionObject2D or n is CollisionObject3D) and n != observe.find_player():
			if observe.classify(n) == "hazard" and n.global_position.distance_to(at) <= radius + _extent(n):
				return str(n.name)
		for c in n.get_children():
			stack.append(c)
	return ""


func _extent(n: Node) -> float:
	for c in n.get_children():
		if c is CollisionShape2D and c.shape != null:
			return c.shape.get_rect().size.length() / 2.0
		if c is CollisionShape3D and c.shape != null:
			return c.shape.get_debug_mesh().get_aabb().size.length() / 2.0
	return 0.0


# ---------------------------------------------------------------- helpers

func _hp_props(p: Node) -> Array:
	if p.get_instance_id() == _hp_cache_id:
		return _hp_cache
	var out: Array = []
	for prop in p.get_property_list():
		if prop["usage"] & PROPERTY_USAGE_SCRIPT_VARIABLE and _hp_re.search(str(prop["name"])) != null:
			out.append(prop["name"])
	_hp_cache = out
	_hp_cache_id = p.get_instance_id()
	return out


func _find_move_actions(roles: Dictionary) -> Array:
	var out: Array = []
	for r in roles:
		if str(r) in MOVE_ROLES and InputMap.has_action(str(roles[r])):
			out.append(str(roles[r]))
	if out.is_empty():
		var re := RegEx.create_from_string(MOVE_RE)
		for a in InputMap.get_actions():
			if not str(a).begins_with("ui_") and re.search(str(a).to_lower()) != null:
				out.append(str(a))
	return out


func _v(v) -> Array:
	return [snappedf(v.x, 0.1), snappedf(v.y, 0.1)] if v is Vector2 else [snappedf(v.x, 0.01), snappedf(v.y, 0.01), snappedf(v.z, 0.01)]
