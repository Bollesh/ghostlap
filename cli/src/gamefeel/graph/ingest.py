"""Turn scan records (engine or text) into graph nodes and edges."""

from __future__ import annotations

from typing import Any

from .store import Graph

PROJECT_FILE = "res://project.godot"


def nid_scene(res: str) -> str:
    return f"scene:{res}"


def nid_node(scene: str, path: str) -> str:
    return f"node:{scene}::{path}"


def nid_script(res: str) -> str:
    return f"script:{res}"


def nid_func(script: str, name: str) -> str:
    return f"func:{script}::{name}"


def nid_prop(script: str, name: str) -> str:
    return f"prop:{script}::{name}"


def nid_signal(script: str, name: str) -> str:
    return f"signal:{script}::{name}"


def nid_res(res: str) -> str:
    if res.endswith((".tscn", ".scn")):
        return nid_scene(res)
    if res.endswith((".gd", ".cs")):
        return nid_script(res)
    return f"resource:{res}"


def ingest_project(g: Graph, rec: dict) -> None:
    g.drop_file(PROJECT_FILE)
    src = rec.get("source", "engine")
    g.add_node("project", "Project", rec.get("name"), PROJECT_FILE, PROJECT_FILE, {
        k: rec.get(k) for k in ("godot_version", "main_scene", "physics", "display", "features", "source")
    }, source=src)
    for action, events in (rec.get("inputs") or {}).items():
        aid = f"action:{action}"
        g.add_node(aid, "InputAction", action, None, PROJECT_FILE, {"events": events, "builtin": action.startswith("ui_")}, source=src)
    for name, path in (rec.get("autoloads") or {}).items():
        aid = f"autoload:{name}"
        g.add_node(aid, "Autoload", name, path, PROJECT_FILE, {}, source=src)
        g.add_edge(aid, nid_res(path), "autoload_of", PROJECT_FILE, source=src)
    for c in rec.get("global_classes") or []:
        g.add_edge(f"class:{c.get('class')}", nid_script(c.get("path", "")), "class_name_of", PROJECT_FILE, source=src)
    main = rec.get("main_scene")
    if main:
        g.add_edge("project", nid_scene(main), "main_scene", PROJECT_FILE, source=src)


def ingest_file(g: Graph, res: str, rec: dict) -> None:
    g.drop_file(res)
    kind = rec.get("kind")
    if kind == "scene":
        _scene(g, res, rec)
    elif kind == "script":
        _script(g, res, rec)
    elif kind == "resource":
        _resource(g, res, rec)


def _shared(g: Graph, id: str, kind: str, name: str) -> None:
    g.add_node(id, kind, name, None, None, {})


def _scene(g: Graph, res: str, rec: dict) -> None:
    src = rec.get("source", "engine")
    sid = nid_scene(res)
    nodes = rec.get("nodes") or []
    root_type = nodes[0].get("type") if nodes else None
    g.add_node(sid, "Scene", res.rsplit("/", 1)[-1], res, res, {
        "root_type": root_type, "node_count": len(nodes), "inherits": rec.get("inherits"),
        "errors": rec.get("errors", []), "uid": rec.get("uid"),
    }, source=src)
    if rec.get("inherits"):
        g.add_edge(sid, nid_scene(rec["inherits"]), "inherits_scene", res, source=src)
    path_to_script: dict[str, str] = {}
    for n in nodes:
        path = n.get("path") or "."
        nid = nid_node(res, path)
        g.add_node(nid, "SceneNode", n.get("name"), path, res, {
            "type": n.get("type"), "groups": n.get("groups", []), "props": n.get("props", {}),
            "instance": n.get("instance"), "script": n.get("script"), "line": n.get("line"),
        }, source=src)
        g.add_edge(sid, nid, "contains", res, source=src)
        parent = n.get("parent")
        if parent is not None:
            g.add_edge(nid_node(res, parent), nid, "has_child", res, source=src)
        if n.get("type"):
            cid = f"class:{n['type']}"
            _shared(g, cid, "EngineClass", n["type"])
            g.add_edge(nid, cid, "is_type", res, source=src)
        if n.get("instance"):
            g.add_edge(nid, nid_scene(n["instance"]), "instances", res, source=src)
        if n.get("script"):
            g.add_edge(nid, nid_script(n["script"]), "attaches_script", res, source=src)
            path_to_script[path] = n["script"]
        for grp in n.get("groups") or []:
            gid = f"group:{grp}"
            _shared(g, gid, "Group", grp)
            g.add_edge(nid, gid, "in_group", res, source=src)
    for c in rec.get("connections") or []:
        from_id = nid_node(res, c.get("from") or ".")
        to_path = c.get("to") or "."
        script = path_to_script.get(to_path)
        dst = nid_func(script, c.get("method")) if script else nid_node(res, to_path)
        g.add_edge(from_id, dst, "connects", res, {"signal": c.get("signal"), "method": c.get("method")}, source=src)
    for dep in rec.get("resources") or []:
        if dep and dep != res:
            g.add_edge(sid, nid_res(dep), "uses_resource", res, source=src)


