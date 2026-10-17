# Test arenas built at runtime from static bodies, sized from the player's collision shape.
# H = player height, W = player width (pixels in 2D, metres in 3D).
#
#   flat    endless floor
#   ledge   2D: floor ends 4H to the right of spawn; 3D: 5H x 5H platform (walk any way, fall off)
#   wall    flat floor + wall 6H away (3D: a room of four walls)
#   air     floor, player spawns 8H up (use settle: none)
#   hazard  flat floor + the profile's hazard scene 4H ahead
#   hazard_open  no floor (top-down) + the hazard 4H ahead, level with the player
#   slope   flat floor + 30° ramp starting 2H ahead (3D: along the calibration run's direction)
#   open    nothing (top-down / no gravity)
extends Node

var hazard: Node = null
var arena_name := ""
var _layer := 1
var _mask := 1


func build(arena_name: String, dims: String, size: Vector2, player: Node, vars: Dictionary) -> void:
	_mask = player.get("collision_layer") if player.get("collision_layer") != null else 1
	var player_mask: int = player.get("collision_mask") if player.get("collision_mask") != null else 1
	_layer = world_layer(player, player_mask, dims)
	self.arena_name = arena_name
	var h := size.y
	if dims == "3d":
		_build_3d(arena_name, h, vars)
	else:
		_build_2d(arena_name, h)


## Pick ONE layer for arena geometry: a bit the player body collides with that none of the
## player's own Area detectors watch (otherwise e.g. a "MobDetector" sees the floor and kills
## the player), preferring layers named like world/ground in Project Settings.
static func world_layer(player: Node, player_mask: int, dims: String) -> int:
	if player_mask == 0:
		return 1
	var watched := 0
	for n in _descendants(player):
		if (n is Area2D or n is Area3D) and n != player:
			watched |= n.collision_mask
	var candidates := []
	for bit in 32:
		var v := 1 << bit
		if player_mask & v and not (watched & v):
			candidates.append(bit)
	if candidates.is_empty():
		return player_mask & -player_mask
	var re := RegEx.new()
	re.compile("(?i)world|ground|level|terrain|environment|floor|wall|solid|static|map|tile")
	var kind := "3d_physics" if dims == "3d" else "2d_physics"
	for bit in candidates:
		var name := str(ProjectSettings.get_setting("layer_names/%s/layer_%d" % [kind, bit + 1], ""))
		if name != "" and re.search(name):
			return 1 << bit
	return 1 << candidates[0]


static func _descendants(n: Node) -> Array:
	var out := []
	for c in n.get_children():
		out.append(c)
		out.append_array(_descendants(c))
	return out


func spawn_position(arena_name: String, dims: String, size: Vector2) -> Variant:
	var h := size.y
	if dims == "3d":
		if arena_name == "air":
			return Vector3(0, 8 * h, 0)
		if arena_name == "open":
			return Vector3.ZERO
		return Vector3(0, h, 0)
	if arena_name == "air":
		return Vector2(0, -8 * h)
	if arena_name == "open" or arena_name == "hazard_open":
		return Vector2.ZERO
	return Vector2(0, -h)


func place_hazard(spawn: Variant, dims: String, size: Vector2, vars: Dictionary) -> void:
	if hazard == null:
		return
	var h := size.y
	if dims == "3d":
		var d := _dir3(vars, Vector3(1, 0, 0))
		hazard.global_position = Vector3(d.x * 4 * h, h * 0.5, d.z * 4 * h)
	elif arena_name == "hazard_open":
		hazard.global_position = Vector2(spawn.x + 4 * h, spawn.y)
	else:
		hazard.global_position = Vector2(spawn.x + 4 * h, -h * 0.5)


func _dir3(vars: Dictionary, fallback: Vector3) -> Vector3:
	if vars.has("cal_dir_x") and vars.has("cal_dir_z"):
		var d := Vector3(vars["cal_dir_x"], 0, vars["cal_dir_z"])
		if d.length() > 0.1:
			return d.normalized()
	return fallback


# ---------------------------------------------------------------- 2D

func _body2d(shape: Shape2D, pos: Vector2, rot := 0.0) -> StaticBody2D:
	var b := StaticBody2D.new()
	b.collision_layer = _layer
	b.collision_mask = _mask
	b.position = pos
	b.rotation = rot
	var cs := CollisionShape2D.new()
	cs.shape = shape
	b.add_child(cs)
	add_child(b)
	return b


func _rect(w: float, hgt: float) -> RectangleShape2D:
	var r := RectangleShape2D.new()
	r.size = Vector2(w, hgt)
	return r


func _build_2d(arena_name: String, h: float) -> void:
	const FAR := 100000.0
	match arena_name:
		"open", "hazard_open":
			pass
		"ledge":
			var edge := 4 * h
			_body2d(_rect(FAR + edge, 200), Vector2((edge - FAR) / 2.0, 100))
		"wall":
			_body2d(_rect(2 * FAR, 200), Vector2(0, 100))
			_body2d(_rect(200, 40 * h), Vector2(6 * h + 100, -20 * h))
		"slope":
			_body2d(_rect(2 * FAR, 200), Vector2(0, 100))
			var b := StaticBody2D.new()
			b.collision_layer = _layer
			b.collision_mask = _mask
			var poly := CollisionPolygon2D.new()
			var run := 40 * h
			var rise := run * tan(deg_to_rad(30.0))
			poly.polygon = PackedVector2Array([Vector2(2 * h, 0), Vector2(2 * h + run, -rise), Vector2(2 * h + run, 0)])
			b.add_child(poly)
			add_child(b)
		_:
			_body2d(_rect(2 * FAR, 200), Vector2(0, 100))


# ---------------------------------------------------------------- 3D

func _body3d(size: Vector3, pos: Vector3, basis := Basis.IDENTITY) -> StaticBody3D:
	var b := StaticBody3D.new()
	b.collision_layer = _layer
	b.collision_mask = _mask
	var cs := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = size
	cs.shape = box
	b.add_child(cs)
	add_child(b)
	b.global_transform = Transform3D(basis, pos)
	return b


func _build_3d(arena_name: String, h: float, vars: Dictionary) -> void:
	const FAR := 2000.0
	match arena_name:
		"open":
			pass
		"ledge":
			_body3d(Vector3(5 * h, 2, 5 * h), Vector3(0, -1, 0))
		"wall":
			_body3d(Vector3(FAR, 2, FAR), Vector3(0, -1, 0))
			var d := 6 * h
			for side in [Vector3(1, 0, 0), Vector3(-1, 0, 0), Vector3(0, 0, 1), Vector3(0, 0, -1)]:
				var sz := Vector3(1, 20 * h, 20 * h) if side.x != 0 else Vector3(20 * h, 20 * h, 1)
				_body3d(sz, side * (d + 0.5) + Vector3(0, 10 * h, 0))
		"slope":
			_body3d(Vector3(FAR, 2, FAR), Vector3(0, -1, 0))
			var dir := _dir3(vars, Vector3(0, 0, -1))
			var theta := deg_to_rad(30.0)
			var up := Vector3.UP
			var along := (dir * cos(theta) + up * sin(theta)).normalized()
			var n := (-dir * sin(theta) + up * cos(theta)).normalized()
			var side := n.cross(along).normalized()
			var length := 40 * h
			var thickness := 1.0
			var start := dir * 2 * h
			var center := start + along * (length / 2.0) - n * (thickness / 2.0)
			_body3d(Vector3(20 * h, thickness, length), center, Basis(side, n, along))
		_:
			_body3d(Vector3(FAR, 2, FAR), Vector3(0, -1, 0))
