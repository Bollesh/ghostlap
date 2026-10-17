# Look views for ghostlap play (design §3.3.0): the frozen game as compact text.
#
#   ui      visible text, buttons, inputs and bars, with on-screen centres for clicking
#   player  the controlled body: position, velocity, floor/wall, animation, tracked props, size
#   near    collision objects around the player, with a kind and distance in player heights
#   map     a player-centred character picture sampled from the physics space (3D: height grid)
#   grid    TileMapLayers + grid-snapped nodes (or a GridContainer) as rows of characters
#   state   script properties of chosen nodes (player / group / path), changed ones first
#   game    whatever the game's optional _ghostlap_state() returns
#
# Everything is read-only: views never call game methods except _ghostlap_state().
extends RefCounted

const HAZARD_RE := "spike|hazard|lava|kill|death|deadly|trap|saw|acid|thorn|danger|damage"
const ENEMY_RE := "enemy|enemies|slime|mob|monster|bat|goblin|skeleton|zombie|boss|foe"
const PICKUP_RE := "coin|gem|pickup|collect|item|key|star|heart|potion|ammo|loot"
const GOAL_RE := "goal|flag|exit|finish|portal|door|checkpoint|lantern|target"
const WALL_RE := "wall|solid|ground|terrain|block|collision|platform"
const SYMBOL := {"solid": "#", "oneway": "=", "hazard": "^", "pickup": "o", "enemy": "E", "goal": "G",
		"player": "P", "unknown": "?", "empty": "."}
const MAX_UI := 40
const MAX_TEXT := 80
const MAX_NEAR := 12
const MAX_STATE_NODES := 12
const GRID_MAX := Vector2i(48, 32)

var tree: SceneTree
var profile: Dictionary
var player: Node = null
var size := Vector2(32, 32)       # player collision size (2D px / 3D m); `h` = size.y
var dims := "2d"
var _prev_state := {}             # node path -> {prop: json value}
var _res := {}                    # compiled regexes

func _init(t: SceneTree, p: Dictionary) -> void:
	tree = t
	profile = p
	dims = str(p.get("dims", "2d"))
	for k in ["hazard", "enemy", "pickup", "goal", "wall"]:
		var re := RegEx.new()
		re.compile("(?i)" + {"hazard": HAZARD_RE, "enemy": ENEMY_RE, "pickup": PICKUP_RE, "goal": GOAL_RE, "wall": WALL_RE}[k])
		_res[k] = re


func default_views() -> Array:
	var v = profile.get("views")
	return v if v is Array and not v.is_empty() else ["ui", "state"]


func look(views: Array, opts: Dictionary) -> Dictionary:
	player = find_player()
	if player != null:
		size = measure_size(player)
		dims = "3d" if player is Node3D else "2d"
	var out := {}
	for v in views:
		var name := str(v)
		var arg := ""
		if name.contains(":"):
			arg = name.get_slice(":", 1)
			name = name.get_slice(":", 0)
		match name:
			"ui":
				out["ui"] = view_ui()
			"player":
				out["player"] = view_player()
			"near":
				out["near"] = view_near(float(opts.get("radius", 12)))
			"map":
				out["map"] = view_map(opts)
			"grid":
				out[str(v)] = view_grid(arg)
			"state":
				out[str(v)] = view_state(arg)
			"game":
				out["game"] = view_game()
			_:
				out[str(v)] = {"error": "unknown view (ui, player, near, map, grid, state[:group|path], game)"}
	return out


# ---------------------------------------------------------------- player

func find_player() -> Node:
	var cs := tree.current_scene
	if cs == null:
		return null
	var pinfo: Dictionary = profile.get("player", {})
	var scene_path := str(pinfo.get("scene", ""))
	var sub := str(pinfo.get("node", "."))
	if scene_path != "":
		var stack: Array[Node] = [cs]
		while not stack.is_empty():
			var n: Node = stack.pop_back()
			if n.scene_file_path == scene_path:
				var p := n if sub in [".", ""] else n.get_node_or_null(NodePath(sub))
				if p != null:
					return p
			for c in n.get_children():
				stack.append(c)
	var g := tree.get_nodes_in_group("player")
	return g[0] if not g.is_empty() else null


