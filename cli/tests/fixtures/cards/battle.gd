extends Control
## Enemy (20 HP) attacks for 6 each turn. 3 energy per turn. Win by bringing it to 0.

const CARDS := [
	{"name": "Strike", "cost": 1, "damage": 6},
	{"name": "Strike", "cost": 1, "damage": 6},
	{"name": "Block", "cost": 1, "block": 5},
	{"name": "Heavy Blow", "cost": 3, "damage": 14},
]

var energy := 3
var enemy_hp := 20
var player_hp := 30
var block := 0


func _ready() -> void:
	for i in CARDS.size():
		var c: Dictionary = CARDS[i]
		var b := Button.new()
		b.name = "Card%d" % (i + 1)
		b.custom_minimum_size = Vector2(160, 80)
		b.text = "%s (%d)\n%s" % [c["name"], c["cost"], ("deal %d" % c["damage"]) if c.has("damage") else ("block %d" % c["block"])]
		b.pressed.connect(_play.bind(i, b))
		$Hand.add_child(b)
	$EndTurn.pressed.connect(_end_turn)
	_refresh()


func _play(i: int, b: Button) -> void:
	var c: Dictionary = CARDS[i]
	if c["cost"] > energy or enemy_hp <= 0:
		return
	energy -= c["cost"]
	enemy_hp = maxi(0, enemy_hp - int(c.get("damage", 0)))
	block += int(c.get("block", 0))
	b.disabled = true
	_refresh()


func _end_turn() -> void:
	if enemy_hp <= 0:
		return
	player_hp -= maxi(0, 6 - block)
	block = 0
	energy = 3
	for b in $Hand.get_children():
		b.disabled = false
	_refresh()


func _refresh() -> void:
	$Energy.text = "Energy %d/3" % energy
	$EnemyHP.text = "Enemy HP %d/20" % enemy_hp
	$Intent.text = "Intent: attack 6"
	$PlayerHP.text = "HP %d/30  Block %d" % [player_hp, block]
	$Result.text = "Victory!" if enemy_hp <= 0 else ""
	for b in $Hand.get_children():
		if not b.disabled:
			b.disabled = CARDS[b.get_index()]["cost"] > energy
