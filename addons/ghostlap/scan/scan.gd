# ghostlap engine extractor.
#
# Run headless from outside the project (no install needed):
#   godot --headless --path <project> --script <abs>/scan.gd -- --out=<abs.json> [--files=<abs.txt>]
#
# Reads scenes through SceneState, which never instantiates nodes, so game code in the scanned
# scenes doesn't run. Note: Godot still adds the project's autoloads after this script's
# _init() returns; we finish writing output inside _init() so autoload code can't affect it.
extends SceneTree

const SCHEMA_VERSION := 1

var _errors: Array = []
var _logger: Object = null


func _init() -> void:
	var args := _parse_args()
	var out_path: String = args.get("out", "")
	if out_path == "":
		printerr("ghostlap scan: missing --out=<path>")
		quit(2)
		return
	if ClassDB.class_exists("Logger"):
		_logger = load(get_script().resource_path.get_base_dir() + "/error_logger.gd").new()
		OS.call("add_logger", _logger)

	var files: PackedStringArray = []
	if args.has("files"):
		var f := FileAccess.open(args["files"], FileAccess.READ)
		if f:
			for line in f.get_as_text().split("\n", false):
				if line.strip_edges() != "":
					files.append(line.strip_edges())

	var result := {
		"schema": SCHEMA_VERSION,
		"project": _project(),
		"files": {},
	}
	for path in files:
		result["files"][path] = _extract(path)

	var out := FileAccess.open(out_path, FileAccess.WRITE)
	if out == null:
		printerr("ghostlap scan: cannot write ", out_path)
		quit(3)
		return
	out.store_string(JSON.stringify(_sanitize(result)))
	out.close()
	if _logger != null:
		OS.call("remove_logger", _logger)
	quit(0)


func _parse_args() -> Dictionary:
	var d := {}
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--") and a.contains("="):
			var kv := a.substr(2).split("=", true, 1)
			d[kv[0]] = kv[1]
	return d


func _take_errors() -> Array:
	if _logger == null:
		return []
	var e: Array = _logger.take()
	return e


# ---------------------------------------------------------------- project

func _project() -> Dictionary:
	var inputs := {}
	for action in InputMap.get_actions():
		var evs := []
		for ev in InputMap.action_get_events(action):
			evs.append(_describe_event(ev))
		inputs[String(action)] = evs
	var autoloads := {}
	for p in ProjectSettings.get_property_list():
		var n: String = p["name"]
		if n.begins_with("autoload/"):
			autoloads[n.substr(9)] = String(ProjectSettings.get_setting(n)).trim_prefix("*")
	var classes := []
	for c in ProjectSettings.get_global_class_list():
		classes.append({"class": c.get("class"), "path": c.get("path"), "base": c.get("base"), "language": c.get("language")})
	return {
		"source": "engine",
		"godot_version": Engine.get_version_info().get("string", ""),
		"name": ProjectSettings.get_setting("application/config/name", ""),
		"features": ProjectSettings.get_setting("application/config/features", PackedStringArray()),
		"main_scene": ProjectSettings.get_setting("application/run/main_scene", ""),
		"physics": {
			"ticks_per_second": ProjectSettings.get_setting("physics/common/physics_ticks_per_second", 60),
			"gravity_2d": ProjectSettings.get_setting("physics/2d/default_gravity", 980.0),
			"gravity_3d": ProjectSettings.get_setting("physics/3d/default_gravity", 9.8),
		},
		"display": {
			"width": ProjectSettings.get_setting("display/window/size/viewport_width", 1152),
			"height": ProjectSettings.get_setting("display/window/size/viewport_height", 648),
			"stretch_mode": ProjectSettings.get_setting("display/window/stretch/mode", "disabled"),
		},
		"autoloads": autoloads,
		"inputs": inputs,
		"global_classes": classes,
		"errors": _take_errors(),
	}


func _describe_event(ev: InputEvent) -> Dictionary:
	if ev is InputEventKey:
		var code: int = ev.physical_keycode if ev.physical_keycode != 0 else ev.keycode
		return {"type": "key", "code": code, "name": OS.get_keycode_string(code)}
	if ev is InputEventJoypadButton:
		return {"type": "joypad_button", "button": ev.button_index}
	if ev is InputEventJoypadMotion:
		return {"type": "joypad_axis", "axis": ev.axis, "value": ev.axis_value}
	if ev is InputEventMouseButton:
		return {"type": "mouse_button", "button": ev.button_index}
	return {"type": ev.get_class()}


