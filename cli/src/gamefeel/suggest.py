"""Turn a finding into concrete suggestions, using the graph to find the knob.

Every suggestion points at something real: a property with file/line/current value, a
built-in node property, or a pattern document (docs for a missing mechanic). Numbers come
from simple physics models where one exists:

* jump: constant-gravity arc, h = v^2 / 2g and t = v / g. To hit time t* and height h*:
  v' = v * (h*/h) * (t/t*),  g' = g * (h*/h) * (t/t*)^2
* fall/rise ratio: r = sqrt(g_fall / g_rise), so multiplier' = multiplier * (r*/r)^2
* accel/decel/turn with constant acceleration: time is inversely proportional to the rate
  (a property named *_time/_duration is proportional instead)
"""

from __future__ import annotations

import re
from typing import Any

from .graph.queries import exports_of, script_of
from .graph.store import Graph

KNOBS: dict[str, list[str]] = {
    "jump_velocity": [r"^jump_(velocity|speed|force|impulse|power|strength)$", r"^jump_?(vel|v)$", r"jump.*(velocity|speed|force|impulse)"],
    "gravity": [r"^gravity$", r"^(rise_|up_|jump_)?gravity(_scale)?$", r"^grav"],
    "fall_multiplier": [r"fall.*(gravity|mult|multiplier|scale)", r"(gravity|grav).*fall", r"^fall_gravity"],
    "coyote": [r"coyote"],
    "buffer": [r"(jump_)?buffer"],
    "jump_cut": [r"jump_cut|jump_release|variable_jump|release_(mult|multiplier)|short_hop"],
    "accel": [r"^(acceleration|accel|acc|move_accel|run_accel|ground_accel)$", r"accel"],
    "decel": [r"^(friction|deceleration|decel|stop_speed|brake|ground_friction)$", r"friction|decel|brake"],
    "turn_accel": [r"turn_?(accel|acceleration)|reverse_accel"],
    "speed": [r"^(speed|max_speed|move_speed|walk_speed|run_speed|top_speed)$"],
    "hitstop": [r"hit_?stop|freeze_frame|hit_?freeze|hit_pause"],
    "shake": [r"shake|trauma"],
    "knockback": [r"knock_?back"],
    "iframes": [r"invuln|i_?frames?|invincib|hurt_cooldown|damage_cooldown"],
    "hitstun": [r"hurt_stun|hit_?stun|stun_time|hurt_time|knockback_time"],
    "attack_startup": [r"startup|wind_?up|anticipation|attack_delay"],
    "attack_active": [r"active_(time|frames)|attack_(duration|time)|hitbox_(time|duration)"],
    "attack_recovery": [r"recovery|attack_cooldown|endlag"],
    "combo_window": [r"combo_(window|time|timeout|buffer)"],
    "dash_speed": [r"dash_(speed|velocity|force|distance|impulse)"],
    "dash_time": [r"dash_(time|duration|length)"],
    "dash_cooldown": [r"dash_cooldown|dash_delay"],
    "turn_rate": [r"turn_(speed|rate)|rotation_speed|rot_speed|angular_(speed|accel)|turn_smooth"],
    "camera_follow": [r"follow_speed|camera_speed|smooth(ing)?_speed|cam_lerp|lerp_speed|camera_lerp"],
}

PATTERNS = {
    "coyote": "coyote_time", "buffer": "jump_buffer", "jump_cut": "variable_jump", "fall_multiplier": "fall_gravity",
    "hitstop": "hitstop", "iframes": "invulnerability", "diagonal": "normalize_diagonal", "shake": "camera_shake",
}


def _mid(r: list | None) -> float | None:
    return (r[0] + r[1]) / 2 if r else None


