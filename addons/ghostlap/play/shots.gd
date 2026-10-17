# Screenshot checkpoints (design §3.3.1): the bridge captures on events; Claude reads a shot only
# when a response hints at it.
#
# A capture is <session>/shots/f<frame>.png, taken at the end of the frame that caused it; several
# triggers on one frame share the file. Triggers:
#
#   start         frame 0
#   scene_change  the current scene's path changed (a reload of the same scene is not a change)
#   incident      any incident but a death
#   death         a death
#   success       the goal's success expression became true
#   ui            a newly visible popup, dialog or end screen, shot UI_SETTLE frames later (fade-ins)
#   rolling       every `every` frames (multiples of it), keeping the last `keep`
#
# Rolling-only shots rotate out, except those listed in shots/keep.txt (cited with `note --saw`
# or read by Claude). Headless has no renderer: nothing is captured.
extends RefCounted

const TRIGGERS := ["start", "scene_change", "incident", "death", "success", "ui"]
const UI_SETTLE := 15
const UI_MIN_AREA := 0.05     # share of the viewport a new element covers to count without a button
const END_RE := "game ?over|you (win|won|lose|lost|died)|victory|defeat|level (complete|clear)|paused|congrat|try again|retry"

var tree: SceneTree
var dir: String
var headless := false
var every := 600
var keep := 5                 # -1: keep all
var on := {}                  # trigger -> true
var width := 960

var taken := {}               # frame -> {path, size, why}
var new_shots: Array = []     # captures since the last response: {f, path, why}
var new_ui: Array = []        # UI appearances since the last response: {f, node, text, textless, shot}
var _rolling: Array = []      # rolling-only frames still on disk, oldest first
var _scene_id := 0
var _scene := ""
var _controls: Array = []
var _controls_dirty := true
var _visible := {}            # instance id -> true, as of the last frame
var _ui_baseline := true      # the first frame of a scene instance: what's there isn't new
var _ui_pending: Array = []
var _ui_due := -1
var _end_re := RegEx.create_from_string("(?i)" + END_RE)


func _init(t: SceneTree, session_dir: String, is_headless: bool) -> void:
	tree = t
	dir = session_dir.path_join("shots")
	headless = is_headless
	for k in TRIGGERS:
		on[k] = true
	tree.node_added.connect(_on_node_added)


func _on_node_added(n: Node) -> void:
	if n is Control:
		_controls_dirty = true


func config() -> Dictionary:
	return {"every": every, "keep": keep, "on": on.keys(), "width": width, "headless": headless}


## Live changes (`play config`): they apply from the next frame.
func set_config(a: Dictionary) -> String:
	if a.has("every"):
		if int(a["every"]) < 0:
			return "every must be >= 0 (0 turns rolling capture off)"
		every = int(a["every"])
	if a.has("keep"):
		keep = int(a["keep"])
		_rotate()
	if a.has("width"):
		if int(a["width"]) < 64:
			return "width must be >= 64"
		width = int(a["width"])
	if a.has("on"):
		var want := {}
		for t in a["on"]:
			if not str(t) in TRIGGERS:
				return "unknown trigger %s (use %s)" % [t, ", ".join(TRIGGERS)]
			want[str(t)] = true
		if want.has("ui") and not on.has("ui"):
			_ui_baseline = true   # what's on screen now isn't new
		on = want
		if not on.has("ui"):
			_ui_pending.clear()
			_ui_due = -1
	return ""


## At the end of every frame, after the incident and goal checks.
func tick(frame: int, incs: Array, goal_met: bool) -> void:
	if headless:
		return
	var why: Array = []
	var cs := tree.current_scene
	if frame == 0:
		why.append("start")
	if cs != null and cs.get_instance_id() != _scene_id:
		if _scene_id != 0 and cs.scene_file_path != _scene:
			why.append("scene_change")
		_scene_id = cs.get_instance_id()
		_scene = cs.scene_file_path
		_ui_baseline = true
		_ui_pending.clear()
		_ui_due = -1
	for inc in incs:
		var t := "death" if inc["type"] == "death" else "incident"
		if not t in why:
			why.append(t)
	if goal_met:
		why.append("success")
	if on.has("ui") and cs != null:
		_track_ui(frame)
		if _ui_due >= 0 and frame >= _ui_due:
			why.append("ui")
	why = why.filter(func(w): return on.has(w))
	if every > 0 and keep != 0 and frame > 0 and frame % every == 0:   # keep 0: nothing to keep
		why.append("rolling")
	if why.is_empty():
		return
	var sh := capture(frame, why)
	if sh.has("path"):
		for inc in incs:
			inc["shot"] = sh["path"]
	if "ui" in why:
		_ui_done(sh)


## A command is about to return: a popup still settling is shot now, so its hint isn't late.
func flush(frame: int) -> void:
	if headless or _ui_due < 0:
		return
	_ui_done(capture(frame, ["ui"]))


