"""Facts about the player candidate that detection rules and probes use."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..graph.queries import node_type, script_chain
from ..graph.store import Graph

BODY_TYPES = {"CharacterBody2D", "CharacterBody3D", "RigidBody2D", "RigidBody3D"}
ROLE_SYNONYMS: dict[str, list[str]] = {
    "left": ["move_left", "left", "walk_left", "run_left", "player_left", "ui_left"],
    "right": ["move_right", "right", "walk_right", "run_right", "player_right", "ui_right"],
    "up": ["move_up", "up", "move_forward", "forward", "walk_up", "player_up", "ui_up"],
    "down": ["move_down", "down", "move_back", "move_backward", "back", "backward", "walk_down", "player_down", "ui_down"],
    "jump": ["jump", "player_jump", "ui_accept"],
    "dash": ["dash", "roll", "dodge", "evade"],
    "attack": ["attack", "hit", "punch", "slash", "melee", "shoot", "fire", "primary_attack"],
}
MOVE_ROLES = ("left", "right", "up", "down")
HITBOX_RE = re.compile(r"hit_?box|attack|weapon|sword|slash|melee|blade", re.I)
HURTBOX_RE = re.compile(r"hurt_?box", re.I)


@dataclass
class PlayerInfo:
    node_id: str
    scene: str
    node_path: str
    type: str
    scripts: list[str] = field(default_factory=list)       # script ids (chain + scripts on descendants)
    functions: list[dict] = field(default_factory=list)
    reads: dict[str, dict] = field(default_factory=dict)  # action -> {"calls": [...], "uses": [...]}
    own_reads: set[str] = field(default_factory=set)       # actions read by the node's own script chain
    subtree: list[dict] = field(default_factory=list)     # scene nodes under the player (same scene)
    properties: list[dict] = field(default_factory=list)

    @property
    def dims(self) -> str:
        return "3d" if self.type.endswith("3D") else "2d"

    def calls(self) -> set[str]:
        out: set[str] = set()
        for f in self.functions:
            for c in f["props"].get("calls", []):
                out.add(c.split(".")[-1])
                out.add(c)
        return out

    def assigns(self) -> list[dict]:
        out = []
        for f in self.functions:
            for a in f["props"].get("assigns", []):
                out.append({**a, "func": f["id"], "file": f["file"]})
        return out

    def nodes_of(self, *types: str) -> list[dict]:
        return [n for n in self.subtree if (n["props"].get("type") or "") in types]


def build_player_info(g: Graph, node: dict) -> PlayerInfo:
    scene = node["file"]
    path = node["path"]
    info = PlayerInfo(node_id=node["id"], scene=scene, node_path=path, type=node_type(g, node) or "")
    prefix = "" if path == "." else path + "/"
    subtree = []
    for n in g.nodes("SceneNode", "file = ?", (scene,)):
        if n["path"] == path or path == "." or n["path"].startswith(prefix):
            subtree.append(n)
    # if the player is an instance of another scene, include that scene's nodes too
    inst = node["props"].get("instance")
    if inst:
        subtree.extend(g.nodes("SceneNode", "file = ?", (inst,)))
    info.subtree = subtree
    scripts: list[str] = []
    own: list[str] = []
    for e in g.out(node["id"], "attaches_script"):
        own.extend(script_chain(g, e["dst"]))
    if not own and inst:
        root = g.node(f"node:{inst}::.")
        for e in (g.out(root["id"], "attaches_script") if root else []):
            own.extend(script_chain(g, e["dst"]))
    for n in subtree:
        for e in g.out(n["id"], "attaches_script"):
            for s in script_chain(g, e["dst"]):
                if s not in scripts:
                    scripts.append(s)
    info.scripts = scripts
    for s in scripts:
        for e in g.out(s, "defines"):
            d = g.node(e["dst"])
            if d is None:
                continue
            if d["kind"] == "Function":
                info.functions.append(d)
            elif d["kind"] in ("Property", "Constant"):
                info.properties.append(d)
    for f in info.functions:
        for e in g.out(f["id"], "reads_input"):
            action = e["dst"].removeprefix("action:")
            if f["file"] in {s.removeprefix("script:") for s in own}:
                info.own_reads.add(action)
            agg = info.reads.setdefault(action, {"calls": [], "uses": []})
            for c in e["props"].get("calls", []):
                if c not in agg["calls"]:
                    agg["calls"].append(c)
            agg["uses"].extend(e["props"].get("uses", []))
    # a prefix read (`"jump" + action_suffix`) that isn't an action itself stands for jump_p1, jump_p2, ...
    defined = {a["name"] for a in g.nodes("InputAction") if a.get("file")}
    for a in [a for a in info.reads if defined and a not in defined]:
        full = sorted(x for x in defined if x.startswith(a))
        if full:
            agg = info.reads.pop(a)
            for x in full:
                info.reads.setdefault(x, agg)
                if a in info.own_reads:
                    info.own_reads.add(x)
            info.own_reads.discard(a)
    return info


def map_roles(info: PlayerInfo | None, all_actions: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Map semantic roles (left/right/up/down/jump/dash/attack) to InputMap action names.

    Returns (roles, evidence). Uses get_axis/get_vector argument order first, then names.
    """
    roles: dict[str, str] = {}
    why: dict[str, str] = {}
    read = list(info.reads) if info else []
    if info:
        for action, agg in info.reads.items():
            for u in agg["uses"]:
                args = u.get("args") or []
                if u["call"] == "get_vector" and len(args) == 4:
                    for role, a in zip(("left", "right", "up", "down"), args):
                        roles.setdefault(role, a)
                        why.setdefault(role, f"get_vector() argument order (line {u['line']})")
                elif u["call"] == "get_axis" and len(args) == 2:
                    for a in args:
                        role = _role_of_name(a)
                        if role:
                            roles.setdefault(role, a)
                            why.setdefault(role, f"get_axis() argument (line {u['line']})")
    pools = [read, [a for a in all_actions if not a.startswith("ui_")], all_actions]
    for role, syns in ROLE_SYNONYMS.items():
        if role in roles:
            continue
        for pool in pools:
            hit = _match(pool, syns)
            if hit:
                roles[role] = hit
                why[role] = "read by player script, name match" if pool is read else "InputMap name match"
                break
    # ui_* fallbacks only count if the player actually reads them
    for role in list(roles):
        if roles[role].startswith("ui_") and roles[role] not in read:
            roles.pop(role)
            why.pop(role, None)
    return roles, why


def _role_of_name(name: str) -> str | None:
    n = name.lower()
    for key, role in (("left", "left"), ("right", "right"), ("forward", "up"), ("up", "up"),
                      ("back", "down"), ("down", "down")):
        if key in n:
            return role
    return None


def _match(pool: list[str], syns: list[str]) -> str | None:
    lower = {a.lower(): a for a in pool}
    for s in syns:
        if s in lower:
            return lower[s]
    for s in syns:
        for la, a in lower.items():
            if la.startswith(s + "_") or la.endswith("_" + s):
                return a
    return None
