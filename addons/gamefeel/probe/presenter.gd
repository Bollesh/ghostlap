# Render mode for `gamefeel clip`: makes probe runs watchable.
#
# - draws the arena's collision geometry (arenas are invisible in normal probe runs)
# - adds ground markings every 2 body-heights so horizontal motion is visible
# - uses its own camera that follows horizontally at a LOCKED height: a camera that follows
#   vertically keeps the player fixed on screen, and jumps then look like the ground moving
# - shows the probe/run name, so a viewer knows what they're looking at
extends RefCounted

const FLOOR := Color(0.27, 0.32, 0.38)
const MARK := Color(0.42, 0.48, 0.55)
const BG := Color(0.16, 0.17, 0.19)
const HAZARD := Color(0.86, 0.32, 0.3)

var camera: Node = null
var player: Node = null
var dims := "2d"
var size := Vector2(32, 32)
var lock_y := 0.0
var follow_both := false
var offset3 := Vector3.ZERO
var _hud: CanvasLayer = null
var _hitbox_polys: Array = []   # [[CollisionShape2D, Polygon2D]] — attack hitboxes drawn while active
var _label: Label = null


func setup(holder: Node, p: Node, d: String, s: Vector2, arena_name: String, title: String, decorate: bool,
		hazard: Node = null) -> void:
	player = p
	dims = d
	size = s
	RenderingServer.set_default_clear_color(BG)
	if decorate:
		if dims == "3d":
			_decorate_3d(holder, arena_name)
		else:
			_decorate_2d(holder, arena_name)
	if hazard != null:
		_show_hazard(hazard)
	if dims == "2d":
		_show_hitboxes(p)
	var h := size.y
	if dims == "3d":
		var cam := Camera3D.new()
		cam.fov = 55.0
		holder.add_child(cam)
		cam.make_current()
		offset3 = Vector3(0, 1.4 * h, 4.0 * h)
		lock_y = (p.global_position.y if p is Node3D else 0.0)
		camera = cam
	else:
		var cam2 := Camera2D.new()
		holder.add_child(cam2)
		cam2.make_current()
		var vp_h := float(holder.get_viewport().get_visible_rect().size.y)
		# floor (y = 0) sits at 75% of the screen height; a full jump stays in view
		var view_h := 12.0 * h if arena_name == "air" else 7.0 * h
		cam2.zoom = Vector2.ONE * (vp_h / view_h)
		lock_y = -0.25 * view_h
		follow_both = arena_name == "open" or arena_name == "hazard_open"
		camera = cam2
	_hud = CanvasLayer.new()
	_hud.layer = 100
	_label = Label.new()
	_label.text = title
	_label.add_theme_font_size_override("font_size", 40)
	_label.add_theme_color_override("font_color", Color(0.95, 0.95, 0.95))
	_label.add_theme_color_override("font_outline_color", Color(0, 0, 0))
	_label.add_theme_constant_override("outline_size", 8)
	var vp := holder.get_viewport().get_visible_rect().size
	_label.size = Vector2(vp.x, 60)
	_label.position = Vector2(0, vp.y - 80)
	_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_hud.add_child(_label)
	holder.add_child(_hud)
	follow()


func follow() -> void:
	for pair in _hitbox_polys:
		if is_instance_valid(pair[0]) and is_instance_valid(pair[1]):
			var area: Node = pair[0].get_parent()
			pair[1].visible = not pair[0].disabled and (area.monitoring or area.monitorable)
	if camera == null or not is_instance_valid(player) or not player.is_inside_tree():
		return
	if dims == "3d":
		var pp: Vector3 = player.global_position
		var target := Vector3(pp.x, lock_y + 0.5 * size.y, pp.z)
		camera.global_position = target + offset3
		camera.look_at(target, Vector3.UP)
	else:
		var p2: Vector2 = player.global_position
		camera.global_position = p2 if follow_both else Vector2(p2.x, lock_y)


# ---------------------------------------------------------------- 2D

func _decorate_2d(holder: Node, arena_name: String) -> void:
	var h := size.y
	for b in holder.get_children():
		if not (b is StaticBody2D):
			continue
		for c in b.get_children():
			var poly := Polygon2D.new()
			poly.color = FLOOR
			if c is CollisionShape2D and c.shape is RectangleShape2D:
				var e: Vector2 = c.shape.size / 2.0
				poly.polygon = PackedVector2Array([Vector2(-e.x, -e.y), Vector2(e.x, -e.y), Vector2(e.x, e.y), Vector2(-e.x, e.y)])
			elif c is CollisionPolygon2D:
				poly.polygon = c.polygon
			else:
				continue
			b.add_child(poly)
	var marks := Node2D.new()
	marks.z_index = 1  # floor ticks sit on top of the floor polygon...
	if arena_name == "open" or arena_name == "hazard_open":
		marks.z_index = -10  # ...but a top-down grid goes behind the player and hazards
		for i in range(-20, 21):  # a sparse, low-contrast grid (no ground in top-down arenas; keeps GIFs small)
			_line(marks, Vector2(i * 4 * h, -80 * h), Vector2(i * 4 * h, 80 * h), BG.lightened(0.08))
			_line(marks, Vector2(-80 * h, i * 4 * h), Vector2(80 * h, i * 4 * h), BG.lightened(0.08))
	else:
		for i in range(-60, 61):
			_line(marks, Vector2(i * 2 * h, 0), Vector2(i * 2 * h, 0.35 * h), MARK)
	holder.add_child(marks)


