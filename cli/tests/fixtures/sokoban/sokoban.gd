extends Node2D
## Push both boxes (B) onto the goals (*). @ = player, # = wall.

const T := 32
const LEVEL := [
	"#######",
	"#.....#",
	"#.@B*.#",
	"#.....#",
	"#.B*..#",
	"#######",
]
const DIRS := {"move_up": Vector2i.UP, "move_down": Vector2i.DOWN, "move_left": Vector2i.LEFT, "move_right": Vector2i.RIGHT}

var walls := {}
var goals := {}
var boxes := {}        # Vector2i -> Node2D
var player: Node2D


func _ready() -> void:
	var ts := TileSet.new()
	ts.tile_size = Vector2i(T, T)
	ts.add_physics_layer()
	var img := Image.create(T * 3, T, false, Image.FORMAT_RGBA8)
	img.fill(Color(0.3, 0.3, 0.35))
	var src := TileSetAtlasSource.new()
	src.texture = ImageTexture.create_from_image(img)
	src.texture_region_size = Vector2i(T, T)
	ts.add_source(src, 0)
	for x in 3:
		src.create_tile(Vector2i(x, 0))
	var wall := src.get_tile_data(Vector2i(0, 0), 0)
	wall.add_collision_polygon(0)
	var h := T / 2.0
	wall.set_collision_polygon_points(0, 0, PackedVector2Array([Vector2(-h, -h), Vector2(h, -h), Vector2(h, h), Vector2(-h, h)]))
	for layer in [$Floor, $Walls, $Goals]:
		layer.tile_set = ts
	for y in LEVEL.size():
		for x in LEVEL[y].length():
			var c := Vector2i(x, y)
			match LEVEL[y][x]:
				"#":
					$Walls.set_cell(c, 0, Vector2i(0, 0))
					walls[c] = true
					continue
				"*":
					$Goals.set_cell(c, 0, Vector2i(2, 0))
					goals[c] = true
				"B":
					var b := _piece("Box%d" % (boxes.size() + 1), Color(0.8, 0.6, 0.3))
					boxes[c] = b
					b.position = _centre(c)
				"@":
					player = _piece("Player", Color(0.3, 0.7, 1.0))
					player.add_to_group("player")
					player.position = _centre(c)
			$Floor.set_cell(c, 0, Vector2i(1, 0))


func _piece(n: String, col: Color) -> Node2D:
	var p := Polygon2D.new()
	p.name = n
	p.color = col
	p.polygon = PackedVector2Array([Vector2(-12, -12), Vector2(12, -12), Vector2(12, 12), Vector2(-12, 12)])
	$Pieces.add_child(p)
	return p


func _centre(c: Vector2i) -> Vector2:
	return Vector2(c) * T + Vector2(T, T) / 2.0


func _cell(n: Node2D) -> Vector2i:
	return Vector2i((n.position / T).floor())


func _physics_process(_d: float) -> void:
	for action in DIRS:
		if Input.is_action_just_pressed(action):
			_move(DIRS[action])


func _move(d: Vector2i) -> void:
	var to := _cell(player) + d
	if walls.has(to):
		return
	if boxes.has(to):
		var beyond := to + d
		if walls.has(beyond) or boxes.has(beyond):
			return
		var b: Node2D = boxes[to]
		boxes.erase(to)
		boxes[beyond] = b
		b.position = _centre(beyond)
	player.position = _centre(to)
	var on := boxes.keys().filter(func(c): return goals.has(c)).size()
	$UI/Status.text = "Solved!" if on == goals.size() else "Boxes on goals: %d/%d" % [on, goals.size()]


## Optional ghostlap hook: exact game state for the `game` look view.
func _ghostlap_state() -> Dictionary:
	return {"boxes_on_goals": boxes.keys().filter(func(c): return goals.has(c)).size(), "goals": goals.size()}
