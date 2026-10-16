"""`gamefeel detect`: player, actions, genres, mechanics, levels, hazards — with evidence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..graph.queries import node_type
from ..graph.store import Graph
from ..packs.loader import Pack, load_packs
from .context import BODY_TYPES, HITBOX_RE, MOVE_ROLES, PlayerInfo, build_player_info, map_roles
from .signals import DetectCtx, evaluate

PLAYER_NAME_RE = re.compile(r"player|hero|character|protagonist|avatar", re.I)
HAZARD_RE = re.compile(r"hazard|spike|enemy|enemies|trap|damage|kill|lava|saw", re.I)
UI_RE = re.compile(r"(^|/)(ui|hud|menu|gui)(/|_|\.|$)", re.I)

MECHANICS: list[tuple[str, dict]] = [
    ("jump", {"role": "jump"}),
    ("double_jump", {"prop": r"double_jump|jumps_left|max_jumps|air_jumps|jump_count"}),
    ("coyote_time", {"prop": r"coyote"}),
    ("jump_buffer", {"prop": r"buffer"}),
    ("variable_jump", {"prop": r"jump_cut|variable_jump|jump_release", "call_with_role": ("is_action_just_released", "jump")}),
    ("wall_jump", {"prop": r"wall_jump|wall_slide|wall_kick", "call": "is_on_wall"}),
    ("dash", {"role": "dash", "prop": r"dash"}),
    ("attack", {"role": "attack"}),
    ("combo", {"prop": r"combo"}),
    ("knockback", {"prop": r"knockback|knock_back", "func": r"take_damage|hurt|on_hit|damage"}),
    ("hitstop", {"assign": r"Engine\.time_scale", "prop": r"hit_?stop|freeze_frame|hit_?freeze"}),
    ("camera_shake", {"prop": r"shake|trauma"}),
    ("health", {"prop": r"^(health|hp|hit_points|life|lives)$"}),
    ("invulnerability", {"prop": r"invuln|iframe|i_frame|invincib"}),
]


def _scene_nodes(g: Graph) -> list[dict]:
    return g.nodes("SceneNode")


def player_candidates(g: Graph, nodes: list[dict], all_actions: list[str]) -> list[dict]:
    instanced = {e["dst"].removeprefix("scene:"): e["file"] for e in g.edges("instances")}
    scripted = {e["src"] for e in g.edges("attaches_script") if e["src"].startswith("node:")}
    cands = []
    for n in nodes:
        if n["props"].get("instance") and n["id"] not in scripted:
            continue  # a plain instance points at its definition, which is scored itself
        t = node_type(g, n) or ""
        is_body = t in BODY_TYPES
        if not is_body and not (scripted and n["id"] in scripted):
            continue
        info = build_player_info(g, n)
        roles, _ = map_roles(info, all_actions)
        score = 0.0
        ev: list[str] = []
        own_move = [roles[r] for r in MOVE_ROLES if roles.get(r) in info.own_reads]
        move_read = [roles[r] for r in MOVE_ROLES if roles.get(r) in info.reads]
        if not is_body and not own_move:
            continue  # non-physics nodes only count if their own script reads movement (Area2D/Node2D players)
        if is_body:
            score += 1
            ev.append(f"physics body ({t})")
        else:
            ev.append(f"{t} moved by script (not a physics body)")
        if own_move:
            score += 3
            ev.append(f"reads movement input {', '.join(sorted(set(own_move)))}")
        elif move_read:
            score += 1  # e.g. a body whose child state machine reads input
            ev.append(f"a child script reads movement input {', '.join(sorted(set(move_read)))}")
        other = [roles[r] for r in ("jump", "dash", "attack") if roles.get(r) in info.reads]
        if other:
            score += 1
            ev.append(f"reads {', '.join(other)}")
        groups = [gr for gr in n["props"].get("groups", []) if PLAYER_NAME_RE.search(gr)]
        if groups:
            score += 2
            ev.append(f"in group {groups[0]}")
        if PLAYER_NAME_RE.search(n["name"] or "") or PLAYER_NAME_RE.search(Path(n["file"]).stem):
            score += 1
            ev.append(f"named '{n['name']}' in {n['file']}")
        if info.nodes_of("Camera2D", "Camera3D"):
            score += 1
            ev.append("has a camera child")
        if n["path"] == "." and n["file"] in instanced:
            score += 1
            ev.append(f"scene instanced in {instanced[n['file']]}")
        if "move_and_slide" in info.calls():
            score += 0.5
            ev.append("calls move_and_slide()")
        cands.append({"node_id": n["id"], "scene": n["file"], "node": n["path"], "type": t, "score": score,
                      "evidence": ev, "_info": info})
    cands.sort(key=lambda c: -c["score"])
    return cands


def score_genres(ctx: DetectCtx, packs: dict[str, Pack]) -> list[dict]:
    out = []
    dims = ctx.player.dims if ctx.player else None
    for pack in packs.values():
        if not pack.selectable or not pack.detect:
            continue
        req_dims = pack.detect.get("requires", {}).get("dims")
        if req_dims and dims and dims not in ([req_dims] if isinstance(req_dims, str) else req_dims):
            continue
        total = 0.0
        got = 0.0
        matched, missing = [], []
        for rule in pack.detect.get("rules", []):
            w = float(rule.get("weight", 1))
            ok, ev = evaluate(ctx, rule)
            label = rule.get("label") or rule["signal"]
            if w > 0:
                total += w
                if ok:
                    got += w
                    matched.append(f"{label}: {ev}")
                else:
                    missing.append(label)
            elif ok:
                got += w
                matched.append(f"(against) {label}: {ev}")
        score = max(got, 0.0) / total if total else 0.0
        out.append({"pack": pack.name, "title": pack.title, "score": round(score, 2), "matched": matched, "missing": missing})
    out.sort(key=lambda d: -d["score"])
    return out


def detect_mechanics(info: PlayerInfo | None, roles: dict[str, str]) -> list[dict]:
    if not info:
        return []
    out = []
    calls = info.calls()
    for name, rule in MECHANICS:
        ev = None
        if "role" in rule and roles.get(rule["role"]) in info.reads:
            ev = f"reads '{roles[rule['role']]}'"
        if ev is None and "prop" in rule:
            r = re.compile(rule["prop"], re.I)
            for p in info.properties:
                if r.search(p["name"]):
                    ev = f"{p['name']} ({p['file']}:{p['props'].get('line')})"
                    break
        if ev is None and "call" in rule and rule["call"] in calls and "prop" not in rule:
            ev = f"calls {rule['call']}()"
        if ev is None and "call_with_role" in rule:
            call, role = rule["call_with_role"]
            act = roles.get(role)
            if act and call in info.reads.get(act, {}).get("calls", []):
                ev = f"{call}('{act}')"
        if ev is None and "assign" in rule:
            r = re.compile(rule["assign"])
            for a in info.assigns():
                if r.search(a["target"]):
                    ev = f"{a['target']} {a['op']} {a.get('value')} ({a['file']}:{a['line']})"
                    break
        if ev is None and "func" in rule and name == "knockback":
            r = re.compile(rule["func"], re.I)
            for f in info.functions:
                if r.search(f["name"]) and any(a["target"].startswith("velocity") for a in f["props"].get("assigns", [])):
                    ev = f"{f['name']}() sets velocity ({f['file']}:{f['props'].get('line')})"
                    break
        if ev:
            out.append({"name": name, "evidence": ev})
    return out


def detect_levels(g: Graph, player_scene: str | None) -> list[dict]:
    if not player_scene:
        return []
    levels: dict[str, str] = {}
    frontier = [player_scene]
    seen = {player_scene}
    depth = 0
    while frontier and depth < 3:
        nxt = []
        for sc in frontier:
            for e in g.inc(f"scene:{sc}", "instances"):
                f = e["file"]
                if f in seen:
                    continue
                seen.add(f)
                if not UI_RE.search(f):
                    levels[f] = "instances the player" if sc == player_scene else f"instances {sc}"
                nxt.append(f)
        frontier = nxt
        depth += 1
    main = g.node("project")
    main_scene = main["props"].get("main_scene") if main else None
    out = [{"scene": s, "why": w, "main": s == main_scene} for s, w in sorted(levels.items())]
    if main_scene and main_scene not in levels and main_scene != player_scene:
        out.append({"scene": main_scene, "why": "main scene", "main": True})
    return out


def detect_hazards(g: Graph, nodes: list[dict], player_scene: str | None) -> list[dict]:
    out: dict[str, str] = {}
    for n in nodes:
        if n["file"] == player_scene:
            continue
        groups = [gr for gr in n["props"].get("groups", []) if HAZARD_RE.search(gr)]
        is_root = n["path"] == "."
        if groups and is_root:
            out.setdefault(n["file"], f"root in group '{groups[0]}'")
        elif is_root and HAZARD_RE.search(Path(n["file"]).stem):
            out.setdefault(n["file"], "scene name")
    for e in g.edges("connects"):
        if e["props"].get("signal") in ("body_entered", "area_entered"):
            fn = g.node(e["dst"])
            if fn and any("damage" in c or "hurt" in c or "kill" in c or "die" in c for c in fn["props"].get("calls", [])):
                out.setdefault(e["file"], f"{e['props']['signal']} -> {fn['name']}() deals damage")
    return [{"scene": s, "why": w} for s, w in sorted(out.items())]


def detect(g: Graph, project_root: Path | None = None) -> dict[str, Any]:
    packs = load_packs(project_root)
    nodes = _scene_nodes(g)
    all_actions = [n["name"] for n in g.nodes("InputAction")]
    cands = player_candidates(g, nodes, all_actions)
    best = cands[0] if cands and cands[0]["score"] > 0 else None
    info: PlayerInfo | None = best["_info"] if best else None
    roles, roles_why = map_roles(info, all_actions)
    ctx = DetectCtx(g=g, player=info, roles=roles, scene_nodes=nodes)
    genres = score_genres(ctx, packs)
    project = g.node("project")
    files = g.files()
    cameras = info.nodes_of("Camera2D", "Camera3D") if info else []
    hitboxes = [n for n in (info.subtree if info else []) if (n["props"].get("type") or "") in ("Area2D", "Area3D")
                and (HITBOX_RE.search(n["name"] or "") or "gamefeel_hitbox" in n["props"].get("groups", []))]
    return {
        "project": {
            "name": project["name"] if project else None,
            "godot_version": project["props"].get("godot_version") if project else None,
            "main_scene": project["props"].get("main_scene") if project else None,
            "physics": project["props"].get("physics") if project else None,
            "scan_source": project["props"].get("source") if project else None,
            "files": len(files),
            "files_with_errors": sorted(p for p, f in files.items() if f.get("errors")),
        },
        "dims": info.dims if info else None,
        "player": _cand_out(best) if best else None,
        "player_candidates": [_cand_out(c) for c in cands[1:4] if c["score"] > 0],
        "roles": roles,
        "roles_evidence": roles_why,
        "genres": genres,
        "mechanics": detect_mechanics(info, roles),
        "levels": detect_levels(g, best["scene"] if best else None),
        "hazards": detect_hazards(g, nodes, best["scene"] if best else None),
        "camera": [{"node": c["path"], "type": c["props"].get("type"), "scene": c["file"]} for c in cameras],
        "hitboxes": [{"node": h["path"], "scene": h["file"]} for h in hitboxes],
    }


def _cand_out(c: dict) -> dict:
    return {k: v for k, v in c.items() if not k.startswith("_")}


def draft_profile(det: dict, packs: dict[str, Pack]) -> dict:
    genres = [g["pack"] for g in det["genres"] if g["score"] >= 0.6]
    if not genres and det["genres"] and det["genres"][0]["score"] >= 0.35:
        genres = [det["genres"][0]["pack"]]
    # an action side-scroller already includes the platformer pack: keep only the most specific
    from ..packs.loader import expand

    covered = {inc.name for g in genres if g in packs for inc in expand(packs, [g]) if inc.name != g}
    genres = [g for g in genres if g not in covered]
    style = packs[genres[0]].default_style if genres and genres[0] in packs else "default"
    player = det.get("player") or {}
    return {
        "confirmed": False,
        "genres": genres,
        "style": style,
        "dims": det.get("dims"),
        "player": {"scene": player.get("scene"), "node": player.get("node"), "type": player.get("type")},
        "camera": det["camera"][0]["node"] if det.get("camera") else None,
        "actions": det.get("roles", {}),
        "mechanics": [m["name"] for m in det.get("mechanics", [])],
        "levels": [lv["scene"] for lv in det.get("levels", [])],
        "hazard": det["hazards"][0]["scene"] if det.get("hazards") else None,
        "hitboxes": [h["node"] for h in det.get("hitboxes", [])],
        "accepted": {},
    }


def write_profile(path: Path, profile: dict) -> None:
    header = (
        "# gamefeel profile — what this game is. Drafted by `gamefeel detect`, confirmed by you.\n"
        "# Edit anything that's wrong, then set confirmed: true (or run `gamefeel confirm`).\n"
        "# accepted: {metric: reason} marks intentional deviations so they stop being reported as problems.\n"
    )
    path.write_text(header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True))


def read_profile(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text()) or {}
