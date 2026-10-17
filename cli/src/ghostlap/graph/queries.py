"""Canned graph queries for the agent (`ghostlap graph query <name> ...`)."""

from __future__ import annotations

from collections import deque
from typing import Callable

from .store import Graph


def scene_root_type(g: Graph, scene_id: str, depth: int = 0) -> str | None:
    """Root node type of a scene, following instanced/inherited roots."""
    root = g.node(f"node:{scene_id.removeprefix('scene:')}::.")
    if root is None or depth > 16:
        return None
    t = root["props"].get("type")
    if t:
        return t
    inst = root["props"].get("instance")
    return scene_root_type(g, f"scene:{inst}", depth + 1) if inst else None


def node_type(g: Graph, node: dict) -> str | None:
    t = node["props"].get("type")
    if t:
        return t
    inst = node["props"].get("instance")
    return scene_root_type(g, f"scene:{inst}") if inst else None


def script_chain(g: Graph, script_id: str) -> list[str]:
    """script and its base scripts, most-derived first."""
    chain, seen = [], set()
    cur: str | None = script_id
    while cur and cur not in seen and cur.startswith("script:"):
        seen.add(cur)
        chain.append(cur)
        nxt = [e["dst"] for e in g.out(cur, "extends") if e["dst"].startswith("script:")]
        cur = nxt[0] if nxt else None
    return chain


def who_reads(g: Graph, action: str) -> list[dict]:
    out = []
    for e in g.inc(f"action:{action}", "reads_input"):
        fn = g.node(e["src"])
        out.append({"function": e["src"], "file": fn["file"] if fn else None,
                    "calls": e["props"].get("calls"), "lines": e["props"].get("lines")})
    return out


def scenes_with(g: Graph, what: str) -> list[dict]:
    """Scenes containing a node of a type, in a group, or named like `what`."""
    out = {}
    for e in g.inc(f"class:{what}", "is_type") + g.inc(f"group:{what}", "in_group"):
        node = g.node(e["src"])
        if node:
            out.setdefault(node["file"], []).append(node["path"])
    for node in g.nodes("SceneNode", "name = ?", (what,)):
        out.setdefault(node["file"], []).append(node["path"])
    return [{"scene": k, "nodes": sorted(set(v))} for k, v in sorted(out.items())]


def script_of(g: Graph, target: str) -> list[dict]:
    """Script(s) for a scene (`res://x.tscn`) or scene node (`res://x.tscn::Path`)."""
    if "::" not in target:
        target = target + "::."
    node_id = target if target.startswith("node:") else f"node:{target}"
    out = []
    for e in g.out(node_id, "attaches_script"):
        out.append({"script": e["dst"], "chain": script_chain(g, e["dst"])})
    node = g.node(node_id)
    if not out and node and node["props"].get("instance"):
        return script_of(g, node["props"]["instance"])
    return out


def instanced_by(g: Graph, scene: str) -> list[dict]:
    sid = scene if scene.startswith("scene:") else f"scene:{scene}"
    return [{"scene": e["file"], "node": e["src"].split("::", 1)[-1]} for e in g.inc(sid, "instances")]


def exports_of(g: Graph, script: str) -> list[dict]:
    sid = script if script.startswith("script:") else f"script:{script}"
    out = []
    for s in script_chain(g, sid):
        for e in g.out(s, "defines"):
            n = g.node(e["dst"])
            if n and n["kind"] in ("Property", "Constant"):
                p = n["props"]
                out.append({"id": n["id"], "name": n["name"], "kind": n["kind"], "exported": p.get("exported"),
                            "default": p.get("default"), "type": p.get("type"), "line": p.get("line"), "file": n["file"]})
    return out


def neighbors(g: Graph, node_id: str) -> dict:
    n = g.node(node_id)
    return {"node": n, "out": [{"kind": e["kind"], "dst": e["dst"], "props": e["props"]} for e in g.out(node_id)],
            "in": [{"kind": e["kind"], "src": e["src"]} for e in g.inc(node_id)]}


def path_between(g: Graph, a: str, b: str, max_depth: int = 6) -> list[dict] | None:
    """Shortest undirected path between two node ids (BFS)."""
    if a == b:
        return []
    prev: dict[str, tuple[str, dict, str]] = {}
    q = deque([(a, 0)])
    seen = {a}
    while q:
        cur, d = q.popleft()
        if d >= max_depth:
            continue
        for e in g.out(cur):
            nxt = e["dst"]
            if nxt not in seen:
                seen.add(nxt)
                prev[nxt] = (cur, e, "->")
                if nxt == b:
                    return _unwind(prev, a, b)
                q.append((nxt, d + 1))
        for e in g.inc(cur):
            nxt = e["src"]
            if nxt not in seen:
                seen.add(nxt)
                prev[nxt] = (cur, e, "<-")
                if nxt == b:
                    return _unwind(prev, a, b)
                q.append((nxt, d + 1))
    return None


def _unwind(prev: dict, a: str, b: str) -> list[dict]:
    steps = []
    cur = b
    while cur != a:
        p, e, direction = prev[cur]
        steps.append({"from": p, "edge": e["kind"], "dir": direction, "to": cur})
        cur = p
    return list(reversed(steps))


QUERIES: dict[str, tuple[Callable, str]] = {
    "who-reads": (who_reads, "<action>  functions that read an input action"),
    "scenes-with": (scenes_with, "<Type|group|name>  scenes containing matching nodes"),
    "script-of": (script_of, "<res://scene.tscn[::NodePath]>  script + base chain"),
    "instanced-by": (instanced_by, "<res://scene.tscn>  scenes that instance it"),
    "exports-of": (exports_of, "<res://script.gd>  properties/constants incl. base scripts"),
    "node": (neighbors, "<node id>  a node with its in/out edges"),
    "path": (path_between, "<id a> <id b>  shortest connection between two nodes"),
    "search": (lambda g, text: g.search(text), "<text>  full-text search over node names/paths"),
}