def _is_time_like(name: str) -> bool:
    return bool(re.search(r"time|duration|delay|cooldown|window|length", name, re.I))


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Knobs:
    """Player-script properties, looked up by role name."""

    def __init__(self, g: Graph, profile: dict):
        self.g = g
        self.props: list[dict] = []
        player = profile.get("player") or {}
        seen: set[str] = set()
        if player.get("scene"):
            # the player's own script first, then every other script in its scene (camera rig, state machine...)
            scripts = [x["script"] for x in script_of(g, f"{player['scene']}::{player.get('node') or '.'}")]
            for n in g.nodes("SceneNode", "file = ?", (player["scene"],)):
                scripts.extend(e["dst"] for e in g.out(n["id"], "attaches_script"))
            for sc in scripts:
                if sc not in seen:
                    seen.add(sc)
                    self.props.extend(exports_of(g, sc))
        self.player = player

    def find(self, role: str) -> dict | None:
        for pat in KNOBS.get(role, []):
            r = re.compile(pat, re.I)
            cands = [p for p in self.props if r.search(p["name"]) and not p["name"].startswith("_")]
            if cands:
                cands.sort(key=lambda p: (not p.get("exported"), len(p["name"])))
                return cands[0]
        return None

    def node_prop(self, types: tuple[str, ...], prop: str) -> dict | None:
        """A built-in property on a node of `types` inside the player scene (e.g. Camera2D smoothing)."""
        scene = self.player.get("scene")
        if not scene:
            return None
        for n in self.g.nodes("SceneNode", "file = ?", (scene,)):
            if (n["props"].get("type") or "") in types:
                return {"node": n["path"], "file": scene, "line": n["props"].get("line"),
                        "current": (n["props"].get("props") or {}).get(prop), "type": n["props"].get("type"), "property": prop}
        return None


def _prop_sugg(p: dict, proposed: Any, why: str, effect: str = "") -> dict:
    cur = p.get("default")
    if isinstance(proposed, float):
        proposed = round(proposed, 4)
    return {"kind": "property", "id": p["id"], "property": p["name"], "file": p.get("file"), "line": p.get("line"),
            "current": cur, "proposed": proposed, "why": why, "predicted_effect": effect}


def _scale(p: dict, ratio: float, why: str, effect: str = "", as_int: bool = False,
           model: str | None = None, metric_value: float | None = None) -> dict:
    out = _scale_raw(p, ratio, why, effect, as_int)
    if model and _num(out.get("proposed")) is not None and _num(out.get("current")):
        out["model"] = model
        out["metric_value"] = metric_value
    return out


# Starting values for knobs that are switched off (0), as (seconds, frames).
ZERO_DEFAULTS = {"iframes": (0.75, 45), "hitstun": (0.2, 12), "hitstop": (0.08, 5), "coyote": (0.1, 6),
                 "buffer": (0.1, 6), "knockback": (300.0, 300.0), "combo_window": (0.25, 15), "attack_active": (0.07, 4),
                 "dash_cooldown": (0.5, 30)}


def _from_zero(p: dict, role: str, why: str, fps: float = 60.0) -> dict | None:
    """If the knob is 0, propose a starting value (seconds if it's a float, frames if an int)."""
    cur = _num(p.get("default"))
    if cur is None or cur != 0 or role not in ZERO_DEFAULTS:
        return None
    secs, frames = ZERO_DEFAULTS[role]
    if role == "knockback":
        return _prop_sugg(p, secs, why + " (currently 0 — a starting value; tune by feel)")
    proposed = secs if isinstance(p.get("default"), float) else frames
    return _prop_sugg(p, proposed, why + " (currently 0 — a common starting value)")


def _scale_raw(p: dict, ratio: float, why: str, effect: str = "", as_int: bool = False) -> dict:
    cur = _num(p.get("default"))
    if cur is None:
        return {"kind": "property", "id": p["id"], "property": p["name"], "file": p.get("file"), "line": p.get("line"),
                "current": p.get("default"), "proposed": f"multiply by {ratio:.2f}", "why": why, "predicted_effect": effect}
    new = cur * ratio
    return _prop_sugg(p, int(round(new)) if as_int or isinstance(p.get("default"), int) and not isinstance(p.get("default"), bool) else new, why, effect)