## Attack hitboxes usually have no art either: draw them (translucent) while they're active.
func _show_hitboxes(p: Node) -> void:
	var re := RegEx.new()
	re.compile("(?i)hit_?box|attack|weapon|sword|slash|melee|blade")
	var nodes := [p]
	var i := 0
	while i < nodes.size():
		nodes.append_array(nodes[i].get_children())
		i += 1
	for n in nodes:
		if n is Area2D and (re.search(n.name) != null or n.is_in_group("gamefeel_hitbox")):
			for c in n.get_children():
				if c is CollisionShape2D and c.shape is RectangleShape2D:
					var e: Vector2 = c.shape.size / 2.0
					var poly := Polygon2D.new()
					poly.color = Color(1.0, 0.9, 0.35, 0.75)
					poly.polygon = PackedVector2Array([Vector2(-e.x, -e.y), Vector2(e.x, -e.y), Vector2(e.x, e.y), Vector2(-e.x, e.y)])
					poly.visible = false
					c.add_child(poly)
					_hitbox_polys.append([c, poly])


## Collision-only hazards (common in prototypes) would be invisible in a clip: draw their shapes.
func _show_hazard(hazard: Node) -> void:
	var nodes := [hazard]
	var i := 0
	while i < nodes.size():
		nodes.append_array(nodes[i].get_children())
		i += 1
	for n in nodes:
		if (n is Sprite2D or n is AnimatedSprite2D or n is Polygon2D or n is MeshInstance2D or n is TextureRect
				or n is MeshInstance3D or n is Sprite3D or n is AnimatedSprite3D) and n.visible:
			return  # it has art
	for n in nodes:
		if n is CollisionShape2D and n.shape != null:
			var poly := Polygon2D.new()
			poly.color = HAZARD
			var pts := PackedVector2Array()
			if n.shape is RectangleShape2D:
				var e: Vector2 = n.shape.size / 2.0
				pts = PackedVector2Array([Vector2(-e.x, -e.y), Vector2(e.x, -e.y), Vector2(e.x, e.y), Vector2(-e.x, e.y)])
			else:
				var r: float = n.shape.get_rect().size.x / 2.0
				for k in 24:
					pts.append(Vector2.from_angle(TAU * k / 24.0) * r)
			poly.polygon = pts
			n.add_child(poly)
		elif n is CollisionShape3D and n.shape != null:
			var mi := MeshInstance3D.new()
			var mat := StandardMaterial3D.new()
			mat.albedo_color = HAZARD
			if n.shape is BoxShape3D:
				var bm := BoxMesh.new()
				bm.size = n.shape.size
				mi.mesh = bm
			else:
				var sm := SphereMesh.new()
				sm.radius = 0.5
				sm.height = 1.0
				mi.mesh = sm
			mi.material_override = mat
			n.add_child(mi)


func _line(parent: Node, a: Vector2, b: Vector2, color: Color) -> void:
	var l := Line2D.new()
	l.points = PackedVector2Array([a, b])
	l.width = maxf(1.0, size.y * 0.05)
	l.default_color = color
	parent.add_child(l)


# ---------------------------------------------------------------- 3D

func _decorate_3d(holder: Node, _arena_name: String) -> void:
	var h := size.y
	var mat := StandardMaterial3D.new()
	mat.albedo_color = FLOOR
	for b in holder.get_children():
		if not (b is StaticBody3D):
			continue
		for c in b.get_children():
			if c is CollisionShape3D and c.shape is BoxShape3D:
				var mi := MeshInstance3D.new()
				var bm := BoxMesh.new()
				bm.size = c.shape.size
				mi.mesh = bm
				mi.material_override = mat
				b.add_child(mi)
	var mark := StandardMaterial3D.new()
	mark.albedo_color = MARK
	for i in range(-20, 21):
		for axis in 2:
			var mi2 := MeshInstance3D.new()
			var strip := BoxMesh.new()
			strip.size = Vector3(0.04 * h, 0.01, 80 * h) if axis == 0 else Vector3(80 * h, 0.01, 0.04 * h)
			mi2.mesh = strip
			mi2.material_override = mark
			holder.add_child(mi2)
			mi2.global_position = Vector3(i * 2 * h, 0.006, 0) if axis == 0 else Vector3(0, 0.006, i * 2 * h)
	var sun := DirectionalLight3D.new()
	holder.add_child(sun)
	sun.rotation_degrees = Vector3(-55, -30, 0)
	var env := WorldEnvironment.new()
	var e := Environment.new()
	e.background_mode = Environment.BG_COLOR
	e.background_color = BG
	e.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	e.ambient_light_color = Color(0.6, 0.62, 0.66)
	env.environment = e
	holder.add_child(env)