func capture(frame: int, why: Array, w: int = -1) -> Dictionary:
	if headless:
		return {"error": "headless: no renderer (start the session with --window or --offscreen for screenshots)"}
	if taken.has(frame):
		var t: Dictionary = taken[frame]
		var grew := false
		for x in why:
			if not x in t["why"]:
				t["why"].append(x)
				grew = true
		if t["why"] != ["rolling"]:
			_rolling.erase(frame)
		if grew:
			new_shots.append({"f": frame, "path": t["path"], "why": t["why"].duplicate()})
		return {"path": t["path"], "size": t["size"], "frame": frame, "why": t["why"].duplicate()}
	RenderingServer.force_draw(false)
	var img := tree.root.get_texture().get_image()
	if img == null or img.is_empty():
		return {"error": "viewport image is empty"}
	var ww := width if w <= 0 else w
	if img.get_width() > ww:
		img.resize(ww, maxi(1, roundi(img.get_height() * float(ww) / img.get_width())), Image.INTERPOLATE_LANCZOS)
	DirAccess.make_dir_recursive_absolute(dir)
	var path := dir.path_join("f%d.png" % frame)
	if img.save_png(path) != OK:
		return {"error": "cannot write " + path}
	var size := [img.get_width(), img.get_height()]
	taken[frame] = {"path": path, "size": size, "why": why.duplicate()}
	new_shots.append({"f": frame, "path": path, "why": why.duplicate()})
	if why == ["rolling"]:
		_rolling.append(frame)
		_rotate()
	return {"path": path, "size": size, "frame": frame, "why": why.duplicate()}


## Deltas for the next response.
func take_news() -> Dictionary:
	var out := {"shots": new_shots, "ui": new_ui}
	new_shots = []
	new_ui = []
	return out


func _rotate() -> void:
	if keep < 0 or _rolling.size() <= keep:
		return
	var pinned := _pinned()
	while _rolling.size() > keep:
		var f = _rolling.pop_front()
		var p: String = taken[f]["path"]
		if p.get_file() in pinned:
			continue
		DirAccess.remove_absolute(p)
		taken.erase(f)


func _pinned() -> PackedStringArray:
	var f := FileAccess.open(dir.path_join("keep.txt"), FileAccess.READ)
	if f == null:
		return PackedStringArray()
	var out := PackedStringArray()
	for ln in f.get_as_text().split("\n", false):
		out.append(ln.strip_edges().get_file())
	return out


# ---------------------------------------------------------------- new UI

func _track_ui(frame: int) -> void:
	if _controls_dirty:
		_controls = tree.root.find_children("*", "Control", true, false)
		_controls_dirty = false
	var vp := tree.root.get_visible_rect()
	var now := {}
	var fresh := {}
	var order: Array = []
	for c in _controls:
		if not is_instance_valid(c) or not _shown(c, vp):
			continue
		var id: int = c.get_instance_id()
		now[id] = true
		if not _visible.has(id):
			fresh[id] = true
			order.append(c)
	_visible = now
	if _ui_baseline:
		_ui_baseline = false
		return
	for c in order:
		# a new element is the topmost newly visible Control; its children are part of it
		var parent: Node = c.get_parent()
		if parent is Control and fresh.has(parent.get_instance_id()):
			continue
		var info := _describe(c, vp)
		if info.is_empty():
			continue
		info["f"] = frame
		_ui_pending.append(info)
		if _ui_due < 0:
			_ui_due = frame + UI_SETTLE


func _ui_done(sh: Dictionary) -> void:
	for info in _ui_pending:
		if sh.has("path"):
			info["shot"] = sh["path"]
		new_ui.append(info)
	_ui_pending.clear()
	_ui_due = -1


func _shown(c: Control, vp: Rect2) -> bool:
	if not c.is_inside_tree() or not c.is_visible_in_tree():
		return false
	var w := c.get_window()
	if w != null and w != tree.root and not w.visible:
		return false
	var r := _rect(c)
	return r.size.x >= 1.0 and r.size.y >= 1.0 and vp.intersects(r)


func _rect(c: Control) -> Rect2:
	var xf := c.get_global_transform_with_canvas()
	return Rect2(xf.origin, c.size * xf.get_scale().abs())


## {node, text, textless} for an element worth a shot, else {}. Worth it: it shows something (text,
## a button or a picture: a bare ColorRect fade isn't) and is a popup window, has a button, covers
## UI_MIN_AREA of the screen, or reads like an end screen. Textless: no readable text at all.
func _describe(root: Control, vp: Rect2) -> Dictionary:
	var texts := PackedStringArray()
	var button := false
	var art := false
	var stack: Array = [root]
	while not stack.is_empty():
		var c = stack.pop_back()
		if c is Control:
			if not c.is_visible_in_tree():
				continue
			var t := _text(c)
			if t != "":
				texts.append(t)
			if c is BaseButton:
				button = true
			if c is TextureRect or c is TextureButton or c is NinePatchRect or c is TextureProgressBar:
				art = true
		var kids: Array = c.get_children()
		kids.reverse()   # depth-first in tree order: texts read top to bottom
		stack.append_array(kids)
	if texts.is_empty() and not button and not art:
		return {}
	var joined := " | ".join(texts)
	var r := _rect(root).intersection(vp)
	var area := (r.size.x * r.size.y) / maxf(vp.size.x * vp.size.y, 1.0)
	var popup := root.get_window() != tree.root
	if not (popup or button or area >= UI_MIN_AREA or _end_re.search(joined) != null):
		return {}
	return {"node": str(root.get_path()), "text": joined.left(80), "textless": texts.is_empty()}


static func _text(c: Control) -> String:
	if c is Label:
		return c.text.strip_edges()
	if c is RichTextLabel:
		return c.get_parsed_text().strip_edges()
	if c is Button or c is LineEdit:
		return str(c.text).strip_edges()
	return ""
