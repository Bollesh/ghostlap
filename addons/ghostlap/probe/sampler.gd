# Records one sample per physics frame (after the game's own physics code ran), plus a light
# per-process-frame log of time_scale / pause so hitstop is measurable even when physics slows.
#
# Sample keys (short on purpose, files get long):
#   f   frame              p   position          v   velocity
#   fl  on_floor           wl  on_wall           fn  floor normal (3D)
#   a   animation state    hb  active hitboxes   hp  tracked props (only when changed)
#   cam camera position    co  camera offset     yaw [body, model] degrees (3D)
#   in  actions held       pt  wall time since previous sample (ms; with --fixed-fps this is
#       the real cost of the tick)   ts  Engine.time_scale   pz  tree paused
#   vz  player visuals [[r,g,b,a,visible], ...] (only when changed; flash/blink detection)
#   snd playing audio players in the scene (only when changed)
#   ptc emitting particle systems in the scene (only when changed)
extends RefCounted

const MODEL_RE := "(?i)model|mesh|visual|skin|armature|pivot|body|rig|character|graphics|sprite"
const DEFAULT_HITBOX_RE := "(?i)hit_?box|attack|weapon|sword|slash|melee|blade"

var player: Node
var dims: String
var size := Vector2(32, 32)
var samples: Array = []
var process_log: Array = []
var held := {}
var hitbox_re := RegEx.new()
var hitbox_paths: Array = []
var track_props: Array = []
var last_props := {}
var model: Node = null
var _prev_pos = null
var _last_usec := 0
var visuals: Array = []        # player itself (if visual) + visual descendants, for flash/blink
var _last_vz = null
var _last_snd := -1
var _last_ptc := -1
var _fx_nodes: Array = []      # cached audio/particle nodes, refreshed periodically


func _init(p: Node, d: String, pattern: String, paths: Array, props: Array) -> void:
	player = p
	dims = d
	hitbox_re.compile(pattern if pattern != "" else DEFAULT_HITBOX_RE)
	hitbox_paths = paths
	track_props = props
	size = _measure_size()
	visuals = _find_visuals()
	if dims == "3d":
		var re := RegEx.new()
		re.compile(MODEL_RE)
		for c in player.get_children():
			if c is Node3D and not (c is CollisionShape3D) and not (c is Camera3D) and re.search(c.name):
				model = c
				break


func _measure_size() -> Vector2:
	var best := Vector2.ZERO
	for c in player.get_children():
		if c is CollisionShape2D and c.shape != null and not c.disabled:
			var s: Shape2D = c.shape
			var sc: Vector2 = (c.scale * (player.scale if player is Node2D else Vector2.ONE)).abs()
			var v := Vector2.ZERO
			if s is RectangleShape2D:
				v = s.size
			elif s is CapsuleShape2D:
				v = Vector2(s.radius * 2, s.height)
			elif s is CircleShape2D:
				v = Vector2(s.radius * 2, s.radius * 2)
			else:
				var r := s.get_rect()
				v = r.size
			v *= sc
			if v.y > best.y:
				best = v
		elif c is CollisionShape3D and c.shape != null and not c.disabled:
			var s3: Shape3D = c.shape
			var v3 := Vector2.ZERO
			if s3 is CapsuleShape3D:
				v3 = Vector2(s3.radius * 2, s3.height)
			elif s3 is BoxShape3D:
				v3 = Vector2(maxf(s3.size.x, s3.size.z), s3.size.y)
			elif s3 is SphereShape3D:
				v3 = Vector2(s3.radius * 2, s3.radius * 2)
			elif s3 is CylinderShape3D:
				v3 = Vector2(s3.radius * 2, s3.height)
			v3 *= c.scale.y
			if v3.y > best.y:
				best = v3
		elif c is CollisionPolygon2D and c.polygon.size() > 0:
			var rect := Rect2(c.polygon[0], Vector2.ZERO)
			for pt in c.polygon:
				rect = rect.expand(pt)
			if rect.size.y > best.y:
				best = rect.size
	if best.y <= 0.0:
		best = Vector2(1, 2) if dims == "3d" else Vector2(32, 32)
	return best


func _find_visuals() -> Array:
	var out := []
	if player is CanvasItem:
		out.append(player)
	for n in _walk(player, 3):
		if n == player or out.size() >= 4:
			continue
		if n is Sprite2D or n is AnimatedSprite2D or n is Polygon2D or n is TextureRect or n is Sprite3D \
				or n is AnimatedSprite3D or n is MeshInstance3D:
			out.append(n)
	return out


