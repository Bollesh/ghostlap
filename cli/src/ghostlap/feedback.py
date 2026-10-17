"""`ghostlap feel feedback "falls too fast"`: the user's eye beats a placeholder range.

A complaint is mapped to (metric, direction) pairs, and each pair tightens that metric's
range in the project's profile so the value the user just saw counts as wrong:

    "falls too fast" -> jump.fall_time is too LOW and jump.fall_rise_ratio too HIGH
    -> profile.ranges[jump.fall_time].good moves its lower bound above the current value

The next `ghostlap feel propose` then fixes toward what the user wants. Every entry is logged in
profile.yaml (`feedback:`) with the values at the time, which doubles as calibration data.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .detect.detect import read_profile, write_profile
from .packs.loader import load_packs, load_ranges
from .project import GhostlapError, Project

# (pattern, [(metric, "high"|"low")]) — "high" means the user says the value is too high
PHRASES: list[tuple[str, list[tuple[str, str]]]] = [
    (r"fall(s|ing)?\b.*\b(too )?(fast|quick|hard)|drops? (too )?fast|yank|slams?|plummet|heavy fall",
     [("jump.fall_time", "low"), ("jump.fall_rise_ratio", "high")]),
    (r"fall(s|ing)?\b.*\b(too )?slow|floaty|floats|moon|hangs? in the air|feather",
     [("jump.fall_time", "high"), ("jump.fall_rise_ratio", "low"), ("jump.time_to_apex", "high")]),
    (r"jump(s)?\b.*\b(too )?high|jumps? too far", [("jump.height_max", "high")]),
    (r"jump(s)?\b.*\b(too )?(low|short|weak|small)", [("jump.height_max", "low")]),
    (r"jump(s)?\b.*\b(too )?(fast|snappy|twitchy|quick)|rises? too fast|shoots? up", [("jump.time_to_apex", "low")]),
    (r"jump(s)?\b.*\b(too )?slow|rises? too slow", [("jump.time_to_apex", "high")]),
    (r"slipp|\bic[ey]\b|slides?|doesn'?t stop|won'?t stop|skates?", [("move.decel_time", "high")]),
    (r"stops? too (abrupt|sudden|fast|quick)|stops? (dead|instantly)", [("move.decel_time", "low")]),
    (r"stiff|robotic|rigid|mechanical|too snappy|twitchy|no momentum|lifeless",
     [("move.accel_time", "low"), ("move.decel_time", "low"), ("move.turnaround_time", "low"),
      ("jump.time_to_apex", "low"), ("jump.fall_time", "low")]),
    (r"sluggish|slow to (start|get going)|heavy start|accelerat\w* (too )?slow", [("move.accel_time", "high")]),
    (r"(starts?|accelerat\w*) too (fast|abrupt|quick)", [("move.accel_time", "low")]),
    (r"(runs?|moves?|walks?) too fast|too zippy|hard to control", [("move.top_speed", "high")]),
    (r"(runs?|moves?|walks?) too slow", [("move.top_speed", "low")]),
    (r"turns? (around )?(too )?slow|heavy turn|tank", [("move.turnaround_time", "high"), ("move.turn_time", "high")]),
    (r"camera\b.*\b(lags?|drags?|slow|behind)", [("camera.follow_lag", "high")]),
    (r"dash\b.*\b(too )?(long|far)|dash overshoots", [("dash.distance", "high")]),
    (r"dash\b.*\b(too )?(short|weak)", [("dash.distance", "low")]),
    (r"hits?\b.*\b(weak|weightless|nothing|no impact|don'?t register|wonky)|no (hit )?feedback|can'?t tell .*hit",
     [("impact.feedback", "low"), ("impact.hitstop_frames", "low")]),
    (r"(hitstop|freeze)\w*\b.*\b(too )?long|freezes? too long", [("impact.hitstop_frames", "high")]),
    (r"knock\s?back\b.*\b(too )?(strong|far|much)", [("impact.knockback", "high")]),
    (r"knock\s?back\b.*\b(too )?(weak|little)|no knock\s?back", [("impact.knockback", "low")]),
    (r"attacks?\b.*\b(too )?slow|slow attacks?", [("attack.startup_frames", "high")]),
    (r"unresponsive|laggy|input lag|delay(ed)? (input|controls)", [("input.latency_frames", "high")]),
]


def interpret(text: str) -> list[tuple[str, str]]:
    t = text.lower()
    out: list[tuple[str, str]] = []
    for pat, pairs in PHRASES:
        if re.search(pat, t):
            for p in pairs:
                if p not in out:
                    out.append(p)
    return out


def _latest_values(project: Project) -> dict[str, float]:
    """What the user most likely just looked at: an unapplied proposal's result, else the last report."""
    vals: dict[str, float] = {}
    rep = project.state / "report.json"
    if rep.is_file():
        for f in json.loads(rep.read_text()).get("findings", []):
            if isinstance(f.get("value"), (int, float)):
                vals[f["metric"]] = f["value"]
    ptr = project.state / "proposals" / "latest"
    if ptr.is_file():
        p = project.state / "proposals" / ptr.read_text().strip() / "proposal.json"
        if p.is_file():
            prop = json.loads(p.read_text())
            if not prop.get("applied"):
                for m in prop.get("metrics", []):
                    if isinstance(m.get("after"), (int, float)):
                        vals[m["metric"]] = m["after"]
    return vals