func measure_size(p: Node) -> Vector2:
	for c in p.get_children():
		if c is CollisionShape2D and c.shape != null and not c.disabled:
			return _shape_size_2d(c.shape) * c.global_transform.get_scale().abs()
		if c is CollisionShape3D and c.shape != null and not c.disabled:
			var s: Shape3D = c.shape
			if s is CapsuleShape3D:
				return Vector2(s.radius * 2.0, s.height)
			if s is BoxShape3D:
				return Vector2(s.size.x, s.size.y)
			if s is SphereShape3D:
				return Vector2(s.radius * 2.0, s.radius * 2.0)
			if s is CylinderShape3D:
				return Vector2(s.radius * 2.0, s.height)
	return Vector2(32, 32) if p is Node2D else Vector2(0.5, 1.8)


## Centre of the player's collision shape (its origin is often at the feet).
func player_centre():
	for c in player.get_children():
		if (c is CollisionShape2D or c is CollisionShape3D) and c.shape != null and not c.disabled:
			return c.global_position
	return player.global_position


func _shape_size_2d(s: Shape2D) -> Vector2:
	if s is RectangleShape2D:
		return s.size
	if s is CapsuleShape2D:
		return Vector2(s.radius * 2.0, s.height)
	if s is CircleShape2D:
		return Vector2(s.radius * 2.0, s.radius * 2.0)
	return s.get_rect().size


func view_player():
	if player == null:
		return null
	var d := {"node": _path(player)}
	if player is Node2D or player is Node3D:
		d["pos"] = _vec(player.global_position)
	if player is CharacterBody2D or player is CharacterBody3D:
		d["vel"] = _vec(player.velocity)
		d["floor"] = player.is_on_floor()
		if player.is_on_wall():
			d["wall"] = true
	elif player is RigidBody2D or player is RigidBody3D:
		d["vel"] = _vec(player.linear_velocity)
	var anim := _animation(player)
	if anim != "":
		d["anim"] = anim
	for prop in profile.get("track_props", []):
		if prop in player:
			d[prop] = _json(player.get(prop))
	d["size"] = _vec(size)
	return d


func _animation(p: Node) -> String:
	for c in p.find_children("*", "", true, false):
		if c is AnimationPlayer and c.is_playing():
			return str(c.current_animation)
		if (c is AnimatedSprite2D or c is AnimatedSprite3D) and c.is_playing():
			return str(c.animation)
	return ""


# ---------------------------------------------------------------- ui

func view_ui() -> Array:
	var out := []
	var vp_rect := tree.root.get_visible_rect()
	var hidden := 0
	for c in tree.root.find_children("*", "Control", true, false):
		var ctl := c as Control
		if not ctl.is_visible_in_tree():
			continue
		var e := _ui_entry(ctl)
		if e.is_empty():
			continue
		var r := Rect2(ctl.get_global_transform_with_canvas().origin, ctl.size * ctl.get_global_transform_with_canvas().get_scale())
		if not vp_rect.intersects(r):
			continue
		if out.size() >= MAX_UI:
			hidden += 1
			continue
		var tagged := {"path": _path(ctl)}
		for k in e:
			tagged[k] = e[k]
		e = tagged
		if e.get("button") or e.get("input"):
			var centre := r.get_center()
			e["at"] = [roundi(centre.x), roundi(centre.y)]
		if ctl.has_focus():
			e["focus"] = true
		out.append(e)
	if hidden > 0:
		out.append({"truncated": hidden})
	return out


func _ui_entry(c: Control) -> Dictionary:
	if c is BaseButton:
		var e := {"button": true}
		var txt := str(c.get("text")) if "text" in c else ""
		if txt != "":
			e["text"] = _short(txt)
		elif c is TextureButton:
			e["art_only"] = true
		if c.disabled:
			e["disabled"] = true
		if c.toggle_mode:
			e["pressed"] = c.button_pressed
		return e
	if c is Label:
		return {"text": _short(c.text)} if c.text.strip_edges() != "" else {}
	if c is RichTextLabel:
		var t: String = c.get_parsed_text()
		return {"text": _short(t)} if t.strip_edges() != "" else {}
	if c is LineEdit:
		return {"input": true, "text": _short(c.text), "placeholder": _short(c.placeholder_text)}
	if c is TextEdit:
		return {"input": true, "text": _short(c.text)}
	if c is Range:
		return {"value": _num(c.value), "max": _num(c.max_value)}
	return {}


