"""Text-mode extractor: builds file records without a Godot binary.

Used when Godot is missing, the project fails to load, or for individual files the engine
couldn't load (e.g. scripts with parse errors). Records use the same schema as the engine
extractor (addons/gamefeel/scan/scan.gd) so ingest is shared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .gdscript import parse_gdscript
from .godot_text import Section, parse_godot_text


def to_res(project: Path, path: Path) -> str:
    return "res://" + path.relative_to(project).as_posix()


def from_res(project: Path, res: str) -> Path:
    return project / res.removeprefix("res://")


def _resolve(value: Any, ext: dict, sub: dict) -> Any:
    """Replace ExtResource/SubResource refs with something readable."""
    if isinstance(value, dict):
        if "$ext" in value:
            e = ext.get(value["$ext"], {})
            return {"$res": e.get("path"), "type": e.get("type")}
        if "$sub" in value:
            s = sub.get(value["$sub"], {})
            return {"$sub": s.get("type"), "props": s.get("props", {})}
        return {k: _resolve(v, ext, sub) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, ext, sub) for v in value]
    return value


def _ext_table(sections: list[Section]) -> dict:
    ext = {}
    for s in sections:
        if s.tag == "ext_resource":
            rid = str(s.attrs.get("id", ""))
            ext[rid] = {"path": s.attrs.get("path"), "type": s.attrs.get("type"), "uid": s.attrs.get("uid")}
    return ext


def _sub_table(sections: list[Section], ext: dict) -> dict:
    sub = {}
    for s in sections:
        if s.tag == "sub_resource":
            sid = str(s.attrs.get("id", ""))
            sub[sid] = {"type": s.attrs.get("type"), "props": {}}
    for s in sections:
        if s.tag == "sub_resource":
            sid = str(s.attrs.get("id", ""))
            sub[sid]["props"] = _resolve(s.props, ext, {k: {"type": v["type"]} for k, v in sub.items()})
    return sub


def parse_scene_text(text: str) -> dict[str, Any]:
    sections = parse_godot_text(text)
    ext = _ext_table(sections)
    sub = _sub_table(sections, ext)
    header = next((s for s in sections if s.tag == "gd_scene"), None)
    rec: dict[str, Any] = {
        "kind": "scene",
        "uid": header.attrs.get("uid") if header else None,
        "inherits": None,
        "nodes": [],
        "connections": [],
        "resources": sorted({e["path"] for e in ext.values() if e.get("path")}),
        "errors": [],
    }
    for s in sections:
        if s.tag == "node":
            name = s.attrs.get("name")
            parent = s.attrs.get("parent")
            if parent is None:
                path = "."
            elif parent == ".":
                path = str(name)
            else:
                path = f"{parent}/{name}"
            inst = s.attrs.get("instance")
            instance = ext.get(inst["$ext"], {}).get("path") if isinstance(inst, dict) and "$ext" in inst else None
            props = _resolve(dict(s.props), ext, sub)
            script = props.pop("script", None)
            script_path = script.get("$res") if isinstance(script, dict) else None
            groups = s.attrs.get("groups") or []
            node = {
                "path": path,
                "name": name,
                "type": s.attrs.get("type"),
                "parent": parent,
                "instance": instance,
                "script": script_path,
                "groups": [str(g) for g in groups] if isinstance(groups, list) else [],
                "props": _compact_props(props),
                "line": s.line,
            }
            if parent is None and instance:
                rec["inherits"] = instance
            rec["nodes"].append(node)
        elif s.tag == "connection":
            rec["connections"].append({
                "signal": s.attrs.get("signal"),
                "from": s.attrs.get("from"),
                "to": s.attrs.get("to"),
                "method": s.attrs.get("method"),
            })
    return rec


def _compact_props(props: dict) -> dict:
    """Keep props small: drop big blobs (tile data, packed arrays, embedded images)."""
    out = {}
    for k, v in props.items():
        s = repr(v)
        if len(s) > 400:
            out[k] = {"$omitted": True, "size": len(s)}
        else:
            out[k] = v
    return out


def parse_resource_text(text: str) -> dict[str, Any]:
    sections = parse_godot_text(text)
    ext = _ext_table(sections)
    sub = _sub_table(sections, ext)
    header = next((s for s in sections if s.tag == "gd_resource"), None)
    res = next((s for s in sections if s.tag == "resource"), None)
    props = _resolve(dict(res.props), ext, sub) if res else {}
    script = props.pop("script", None)
    return {
        "kind": "resource",
        "uid": header.attrs.get("uid") if header else None,
        "type": (header.attrs.get("script_class") or header.attrs.get("type")) if header else None,
        "script": script.get("$res") if isinstance(script, dict) else None,
        "props": _compact_props(props),
        "resources": sorted({e["path"] for e in ext.values() if e.get("path")}),
        "errors": [],
    }


def _describe_event(ev: Any) -> dict | None:
    if not isinstance(ev, dict) or "$object" not in ev:
        return None
    cls = ev["$object"]
    if cls == "InputEventKey":
        code = ev.get("physical_keycode") or ev.get("keycode") or ev.get("unicode") or 0
        return {"type": "key", "code": code}
    if cls == "InputEventJoypadButton":
        return {"type": "joypad_button", "button": ev.get("button_index")}
    if cls == "InputEventJoypadMotion":
        return {"type": "joypad_axis", "axis": ev.get("axis"), "value": ev.get("axis_value")}
    if cls == "InputEventMouseButton":
        return {"type": "mouse_button", "button": ev.get("button_index")}
    return {"type": cls}


def parse_project_text(text: str) -> dict[str, Any]:
    sections = parse_godot_text(text)
    by_tag: dict[str, dict] = {}
    for s in sections:
        by_tag.setdefault(s.tag, {}).update(s.props)

    def get(tag: str, key: str, default=None):
        return by_tag.get(tag, {}).get(key, default)

    inputs = {}
    for action, val in by_tag.get("input", {}).items():
        events = val.get("events", []) if isinstance(val, dict) else []
        inputs[action] = [d for d in (_describe_event(e) for e in events) if d]
    autoloads = {}
    for name, val in by_tag.get("autoload", {}).items():
        if isinstance(val, str):
            autoloads[name] = val.lstrip("*")
    features = get("application", "config/features") or []
    if isinstance(features, dict) and "$type" in features:
        features = features.get("args", [])
    return {
        "source": "text",
        "godot_version": None,
        "features": features if isinstance(features, list) else [],
        "name": get("application", "config/name"),
        "main_scene": get("application", "run/main_scene"),
        "physics": {
            "ticks_per_second": get("physics", "common/physics_ticks_per_second", 60),
            "gravity_2d": get("physics", "2d/default_gravity", 980.0),
            "gravity_3d": get("physics", "3d/default_gravity", 9.8),
        },
        "display": {
            "width": get("display", "window/size/viewport_width", 1152),
            "height": get("display", "window/size/viewport_height", 648),
            "stretch_mode": get("display", "window/stretch/mode", "disabled"),
        },
        "autoloads": autoloads,
        "inputs": inputs,
        "global_classes": [],
    }


def extract_file_text(project: Path, res_path: str) -> dict[str, Any]:
    path = from_res(project, res_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"kind": "unknown", "errors": [{"kind": "io", "message": str(e)}]}
    suffix = path.suffix.lower()
    if suffix == ".gd":
        rec = parse_gdscript(text)
    elif suffix == ".tscn":
        rec = parse_scene_text(text)
    elif suffix == ".tres":
        rec = parse_resource_text(text)
    elif suffix == ".cs":
        rec = {"kind": "script", "language": "csharp", "class_name": None, "extends": None, "methods": [],
               "signals": [], "properties": [], "constants": {}, "facts": {}, "errors": [
                   {"kind": "unsupported", "message": "C# is not parsed in text mode; use the engine scan on a built project"}]}
    else:
        rec = {"kind": "unknown", "errors": []}
    rec["source"] = "text"
    return rec
