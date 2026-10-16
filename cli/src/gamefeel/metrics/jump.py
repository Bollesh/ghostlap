"""Jump metrics (platformer pack, also used by 3D and side-scrolling action)."""

from __future__ import annotations

from .base import MetricValue, ProbeData, Run, metric, none


def _jump_action(pd: ProbeData) -> str | None:
    return pd.spec.get("roles", {}).get("jump")


def _arc(run: Run, action: str) -> dict | None:
    """Takeoff/apex/landing for the first jump after pressing `action`."""
    presses = [f for f, a in run.presses(action)]
    if not presses:
        return None
    p = presses[0]
    by_f = {s["f"]: s for s in run.frames}
    ground = by_f.get(p - 1) or by_f.get(p)
    if ground is None:
        return None
    thr = 0.5 * run.height  # H/s: clearly moving up, in 2D (px) and 3D (m) alike
    t0 = next((s["f"] for s in run.frames if s["f"] >= p and run.vy(s) > thr), None)
    if t0 is None:
        return {"press": p, "jumped": False}
    apex = next((s["f"] for s in run.frames if s["f"] > t0 and run.vy(s) <= 0), None)
    y0 = run.y_up(ground)
    peak = max(run.y_up(s) for s in run.frames if s["f"] >= p)
    landing = None
    if apex is not None:
        landing = next((s["f"] for s in run.frames if s["f"] > apex and s.get("fl") is True), None)
    return {"press": p, "jumped": True, "takeoff": t0, "apex": apex, "landing": landing,
            "height": (peak - y0) / run.height, "takeoff_speed": run.vy(by_f[t0]) / run.height}


def _arcs(pd: ProbeData) -> list[tuple[Run, dict]]:
    action = _jump_action(pd)
    if not action:
        return []
    out = []
    for r in pd.ok_runs():
        a = _arc(r, action)
        if a and a.get("jumped"):
            hold = sum(1 for s in r.frames if action in s.get("in", []))
            a["hold"] = hold
            out.append((r, a))
    out.sort(key=lambda ra: ra[1]["hold"])
    return out


@metric("jump.time_to_apex", "s", "Time from takeoff to the top of a full (held) jump.")
def time_to_apex(pd: ProbeData) -> MetricValue:
    arcs = _arcs(pd)
    if not arcs or arcs[-1][1].get("apex") is None:
        return none("s", "no complete jump recorded (did the player jump at all?)")
    r, a = arcs[-1]
    t = (a["apex"] - a["takeoff"]) / r.fps
    return MetricValue(round(t, 4), "s", details={"takeoff_speed_H_per_s": round(a["takeoff_speed"], 3), "hold_frames": a["hold"]},
                       evidence={"probe": pd.id, "run": r.name, "frames": [a["takeoff"], a["apex"]]})


@metric("jump.height_max", "H", "Height of a full (held) jump, in player-heights.")
def height_max(pd: ProbeData) -> MetricValue:
    arcs = _arcs(pd)
    if not arcs:
        return none("H", "no jump recorded")
    r, a = arcs[-1]
    return MetricValue(round(a["height"], 3), "H", evidence={"probe": pd.id, "run": r.name, "frames": [a["press"], a.get("apex")]})


@metric("jump.height_min", "H", "Height of the shortest jump (1-frame tap), in player-heights.")
def height_min(pd: ProbeData) -> MetricValue:
    arcs = _arcs(pd)
    if not arcs:
        return none("H", "no jump recorded")
    r, a = arcs[0]
    return MetricValue(round(a["height"], 3), "H", evidence={"probe": pd.id, "run": r.name})


@metric("jump.variable", "x", "Held-jump height divided by tapped-jump height (1.0 = jump height ignores how long you hold).")
def variable(pd: ProbeData) -> MetricValue:
    arcs = _arcs(pd)
    if len(arcs) < 2:
        return none("x", "needs a tap run and a hold run")
    lo, hi = arcs[0][1]["height"], arcs[-1][1]["height"]
    if lo <= 0:
        return none("x", "tap jump had no height")
    return MetricValue(round(hi / lo, 3), "x", details={"tap_H": round(lo, 3), "hold_H": round(hi, 3)}, evidence={"probe": pd.id})


