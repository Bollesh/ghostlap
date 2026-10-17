"""Attack and impact metrics (side-scrolling action pack; impact also in the shared core)."""

from __future__ import annotations

import math

from .base import MetricValue, ProbeData, Run, median, metric, none


def _segments(run: Run) -> list[tuple[int, int]]:
    """[start, end] frame ranges where any hitbox is active."""
    segs = []
    start = None
    last = None
    for s in run.frames:
        active = bool(s.get("hb"))
        if active and start is None:
            start = s["f"]
        if not active and start is not None:
            segs.append((start, last))
            start = None
        last = s["f"]
    if start is not None:
        segs.append((start, last))
    return segs


def _attack_press(pd: ProbeData, run: Run) -> int | None:
    action = pd.spec.get("roles", {}).get("attack")
    presses = [f for f, a in run.presses(action)] if action else []
    return presses[0] if presses else None


@metric("attack.startup_frames", "frames", "Frames from pressing attack until the hitbox is active.")
def startup(pd: ProbeData) -> MetricValue:
    vals = []
    for r in pd.ok_runs():
        p = _attack_press(pd, r)
        segs = [sg for sg in _segments(r) if p is not None and sg[0] >= p]
        if p is not None and segs:
            vals.append(segs[0][0] - p)
    if not vals:
        return none("frames", "no active hitbox after pressing attack — set profile.hitboxes or add the hitbox Area to group 'ghostlap_hitbox'")
    return MetricValue(int(median(vals)), "frames", evidence={"probe": pd.id})


@metric("attack.active_frames", "frames", "Frames the attack hitbox stays active.")
def active(pd: ProbeData) -> MetricValue:
    vals = []
    for r in pd.ok_runs():
        p = _attack_press(pd, r)
        segs = [sg for sg in _segments(r) if p is not None and sg[0] >= p]
        if segs:
            vals.append(segs[0][1] - segs[0][0] + 1)
    if not vals:
        return none("frames", "no active hitbox found")
    return MetricValue(int(median(vals)), "frames", evidence={"probe": pd.id})


@metric("attack.recovery_frames", "frames", "Frames from the hitbox turning off until the player is back in its pre-attack animation state.")
def recovery(pd: ProbeData) -> MetricValue:
    vals = []
    for r in pd.ok_runs():
        p = _attack_press(pd, r)
        if p is None:
            continue
        by_f = {s["f"]: s for s in r.frames}
        base = (by_f.get(p - 1) or {}).get("a")
        segs = [sg for sg in _segments(r) if sg[0] >= p]
        if not segs or base is None:
            continue
        end = segs[0][1]
        back = next((s["f"] for s in r.frames if s["f"] > end and s.get("a") == base), None)
        if back is not None:
            vals.append(back - end)
    if not vals:
        return none("frames", "needs an animation the player returns to after attacking (AnimationPlayer/AnimatedSprite/AnimationTree)")
    return MetricValue(int(median(vals)), "frames", evidence={"probe": pd.id})


@metric("attack.combo_window_frames", "frames", "Width of the window (in frames after the first press) in which a second press produces a second hit.")
def combo_window(pd: ProbeData) -> MetricValue:
    ok_ks = []
    tried = []
    for r in pd.ok_runs():
        k = int(r.vars.get("k", 0))
        tried.append(k)
        if len(_segments(r)) >= 2:
            ok_ks.append(k)
    if not tried:
        return none("frames", "no runs")
    if not ok_ks:
        return MetricValue(0, "frames", details={"tried": sorted(tried)}, evidence={"probe": pd.id},
                           note="no second hit at any timing — no combo, or the second attack reuses an always-on hitbox")
    step = min((b - a for a, b in zip(sorted(tried), sorted(tried)[1:])), default=1)
    return MetricValue(max(ok_ks) - min(ok_ks) + step, "frames", details={"works_at_k": sorted(ok_ks), "tried": sorted(tried)},
                       evidence={"probe": pd.id})


def _hit_frame(run: Run) -> int | None:
    drops = [f for f, k, old, new in run.hp_changes() if isinstance(old, (int, float)) and isinstance(new, (int, float)) and new < old]
    if drops:
        return drops[0]
    for pf, ts, paused, n in run.process:
        return int(n)
    slow = next((s["f"] for s in run.frames if s.get("ts", 1.0) < 0.95 or s.get("pz")), None)
    return slow


def _first_hit(pd: ProbeData) -> tuple[Run, int] | None:
    for r in pd.ok_runs():
        h = _hit_frame(r)
        if h is not None:
            return r, h
    return None


@metric("impact.hitstop_frames", "frames", "Real frames the game slows/freezes (Engine.time_scale < 1 or paused) after the player is hit.")
def hitstop(pd: ProbeData) -> MetricValue:
    hit = _first_hit(pd)
    if hit is None:
        return none("frames", "the player was never hit — check profile.hazard and that it damages the player")
    r, h = hit
    frozen = [e for e in r.process if h - 1 <= int(e[3]) <= h + 90 and (e[1] < 0.95 or e[2])]
    first: list = []
    for e in frozen:  # only the first contiguous freeze: later ones belong to later hits
        if first and int(e[0]) != int(first[-1][0]) + 1:
            break
        first.append(e)
    return MetricValue(len(first), "frames", details={"hit_frame": h, "min_time_scale": min((e[1] for e in first), default=1.0),
                                                      "freezes_in_window": len(frozen)},
                       evidence={"probe": pd.id, "run": r.name, "frames": [h, h + 90]})


