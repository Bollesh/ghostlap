"""Top-down and 3D movement metrics: diagonals, dashes, camera follow, turning, slopes."""

from __future__ import annotations

import math

from .base import MetricValue, ProbeData, Run, median, metric, none
from .movement import _held_move, move_actions


@metric("move.diagonal_ratio", "x", "Top speed moving diagonally divided by top speed moving straight (1.0 = normalized).")
def diagonal_ratio(pd: ProbeData) -> MetricValue:
    moves = move_actions(pd)
    for r in pd.ok_runs():
        straight, diag = [], []
        for s in r.frames:
            held = set(s.get("in", [])) & moves
            sp = r.planar(s, pd.plane)
            if len(held) == 1:
                straight.append(sp)
            elif len(held) >= 2:
                diag.append(sp)
        if straight and diag and max(straight) > 0:
            return MetricValue(round(max(diag) / max(straight), 3), "x",
                               details={"straight_H_per_s": round(max(straight) / r.height, 3), "diagonal_H_per_s": round(max(diag) / r.height, 3)},
                               evidence={"probe": pd.id, "run": r.name})
    return none("x", "needs a run with one direction held, then two")


def _walk_speed(pd: ProbeData, r: Run) -> float:
    cal = (pd.cal.header.get("cal") if pd.cal else {}) or {}
    if pd.cal:
        moves = move_actions(pd)
        sp = [pd.cal.planar(s, pd.plane) for s in pd.cal.frames if _held_move(pd.cal, s, moves)]
        if sp:
            return max(sp)
    return float(cal.get("cal_max_speed", 0.0))


def _dash_segments(pd: ProbeData, r: Run) -> list[tuple[int, int]]:
    walk = _walk_speed(pd, r)
    if walk <= 0:
        return []
    segs, start, last = [], None, None
    for s in r.frames:
        fast = r.planar(s, pd.plane) > 1.25 * walk
        if fast and start is None:
            start = s["f"]
        if not fast and start is not None:
            segs.append((start, last))
            start = None
        last = s["f"]
    if start is not None:
        segs.append((start, last))
    return segs


@metric("dash.distance", "H", "Distance covered while dashing (speed above 1.25x walking speed), in player-heights.")
def dash_distance(pd: ProbeData) -> MetricValue:
    for r in pd.ok_runs():
        segs = _dash_segments(pd, r)
        if segs:
            a, b = segs[0]
            by_f = {s["f"]: s for s in r.frames}
            pa, pb = by_f[a]["p"], by_f[b]["p"]
            dist = math.dist(pa, pb)
            return MetricValue(round(dist / r.height, 3), "H", details={"frames": b - a + 1},
                               evidence={"probe": pd.id, "run": r.name, "frames": [a, b]})
    return none("H", "no dash detected (speed never exceeded 1.25x walking speed after pressing dash)")


@metric("dash.duration_frames", "frames", "Frames the dash holds near its peak speed (the slow-down tail afterwards isn't counted).")
def dash_duration(pd: ProbeData) -> MetricValue:
    for r in pd.ok_runs():
        segs = _dash_segments(pd, r)
        if segs:
            a, b = segs[0]
            speeds = [(s["f"], r.planar(s, pd.plane)) for s in r.frames if a <= s["f"] <= b]
            peak = max(sp for _, sp in speeds)
            plateau = [f for f, sp in speeds if sp >= 0.95 * peak]
            return MetricValue(len(plateau), "frames", details={"fast_frames_incl_slowdown": b - a + 1},
                               evidence={"probe": pd.id, "run": r.name, "frames": [plateau[0], plateau[-1]]})
    return none("frames", "no dash detected")


@metric("dash.cooldown_frames", "frames", "Smallest gap between two dash presses that produces two dashes.")
def dash_cooldown(pd: ProbeData) -> MetricValue:
    ok_ks, tried = [], []
    for r in pd.ok_runs():
        k = int(r.vars.get("k", 0))
        tried.append(k)
        if len(_dash_segments(pd, r)) >= 2:
            ok_ks.append(k)
    if not tried:
        return none("frames", "no runs")
    if not ok_ks:
        step = min((b - a for a, b in zip(sorted(tried), sorted(tried)[1:])), default=1)
        return MetricValue(max(tried) + step, "frames", details={"lower_bound": True, "tried": sorted(tried)},
                           evidence={"probe": pd.id},
                           note=f"no second dash within {max(tried)} frames — the cooldown is at least this long")
    return MetricValue(min(ok_ks), "frames", details={"works_at_k": sorted(ok_ks)}, evidence={"probe": pd.id})