def _script(g: Graph, res: str, rec: dict) -> None:
    src = rec.get("source", "engine")
    sid = nid_script(res)
    facts = rec.get("facts") or {}
    g.add_node(sid, "Script", res.rsplit("/", 1)[-1], res, res, {
        "language": rec.get("language"), "class_name": rec.get("class_name"),
        "extends": rec.get("extends"), "native_base": rec.get("native_base"),
        "base_script": rec.get("base_script"), "tool": rec.get("tool", False),
        "compiled": rec.get("compiled"), "errors": rec.get("errors", []),
        "inner_classes": rec.get("inner_classes", []),
    }, source=src)
    if rec.get("class_name"):
        cid = f"class:{rec['class_name']}"
        _shared(g, cid, "ScriptClass", rec["class_name"])
        g.add_edge(cid, sid, "class_name_of", res, source=src)
    base_script = rec.get("base_script") or (rec.get("extends") if str(rec.get("extends") or "").startswith("res://") else None)
    if base_script:
        g.add_edge(sid, nid_script(base_script), "extends", res, source=src)
    elif rec.get("native_base") or rec.get("extends"):
        base = rec.get("native_base") or rec.get("extends")
        cid = f"class:{base}"
        _shared(g, cid, "EngineClass", base)
        g.add_edge(sid, cid, "extends", res, source=src)

    prop_names = set()
    for p in rec.get("properties") or []:
        pid = nid_prop(res, p["name"])
        prop_names.add(p["name"])
        g.add_node(pid, "Property", p["name"], res, res, {k: p.get(k) for k in (
            "type", "exported", "default", "line", "annotations", "default_text", "own", "class")}, source=src)
        g.add_edge(sid, pid, "defines", res, source=src)
    for name, c in (rec.get("constants") or {}).items():
        val = c.get("value") if isinstance(c, dict) and "value" in c else c
        line = c.get("line") if isinstance(c, dict) else None
        pid = nid_prop(res, name)
        prop_names.add(name)
        g.add_node(pid, "Constant", name, res, res, {"default": val, "line": line, "exported": False}, source=src)
        g.add_edge(sid, pid, "defines", res, source=src)
    signal_names = set()
    for s in rec.get("signals") or []:
        sgid = nid_signal(res, s["name"])
        signal_names.add(s["name"])
        g.add_node(sgid, "Signal", s["name"], res, res, {"line": s.get("line")}, source=src)
        g.add_edge(sid, sgid, "defines", res, source=src)

    by_func: dict[str, dict[str, list]] = {}
    for key in ("reads_input", "emits", "assigns", "calls", "uses"):
        for f in facts.get(key) or []:
            by_func.setdefault(f["func"], {}).setdefault(key, []).append(f)
    for m in rec.get("methods") or []:
        fid = nid_func(res, m["name"])
        ff = by_func.get(m["name"], {})
        g.add_node(fid, "Function", m["name"], res, res, {
            "line": m.get("line"), "end_line": m.get("end_line"), "args": m.get("args", []),
            "calls": sorted({c["call"] for c in ff.get("calls", [])}),
            "assigns": [{k: a[k] for k in ("target", "op", "value", "line")} for a in ff.get("assigns", [])],
        }, source=src)
        g.add_edge(sid, fid, "defines", res, source=src)
        reads: dict[str, dict] = {}
        for r in ff.get("reads_input", []):
            for i, action in enumerate(r["actions"]):
                agg = reads.setdefault(action, {"calls": [], "lines": [], "uses": []})
                if r["call"] not in agg["calls"]:
                    agg["calls"].append(r["call"])
                agg["lines"].append(r["line"])
                agg["uses"].append({"call": r["call"], "line": r["line"], "arg_index": i,
                                    "args": r["actions"], "receiver": r.get("receiver")})
        for action, agg in reads.items():
            aid = f"action:{action}"
            _shared(g, aid, "InputAction", action)
            g.add_edge(fid, aid, "reads_input", res, agg, source="text")
        for e in ff.get("emits", []):
            dst = nid_signal(res, e["signal"]) if e["signal"] in signal_names else f"signal:?::{e['signal']}"
            g.add_edge(fid, dst, "emits", res, {"line": e["line"]}, source="text")
        sets: dict[str, list] = {}
        for a in ff.get("assigns", []):
            root = a["target"].split(".")[0].split("[")[0]
            if root in prop_names:
                sets.setdefault(root, []).append({"line": a["line"], "target": a["target"]})
        for root, where in sets.items():
            g.add_edge(fid, nid_prop(res, root), "sets_property", res, {"at": where}, source="text")
        for u in ff.get("uses", []):
            if u["name"] in prop_names:
                g.add_edge(fid, nid_prop(res, u["name"]), "uses_property", res, {}, source="text")