# ---------------------------------------------------------------- files

func _extract(path: String) -> Dictionary:
	var rec: Dictionary
	var ext := path.get_extension().to_lower()
	if ext == "tscn" or ext == "scn":
		rec = _scene(path)
	elif ext == "gd" or ext == "cs":
		rec = _script(path)
	elif ext == "tres" or ext == "res":
		rec = _resource(path)
	else:
		rec = {"kind": "unknown", "errors": []}
	rec["source"] = "engine"
	rec["uid"] = _uid(path)
	var errs: Array = rec.get("errors", [])
	errs.append_array(_take_errors())
	rec["errors"] = errs
	return rec


func _uid(path: String) -> Variant:
	var id := ResourceLoader.get_resource_uid(path)
	if id == ResourceUID.INVALID_ID:
		return null
	return ResourceUID.id_to_text(id)


func _scene(path: String) -> Dictionary:
	var ps = ResourceLoader.load(path)
	if not (ps is PackedScene):
		return {"kind": "scene", "load_failed": true, "errors": [{"kind": "load", "message": "could not load scene"}]}
	var st: SceneState = ps.get_state()
	var rec := {"kind": "scene", "inherits": null, "nodes": [], "connections": [], "resources": [], "errors": []}
	var deps := {}
	for d in ResourceLoader.get_dependencies(path):
		# "uid://...::::res://path" (4.x) or a bare path
		var parts: PackedStringArray = String(d).split("::")
		deps[parts[parts.size() - 1]] = true
	rec["resources"] = deps.keys()

	for i in st.get_node_count():
		var npath := _norm_path(String(st.get_node_path(i)))
		var parent: Variant = null
		if i > 0:
			parent = _norm_path(String(st.get_node_path(i, true)))
		var inst: PackedScene = st.get_node_instance(i)
		var instance_path: Variant = inst.resource_path if inst != null else null
		var type := String(st.get_node_type(i))
		if type == "" and inst != null:
			type = _root_type(inst)
		var script_path: Variant = null
		var props := {}
		for j in st.get_node_property_count(i):
			var pname := String(st.get_node_property_name(i, j))
			var pval = st.get_node_property_value(i, j)
			if pname == "script":
				if pval is Script:
					script_path = pval.resource_path
				continue
			props[pname] = _val(pval, 0)
		var groups := []
		for g in st.get_node_groups(i):
			groups.append(String(g))
		if i == 0 and inst != null:
			rec["inherits"] = instance_path
		rec["nodes"].append({
			"path": npath,
			"name": String(st.get_node_name(i)),
			"type": type if type != "" else null,
			"parent": parent,
			"instance": instance_path,
			"script": script_path,
			"groups": groups,
			"props": props,
		})
	for c in st.get_connection_count():
		rec["connections"].append({
			"signal": String(st.get_connection_signal(c)),
			"from": _norm_path(String(st.get_connection_source(c))),
			"to": _norm_path(String(st.get_connection_target(c))),
			"method": String(st.get_connection_method(c)),
		})
	return rec


## SceneState paths look like "./Ground/Shape"; .tscn files say parent="Ground". Use the latter.
func _norm_path(p: String) -> String:
	if p.begins_with("./"):
		return p.substr(2)
	return p


func _root_type(ps: PackedScene) -> String:
	var st := ps.get_state()
	var depth := 0
	while st != null and depth < 16:
		var t := String(st.get_node_type(0))
		if t != "":
			return t
		var inst: PackedScene = st.get_node_instance(0)
		if inst == null:
			break
		st = inst.get_state()
		depth += 1
	return ""