def _pattern(name: str, why: str) -> dict:
    return {"kind": "pattern", "pattern": PATTERNS.get(name, name), "why": why}


def _hint(text: str, **extra) -> dict:
    return {"kind": "hint", "why": text, **extra}


def _window_knob(k: dict, target_frames: float, fps: float, why: str) -> dict:
    cur = _num(k.get("default"))
    if cur is not None and cur < 1.0 and isinstance(k.get("default"), float):  # seconds
        return _prop_sugg(k, round(target_frames / fps, 3), why, f"≈{target_frames:.0f} frames at {fps:.0f} fps")
    return _prop_sugg(k, int(round(target_frames)), why)


def _damage_site(g: Graph, knobs: Knobs) -> dict | None:
    """The player function that takes damage and sets velocity (where knockback happens)."""
    scripts = {p["file"] for p in knobs.props}
    for script in scripts:
        for e in g.out(f"script:{script}", "defines"):
            fn = g.node(e["dst"])
            if fn and fn["kind"] == "Function" and re.search(r"damage|hurt|hit|knock", fn["name"], re.I):
                for a in fn["props"].get("assigns", []):
                    if a["target"].startswith("velocity"):
                        return {"name": fn["name"], "file": fn["file"], "line": a["line"]}
    return None


def suggest(g: Graph, profile: dict, finding: dict, measured: dict[str, dict], ranges: dict[str, dict], fps: float) -> list[dict]:
    m = finding["metric"]
    v = finding["value"]
    direction = finding.get("direction")
    rng = ranges.get(m, {})
    target = _mid(rng.get("good"))
    knobs = Knobs(g, profile)
    out: list[dict] = []

    if m in ("jump.time_to_apex", "jump.height_max"):
        t = (measured.get("jump.time_to_apex") or {}).get("value")
        h = (measured.get("jump.height_max") or {}).get("value")
        if t and h:
            t_rng = ranges.get("jump.time_to_apex", {}).get("good")
            h_rng = ranges.get("jump.height_max", {}).get("good")
            t_star = t if not t_rng or t_rng[0] <= t <= t_rng[1] else _mid(t_rng)
            h_star = h if not h_rng or h_rng[0] <= h <= h_rng[1] else _mid(h_rng)
            v_ratio = (h_star / h) * (t / t_star)
            g_ratio = (h_star / h) * (t / t_star) ** 2
            effect = f"time_to_apex {t:.2f}s → {t_star:.2f}s, height {h:.2f}H → {h_star:.2f}H"
            jv, gr = knobs.find("jump_velocity"), knobs.find("gravity")
            if jv:
                out.append(_scale(jv, v_ratio, "constant-gravity jump model (v' = v·(h*/h)·(t/t*))", effect))
            if gr:
                out.append(_scale(gr, g_ratio, "constant-gravity jump model (g' = g·(h*/h)·(t/t*)²)", effect))
            elif jv:
                proj = g.node("project")
                key = "gravity_3d" if profile.get("dims") == "3d" else "gravity_2d"
                cur = ((proj or {}).get("props", {}).get("physics") or {}).get(key)
                setting = "physics/3d/default_gravity" if profile.get("dims") == "3d" else "physics/2d/default_gravity"
                out.append({"kind": "setting", "setting": setting, "current": cur,
                            "proposed": round(cur * g_ratio, 3) if isinstance(cur, (int, float)) else f"multiply by {g_ratio:.2f}",
                            "why": "no gravity property on the player; it likely uses the project gravity (affects every body — a per-player gravity property is safer)",
                            "predicted_effect": effect})
            if not jv and not gr:
                out.append(_hint(f"couldn't find jump velocity / gravity properties; scale jump velocity by {v_ratio:.2f} and gravity by {g_ratio:.2f}"))
    elif m == "jump.fall_rise_ratio":
        k = knobs.find("fall_multiplier")
        if k and v and target:
            out.append(_scale(k, (target / v) ** 2, "ratio = sqrt(g_fall/g_rise)", f"fall/rise {v:.2f} → {target:.2f}"))
        else:
            out.append(_pattern("fall_multiplier", f"falls take as long as rises (ratio {v}); apply ~{(target or 1.6) ** 2:.1f}x gravity while falling"))
    elif m in ("jump.coyote_frames", "jump.buffer_frames"):
        role = "coyote" if m == "jump.coyote_frames" else "buffer"
        k = knobs.find(role)
        want = target or 6
        if k:
            out.append(_window_knob(k, want, fps, f"target ≈{want:.0f} frames"))
            if v == 0 and _num(k.get("default")) not in (None, 0):
                out.append(_hint(f"`{k['name']}` is set but the measured window is 0 — check that the code actually uses it"))
        if not k or v == 0:
            out.append(_pattern(role, f"{'coyote time' if role == 'coyote' else 'jump buffering'} is {'missing' if v == 0 else 'short'}"))
    elif m == "jump.variable":
        k = knobs.find("jump_cut")
        if k and _num(k.get("default")) is not None and direction == "low":
            out.append(_prop_sugg(k, 0.5, "cut upward velocity when jump is released early", "short taps give lower jumps"))
        elif direction == "low":
            out.append(_pattern("jump_cut", "jump height ignores how long jump is held"))
        elif k:
            out.append(_scale(k, 1.4, "cut less velocity on release (higher minimum jump)"))
    elif m in ("move.accel_time", "move.decel_time", "move.turnaround_time"):
        role = {"move.accel_time": "accel", "move.decel_time": "decel", "move.turnaround_time": "turn_accel"}[m]
        k = knobs.find(role) or (knobs.find("accel") if role == "turn_accel" else None)
        if k and v and target:
            time_like = _is_time_like(k["name"])
            ratio = (target / v) if time_like else (v / max(target, 1e-3))
            out.append(_scale(k, ratio, "constant-acceleration model: time ∝ 1/rate", f"{m.split('.')[1]} {v:.2f}s → {target:.2f}s",
                              model="proportional" if time_like else "inverse", metric_value=v))
        else:
            out.append(_hint(f"no {role} property found on the player; movement may set velocity directly — look at who-reads for the move actions"))
    elif m == "input.latency_frames":
        worst = (finding.get("details") or {}).get("worst")
        out.append(_hint("something delays the response: an await/timer, animation-driven movement, or input read in _process and applied next physics tick",
                         query=f"gamefeel graph query who-reads {worst}" if worst else None))
    elif m == "impact.hitstop_frames":
        k = knobs.find("hitstop")
        if k and target:
            out.append(_window_knob(k, target, fps, f"target ≈{target:.0f} frames of freeze"))
        else:
            out.append(_pattern("hitstop", "no hit-freeze when the player is hit"))
    elif m == "impact.knockback":
        k = knobs.find("knockback")
        stun = knobs.find("hitstun")
        if k and v and target:
            out.append(_from_zero(k, "knockback", "knockback impulse") or _scale(k, target / max(v, 0.05), "knockback distance scales with impulse"))
        if stun and direction == "low":
            out.append(_from_zero(stun, "hitstun", "ignore movement input briefly after a hit so knockback isn't cancelled")
                       or _scale(stun, 1.5, "ignore movement input a bit longer so knockback plays out"))
        site = _damage_site(g, knobs)
        if site:
            out.append(_hint(f"knockback velocity is set in {site['name']}() ({site['file']}:{site['line']}); "
                             + ("movement input may be overriding it right away" if direction == "low" else "reduce it"),
                             file=site["file"], line=site["line"]))
        if not out:
            out.append(_hint("knockback is set where damage is taken; look for take_damage/hurt functions that assign velocity"))
    elif m == "impact.hp_lost":
        k = knobs.find("iframes")
        if k:
            out.append(_from_zero(k, "iframes", "invulnerability after a hit")
                       or _scale(k, 2.0, "invulnerability ends before the player leaves the hazard"))
        else:
            out.append(_pattern("iframes", "one hazard contact removes health several times"))
    elif m in ("attack.startup_frames", "attack.active_frames", "attack.recovery_frames", "attack.combo_window_frames"):
        role = {"attack.startup_frames": "attack_startup", "attack.active_frames": "attack_active",
                "attack.recovery_frames": "attack_recovery", "attack.combo_window_frames": "combo_window"}[m]
        k = knobs.find(role)
        zero = _from_zero(k, role, role.replace("_", " ")) if k else None
        if zero:
            out.append(zero)
        elif k and v and target:
            cur = _num(k.get("default"))
            if cur is not None and cur < 2 and isinstance(k.get("default"), float):
                out.append(_scale(k, target / max(v, 1), "time-based property scales with frames", model="proportional", metric_value=v))
            else:
                out.append(_scale(k, target / max(v, 1), "frame-based property", as_int=True, model="proportional", metric_value=v))
        else:
            out.append(_hint(f"no {role.replace('_', ' ')} property found; check the attack state/animation timings"))
    elif m == "move.diagonal_ratio":
        out.append(_pattern("diagonal", "diagonal input isn't normalized; use Input.get_vector() or .normalized() / .limit_length(1.0)"))
    elif m in ("dash.distance", "dash.duration_frames", "dash.cooldown_frames"):
        role = {"dash.distance": "dash_speed", "dash.duration_frames": "dash_time", "dash.cooldown_frames": "dash_cooldown"}[m]
        if m == "dash.distance":
            dur = measured.get("dash.duration_frames") or {}
            dur_rng = ranges.get("dash.duration_frames", {}).get("good")
            if dur.get("value") and dur_rng and not (dur_rng[0] <= dur["value"] <= dur_rng[1]):
                role = "dash_time"  # the dash lasts too long/short; fixing its time fixes the distance too
        k = knobs.find(role) or (knobs.find("dash_time") if role == "dash_speed" else None)
        zero = _from_zero(k, role, role.replace("_", " ")) if k else None
        if zero:
            out.append(zero)
        elif k and v and target:
            out.append(_scale(k, target / v, "dash distance = speed × time" if m == "dash.distance" else "scales linearly",
                              model="proportional", metric_value=v))
        else:
            out.append(_hint(f"no {role.replace('_', ' ')} property found"))
    elif m == "camera.follow_lag":
        k = knobs.find("camera_follow")
        if k and v and target and v > 0:
            out.append(_scale(k, v / max(target, 0.02), "lag ∝ 1/follow speed", model="inverse", metric_value=v))
        else:
            cam = knobs.node_prop(("Camera2D",), "position_smoothing_speed")
            if cam:
                cur = cam["current"] if isinstance(cam["current"], (int, float)) else 5.0
                ratio = v / max(target or 0.1, 0.02) if v and v > 0 else 1.5
                out.append({"kind": "node_property", **cam, "proposed": round(cur * ratio, 2),
                            "why": "Camera2D smoothing speed: higher = less lag"})
            else:
                out.append(_hint("camera follow is scripted; increase its follow/lerp speed"))
    elif m == "move.turn_time":
        k = knobs.find("turn_rate")
        if k and v and target:
            out.append(_scale(k, v / max(target, 0.02), "turn time ∝ 1/turn rate", model="inverse", metric_value=v))
        else:
            out.append(_hint("no turn-rate property found; the model probably rotates with lerp/slerp — raise its weight"))
    elif m == "move.slope_speed_ratio":
        if direction == "low":
            out.append({"kind": "node_property", "node": profile.get("player", {}).get("node") or ".",
                        "file": profile.get("player", {}).get("scene"), "property": "floor_constant_speed", "current": None,
                        "proposed": True, "why": "CharacterBody3D keeps the same speed on slopes when floor_constant_speed is on"})
    elif m == "perf.frame_spikes":
        out.append(_hint("open the Godot profiler during the probe's actions; headless timings exclude rendering"))
    return out