def _resource(g: Graph, res: str, rec: dict) -> None:
    src = rec.get("source", "engine")
    rid = f"resource:{res}"
    g.add_node(rid, "Resource", res.rsplit("/", 1)[-1], res, res, {
        "type": rec.get("type"), "props": rec.get("props", {}), "errors": rec.get("errors", []),
    }, source=src)
    if rec.get("script"):
        g.add_edge(rid, nid_script(rec["script"]), "attaches_script", res, source=src)
    for dep in rec.get("resources") or []:
        if dep and dep != res:
            g.add_edge(rid, nid_res(dep), "uses_resource", res, source=src)


def merge_script_records(engine: dict, text: dict) -> dict:
    """Engine structure + text facts/lines. Text-only fields fill gaps."""
    if engine.get("load_failed"):
        out = dict(text)
        out["errors"] = list(text.get("errors", [])) + list(engine.get("errors", []))
        out["source"] = "text-fallback"
        return out
    out = dict(engine)
    text_props = {p["name"]: p for p in text.get("properties", [])}
    props = []
    for p in engine.get("properties", []):
        tp = text_props.get(p["name"])
        if tp is None and text_props:
            continue  # inherited from a base script; that script owns it
        q = dict(p)
        q["own"] = tp is not None
        if tp:
            q["line"] = tp.get("line")
            q["annotations"] = tp.get("annotations")
            q["default_text"] = tp.get("default_text")
            # Godot < 4.4 doesn't report defaults of exported vars for non-tool scripts:
            # fall back to the literal in the source
            if q.get("default") is None:
                q["default"] = tp.get("default")
        props.append(q)
    out["properties"] = props
    text_methods = {m["name"]: m for m in text.get("methods", [])}
    methods = []
    for m in engine.get("methods", []):
        tm = text_methods.get(m["name"])
        if tm is None:
            continue  # inherited from a base script; that script owns it
        methods.append({**m, "line": tm.get("line"), "end_line": tm.get("end_line")})
    for name, tm in text_methods.items():  # inner-class methods etc.
        if name not in {m["name"] for m in methods}:
            methods.append(tm)
    out["methods"] = methods
    tsig = {s["name"]: s for s in text.get("signals", [])}
    out["signals"] = [{**s, "line": tsig.get(s["name"], {}).get("line")} for s in engine.get("signals", []) if s["name"] in tsig] \
        or text.get("signals", [])
    consts = {}
    for k, v in (engine.get("constants") or {}).items():
        tc = (text.get("constants") or {}).get(k)
        if tc is None:
            continue
        consts[k] = {"value": v, "line": tc.get("line")}
    out["constants"] = consts
    out["facts"] = text.get("facts", {})
    out["extends"] = text.get("extends")
    out["tool"] = text.get("tool", False)
    out["inner_classes"] = text.get("inner_classes", [])
    out["errors"] = list(engine.get("errors", [])) + list(text.get("errors", []))
    return out