# ---------------------------------------------------------------- near

func classify(n: Node) -> String:
	if n == null:
		return "unknown"
	var hz := str(profile.get("hazard", ""))
	var probe := n
	for _i in 3:  # the node, its parent, and grandparent carry the meaning (Area2D under "Spikes")
		if probe == null or probe == tree.current_scene:
			break
		if hz != "" and probe.scene_file_path == hz:
			return "hazard"
		var label := str(probe.name)
		for gname in probe.get_groups():
			label += " " + str(gname)
		label = label.to_lower()
		if probe.scene_file_path != "":
			label += " " + probe.scene_file_path.get_file().get_basename()
		for k in ["hazard", "enemy", "pickup", "goal"]:
			if _res[k].search(label) != null:
				return k
		probe = probe.get_parent()
	if n is StaticBody2D or n is StaticBody3D or n is AnimatableBody2D or n is AnimatableBody3D or n is TileMapLayer:
		return "oneway" if _is_oneway(n) else "solid"
	return "unknown"


func _is_oneway(n: Node) -> bool:
	for c in n.get_children():
		if c is CollisionShape2D and c.one_way_collision:
			return true
		if c is CollisionPolygon2D and c.one_way_collision:
			return true
	return false


func view_near(radius_h: float) -> Array:
	if player == null:
		return []
	var h := maxf(size.y, 0.001)
	var pp = player_centre()
	var found := []
	var skip := {}
	for c in player.find_children("*", "", true, false):
		skip[c] = true
	skip[player] = true
	for n in tree.current_scene.find_children("*", "CollisionObject2D" if dims == "2d" else "CollisionObject3D", true, false):
		if skip.has(n) or n is TileMapLayer:
			continue
		var near_pt = _nearest_point(n, pp)
		var rel = (near_pt - pp) / h
		if rel.length() > radius_h:
			continue
		var e := {"name": str(n.name), "type": n.get_class(), "kind": classify(n), "rel_h": _vec(rel, 1)}
		if n is CharacterBody2D or n is CharacterBody3D:
			e["vel"] = _vec(n.velocity)
		elif n is RigidBody2D or n is RigidBody3D:
			e["vel"] = _vec(n.linear_velocity)
		var ext = _extent(n)
		if ext != null and (ext.x / h > 1.5 or ext.y / h > 1.5):
			e["size_h"] = _vec(ext / h, 1)
		found.append([rel.length(), e])
	found.sort_custom(func(a, b): return a[0] < b[0])
	return found.slice(0, MAX_NEAR).map(func(x): return x[1])


## World rect of a 2D collision object's shapes (null if none): used for nearest point / extent.
func _rect_2d(n: Node):
	var r = null
	for c in n.get_children():
		var cr = null
		if c is CollisionShape2D and c.shape != null:
			var s: Vector2 = _shape_size_2d(c.shape) * c.global_transform.get_scale().abs()
			cr = Rect2(c.global_position - s / 2.0, s)
		elif c is CollisionPolygon2D:
			var pts: PackedVector2Array = c.global_transform * c.polygon
			if pts.size() > 0:
				cr = Rect2(pts[0], Vector2.ZERO)
				for q in pts:
					cr = cr.expand(q)
		if cr != null:
			r = cr if r == null else r.merge(cr)
	return r


func _nearest_point(n: Node, from):
	if dims == "2d":
		var r = _rect_2d(n)
		if r == null:
			return n.global_position
		return Vector2(clampf(from.x, r.position.x, r.end.x), clampf(from.y, r.position.y, r.end.y))
	return n.global_position


func _extent(n: Node):
	if dims == "2d":
		var r = _rect_2d(n)
		return r.size if r != null else null
	return null


# ---------------------------------------------------------------- map