func _visual_state() -> Array:
	var out := []
	for v in visuals:
		if not is_instance_valid(v):
			continue
		var c := Color.WHITE
		if v is CanvasItem:
			c = v.modulate * v.self_modulate
		elif v is SpriteBase3D:
			c = v.modulate
		elif v is GeometryInstance3D:
			c = Color(1, 1, 1, 1.0 - v.transparency)
		var vis: bool = v.is_visible_in_tree() if v.has_method("is_visible_in_tree") else true
		out.append([snappedf(c.r, 0.01), snappedf(c.g, 0.01), snappedf(c.b, 0.01), snappedf(c.a, 0.01), vis])
	return out


func _count_fx() -> Array:
	if Engine.get_physics_frames() % 15 == 0 or _fx_nodes.is_empty():
		_fx_nodes.clear()
		var root := player.get_tree().root
		for t in ["AudioStreamPlayer", "AudioStreamPlayer2D", "AudioStreamPlayer3D",
				"GPUParticles2D", "CPUParticles2D", "GPUParticles3D", "CPUParticles3D"]:
			_fx_nodes.append_array(root.find_children("*", t, true, false))
	var snd := 0
	var ptc := 0
	for n in _fx_nodes:
		if not is_instance_valid(n):
			continue
		if n is AudioStreamPlayer or n is AudioStreamPlayer2D or n is AudioStreamPlayer3D:
			if n.playing:
				snd += 1
		elif n.emitting:
			ptc += 1
	return [snd, ptc]


func on_floor() -> Variant:
	if player.has_method("is_on_floor"):
		return player.is_on_floor()
	return null


func _velocity() -> Variant:
	if player is CharacterBody2D or player is CharacterBody3D:
		return player.velocity
	if player is RigidBody2D or player is RigidBody3D:
		return player.linear_velocity
	if _prev_pos != null:
		return (player.global_position - _prev_pos) * Engine.physics_ticks_per_second
	return Vector3.ZERO if dims == "3d" else Vector2.ZERO


func speed() -> float:
	var v = _velocity()
	return v.length()


func begin() -> void:
	samples.clear()
	process_log.clear()
	_last_vz = null
	_last_snd = -1
	_last_ptc = -1
	_fx_nodes.clear()
	_prev_pos = player.global_position
	_last_usec = Time.get_ticks_usec()


func note_input(_f: int, action: String, pressed: bool) -> void:
	if pressed:
		held[action] = true
	else:
		held.erase(action)


func _vec(v) -> Array:
	if v is Vector3:
		return [snappedf(v.x, 0.0001), snappedf(v.y, 0.0001), snappedf(v.z, 0.0001)]
	return [snappedf(v.x, 0.0001), snappedf(v.y, 0.0001)]


func sample(f: int) -> void:
	if not is_instance_valid(player):
		samples.append({"f": f, "gone": true})
		return
	var s := {"f": f, "p": _vec(player.global_position), "v": _vec(_velocity())}
	var fl = on_floor()
	if fl != null:
		s["fl"] = fl
	if player.has_method("is_on_wall"):
		s["wl"] = player.is_on_wall()
	if dims == "3d" and fl == true and player.has_method("get_floor_normal"):
		s["fn"] = _vec(player.get_floor_normal())
	var anim := _anim_state()
	if anim != "":
		s["a"] = anim
	var hb := _active_hitboxes()
	if not hb.is_empty():
		s["hb"] = hb
	var changed := {}
	for pname in track_props:
		var val = player.get(pname)
		if typeof(val) in [TYPE_INT, TYPE_FLOAT, TYPE_BOOL] and (not last_props.has(pname) or last_props[pname] != val):
			changed[pname] = val
			last_props[pname] = val
	if not changed.is_empty():
		s["hp"] = changed
	var vp := player.get_viewport()
	if dims == "3d":
		var cam := vp.get_camera_3d() if vp else null
		if cam:
			s["cam"] = _vec(cam.global_position)
			if cam.h_offset != 0.0 or cam.v_offset != 0.0:
				s["co"] = [cam.h_offset, cam.v_offset]
		s["yaw"] = [snappedf(rad_to_deg(player.global_rotation.y), 0.01),
				snappedf(rad_to_deg(model.global_rotation.y), 0.01) if model else null]
	else:
		var cam2 := vp.get_camera_2d() if vp else null
		if cam2:
			s["cam"] = _vec(cam2.get_screen_center_position())
			if cam2.offset != Vector2.ZERO:
				s["co"] = _vec(cam2.offset)
	if not held.is_empty():
		s["in"] = held.keys()
	var now := Time.get_ticks_usec()
	s["pt"] = snappedf((now - _last_usec) / 1000.0, 0.001)
	_last_usec = now
	if Engine.time_scale != 1.0:
		s["ts"] = Engine.time_scale
	if player.get_tree().paused:
		s["pz"] = true
	var vz := _visual_state()
	if vz != _last_vz:
		s["vz"] = vz
		_last_vz = vz
	var fx := _count_fx()
	if fx[0] != _last_snd:
		s["snd"] = fx[0]
		_last_snd = fx[0]
	if fx[1] != _last_ptc:
		s["ptc"] = fx[1]
		_last_ptc = fx[1]
	samples.append(s)
	_prev_pos = player.global_position