def tighten(rng: dict, value: float, direction: str) -> tuple[dict, str]:
    """Move the good range so `value` is outside it on the complained-about side."""
    lo, hi = rng["good"]
    wlo, whi = rng.get("warn") or [lo, hi]
    if direction == "high":
        if value > hi:
            return rng, f"already above the range ({hi}); it's flagged — run `ghostlap feel propose`"
        new_hi = lo + (value - lo) * 0.85 if value > lo else None
        if new_hi is None or new_hi <= lo:
            return rng, f"{value} is already at the bottom of the range [{lo}, {hi}]; another metric is probably responsible"
        return {**rng, "good": [lo, round(new_hi, 4)], "warn": [wlo, round(max(value, new_hi), 4)]}, f"good range {lo}–{hi} → {lo}–{round(new_hi, 4)}"
    if value < lo:
        return rng, f"already below the range ({lo}); it's flagged — run `ghostlap feel propose`"
    new_lo = hi - (hi - value) * 0.85 if value < hi else None
    if new_lo is None or new_lo >= hi:
        return rng, f"{value} is already at the top of the range [{lo}, {hi}]; another metric is probably responsible"
    return {**rng, "good": [round(new_lo, 4), hi], "warn": [round(min(value, new_lo), 4), whi]}, f"good range {lo}–{hi} → {round(new_lo, 4)}–{hi}"


def feedback(project: Project, text: str | None, metric: str | None = None, direction: str | None = None,
             dry_run: bool = False) -> dict:
    prof = read_profile(project.profile_path)
    if prof is None:
        raise GhostlapError("no profile — run `ghostlap detect` first")
    pairs = [(metric, direction)] if metric and direction else interpret(text or "")
    if not pairs:
        raise GhostlapError(f"couldn't map {text!r} to a metric. Pick one: `ghostlap feel feedback --metric <name> "
                            "--direction high|low` (see `ghostlap feel metrics`), e.g. --metric jump.fall_time --direction low")
    values = _latest_values(project)
    base = load_ranges(load_packs(project.root), prof.get("genres") or [], prof.get("style"))
    overrides = dict(prof.get("ranges") or {})
    changes = []
    for m, d in pairs:
        v = values.get(m)
        current = {**base.get(m, {}), **overrides.get(m, {})}
        if v is None:
            changes.append({"metric": m, "direction": d, "result": "not measured yet — run `ghostlap feel check` (or this genre doesn't measure it)"})
            continue
        if not current.get("good"):
            changes.append({"metric": m, "direction": d, "value": v, "result": "no range to adjust (informational metric)"})
            continue
        new, msg = tighten({k: current[k] for k in ("good", "warn") if k in current}, v, d)
        changed = new.get("good") != current.get("good")
        if changed:
            overrides[m] = {**new, "reason": f"feedback: {text or f'{m} too {d}'!r} ({time.strftime('%Y-%m-%d')})"}
        changes.append({"metric": m, "direction": d, "value": v, "result": msg, "changed": changed})
    if not dry_run and any(c.get("changed") for c in changes):
        prof["ranges"] = overrides
        log = list(prof.get("feedback") or [])
        log.append({"when": time.strftime("%Y-%m-%d %H:%M"), "text": text, "values": {c["metric"]: c.get("value") for c in changes},
                    "mapped": [f"{c['metric']} too {c['direction']}" for c in changes]})
        prof["feedback"] = log
        write_profile(project.profile_path, prof)
    return {"text": text, "changes": changes, "profile": str(project.profile_path), "dry_run": dry_run}