## Player-centred picture sampled from the physics space: 3x3 point queries per cell catch thin
## platforms and small hazards. Cell = tile size when the level has tiles, else player height.
func view_map(opts: Dictionary):
	if player == null:
		return {"error": "no player (set player in .ghostlap/profile.yaml)"}
	if dims == "3d":
		return _map_3d(opts)
	var w := int(opts.get("map_w", 32))
	var hgt := int(opts.get("map_h", 12))
	var cell := _tile_px()
	if cell == Vector2.ZERO:
		cell = Vector2(size.y, size.y)
	var space: PhysicsDirectSpaceState2D = player.get_world_2d().direct_space_state
	var q := PhysicsPointQueryParameters2D.new()
	q.collide_with_areas = true
	q.collide_with_bodies = true
	q.collision_mask = 0xFFFFFFFF
	var ex: Array[RID] = []
	for c in [player] + player.find_children("*", "CollisionObject2D", true, false):
		if c is CollisionObject2D:
			ex.append(c.get_rid())
	q.exclude = ex
	var pp: Vector2 = player_centre()
	var origin := (pp - (Vector2(w / 2, hgt / 2) + Vector2(0.5, 0.5)) * cell).round()
	var rows := PackedStringArray()
	var used := {}
	var pcell := Vector2i(w / 2, hgt / 2)
	for y in hgt:
		var row := ""
		for x in w:
			var kind := "empty"
			if Vector2i(x, y) == pcell:
				kind = "player"
			else:
				kind = _sample_cell(space, q, origin + Vector2(x, y) * cell, cell)
			row += SYMBOL[kind]
			used[kind] = true
		rows.append(row)
	var legend := {}
	for k in used:
		if k != "empty":
			legend[SYMBOL[k]] = k
	return {"cell_px": _vec(cell), "origin": _vec(origin), "rows": rows, "legend": legend}


const _RANK := {"empty": 0, "solid": 1, "oneway": 2, "unknown": 3, "pickup": 4, "goal": 5, "enemy": 6, "hazard": 7}

func _sample_cell(space: PhysicsDirectSpaceState2D, q: PhysicsPointQueryParameters2D, top_left: Vector2, cell: Vector2) -> String:
	var best := "empty"
	for iy in 3:
		for ix in 3:
			q.position = top_left + Vector2((ix + 0.5) / 3.0, (iy + 0.5) / 3.0) * cell
			for hit in space.intersect_point(q, 8):
				var k := _classify_hit(hit, q.position)
				if _RANK[k] > _RANK[best]:
					best = k
	return best


func _classify_hit(hit: Dictionary, at: Vector2) -> String:
	var col = hit.get("collider")
	if col is TileMapLayer:
		var k := classify(col) if _res["wall"].search(str(col.name)) == null else "solid"
		if k == "solid" or k == "unknown":
			var td: TileData = col.get_cell_tile_data(col.local_to_map(col.to_local(at)))
			return "oneway" if _tile_oneway(col, td) else "solid"
		return k
	if col is CollisionObject2D:
		var k2 := classify(col)
		if k2 == "solid":
			var sh = col.shape_owner_get_owner(col.shape_find_owner(int(hit.get("shape", 0))))
			if sh is CollisionShape2D and sh.one_way_collision:
				return "oneway"
		return k2
	return "unknown"


func _tile_px() -> Vector2:
	for tm in tree.current_scene.find_children("*", "TileMapLayer", true, false):
		if tm.tile_set != null and tm.get_used_cells().size() > 0:
			return Vector2(tm.tile_set.tile_size) * tm.global_transform.get_scale().abs()
	return Vector2.ZERO


## 3D: a top-down grid of floor heights relative to the player's feet, in player heights.
func _map_3d(opts: Dictionary) -> Dictionary:
	var w := int(opts.get("map_w", 21))
	var hgt := int(opts.get("map_h", 15))
	var h := size.y
	var cell := maxf(h * 0.5, 0.25)
	var space: PhysicsDirectSpaceState3D = player.get_world_3d().direct_space_state
	var feet: Vector3 = player.global_position
	var rows := PackedStringArray()
	var ex: Array[RID] = []
	if player is CollisionObject3D:
		ex.append(player.get_rid())
	for z in hgt:
		var row := ""
		for x in w:
			if x == w / 2 and z == hgt / 2:
				row += "P"
				continue
			var p := feet + Vector3((x - w / 2) * cell, 0, (z - hgt / 2) * cell)
			var rq := PhysicsRayQueryParameters3D.create(p + Vector3(0, 6 * h, 0), p - Vector3(0, 6 * h, 0))
			rq.exclude = ex
			rq.collide_with_areas = true
			var hit := space.intersect_ray(rq)
			if hit.is_empty():
				row += " "
				continue
			var k := classify(hit["collider"])
			if k in ["hazard", "enemy", "pickup", "goal"]:
				row += SYMBOL[k]
				continue
			var dh: float = (hit["position"].y - feet.y) / h
			row += "_" if dh < -0.25 else (str(clampi(roundi(dh), 0, 9)) if dh < 9.5 else "9")
		rows.append(row)
	return {"cell_m": snappedf(cell, 0.01), "rows": rows,
			"legend": {"P": "player", "0-9": "floor height above feet (player heights)", "_": "lower floor",
					" ": "no floor (drop)", "^": "hazard", "E": "enemy", "o": "pickup", "G": "goal"},
			"up": "-z"}


