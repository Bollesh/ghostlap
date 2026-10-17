"""Shared-core metrics: ground movement, input latency, frame cost."""

from __future__ import annotations

import math

from .base import MetricValue, ProbeData, Run, median, metric, none, spread

MOVE_ROLES = ("left", "right", "up", "down")


def move_actions(pd: ProbeData) -> set[str]:
    roles = pd.spec.get("roles", {})
    return {roles[r] for r in MOVE_ROLES if r in roles}


def _held_move(run: Run, s: dict, moves: set[str]) -> bool:
    return bool(set(s.get("in", [])) & moves)


def _top_speed(run: Run, plane: str, moves: set[str], lo: int = 0, hi: int | None = None) -> float:
    frames = [s for s in run.frames if lo <= s["f"] < (hi if hi is not None else 1 << 30)]
    held = [run.planar(s, plane) for s in frames if _held_move(run, s, moves)]
    return max(held) if held else 0.0


@metric("move.top_speed", "H/s", "Highest ground speed while a move input is held, in player-heights per second.")
def top_speed(pd: ProbeData) -> MetricValue:
    moves = move_actions(pd)
    vals = []
    for r in pd.ok_runs():
        sp = _top_speed(r, pd.plane, moves)
        if sp > 0:
            vals.append(sp / r.height)
    if not vals:
        return none("H/s", "player never moved while a move input was held")
    return MetricValue(round(median(vals), 3), "H/s", spread=spread(vals), evidence={"probe": pd.id})


@metric("move.accel_time", "s", "From pressing a move input at rest until reaching 90% of top speed.")
def accel_time(pd: ProbeData) -> MetricValue:
    moves = move_actions(pd)
    vals, ev = [], {}
    for r in pd.ok_runs():
        top = _top_speed(r, pd.plane, moves)
        presses = [(f, a) for f, a in r.presses() if a in moves]
        if not presses or top <= 0:
            continue
        p = presses[0][0]
        before = next((s for s in r.frames if s["f"] == p - 1), None)
        if before and r.planar(before, pd.plane) > 0.05 * top:
            continue  # not starting from rest
        hit = next((s["f"] for s in r.frames if s["f"] >= p and r.planar(s, pd.plane) >= 0.9 * top), None)
        if hit is None:
            continue
        vals.append((hit - p + 1) / r.fps)
        ev = {"run": r.name, "frames": [p, hit]}
    if not vals:
        return none("s", "no start-from-rest movement found in this probe")
    return MetricValue(round(median(vals), 4), "s", spread=spread(vals), evidence={"probe": pd.id, **ev})


@metric("move.decel_time", "s", "From releasing all move inputs at speed until slowing to 10% of that speed.")
def decel_time(pd: ProbeData) -> MetricValue:
    moves = move_actions(pd)
    vals, ev = [], {}
    for r in pd.ok_runs():
        top = _top_speed(r, pd.plane, moves)
        held_prev = False
        for s in r.frames:
            held = _held_move(r, s, moves)
            if held_prev and not held:
                rf = s["f"]
                prev = next((x for x in r.frames if x["f"] == rf - 1), None)
                s0 = r.planar(prev, pd.plane) if prev else 0.0
                if s0 < 0.5 * top or s0 <= 0:
                    break
                stop = next((x["f"] for x in r.frames if x["f"] >= rf and r.planar(x, pd.plane) <= 0.1 * s0), None)
                if stop is not None:
                    vals.append((stop - rf + 1) / r.fps)
                    ev = {"run": r.name, "frames": [rf, stop]}
                break
            held_prev = held
    if not vals:
        return none("s", "no release-at-speed found (or the player never slowed down)")
    return MetricValue(round(median(vals), 4), "s", spread=spread(vals), evidence={"probe": pd.id, **ev})


@metric("move.turnaround_time", "s", "From pressing the opposite direction at top speed until moving 90% of top speed the other way.")
def turnaround_time(pd: ProbeData) -> MetricValue:
    moves = move_actions(pd)
    vals, ev = [], {}
    for r in pd.ok_runs():
        top = _top_speed(r, pd.plane, moves)
        presses = [(f, a) for f, a in r.presses() if a in moves]
        if len(presses) < 2 or top <= 0:
            continue
        q = presses[1][0]
        prev = next((x for x in r.frames if x["f"] == q - 1), None)
        if not prev:
            continue
        vx, vz = r.planar_vec(prev, pd.plane)
        n = math.hypot(vx, vz)
        if n < 0.5 * top:
            continue
        d = (-vx / n, -vz / n)
        hit = None
        for s in r.frames:
            if s["f"] < q:
                continue
            a, b = r.planar_vec(s, pd.plane)
            if a * d[0] + b * d[1] >= 0.9 * top:
                hit = s["f"]
                break
        if hit is not None:
            vals.append((hit - q + 1) / r.fps)
            ev = {"run": r.name, "frames": [q, hit]}
    if not vals:
        return none("s", "no direction reversal at speed found")
    return MetricValue(round(median(vals), 4), "s", spread=spread(vals), evidence={"probe": pd.id, **ev})


def _state_sig(s: dict) -> tuple:
    return (s.get("a"), tuple(s.get("hb", [])))


@metric("input.latency_frames", "frames", "Frames from an input press until the player visibly responds (velocity, animation or hitbox change). Max over actions.")
def input_latency(pd: ProbeData) -> MetricValue:
    per_action: dict[str, int | None] = {}
    ev = {}
    for r in pd.ok_runs():
        by_f = {s["f"]: s for s in r.frames}
        eps = 0.001 * r.height * r.fps  # any response at all; slow ramps are accel_time's job
        for p, action in r.presses():
            base = by_f.get(p - 1)
            if base is None:
                continue
            bv = base.get("v") or [0, 0, 0]
            found = None
            for f in range(p, p + 31):
                s = by_f.get(f)
                if s is None:
                    break
                v = s.get("v") or [0, 0, 0]
                dv = math.sqrt(sum((a - b) ** 2 for a, b in zip(v, bv)))
                if dv > eps or _state_sig(s) != _state_sig(base):
                    found = f - p
                    break
            prev = per_action.get(action)
            per_action[action] = found if prev is None else max(prev, found if found is not None else prev)
            ev.setdefault(action, {"run": r.name, "press": p})
    measured = {a: v for a, v in per_action.items() if v is not None}
    silent = sorted(a for a, v in per_action.items() if v is None)
    if not measured:
        return none("frames", "no action produced a visible response within 30 frames", silent=silent)
    worst = max(measured, key=lambda a: measured[a])
    return MetricValue(measured[worst], "frames", details={"per_action": measured, "no_response": silent, "worst": worst},
                       evidence={"probe": pd.id, **ev.get(worst, {})},
                       note=f"no visible response to: {', '.join(silent)}" if silent else "")


@metric("perf.frame_spikes", "count", "Physics ticks that took more than 2x the median tick (and over 4 ms) of real time during the probe.")
def frame_spikes(pd: ProbeData) -> MetricValue:
    pts = [s.get("pt") for r in pd.ok_runs() for s in r.frames[1:] if s.get("pt") is not None]
    if len(pts) < 10:
        return none("count", "not enough frames")
    med = median(pts) or 0.0
    limit = max(2 * med, 4.0)
    spikes = [p for p in pts if p > limit]
    return MetricValue(len(spikes), "count", details={"median_ms": round(med, 3), "limit_ms": round(limit, 3),
                                                      "worst_ms": round(max(pts), 3), "ticks": len(pts)},
                       evidence={"probe": pd.id},
                       note="headless timing: no rendering cost included")