func process_sample(pf: int, tree: SceneTree) -> void:
	if Engine.time_scale != 1.0 or tree.paused:
		process_log.append([pf, Engine.time_scale, tree.paused, samples.size()])


func _anim_state() -> String:
	var parts := []
	for n in _walk(player, 3):
		if n is AnimationPlayer and n.is_playing():
			parts.append("AP:" + str(n.current_animation))
		elif (n is AnimatedSprite2D or n is AnimatedSprite3D) and n.is_playing():
			parts.append("SP:" + str(n.animation))
		elif n is AnimationTree and n.active:
			var pb = n.get("parameters/playback")
			if pb is AnimationNodeStateMachinePlayback:
				parts.append("AT:" + str(pb.get_current_node()))
	return "|".join(parts)


func _active_hitboxes() -> Array:
	var out := []
	for n in _walk(player, 4):
		if not (n is Area2D or n is Area3D):
			continue
		var rel := str(player.get_path_to(n))
		var named: bool = hitbox_re.search(n.name) != null or hitbox_paths.has(rel) or n.is_in_group("ghostlap_hitbox")
		if not named:
			continue
		if not (n.monitoring or n.monitorable) or (n.collision_layer == 0 and n.collision_mask == 0):
			continue
		var enabled := false
		for c in n.get_children():
			if (c is CollisionShape2D or c is CollisionShape3D) and c.shape != null and not c.disabled:
				enabled = true
			elif (c is CollisionPolygon2D or c is CollisionPolygon3D) and not c.disabled:
				enabled = true
		if enabled:
			out.append(rel)
	return out


func _walk(n: Node, depth: int) -> Array:
	var out := [n]
	if depth <= 0:
		return out
	for c in n.get_children():
		out.append_array(_walk(c, depth - 1))
	return out


func calibration_vars() -> Dictionary:
	var out := {}
	var was_floor = null
	var prev_vy = null
	var up_sign := -1.0 if dims == "2d" else 1.0
	var max_speed := 0.0
	var max_speed_x := 0.0
	for s in samples:
		if s.get("gone", false):
			continue
		var fl = s.get("fl", null)
		var v: Array = s["v"]
		var vy: float = v[1] * up_sign  # positive = moving up
		if fl != null:
			if was_floor == true and fl == false and not out.has("cal_left_floor"):
				out["cal_left_floor"] = s["f"]
			if was_floor == false and fl == true and not out.has("cal_landed"):
				out["cal_landed"] = s["f"]
			was_floor = fl
		if prev_vy != null and prev_vy > 0.0 and vy <= 0.0 and not out.has("cal_apex"):
			out["cal_apex"] = s["f"]
		prev_vy = vy
		var planar := absf(v[0]) if dims == "2d" else Vector2(v[0], v[2]).length()
		max_speed = maxf(max_speed, planar if dims == "3d" else Vector2(v[0], v[1]).length())
		max_speed_x = maxf(max_speed_x, absf(v[0]))
	out["cal_max_speed"] = max_speed
	out["cal_max_speed_x"] = max_speed_x
	for s in samples:
		if s.get("gone", false):
			continue
		var v2: Array = s["v"]
		var sp: float = Vector2(v2[0], v2[2]).length() if dims == "3d" else Vector2(v2[0], v2[1]).length()
		if max_speed > 0.0 and sp >= 0.95 * max_speed:
			out["cal_top_frame"] = s["f"]
			break
	if samples.size() >= 2:
		var a: Array = samples[0].get("p", [])
		var b: Array = samples[samples.size() - 1].get("p", [])
		if a.size() == b.size() and a.size() > 0:
			if dims == "3d":
				var d := Vector2(b[0] - a[0], b[2] - a[2])
				if d.length() > 0.0001:
					d = d.normalized()
				out["cal_dir_x"] = d.x
				out["cal_dir_z"] = d.y
			else:
				var d2 := Vector2(b[0] - a[0], b[1] - a[1])
				if d2.length() > 0.0001:
					d2 = d2.normalized()
				out["cal_dir_x"] = d2.x
				out["cal_dir_y"] = d2.y
	out["cal_end"] = samples.size()
	return out