@metric("jump.fall_rise_ratio", "x", "Rise time divided by fall time of a full jump (>1 means falling is faster than rising).")
def fall_rise_ratio(pd: ProbeData) -> MetricValue:
    arcs = _arcs(pd)
    if not arcs:
        return none("x", "no jump recorded")
    r, a = arcs[-1]
    if a.get("apex") is None or a.get("landing") is None:
        return none("x", "jump never landed within the probe's frames")
    rise = a["apex"] - a["takeoff"]
    fall = a["landing"] - a["apex"]
    if fall <= 0:
        return none("x", "zero fall time")
    return MetricValue(round(rise / fall, 3), "x", details={"rise_frames": rise, "fall_frames": fall},
                       evidence={"probe": pd.id, "run": r.name, "frames": [a["takeoff"], a["landing"]]})


@metric("jump.air_time", "s", "Takeoff to landing for a full jump on flat ground.")
def air_time(pd: ProbeData) -> MetricValue:
    arcs = _arcs(pd)
    if not arcs or arcs[-1][1].get("landing") is None:
        return none("s", "no landed jump recorded")
    r, a = arcs[-1]
    return MetricValue(round((a["landing"] - a["takeoff"]) / r.fps, 4), "s", evidence={"probe": pd.id, "run": r.name})


def _jumped_after(run: Run, press: int, window: int = 12) -> bool:
    thr = 0.5 * run.height
    return any(press <= s["f"] <= press + window and run.vy(s) > thr for s in run.frames)


def _sweep(pd: ProbeData, ok) -> MetricValue:
    action = _jump_action(pd)
    if not action:
        return none("frames", "no jump action in profile")
    results = {}
    for r in pd.ok_runs():
        presses = [f for f, a in r.presses(action)]
        if not presses:
            continue
        results[int(r.vars.get("k", 0))] = ok(r, presses[0])
    if not results:
        return none("frames", "no runs recorded")
    control = min(results)
    if not results[control]:
        return none("frames", f"the control run (k={control}) didn't jump — check the jump action / arena",
                    results={str(k): v for k, v in sorted(results.items())})
    best = 0  # some games check is_on_floor() after move_and_slide(), so even k=0 can fail: that's 0
    for k in sorted(results):
        if k < 0:
            continue
        if results[k]:
            best = k
        else:
            break
    return MetricValue(best, "frames", details={"results": {str(k): v for k, v in sorted(results.items())}},
                       evidence={"probe": pd.id})


@metric("jump.coyote_frames", "frames", "How many frames after walking off a ledge a jump still works (0 = no coyote time).")
def coyote_frames(pd: ProbeData) -> MetricValue:
    return _sweep(pd, lambda r, p: _jumped_after(r, p))


@metric("jump.buffer_frames", "frames", "How many frames before landing a jump press is remembered (0 = no jump buffer).")
def buffer_frames(pd: ProbeData) -> MetricValue:
    cal_landed = None
    if pd.cal:
        for s in pd.cal.frames:
            if s.get("fl") is True:
                cal_landed = s["f"]
                break

    def ok(r: Run, p: int) -> bool:
        if cal_landed is None:
            return False
        lo, hi = cal_landed - 1, cal_landed + 3
        on_floor_seen = any(lo <= s["f"] <= hi and s.get("fl") is True for s in r.frames)
        thr = 0.5 * r.height
        rose = any(lo <= s["f"] <= hi + 1 and r.vy(s) > thr for s in r.frames)
        return on_floor_seen and rose

    mv = _sweep(pd, ok)
    if cal_landed is None:
        return none("frames", "calibration fall never landed")
    mv.details["landing_frame"] = cal_landed
    return mv