func _script(path: String) -> Dictionary:
	var s = ResourceLoader.load(path)
	if not (s is Script):
		return {"kind": "script", "load_failed": true, "errors": [{"kind": "load", "message": "could not load script"}]}
	var rec := {
		"kind": "script",
		"language": "csharp" if path.ends_with(".cs") else "gdscript",
		"class_name": null,
		"native_base": String(s.get_instance_base_type()),
		"base_script": null,
		"compiled": s.can_instantiate(),
		"methods": [],
		"signals": [],
		"properties": [],
		"constants": {},
		"errors": [],
	}
	if s.has_method("get_global_name"):
		var g := String(s.get_global_name())
		rec["class_name"] = g if g != "" else null
	var base: Script = s.get_base_script()
	if base != null:
		rec["base_script"] = base.resource_path
	if not s.can_instantiate():
		rec["errors"].append({"kind": "compile", "message": "script does not compile (or is abstract)"})
	for m in s.get_script_method_list():
		var a := []
		for arg in m.get("args", []):
			a.append(arg.get("name"))
		rec["methods"].append({"name": m.get("name"), "args": a})
	for sig in s.get_script_signal_list():
		rec["signals"].append({"name": sig.get("name")})
	for p in s.get_script_property_list():
		var usage: int = p.get("usage", 0)
		if usage & (PROPERTY_USAGE_CATEGORY | PROPERTY_USAGE_GROUP | PROPERTY_USAGE_SUBGROUP):
			continue
		var exported := (usage & PROPERTY_USAGE_EDITOR) != 0 and (usage & PROPERTY_USAGE_SCRIPT_VARIABLE) != 0
		var default_value = null
		if exported:
			default_value = _val(s.get_property_default_value(p["name"]), 0)
		rec["properties"].append({
			"name": p["name"],
			"type": type_string(p.get("type", TYPE_NIL)),
			"class": p.get("class_name", ""),
			"exported": exported,
			"default": default_value,
		})
	var consts: Dictionary = s.get_script_constant_map()
	for k in consts:
		rec["constants"][String(k)] = _val(consts[k], 0)
	return rec


func _resource(path: String) -> Dictionary:
	var r = ResourceLoader.load(path)
	if r == null:
		return {"kind": "resource", "load_failed": true, "errors": [{"kind": "load", "message": "could not load resource"}]}
	var script_path: Variant = null
	var sc = r.get_script()
	if sc is Script:
		script_path = sc.resource_path
	var props := {}
	for p in r.get_property_list():
		if p.get("usage", 0) & PROPERTY_USAGE_STORAGE and not String(p["name"]).begins_with("resource_") and p["name"] != "script":
			props[p["name"]] = _val(r.get(p["name"]), 1)
	return {"kind": "resource", "type": r.get_class(), "script": script_path, "props": props, "resources": [], "errors": []}


# ---------------------------------------------------------------- values

func _val(v: Variant, depth: int) -> Variant:
	match typeof(v):
		TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_STRING:
			return v
		TYPE_FLOAT:
			return v
		TYPE_STRING_NAME, TYPE_NODE_PATH:
			return String(v)
		TYPE_VECTOR2, TYPE_VECTOR2I:
			return {"$type": type_string(typeof(v)), "args": [v.x, v.y]}
		TYPE_VECTOR3, TYPE_VECTOR3I:
			return {"$type": type_string(typeof(v)), "args": [v.x, v.y, v.z]}
		TYPE_COLOR:
			return {"$type": "Color", "args": [v.r, v.g, v.b, v.a]}
		TYPE_OBJECT:
			if v == null:
				return null
			if v is Resource:
				var rp: String = v.resource_path
				if rp != "" and not rp.contains("::"):
					return {"$res": rp, "type": v.get_class()}
				var sub := {"$sub": v.get_class(), "props": {}}
				if depth < 1:
					for p in v.get_property_list():
						if p.get("usage", 0) & PROPERTY_USAGE_STORAGE and not String(p["name"]).begins_with("resource_"):
							sub["props"][p["name"]] = _val(v.get(p["name"]), depth + 1)
				return sub
			return {"$object": v.get_class()}
		TYPE_ARRAY:
			if v.size() > 50:
				return {"$omitted": true, "size": v.size()}
			var arr := []
			for x in v:
				arr.append(_val(x, depth + 1))
			return arr
		TYPE_DICTIONARY:
			if v.size() > 50:
				return {"$omitted": true, "size": v.size()}
			var d := {}
			for k in v:
				d[str(k)] = _val(v[k], depth + 1)
			return d
		_:
			if typeof(v) >= TYPE_PACKED_BYTE_ARRAY and typeof(v) <= TYPE_PACKED_VECTOR4_ARRAY:
				if v.size() > 50:
					return {"$omitted": true, "size": v.size()}
				var arr2 := []
				for x in v:
					arr2.append(_val(x, depth + 1))
				return arr2
			return {"$raw": var_to_str(v)}


func _sanitize(v: Variant) -> Variant:
	match typeof(v):
		TYPE_FLOAT:
			if is_nan(v) or is_inf(v):
				return str(v)
			return v
		TYPE_ARRAY:
			var a := []
			for x in v:
				a.append(_sanitize(x))
			return a
		TYPE_DICTIONARY:
			var d := {}
			for k in v:
				d[k] = _sanitize(v[k])
			return d
		TYPE_PACKED_STRING_ARRAY:
			return Array(v)
	return v