# ---------------------------------------------------------------- grid

func view_grid(layer_filter: String):
	var layers: Array = []
	for tm in tree.current_scene.find_children("*", "TileMapLayer", true, false):
		if tm.tile_set == null or tm.get_used_cells().is_empty() or not tm.is_visible_in_tree():
			continue
		if layer_filter != "" and str(tm.name).to_lower() != layer_filter.to_lower():
			continue
		layers.append(tm)
	if layers.is_empty():
		return _grid_container()
	var base: TileMapLayer = layers[0]
	var half := Vector2(base.tile_set.tile_size) * 0.5
	var pieces := _grid_pieces(base, half)
	var used := Rect2i()
	for tm in layers:
		if tm.tile_set.tile_size != base.tile_set.tile_size:
			continue
		var r: Rect2i = tm.get_used_rect()
		used = r if used.size == Vector2i.ZERO else used.merge(r)
	for pc in pieces:
		used = used.expand(pc[1])
	var truncated := false
	if used.size.x > GRID_MAX.x or used.size.y > GRID_MAX.y:
		truncated = true
		var centre := used.get_center()
		if player != null and player is Node2D:
			centre = base.local_to_map(base.to_local(player.global_position))
		var sz := Vector2i(mini(used.size.x, GRID_MAX.x), mini(used.size.y, GRID_MAX.y))
		var pos := (centre - sz / 2).clamp(used.position, used.end - sz)
		used = Rect2i(pos, sz)
	var cells := {}      # Vector2i -> char
	var legend := {}     # char -> meaning
	for tm in layers:
		if tm.tile_set.tile_size != base.tile_set.tile_size:
			continue
		var lchar := _layer_char(tm, legend)
		for c in tm.get_used_cells():
			if not used.has_point(c):
				continue
			var ch := lchar
			if ch == "":
				var td: TileData = tm.get_cell_tile_data(c)
				ch = ("=" if _tile_oneway(tm, td) else "#") if _tile_solid(tm, td) else "."
				legend[ch] = {"#": "solid", "=": "one-way platform", ".": "floor"}[ch]
			if ch == "." and cells.has(c):
				continue  # floors don't cover walls/goals painted on an earlier layer
			cells[c] = ch
	# grid-snapped nodes (boxes, the player, enemies) on top of the tiles
	var used_names := {}
	for pc in pieces:
		if used.has_point(pc[1]):
			cells[pc[1]] = _node_char(pc[0], used_names, legend)
	var rows := PackedStringArray()
	for y in range(used.position.y, used.end.y):
		var row := ""
		for x in range(used.position.x, used.end.x):
			row += cells.get(Vector2i(x, y), " ")
		rows.append(row)
	if not legend.has(" "):
		legend[" "] = "nothing"
	return {"source": layers.map(func(l): return _path(l)), "cell_px": _vec(Vector2(base.tile_set.tile_size)),
			"origin_cell": [used.position.x, used.position.y], "rows": rows, "legend": legend, "truncated": truncated}


## [node, cell] for every grid-snapped object outside the tile layers. An object is a node that
## would be drawn or does something (sprite, polygon, collision object, script); its own parts
## (a box's sprite) are skipped.
func _grid_pieces(base: TileMapLayer, half: Vector2) -> Array:
	var out := []
	for n in tree.current_scene.find_children("*", "Node2D", true, false):
		if n is TileMapLayer or _inside_tilemap(n) or not _is_piece(n):
			continue
		var par := n.get_parent()
		if par != tree.current_scene and _is_piece(par) and _snapped(par, base, half):
			continue
		if _snapped(n, base, half):
			out.append([n, base.local_to_map(base.to_local(n.global_position))])
	return out