@metric("impact.knockback", "H", "How far the player is pushed back (against its movement) within 0.5 s of a hit, in player-heights.")
def knockback(pd: ProbeData) -> MetricValue:
    hit = _first_hit(pd)
    if hit is None:
        return none("H", "the player was never hit")
    r, h = hit
    by_f = {s["f"]: s for s in r.frames}
    before = by_f.get(h - 1) or by_f.get(h)
    if before is None:
        return none("H", "no frames around the hit")
    vx, vz = r.planar_vec(before, "xz" if r.dims == "3d" else "x")
    n = math.hypot(vx, vz) or 1.0
    d = (vx / n, vz / n)
    p0 = before["p"]
    best = 0.0
    for f in range(h, h + int(r.fps * 0.5) + 1):
        s = by_f.get(f)
        if not s:
            continue
        dx = s["p"][0] - p0[0]
        dz = (s["p"][2] - p0[2]) if r.dims == "3d" else 0.0
        best = max(best, -(dx * d[0] + dz * d[1]))
    return MetricValue(round(best / r.height, 3), "H", evidence={"probe": pd.id, "run": r.name, "frames": [h, h + int(r.fps * 0.5)]})


@metric("impact.shake", "px", "Peak camera shake offset after a hit (Camera2D.offset / Camera3D h_offset+v_offset).")
def shake(pd: ProbeData) -> MetricValue:
    hit = _first_hit(pd)
    if hit is None:
        return none("px", "the player was never hit")
    r, h = hit
    peak = 0.0
    for s in r.frames:
        if h <= s["f"] <= h + 90 and s.get("co"):
            peak = max(peak, math.hypot(*s["co"][:2]))
    unit = "m" if r.dims == "3d" else "px"
    return MetricValue(round(peak, 3), unit, evidence={"probe": pd.id, "run": r.name},
                       note="" if peak else "no camera offset change after the hit (shakes done by moving the camera node itself aren't detected yet)")


@metric("impact.hp_lost", "count", "Health lost within 0.5 s of first touching a hazard (detects multi-hits / missing i-frames).")
def hp_lost(pd: ProbeData) -> MetricValue:
    for r in pd.ok_runs():
        drops = [(f, k, old, new) for f, k, old, new in r.hp_changes() if isinstance(new, (int, float)) and new < old]
        if drops:
            k = drops[0][1]
            window = drops[0][0] + int(r.fps * 0.5)  # one contact; walking back in later is a new hit
            total = sum(old - new for f, kk, old, new in drops if kk == k and f <= window)
            return MetricValue(total, "count", details={"prop": k, "hits": len([d for d in drops if d[0] <= window])},
                               evidence={"probe": pd.id, "run": r.name})
    return none("count", "no tracked health property decreased")


HURT_ANIM = ("hurt", "hit", "damage", "pain", "stun", "ouch")


def _carry(run: Run, key: str) -> dict[int, object]:
    """Per-frame value of a key that the sampler only writes when it changes."""
    out: dict[int, object] = {}
    cur = None
    for s in run.frames:
        if key in s:
            cur = s[key]
        out[s["f"]] = cur
    return out


@metric("impact.feedback", "channels", "How many distinct reactions a hit gets within 0.75 s: flash, blink, camera shake, hitstop, sound, particles, hurt animation.")
def feedback(pd: ProbeData) -> MetricValue:
    hit = _first_hit(pd)
    if hit is None:
        return none("channels", "the player was never hit — check profile.hazard and that it damages the player")
    r, h = hit
    end = h + int(r.fps * 0.75)
    window = [s for s in r.frames if h - 1 <= s["f"] <= end]
    channels: dict[str, str] = {}

    vz = _carry(r, "vz")
    base = vz.get(h - 1) or vz.get(h)
    if base:
        for s in window:
            cur = vz.get(s["f"])
            if not cur or len(cur) != len(base):
                continue
            for a, b in zip(base, cur):
                if max(abs(a[i] - b[i]) for i in range(3)) > 0.15:
                    channels.setdefault("flash", f"colour change at frame {s['f']}")
                if (a[4] and not b[4]) or (a[3] - b[3] > 0.4):
                    channels.setdefault("blink", f"hidden/transparent at frame {s['f']}")
    if any(s.get("co") and math.hypot(*s["co"][:2]) > 0.5 for s in window):
        channels["shake"] = "camera offset moved"
    frozen = [e for e in r.process if h - 1 <= int(e[3]) <= end and (e[1] < 0.95 or e[2])]
    if frozen:
        channels["hitstop"] = f"{len(frozen)} frozen frames"
    for key, name in (("snd", "sound"), ("ptc", "particles")):
        series = _carry(r, key)
        before = series.get(h - 1) or 0
        if any((series.get(s["f"]) or 0) > before for s in window):
            channels[name] = f"{key} count rose after the hit"
    if any(any(w in (s.get("a") or "").lower() for w in HURT_ANIM) for s in window):
        channels["hurt animation"] = "a hurt/hit animation played"
    missing = [c for c in ("flash", "blink", "shake", "hitstop", "sound", "particles", "hurt animation") if c not in channels]
    return MetricValue(len(channels), "channels", details={"channels": channels, "missing": missing},
                       evidence={"probe": pd.id, "run": r.name, "frames": [h, end]},
                       note="" if channels else "nothing visibly or audibly reacts to the hit")
