extends Node2D
## Builds the TileSet and paints LAYOUT: # solid, = one-way, ^ spikes, o coin, G goal, P player.

const T := 32
const LAYOUT := [
	"..............................",
	"..............................",
	"..................o.......G...",
	"...............=====......#...",
	"..........=====...........#...",
	"..P.......................#...",
	"##########....^^^^...#########",
	"##############################",
]


func _ready() -> void:
	var ts := TileSet.new()
	ts.tile_size = Vector2i(T, T)
	ts.add_physics_layer()
	var img := Image.create(T * 3, T, false, Image.FORMAT_RGBA8)
	img.fill(Color(0.4, 0.4, 0.45))
	var src := TileSetAtlasSource.new()
	src.texture = ImageTexture.create_from_image(img)
	src.texture_region_size = Vector2i(T, T)
	ts.add_source(src, 0)
	var h := T / 2.0
	for x in 3:
		src.create_tile(Vector2i(x, 0))
		var td := src.get_tile_data(Vector2i(x, 0), 0)
		td.add_collision_polygon(0)
		if x == 1:  # one-way: a thin strip on top
			td.set_collision_polygon_points(0, 0, PackedVector2Array([Vector2(-h, -h), Vector2(h, -h), Vector2(h, -h + 6), Vector2(-h, -h + 6)]))
			td.set_collision_polygon_one_way(0, 0, true)
		else:
			td.set_collision_polygon_points(0, 0, PackedVector2Array([Vector2(-h, -h), Vector2(h, -h), Vector2(h, h), Vector2(-h, h)]))
	$Ground.tile_set = ts
	$Spikes.tile_set = ts
	for y in LAYOUT.size():
		for x in LAYOUT[y].length():
			var c: String = LAYOUT[y][x]
			var cell := Vector2i(x, y)
			var centre := Vector2(x * T + h, y * T + h)
			match c:
				"#": $Ground.set_cell(cell, 0, Vector2i(0, 0))
				"=": $Ground.set_cell(cell, 0, Vector2i(1, 0))
				"^": $Spikes.set_cell(cell, 0, Vector2i(2, 0))
				"o": $Coin.position = centre
				"G": $Goal.position = centre
				"P": $Player.position = centre