func _is_piece(n: Node) -> bool:
	return n is Node2D and not (n is TileMapLayer) and (n is Sprite2D or n is AnimatedSprite2D
			or n is CollisionObject2D or n is Polygon2D or n.get_script() != null)


func _layer_char(tm: TileMapLayer, legend: Dictionary) -> String:
	var lname := str(tm.name).to_lower()
	if _res["hazard"].search(lname) != null:
		legend["^"] = "hazard (" + str(tm.name) + ")"
		return "^"
	if _res["goal"].search(lname) != null:
		legend["*"] = "goal (" + str(tm.name) + ")"
		return "*"
	if _res["pickup"].search(lname) != null:
		legend["o"] = "pickup (" + str(tm.name) + ")"
		return "o"
	return ""  # decide per tile: solid (has collision) or floor


func _tile_oneway(tm: TileMapLayer, td: TileData) -> bool:
	if td == null or tm.tile_set == null:
		return false
	for layer in tm.tile_set.get_physics_layers_count():
		for i in td.get_collision_polygons_count(layer):
			if td.is_collision_polygon_one_way(layer, i):
				return true
	return false


func _tile_solid(tm: TileMapLayer, td: TileData) -> bool:
	if td == null:
		return false
	for layer in tm.tile_set.get_physics_layers_count():
		if td.get_collision_polygons_count(layer) > 0:
			return true
	return _res["wall"].search(str(tm.name)) != null and not str(tm.name).to_lower().contains("floor")


func _inside_tilemap(n: Node) -> bool:
	var p := n.get_parent()
	while p != null:
		if p is TileMapLayer:
			return true
		p = p.get_parent()
	return false


func _snapped(n: Node, base: TileMapLayer, half: Vector2) -> bool:
	if not (n is Node2D):
		return false
	var local: Vector2 = base.to_local(n.global_position)
	var ts := Vector2(base.tile_set.tile_size)
	var off := Vector2(fposmod(local.x, ts.x), fposmod(local.y, ts.y))
	var tol := ts * 0.15
	# centred on a cell, or on its top-left corner
	return (absf(off.x - half.x) <= tol.x and absf(off.y - half.y) <= tol.y) \
			or ((off.x <= tol.x or off.x >= ts.x - tol.x) and (off.y <= tol.y or off.y >= ts.y - tol.y))


func _node_char(n: Node, used: Dictionary, legend: Dictionary) -> String:
	if n == player:
		legend["@"] = "player"
		return "@"
	var base_name := str(n.name).rstrip("0123456789").rstrip("_ ").to_lower()
	if base_name == "":
		base_name = n.get_class().to_lower()
	if used.has(base_name):
		return used[base_name]
	var kind := classify(n)
	var ch := ""
	if kind in ["hazard", "enemy", "pickup", "goal"]:
		ch = SYMBOL[kind] if not legend.has(SYMBOL[kind]) else ""
	if ch == "":
		for cand in (base_name.to_upper() + "ABCDEFGHIJKLMNQRSTUVWXYZ"):
			if cand >= "A" and cand <= "Z" and not legend.has(cand) and cand != "P":
				ch = cand
				break
	used[base_name] = ch
	legend[ch] = base_name
	return ch


## Boards built from UI: a GridContainer's children as rows of short texts.
func _grid_container():
	for g in tree.root.find_children("*", "GridContainer", true, false):
		if not g.is_visible_in_tree() or g.get_child_count() < 4:
			continue
		var cols: int = maxi(1, g.columns)
		var rows := []
		var row := []
		for c in g.get_children():
			if not (c is Control) or not c.visible:
				continue
			var t := ""
			if "text" in c:
				t = str(c.get("text"))
			row.append(_short(t, 12) if t != "" else ".")
			if row.size() == cols:
				rows.append(row)
				row = []
		if not row.is_empty():
			rows.append(row)
		return {"source": [_path(g)], "rows": rows}
	return {"error": "no TileMapLayer or GridContainer in this scene"}


# ---------------------------------------------------------------- state

