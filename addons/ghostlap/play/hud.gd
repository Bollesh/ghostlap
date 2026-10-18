# The caption layer of a session video (design §4.2), drawn over the game during a Movie Maker
# replay: the goal, the inputs held right now as key chips, Claude's notes at their frames,
# incidents, and the verdict from the final frame on.
#
# --hud=<abs json>: {goal, describe, verdict, basis, why, tps, final, inputs: [{f, action|key|mouse,
# pressed}], notes: [{f, text}], incidents: [{f, type, why}], goal_met_at}
extends CanvasLayer

const NOTE_S := 3.0
const INCIDENT_S := 2.0
const CHIP_MIN := 8          # frames a chip stays up, so a 1-frame tap is visible at 30 fps
const VERDICT_COLOR := {"pass": Color(0.18, 0.62, 0.35), "fail": Color(0.78, 0.24, 0.22), "blocked": Color(0.42, 0.45, 0.5)}

var d: Dictionary
var tps := 60
var _inputs: Array = []
var _next := 0
var _down := {}              # chip label -> frame pressed
var _up_at := {}             # chip label -> frame it may disappear (released, CHIP_MIN shown)
var _fs := 20
var _clock: Label
var _chips: HBoxContainer
var _note: Label
var _incident: Label
var _banner: PanelContainer
var _chip_nodes := {}


func setup(data: Dictionary, vp: Vector2) -> void:
	d = data
	tps = int(d.get("tps", 60))
	_inputs = d.get("inputs", [])
	layer = 128
	_fs = maxi(14, int(vp.y / 32.0))
	var top := _panel(Color(0, 0, 0, 0.55))
	top.position = Vector2(_fs * 0.6, _fs * 0.6)
	var box := VBoxContainer.new()
	top.add_child(box)
	box.add_child(_label("GOAL  " + str(d.get("goal", "")), _fs, Color(1, 1, 1)))
	if str(d.get("describe", "")) != "":
		var ds := _label(str(d["describe"]), int(_fs * 0.7), Color(0.85, 0.85, 0.85))
		ds.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		ds.custom_minimum_size.x = vp.x * 0.45
		box.add_child(ds)
	_clock = _label("", int(_fs * 0.7), Color(0.8, 0.8, 0.8))
	box.add_child(_clock)
	add_child(top)

	_chips = HBoxContainer.new()
	_chips.add_theme_constant_override("separation", int(_fs * 0.4))
	_chips.position = Vector2(_fs * 0.6, vp.y - _fs * 2.6)
	add_child(_chips)

	_note = _label("", int(_fs * 0.9), Color(1, 1, 1))
	_note.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_note.size = Vector2(vp.x * 0.7, _fs * 3)
	_note.position = Vector2(vp.x * 0.15, vp.y - _fs * 6.2)
	_note.add_theme_color_override("font_outline_color", Color(0, 0, 0))
	_note.add_theme_constant_override("outline_size", maxi(4, _fs / 4))
	add_child(_note)

	_incident = _label("", int(_fs * 0.85), Color(1, 0.85, 0.3))
	_incident.add_theme_color_override("font_outline_color", Color(0, 0, 0))
	_incident.add_theme_constant_override("outline_size", maxi(4, _fs / 4))
	_incident.position = Vector2(vp.x * 0.55, _fs * 0.8)
	_incident.size = Vector2(vp.x * 0.43, _fs * 2)
	_incident.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	add_child(_incident)

	var verdict := str(d.get("verdict", ""))
	var kind := verdict.split(" ")[0] if verdict != "" else ""
	_banner = _panel(VERDICT_COLOR.get(kind, Color(0.3, 0.3, 0.3)))
	var bb := VBoxContainer.new()
	bb.alignment = BoxContainer.ALIGNMENT_CENTER
	_banner.add_child(bb)
	var big := _label(verdict.to_upper(), _fs * 2, Color(1, 1, 1))
	big.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	bb.add_child(big)
	if str(d.get("why", "")) != "":
		var w := _label(str(d["why"]), _fs, Color(1, 1, 1))
		w.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		w.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		w.custom_minimum_size.x = vp.x * 0.6
		bb.add_child(w)
	_banner.custom_minimum_size = Vector2(vp.x * 0.66, 0)
	_banner.position = Vector2(vp.x * 0.17, vp.y * 0.36)
	_banner.visible = false
	add_child(_banner)


func show_frame(f: int) -> void:
	var shown := mini(f, int(d.get("final", f)))   # the clock stops on the final frame's hold
	_clock.text = "f=%d · %.2f s" % [shown, float(shown) / tps]
	# inputs the game sees on frame f
	while _next < _inputs.size() and int(_inputs[_next]["f"]) <= f:
		var e: Dictionary = _inputs[_next]
		_next += 1
		var name := _name(e)
		if bool(e.get("pressed", false)):
			_down[name] = f
			_up_at.erase(name)
		elif _down.has(name):
			_up_at[name] = maxi(f, int(_down[name]) + CHIP_MIN)
			_down.erase(name)
	for name in _up_at.keys():
		if f >= int(_up_at[name]):
			_up_at.erase(name)
	var want := {}
	for name in _down:
		want[name] = true
	for name in _up_at:
		want[name] = true
	for name in _chip_nodes.keys():
		if not want.has(name):
			_chip_nodes[name].queue_free()
			_chip_nodes.erase(name)
	for name in want:
		if not _chip_nodes.has(name):
			var chip := _panel(Color(0.95, 0.95, 0.95, 0.92))
			chip.add_child(_label(name, int(_fs * 0.85), Color(0.08, 0.08, 0.08)))
			_chips.add_child(chip)
			_chip_nodes[name] = chip

	_note.text = _latest(d.get("notes", []), f, NOTE_S, func(n): return "“%s”" % n["text"])
	_incident.text = _latest(d.get("incidents", []), f, INCIDENT_S, func(i): return "⚠ %s" % i["type"])
	_incident.add_theme_color_override("font_color", Color(1, 0.85, 0.3))
	var met = d.get("goal_met_at")
	if met != null and f >= int(met) and f < int(met) + int(NOTE_S * tps) and _incident.text == "":
		_incident.text = "✓ goal met"
		_incident.add_theme_color_override("font_color", Color(0.5, 1, 0.6))
	_banner.visible = f >= int(d.get("final", 1 << 30))


func _latest(items: Array, f: int, secs: float, fmt: Callable) -> String:
	var out := ""
	for it in items:
		var at := int(it["f"])
		if at <= f and f < at + int(secs * tps):
			out = fmt.call(it)
	return out


static func _name(e: Dictionary) -> String:
	if e.has("action"):
		return str(e["action"])
	if e.has("key"):
		return "key " + str(e["key"])
	return "click"


func _label(t: String, size: int, color: Color) -> Label:
	var l := Label.new()
	l.text = t
	l.add_theme_font_size_override("font_size", size)
	l.add_theme_color_override("font_color", color)
	return l


func _panel(color: Color) -> PanelContainer:
	var p := PanelContainer.new()
	var sb := StyleBoxFlat.new()
	sb.bg_color = color
	sb.set_corner_radius_all(maxi(4, _fs / 3))
	sb.content_margin_left = _fs * 0.5
	sb.content_margin_right = _fs * 0.5
	sb.content_margin_top = _fs * 0.25
	sb.content_margin_bottom = _fs * 0.25
	p.add_theme_stylebox_override("panel", sb)
	return p