@metric("camera.follow_lag", "s", "How far the camera trails the player at steady speed, expressed in seconds of movement.")
def follow_lag(pd: ProbeData) -> MetricValue:
    for r in pd.ok_runs():
        frames = [s for s in r.frames if s.get("cam")]
        if len(frames) < 40:
            continue
        last = frames[-30:]
        vx, vz = r.planar_vec(last[-1], pd.plane)
        speed = math.hypot(vx, vz)
        if speed <= 1e-6:
            continue
        d = (vx / speed, vz / speed)

        def along(s: dict) -> float:
            if pd.plane == "xz":
                return (s["cam"][0] - s["p"][0]) * d[0] + (s["cam"][2] - s["p"][2]) * d[1]
            if pd.plane == "xy":
                return (s["cam"][0] - s["p"][0]) * d[0] + (s["cam"][1] - s["p"][1]) * d[1]
            return (s["cam"][0] - s["p"][0]) * d[0]

        o0 = along(frames[0])
        oss = median([along(s) for s in last])
        sp = median([math.hypot(*r.planar_vec(s, pd.plane)) for s in last])
        if not sp:
            continue
        lag = (o0 - oss) / sp
        return MetricValue(round(lag, 4), "s", details={"trail_distance_H": round((o0 - oss) / r.height, 3)},
                           evidence={"probe": pd.id, "run": r.name})
    return none("s", "no camera found, or the player didn't reach a steady speed")


def _yaw_series(r: Run) -> list[tuple[int, float]]:
    out = []
    for s in r.frames:
        y = s.get("yaw")
        if not y:
            continue
        val = y[1] if len(y) > 1 and y[1] is not None else y[0]
        out.append((s["f"], val))
    return out


def _ang(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


@metric("move.turn_time", "s", "Time for the character to turn and face the opposite direction after reversing input.")
def turn_time(pd: ProbeData) -> MetricValue:
    moves = move_actions(pd)
    for r in pd.ok_runs():
        presses = [(f, a) for f, a in r.presses() if a in moves]
        if len(presses) < 2:
            continue
        q = presses[1][0]
        ys = _yaw_series(r)
        if not ys:
            return none("s", "no facing data (3D only)")
        y0 = next((v for f, v in reversed(ys) if f < q), None)
        if y0 is None:
            continue
        if max(_ang(v, y0) for f, v in ys) < 10:
            return none("s", "the character never rotates to face movement (no model/body rotation detected)")
        hit = next((f for f, v in ys if f >= q and _ang(v, y0) >= 170), None)
        if hit is None:
            return none("s", "didn't finish turning around within the probe")
        return MetricValue(round((hit - q + 1) / r.fps, 4), "s", evidence={"probe": pd.id, "run": r.name, "frames": [q, hit]})
    return none("s", "needs a run that reverses direction")


@metric("move.slope_speed_ratio", "x", "Speed walking up a 30° slope divided by speed on flat ground.")
def slope_ratio(pd: ProbeData) -> MetricValue:
    flat = 0.0
    if pd.cal:
        flat = max((pd.cal.planar(s, pd.plane) for s in pd.cal.frames), default=0.0)
    if flat <= 0:
        return none("x", "calibration on flat ground didn't move")
    for r in pd.ok_runs():
        on_slope = []
        for s in r.frames:
            fnorm = s.get("fn")
            if not fnorm:
                continue
            ang = math.degrees(math.acos(max(-1.0, min(1.0, fnorm[1]))))
            if ang >= 20:
                v = s.get("v") or [0, 0, 0]
                on_slope.append(math.sqrt(sum(c * c for c in v)))
        if on_slope:
            return MetricValue(round(median(on_slope) / flat, 3), "x", details={"slope_frames": len(on_slope)},
                               evidence={"probe": pd.id, "run": r.name})
        return none("x", "never stood on the slope (blocked at its base? check floor_max_angle)")
    return none("x", "no runs")