func view_state(arg: String):
	var nodes: Array = []
	if arg == "":
		if player != null:
			nodes.append(player)
		elif tree.current_scene != null and tree.current_scene.get_script() != null:
			nodes.append(tree.current_scene)
		for p in profile.get("state_nodes", []):
			var n := tree.current_scene.get_node_or_null(NodePath(str(p)))
			if n != null and not nodes.has(n):
				nodes.append(n)
	elif tree.has_group(arg):
		nodes = tree.get_nodes_in_group(arg)
	else:
		var n := tree.current_scene.get_node_or_null(NodePath(arg))
		if n == null:
			return {"error": "no group or node '%s'" % arg}
		nodes = [n]
	var out := {}
	for n in nodes.slice(0, MAX_STATE_NODES):
		out[_path(n)] = _node_state(n)
	if nodes.size() > MAX_STATE_NODES:
		out["truncated"] = nodes.size() - MAX_STATE_NODES
	return out


func _node_state(n: Node) -> Dictionary:
	var key := str(n.get_path())
	var prev: Dictionary = _prev_state.get(key, {})
	var cur := {}
	if n is Node2D or n is Node3D:
		cur["pos"] = _vec(n.global_position)
	for p in n.get_property_list():
		if not (int(p["usage"]) & PROPERTY_USAGE_SCRIPT_VARIABLE):
			continue
		var pname: String = p["name"]
		if pname.begins_with("_"):
			continue
		var v = n.get(pname)
		if v is Object and v != null:
			continue  # node/resource references are noise here
		cur[pname] = _json(v, true)
	_prev_state[key] = cur
	var changed := []
	var out := {}
	if not prev.is_empty():
		for k in cur:
			if not prev.has(k) or prev[k] != cur[k]:
				changed.append(k)
		for k in changed:
			out[k] = cur[k]
	for k in cur:
		if not out.has(k):
			out[k] = cur[k]
	if not changed.is_empty():
		out["changed"] = changed
	return out


# ---------------------------------------------------------------- game

func view_game():
	var cands: Array = [tree.current_scene]
	for c in tree.root.get_children():
		if c != tree.current_scene:
			cands.append(c)
	for n in cands:
		if n != null and n.has_method("_ghostlap_state"):
			var v = n.call("_ghostlap_state")
			var s := JSON.stringify(_json(v, true))
			if s.length() > 2000:
				return {"truncated": true, "text": s.substr(0, 2000)}
			return _json(v, true)
	return null


# ---------------------------------------------------------------- helpers

func _path(n: Node) -> String:
	var cs := tree.current_scene
	if cs != null and (n == cs or cs.is_ancestor_of(n)):
		return "." if n == cs else str(cs.get_path_to(n))
	return str(n.get_path()).trim_prefix("/root/")


func _short(s: String, n: int = MAX_TEXT) -> String:
	s = s.strip_edges().replace("\n", " / ")
	return s if s.length() <= n else s.substr(0, n - 1) + "…"


func _num(x: float):
	return int(x) if is_equal_approx(x, roundf(x)) else snappedf(x, 0.01)


## Rounded vector as a JSON array: 2D world units to whole pixels, 3D to cm; `digits` overrides.
func _vec(v, digits: int = -1) -> Array:
	var step := pow(10.0, -digits) if digits >= 0 else (1.0 if dims == "2d" else 0.01)
	var comps: Array = [v.x, v.y] if (v is Vector2 or v is Vector2i) else [v.x, v.y, v.z]
	return comps.map(func(c): return roundi(c) if step == 1.0 else snappedf(c, step))


func _json(v, compact: bool = false):
	match typeof(v):
		TYPE_NIL, TYPE_BOOL, TYPE_INT:
			return v
		TYPE_FLOAT:
			return _num(v)
		TYPE_STRING, TYPE_STRING_NAME, TYPE_NODE_PATH:
			return _short(str(v), 60) if compact else str(v)
		TYPE_VECTOR2, TYPE_VECTOR2I, TYPE_VECTOR3, TYPE_VECTOR3I:
			return _vec(v)
		TYPE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_FLOAT32_ARRAY, \
				TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_STRING_ARRAY, TYPE_PACKED_VECTOR2_ARRAY:
			var out := []
			for x in v:
				if compact and out.size() >= 8:
					out.append("…+%d" % (v.size() - 8))
					break
				out.append(_json(x, compact))
			return out
		TYPE_DICTIONARY:
			var d := {}
			for k in v:
				d[str(k)] = _json(v[k], compact)
			return d
		TYPE_OBJECT:
			return str(v) if v != null else null
	return var_to_str(v)
