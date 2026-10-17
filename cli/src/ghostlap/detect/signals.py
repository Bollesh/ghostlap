"""Detection signals. Pack `detect.rules` reference these by name:

    - {signal: role_read, args: jump, weight: 3}

Each signal returns (matched, evidence). Evidence is a short human-readable string with a
file:line where possible, so the agent can show *why* it thinks the game is a platformer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..graph.store import Graph
from .context import PlayerInfo


@dataclass
class DetectCtx:
    g: Graph
    player: PlayerInfo | None
    roles: dict[str, str]
    scene_nodes: list[dict] = field(default_factory=list)

    def all_nodes_of(self, *types: str) -> list[dict]:
        return [n for n in self.scene_nodes if (n["props"].get("type") or "") in types]


Signal = Callable[[DetectCtx, Any], tuple[bool, str]]
SIGNALS: dict[str, Signal] = {}


def signal(name: str):
    def deco(fn: Signal) -> Signal:
        SIGNALS[name] = fn
        return fn
    return deco


def _loc(item: dict) -> str:
    f = item.get("file") or ""
    line = item.get("line")
    return f"{f}:{line}" if line else f


@signal("player_type")
def player_type(ctx: DetectCtx, types) -> tuple[bool, str]:
    if not ctx.player:
        return False, "no player candidate"
    types = [types] if isinstance(types, str) else types
    return ctx.player.type in types, f"player is {ctx.player.type}"


@signal("role_read")
def role_read(ctx: DetectCtx, role: str) -> tuple[bool, str]:
    action = ctx.roles.get(role)
    if not ctx.player or not action:
        return False, f"no '{role}' action"
    ok = action in ctx.player.reads
    return ok, f"player reads '{action}'" if ok else f"'{action}' exists but player doesn't read it"


@signal("role_missing")
def role_missing(ctx: DetectCtx, role: str) -> tuple[bool, str]:
    ok, ev = role_read(ctx, role)
    return (not ok), (f"no '{role}' input read by player" if not ok else ev)


@signal("calls_any")
def calls_any(ctx: DetectCtx, names) -> tuple[bool, str]:
    if not ctx.player:
        return False, ""
    names = [names] if isinstance(names, str) else names
    calls = ctx.player.calls()
    hit = [n for n in names if n in calls]
    return bool(hit), f"player calls {', '.join(hit)}" if hit else ""


@signal("assigns_match")
def assigns_match(ctx: DetectCtx, args: dict) -> tuple[bool, str]:
    if not ctx.player:
        return False, ""
    t_re = re.compile(args.get("target", ".*"))
    v_re = re.compile(args.get("value", ".*"), re.I)
    ops = args.get("ops")
    for a in ctx.player.assigns():
        if t_re.search(a["target"]) and v_re.search(a.get("value") or "") and (not ops or a["op"] in ops):
            return True, f"{a['target']} {a['op']} {a.get('value')}  ({_loc(a)})"
    return False, ""


@signal("gravity_on_velocity")
def gravity_on_velocity(ctx: DetectCtx, _=None) -> tuple[bool, str]:
    if not ctx.player:
        return False, ""
    for a in ctx.player.assigns():
        tgt = a["target"].replace(" ", "")
        val = (a.get("value") or "").lower()
        # gravity is an acceleration: it mentions gravity, or is integrated with delta.
        # (`velocity.y += 1` building a direction vector from input is not gravity.)
        if tgt in ("velocity.y", "linear_velocity.y") and ("gravity" in val or (a["op"] in ("+=", "-=") and "delta" in val)):
            return True, f"{a['target']} {a['op']} {a.get('value')}  ({_loc(a)})"
    if "get_gravity" in ctx.player.calls():
        return True, "player calls get_gravity()"
    if ctx.player.type.startswith("RigidBody"):
        return True, "RigidBody (engine gravity)"
    return False, "no gravity applied to velocity.y"


@signal("node_in_player")
def node_in_player(ctx: DetectCtx, args) -> tuple[bool, str]:
    """args: {types: [...], name: regex}"""
    if not ctx.player:
        return False, ""
    types = args.get("types") or []
    name_re = re.compile(args.get("name", ".*"), re.I)
    for n in ctx.player.subtree:
        t = n["props"].get("type") or ""
        if (not types or t in types) and (name_re.search(n["name"] or "") or any(name_re.search(gr) for gr in n["props"].get("groups", []))):
            return True, f"{t} '{n['path']}' in {n['file']}"
    return False, ""


@signal("player_property")
def player_property(ctx: DetectCtx, pattern: str) -> tuple[bool, str]:
    if not ctx.player:
        return False, ""
    r = re.compile(pattern, re.I)
    for p in ctx.player.properties:
        if r.search(p["name"]):
            return True, f"property {p['name']}  ({p['file']}:{p['props'].get('line')})"
    return False, ""


@signal("project_node_type")
def project_node_type(ctx: DetectCtx, types) -> tuple[bool, str]:
    types = [types] if isinstance(types, str) else types
    hits = ctx.all_nodes_of(*types)
    return bool(hits), f"{len(hits)} {'/'.join(types)} node(s), e.g. {hits[0]['file']}::{hits[0]['path']}" if hits else ""


@signal("group_exists")
def group_exists(ctx: DetectCtx, pattern: str) -> tuple[bool, str]:
    r = re.compile(pattern, re.I)
    for n in ctx.g.nodes("Group"):
        if r.search(n["name"]):
            return True, f"group '{n['name']}'"
    return False, ""


@signal("orthographic_camera")
def orthographic_camera(ctx: DetectCtx, _=None) -> tuple[bool, str]:
    for n in ctx.all_nodes_of("Camera3D"):
        if n["props"].get("props", {}).get("projection") in (1, "1", "PROJECTION_ORTHOGONAL"):
            return True, f"orthographic Camera3D {n['file']}::{n['path']}"
    return False, ""


@signal("y_sort")
def y_sort(ctx: DetectCtx, _=None) -> tuple[bool, str]:
    for n in ctx.scene_nodes:
        if n["props"].get("props", {}).get("y_sort_enabled") is True:
            return True, f"y_sort_enabled on {n['file']}::{n['path']}"
    return False, ""


@signal("planar_movement_3d")
def planar_movement_3d(ctx: DetectCtx, _=None) -> tuple[bool, str]:
    if not ctx.player or ctx.player.dims != "3d":
        return False, ""
    for r in ("up", "down"):
        if ctx.roles.get(r) in (ctx.player.reads if ctx.player else {}):
            return True, f"3D body reads '{ctx.roles[r]}' (forward/back movement)"
    return False, ""


def evaluate(ctx: DetectCtx, rule: dict) -> tuple[bool, str]:
    fn = SIGNALS.get(rule["signal"])
    if fn is None:
        return False, f"unknown signal {rule['signal']}"
    try:
        return fn(ctx, rule.get("args"))
    except Exception as e:  # a bad community rule must not break detection
        return False, f"signal error: {e}"
