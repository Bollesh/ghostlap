"""`ghostlap play …`: one short command per turn against a frozen, running game (design §3.2)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
from pathlib import Path

from ..project import GhostlapError, Project, ensure_imported, find_godot, find_project
from . import config as cfgmod
from . import preflight as preflightmod
from . import replay as replaymod
from . import verdict as verdictmod
from .bridge_client import Bridge, BridgeError
from .session import Session, load_goal, play_dir

GOAL_ALIAS = "goal"


def _j(v) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def render_views(views: dict) -> list[str]:
    out = []
    for name, v in views.items():
        if v == "unchanged":
            out.append(f"{name}: unchanged")
        elif isinstance(v, dict) and isinstance(v.get("rows"), list):
            rest = {k: x for k, x in v.items() if k != "rows"}
            out.append(f"{name}: {_j(rest)}")
            out += ["  " + r for r in v["rows"]]
        elif isinstance(v, list):
            out.append(f"{name}:" + (" none" if not v else ""))
            out += ["  " + _j(x) for x in v]
        else:
            out.append(f"{name}: {_j(v)}")
    return out


def header(s: Session, res: dict, ran: int | None = None) -> str:
    m, u, b = s.meta, s.meta["used"], s.meta["budget"]
    goal = m["goal"].get("success")
    if m.get("goal_met_at") is not None:
        g = f"goal MET at f={m['goal_met_at']}"
    elif goal:
        g = "goal not met"
    else:
        g = "no success expression"
    if res.get("goal_error") and m.get("goal_met_at") is None:
        g += f" (check failing: {res['goal_error']})"
    held = ", ".join(h.split(":", 1)[1] for h in m.get("held") or []) or "nothing"
    parts = [f"f={res['f']}" + (f" (+{ran})" if ran is not None else ""), res.get("scene", ""), g, f"held: {held}",
             f"budget: {u['frames']}/{b['frames']} frames, {u['commands']}/{b['commands']} commands, {u['shots']}/{b['shots']} shots"]
    if res.get("paused"):
        parts.append("tree PAUSED")
    if res.get("time_scale", 1.0) != 1.0:
        parts.append(f"time_scale {res['time_scale']}")
    return " · ".join(parts)


def hint_line(hint: dict | None) -> list[str]:
    if not hint:
        return []
    also = f" (also: {', '.join(hint['also'])})" if hint.get("also") else ""
    return [f"SHOT HINT {hint['reason']}: read {hint['path']} before acting — {hint['why']}{also}"]


def _emit(args, s: Session | None, text: str, data: dict) -> None:
    out = json.dumps(data, indent=1, default=str) if args.json else text
    if s is not None:
        s.meta["used"]["bytes"] += len(out.encode())
        s.save()
    print(out)


def _session(args, project: Project, cmd: str) -> Session:
    s = Session.active(project, args.session)
    if s.meta.get("status") != "running":
        raise GhostlapError(f"session {s.id} is {s.meta.get('status')}; start a new one")
    s.count(cmd)
    return s


def _bridge_call(s: Session, fn):
    try:
        return fn()
    except BridgeError as e:
        if s.bridge.exited():
            raise GhostlapError(s.mark_dead(str(e))) from e
        raise


# ------------------------------------------------------------------ commands

def cmd_start(args, project: Project) -> int:
    from ..detect.detect import read_profile
    from .profile import bridge_profile

    godot = find_godot(args.godot)
    if not godot:
        raise GhostlapError("Godot not found — set GHOSTLAP_GODOT or pass --godot")
    goal = load_goal(Path(args.goal)) if args.goal else None
    prof = read_profile(project.profile_path)
    display = args.display or "offscreen"
    flags = cfgmod.rolling_flag(args.rolling_shots) if args.rolling_shots else {}
    ensure_imported(godot, project)
    s, res = Session.start(project, godot, goal=goal, scene=args.scene, display=display, seed=args.seed,
                           profile=bridge_profile(project, prof) if prof else None, flags=flags)
    look = res
    s.remember(look["views"])
    hint = _bridge_call(s, lambda: s.shot_hint([look], look["views"]))
    if hint:
        look["shot_hint"] = hint
    lines = [f"session {s.id} started ({display}), frozen at frame 0"]
    if display == "offscreen" and sys.platform.startswith("linux") and not shutil.which("xvfb-run"):
        lines.append("note: no xvfb-run here, so offscreen is a desktop window: focus changes can release held inputs "
                     "(the bridge presses them again). Install xvfb for a true offscreen run")
    if goal:
        lines.append(f"goal: {goal['goal']} — {goal.get('describe', '')}".rstrip(" —"))
        if goal.get("success"):
            lines.append(f"success: {goal['success']}")
    if prof is None:
        lines.append("note: no .ghostlap/profile.yaml — run `ghostlap scan && ghostlap detect` for player-aware views")
    lines += [incident_line(i) for i in s.meta.get("load_incidents") or []]
    pf = preflightmod.cached(project)
    if pf is None:
        lines.append("preflight: not run for this version of the project (`ghostlap play preflight`)")
    else:
        warn = preflightmod.warnings(pf)
        lines += [f"preflight: {w}" for w in warn] or ["preflight: no warnings"]
        if display == "headless" and pf.get("render_dependent"):
            lines.append("WARNING: this game has render-dependent logic and the session is headless; restart without --headless")
    lines.append(shots_line(s))
    lines += [header(s, look)] + render_views(look["views"]) + hint_line(hint)
    _emit(args, s, "\n".join(lines), {"session": s.id, "look": look})
    return 0


def shots_line(s: Session) -> str:
    if s.meta.get("headless"):
        return "screenshots: off (headless: no renderer, no shot hints)"
    c = s.config()
    every, keep = cfgmod.value(c, "shots.rolling.every"), cfgmod.value(c, "shots.rolling.keep")
    on = cfgmod.value(c, "shots.on")
    rolling = f"every {every} frames, keeping {keep}" if every and keep != 0 else "off"
    return (f"screenshots: {s.dir / 'shots'} · captured on {', '.join(on) or 'nothing'} · rolling {rolling} "
            f"(`ghostlap play config` to change)")


def cmd_look(args, project: Project) -> int:
    s = _session(args, project, "look")
    opts = {k: v for k, v in (("radius", args.radius), ("map_w", args.map_w), ("map_h", args.map_h),
                              ("shot_width", args.shot_width)) if v is not None}
    if args.shot:
        s.take_shot()
    views = [v.strip() for x in args.view or [] for v in x.split(",") if v.strip()]
    res = _bridge_call(s, lambda: s.look_raw(views=views or None, shot=args.shot, **opts))
    s.remember(res["views"])
    lines = [header(s, res)] + render_views(res["views"])
    if args.shot:
        sh = res.get("shot") or {}
        lines.append(f"shot: {sh['path']}" if sh.get("path") else f"shot: unavailable ({sh.get('error')})")
        if not sh.get("path"):
            s.meta["used"]["shots"] -= 1
    hint = _bridge_call(s, lambda: s.shot_hint([res], res["views"]))
    if hint:
        res["shot_hint"] = hint
        lines += hint_line(hint)
    _emit(args, s, "\n".join(lines), res)
    return 0


def _inputs(args, s: Session, frames: int):
    return s.plan_inputs(frames, hold=args.hold, key_hold=args.key_hold, press=args.press, keys=args.key,
                         release=args.release, mouse=args.mouse, keep=args.keep)


def incident_line(inc: dict) -> str:
    where = f" ({inc['file']}:{inc['line']})" if inc.get("file") else ""
    shot = f" · shot {inc['shot']}" if inc.get("shot") else ""
    return f"INCIDENT f={inc['f']} {inc['type']} [{inc['class']}]: {inc['why']}{where}{shot}"


def _split_views(v: list[str] | None) -> list[str] | None:
    return [x.strip() for part in v for x in part.split(",") if x.strip()] if v else None


def _after_step(args, s: Session, res: dict, ran: int, extra: list[str]) -> None:
    lines = [header(s, res, ran)] + extra + [incident_line(i) for i in res.get("incidents") or []]
    lost = res.get("input_lost") or []
    if lost:
        lines.append("WARNING: Godot released " + ", ".join(x["input"].split(":", 1)[1] for x in lost)
                     + " (its window gained or lost focus); the bridge pressed it again on f="
                     + str(lost[0]["f"]) + ", recorded in the input line. Without render-dependent logic, --headless avoids this")
    lines += [f"UI f={u['f']}: {u['node']} appeared" + (f" ({u['text']})" if u.get("text") else " (no text)")
              for u in res.get("new_ui") or []]
    responses, views = [res], None
    if not args.quiet:
        look = _bridge_call(s, lambda: s.look_raw(views=_split_views(getattr(args, "view", None))))
        responses.append(look)
        views = look["views"]
        lines += render_views(s.collapse(look["views"]))
    hint = _bridge_call(s, lambda: s.shot_hint(responses, views, res.get("until")))
    if hint:
        res["shot_hint"] = hint
        lines += hint_line(hint)
    _emit(args, s, "\n".join(lines), res)


def cmd_step(args, project: Project) -> int:
    s = _session(args, project, "step")
    if args.n < 1:
        raise GhostlapError("-n must be at least 1")
    s.check_can_step(args.n)
    ev, held, taps = _inputs(args, s, args.n)
    start = s.meta["frame"]
    res = s.run("step", args.n, ev, held, taps)
    _after_step(args, s, res, res["f"] - start, [])
    return 0


def cmd_until(args, project: Project) -> int:
    s = _session(args, project, "until")
    s.check_can_step(None)
    expr = args.expr
    if expr.strip() == GOAL_ALIAS:
        expr = s.meta["goal"].get("success")
        if not expr:
            raise GhostlapError("this goal has no success expression; give `until` an expression")
    mx = args.max
    clamped = mx > s.frames_left()
    mx = min(mx, s.frames_left())
    ev, held, taps = _inputs(args, s, mx)
    start = s.meta["frame"]
    res = s.run("until", mx, ev, held, taps, expr=expr)
    ran = res["f"] - start
    u = res.get("until") or {}
    if u.get("met"):
        msg = f"until: true at f={res['f']} after {ran} frames"
    elif u.get("reason") == "incident":
        msg = f"until: stopped by an incident ({u.get('incident')}) at f={res['f']} after {ran} frames"
    elif u.get("reason") == "error":
        msg = f"until: stopped at f={res['f']}, the expression failed: {u.get('error')}"
    else:
        msg = f"until: still false after {ran} frames (max{', clamped to the budget' if clamped else ''})"
    s.log("until", expr=expr, result=u, frames=ran)
    _after_step(args, s, res, ran, [msg])
    return 0


def cmd_eval(args, project: Project) -> int:
    s = _session(args, project, "eval")
    resp = _bridge_call(s, lambda: s.bridge.request("eval", expr=args.expr, write=args.write))
    if resp.get("write"):
        s.meta["cheats"].append({"f": s.meta["frame"], "eval": args.expr})
        s.log("write", expr=args.expr)
    _emit(args, s, _j(resp["result"]), {"result": resp["result"], "write": bool(resp.get("write"))})
    return 0


def cmd_note(args, project: Project) -> int:
    s = _session(args, project, "note")
    data = {"text": " ".join(args.text)}
    if args.saw:
        p = Path(args.saw)
        if not p.is_absolute():
            p = s.dir / p
        if not p.is_file():
            raise GhostlapError(f"--saw {args.saw}: no such screenshot (shots are in {s.dir / 'shots'})")
        data["saw"] = s.rel(str(p))
        s.pin_shot(p)
    s.log("note", **data)
    _emit(args, s, f"noted at f={s.meta['frame']}", data)
    return 0


def cmd_status(args, project: Project) -> int:
    s = Session.active(project, args.session)
    m = s.meta
    g = m["goal"]
    lines = [f"session {s.id} ({m['status']}, {m['display']}) · scene {m['scene']}"]
    if g:
        lines.append(f"goal: {g.get('goal')} — {g.get('describe', '')}".rstrip(" —"))
        if g.get("success"):
            lines.append(f"success: {g['success']}" + (f" · MET at f={m['goal_met_at']}" if m.get("goal_met_at") is not None else ""))
        for c in g.get("constraints") or []:
            lines.append(f"constraint: {c}")
    if m["status"] == "running" and s.alive():
        res = _bridge_call(s, lambda: s.look_raw(views=["player"]))
        lines.append(header(s, res))
    else:
        lines.append(f"frame {m['frame']} · budget used {m['used']}")
    if m.get("cheats"):
        lines.append(f"setup cheats: {len(m['cheats'])} (eval --write)")
    if m.get("incidents"):
        lines.append("incidents: " + ", ".join(f"{k} ×{v}" for k, v in m["incidents"].items()))
    lines.append(f"tokens returned so far: ~{m['used']['bytes'] // 4}")
    notes, shots, reads = [], set(), []
    if (s.dir / "events.jsonl").is_file():
        for ln in (s.dir / "events.jsonl").read_text().splitlines():
            e = json.loads(ln)
            if e["kind"] in ("note", "goal_met", "incident", "crashed", "abandoned", "refused", "goal_error", "rewind"):
                notes.append(e)
            elif e["kind"] == "shot":
                shots.add(e["path"])
            elif e["kind"] == "shot_read":
                reads.append(e["reason"])
    if shots or reads:
        why = ", ".join(f"{r} ×{reads.count(r)}" for r in sorted(set(reads)))
        lines.append(f"shots: {len(shots)} captured, {len(reads)} read" + (f" ({why})" if why else ""))
    for e in notes[-12:]:
        if e["kind"] == "incident":
            lines.append("  " + incident_line(e))
            continue
        txt = e.get("text") or e.get("why") or e.get("error") or (f"at f={e['at']}" if "at" in e else "") \
            or (f"back to f={e['to']}, attempt {e['attempt']} set aside" if e["kind"] == "rewind" else "")
        lines.append(f"  f={e['f']} {e['kind']}: {txt}" + (f" (saw {e['saw']})" if e.get("saw") else ""))
    if m.get("verdict"):
        lines.append(f"verdict: {m['verdict']} — {m.get('why', '')}")
    _emit(args, None, "\n".join(lines), m)
    return 0


def cmd_end(args, project: Project) -> int:
    s = Session.active(project, args.session)
    m = s.meta
    if m.get("verdict"):
        raise GhostlapError(f"session {s.id} already ended: {m['verdict']}")
    if args.video:
        replaymod._need("ffmpeg")   # before the verdict is saved, so a missing tool doesn't hide it
    alive = m["status"] == "running" and s.alive()

    def shoot():
        sh = _bridge_call(s, lambda: s.bridge.shot(why="hint"))
        s._sync_shot(sh)
        return sh

    try:
        res = verdictmod.check(s, args.verdict, args.evidence, shoot if alive else None)
    except verdictmod.Refused as e:
        s.log("refused", cmd="end", verdict=args.verdict, why=str(e).splitlines()[0])
        s.save()
        raise
    samples, hashes = None, None
    if alive:
        b = s.bridge
        try:
            look = s.look_raw(views=["player"])
            hashes = b.hashes()
            samples = b.samples()
            with open(s.dir / "hashes.jsonl", "w") as f:
                for i, h in enumerate(hashes):
                    if i % 60 == 0 or i == len(hashes) - 1:
                        f.write(_j({"f": i, "hash": h}) + "\n")
            m["final"] = {"f": look["f"], "hash": look["hash"], "player": look["views"].get("player")}
        except BridgeError:
            pass
        b.quit()
    rep = None
    godot = find_godot(args.godot)
    if not args.no_replay and godot:
        rep = replaymod.check(s, godot, hashes, samples)
    verdictmod.confirm(res, rep)
    verdictmod.write(s, res, args.why, samples, rep)
    m.update({"verdict": res["verdict"], "basis": res["basis"], "why": args.why, "evidence": args.evidence,
              "status": "ended"})
    s.log("end", verdict=res["verdict"], basis=res["basis"], why=args.why)
    s.save()
    pointer = play_dir(project) / "active"
    if pointer.is_file() and pointer.read_text().strip() == s.id:
        pointer.unlink()
    basis = {"machine": "machine-checked", "visual": "visual evidence",
             "incident": "confirmed by the replay" if res.get("confirmed") else "the replay didn't hit it again"
             if res.get("confirmed") is False else "no replay check", "blocked": "not a game bug"}[res["basis"]]
    lines = [f"session {s.id} ended: {res['verdict']} ({basis}) — {args.why}", replay_line(rep),
             *(["warning: incident shots were off (shots.on), so this fail has no pictures of its incidents"]
               if args.verdict == "fail" and "incident" not in cfgmod.value(s.config(), "shots.on") else []),
             f"goal met: {'yes, at f=' + str(m['goal_met_at']) if m.get('goal_met_at') is not None else 'no'}",
             f"frames {m['used']['frames']}, commands {m['used']['commands']}, shots {m['used']['shots']}, ~{m['used']['bytes'] // 4} tokens returned",
             f"report: {s.dir / 'report.md'} · evidence: {s.dir / 'evidence.json'}"]
    if args.video:
        lines += video_lines(s, godot, full=False)
    _emit(args, None, "\n".join(lines), m)
    return 0


def focus_note(lost: list[dict] | None) -> str:
    """A replay window that gained or lost focus: Godot released the held inputs mid-run."""
    if not lost:
        return ""
    return (f" · its window's focus changed at f={lost[0]['f']}: Godot released the held inputs and the bridge pressed "
            "them again, which can change what follows (a run without a desktop window, under xvfb, avoids it)")


def replay_line(rep: dict | None) -> str:
    if rep is None:
        return "replay check: not run"
    if rep["result"] == "exact":
        return f"replay check: exact ({rep['frames']} frames, {rep['mode']}, {rep['seconds']} s) — every frame matches"
    if rep["result"] == "diverged":
        return f"replay check: diverged at f={rep['diverged_at']}: {rep['field']}" + (
            f" · likely: {'; '.join(rep['likely_causes'])}" if rep.get("likely_causes") else "") + focus_note(rep.get("input_lost"))
    return f"replay check: {rep['result']} ({rep.get('why', '')})"


def _ended(args, project: Project) -> Session:
    """--session, else the most recent session (ended ones aren't `active` any more)."""
    if args.session:
        return Session.active(project, args.session)
    ids = sorted(d.name for d in play_dir(project).iterdir() if (d / "session.json").is_file())
    if not ids:
        raise GhostlapError("no play sessions yet")
    return Session(project, ids[-1])


def video_lines(s: Session, godot: str | None, full: bool) -> list[str]:
    if not godot:
        raise GhostlapError("Godot not found — set GHOSTLAP_GODOT or pass --godot")
    res, why, rep = verdictmod.load(s)
    hud, moments = verdictmod.hud(s, res, why)
    v = replaymod.video(s, godot, hud, moments, full=full)
    (s.dir / "report.md").write_text(verdictmod.report(s, res, why, rep, v))
    spans = v["spans"]
    cut = "" if spans == [(0, v["frames"])] else f" (highlights: {len(spans)} windows of the {v['frames']} frames; --full for all)"
    lines = [f"video: {v['mp4']}{cut}", f"gif: {v['gif']} ({v['gif_bytes'] // 1024} KB)"
             + (f" — still over {replaymod.GIF_MAX // 1_000_000} MB at the smallest setting: share the MP4"
                if v["gif_bytes"] > replaymod.GIF_MAX else "")]
    if v.get("sheet"):
        lines.append(f"contact sheet (read this to check the video): {v['sheet']}")
    if not v["matches_session"]:
        lines.append(f"WARNING: the rendered replay diverged from the session (by f={v.get('diverged_by')}): "
                     "the video may not show what happened" + focus_note(v.get("input_lost")))
    return lines


def cmd_video(args, project: Project) -> int:
    s = _ended(args, project)
    if s.meta.get("status") == "running":
        raise GhostlapError(f"session {s.id} is still running: end it first, so the video can show the verdict")
    _emit(args, None, "\n".join(video_lines(s, find_godot(args.godot), args.full)), {})
    return 0


def cmd_replay(args, project: Project) -> int:
    s = _ended(args, project)
    if s.meta.get("status") == "running":
        raise GhostlapError(f"session {s.id} is still running; `play rewind` goes back within a session")
    godot = find_godot(args.godot)
    if not godot:
        raise GhostlapError("Godot not found — set GHOSTLAP_GODOT or pass --godot")
    r = replaymod.watch(s, godot, args.display or "window", fast=args.fast)
    msg = {"exact": "matched the session at every recorded frame", "diverged": f"DIVERGED from the session by f={r['diverged_by']}",
           "not compared": "not compared (the session has no hashes)"}[r["result"]]
    extra = f" · crashed near f={r['crashed_at']}" if r["crashed_at"] is not None else (
        f" · hung at f={r['hung_at']}" if r["hung_at"] is not None else "")
    _emit(args, None, f"replayed {s.id}: {r['frames']} frames, {msg}{extra}{focus_note(r['input_lost'])}", r)
    return 0


def cmd_rewind(args, project: Project) -> int:
    s = _session(args, project, "rewind")
    godot = find_godot(args.godot)
    if not godot:
        raise GhostlapError("Godot not found — set GHOSTLAP_GODOT or pass --godot")
    r = _bridge_call(s, lambda: s.rewind(godot, args.to))
    a = r["attempt"]
    lines = [f"rewound to f={args.to}: attempt {a['n']} (f={a['to'] + 1}..{a['from']}, {a['inputs']} inputs) moved to "
             f"attempts/{a['n']}.jsonl; the input line now ends here"]
    if r["diverged_at"] is not None:
        lines.append(f"WARNING: the replay diverged at f={r['diverged_at']}: the game isn't deterministic from these inputs, "
                     f"so this state isn't exactly the one you saw (and a test of this line won't replay)")
    else:
        lines.append("replay matched the first run frame for frame")
    look = r["look"]
    lines += [header(s, look)] + render_views(look["views"])
    s.remember(look["views"])
    _emit(args, s, "\n".join(lines), {"rewind": a, "diverged_at": r["diverged_at"], "look": look})
    return 0


def cmd_config(args, project: Project) -> int:
    """Read the session's settings, or change them from the current frame on."""
    if not args.set:
        s = Session.active(project, args.session)
        _emit(args, None, "\n".join(cfgmod.describe(s.config())), s.config())
        return 0
    s = _session(args, project, "config")
    changes = {}
    for kv in args.set:
        key, eq, raw = kv.partition("=")
        if not eq:
            raise GhostlapError(f"{kv!r}: use key=value (`ghostlap play config` lists the settings)")
        changes[key.strip()] = cfgmod.parse(key.strip(), raw)
    cfg = s.config()
    for k, v in changes.items():
        cfg[k] = {"value": v, "source": f"play config at f={s.meta['frame']}"}
    if any(k.startswith("shots.") for k in changes):
        _bridge_call(s, lambda: s.bridge.request("config", shots=cfgmod.bridge_shots(cfg)))
    s.meta["config"] = cfg
    s.log("config", changes=changes)
    lines = [f"{k} = {', '.join(v) if isinstance(v, list) else v} from f={s.meta['frame'] + 1} on" for k, v in changes.items()]
    if "shots.on" in changes and "incident" not in changes["shots.on"]:
        lines.append("warning: incident shots are off — a fail verdict will have no pictures of its incidents")
    _emit(args, s, "\n".join(lines), {k: cfg[k] for k in changes})
    return 0


SHOT_PATH = re.compile(r"^(?P<dir>.*[/\\]\.ghostlap[/\\]play[/\\][^/\\]+)[/\\]shots[/\\]f(?P<f>\d+)\.png$")


def log_read(path: str) -> dict | None:
    """Record that Claude read a session screenshot (from the PostToolUse hook on Read): appends
    {kind: shot_read, f, path, reason} to the session's events.jsonl and keeps the shot from
    rolling rotation. reason: the hint that pointed at it, else why it was captured."""
    m = SHOT_PATH.match(str(path))
    if not m:
        return None
    d = Path(m.group("dir"))
    ev = d / "events.jsonl"
    if not (d / "session.json").is_file():
        return None
    name = Path(path).name
    hinted, captured = None, None
    if ev.is_file():
        for ln in ev.read_text().splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if Path(str(e.get("path", ""))).name != name:
                continue
            if e["kind"] == "shot_hint":
                hinted = e["reason"]
            elif e["kind"] == "shot":
                captured = e.get("why") or []   # the last record has every trigger of that frame
    if hinted:
        reason = hinted
    elif captured and "requested" in captured:
        reason = "requested"
    elif captured:
        reason = "auto:" + "+".join(w for w in captured if w != "hint")
    else:
        reason = "unprompted"
    rec = {"f": int(m.group("f")), "kind": "shot_read", "path": f"shots/{name}", "reason": reason}
    with open(ev, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    if (d / "shots").is_dir():
        with open(d / "shots" / "keep.txt", "a") as fh:
            fh.write(name + "\n")
    return rec


def cmd_log_read(args, project: Project | None) -> int:
    if args.hook:
        # never fail a Read: any problem here is ignored
        try:
            data = json.loads(sys.stdin.read() or "{}")
            p = Path(str((data.get("tool_input") or {}).get("file_path") or ""))
            if not p.is_absolute():
                p = Path(data.get("cwd") or os.getcwd()) / p
            log_read(str(p))
        except Exception:  # noqa: BLE001
            pass
        return 0
    rec = log_read(str(Path(args.path).resolve()))
    if rec is None:
        raise GhostlapError(f"{args.path} is not a play session screenshot (.ghostlap/play/<session>/shots/f<N>.png)")
    print(f"logged: read {rec['path']} (f={rec['f']}, {rec['reason']})")
    return 0


def cmd_preflight(args, project: Project) -> int:
    godot = find_godot(args.godot)
    if not godot:
        raise GhostlapError("Godot not found — set GHOSTLAP_GODOT or pass --godot")
    pf = preflightmod.run(project, godot, args.scene, args.force)
    lines = preflightmod.render(pf)
    warn = preflightmod.warnings(pf)
    lines.append(f"{len(warn)} warning(s) to raise with the user" if warn else "nothing to raise with the user")
    _emit(args, None, "\n".join(lines), pf)
    return 0


def cmd_kill(args, project: Project) -> int:
    targets = []
    if args.all:
        targets = [d.name for d in play_dir(project).iterdir() if (d / "bridge.json").is_file()]
    else:
        s = Session.active(project, args.session, required=False)
        targets = [s.id] if s else []
    killed = []
    for sid in targets:
        s = Session.active(project, sid, required=False)
        if s is None:
            continue
        try:
            b = Bridge.attach(s.dir)
        except BridgeError:
            continue
        if b.alive() and _is_our_bridge(b.pid, s.dir):
            try:
                os.kill(b.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed.append(sid)
        if s.meta.get("status") == "running":
            s.meta["status"] = "killed"
            s.log("killed")
            s.save()
    pointer = play_dir(project) / "active"
    if pointer.is_file() and pointer.read_text().strip() in targets:
        pointer.unlink()
    _emit(args, None, f"killed {len(killed)} Godot process(es)" + (f": {', '.join(killed)}" if killed else ""), {"killed": killed})
    return 0


def _is_our_bridge(pid: int, session_dir: Path) -> bool:
    """An old session's pid may belong to another program by now: only kill our own bridge."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return os.name != "posix" or not Path("/proc").is_dir()  # no /proc (macOS): trust the pid
    return "bridge.gd" in cmdline and str(session_dir.resolve()) in cmdline


# ------------------------------------------------------------------ parser

def _input_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("inputs (holds are declarative: anything held before but not listed is released)")
    g.add_argument("--hold", action="append", help="actions held for the whole command, e.g. move_right,jump")
    g.add_argument("--press", action="append", help="tap an action: jump, jump@5 (on frame 5), jump@5:12 (held 12 frames)")
    g.add_argument("--release", action="append", help="release a held action on the first frame")
    g.add_argument("--keep", action="store_true", help="keep holding what was held before, plus --hold")
    g.add_argument("--key", action="append", help="tap a raw key: A, Space@3, Enter@1:5")
    g.add_argument("--key-hold", action="append", help="raw keys held for the whole command")
    g.add_argument("--mouse", action="append", help="click at screen x,y[,button][@frame]")
    p.add_argument("--quiet", action="store_true", help="only the header line, no look")
    p.add_argument("--view", action="append", help="views for the look after it (default: the session's)")


def add_parser(sub, common) -> None:
    sp = sub.add_parser("play", help="Claude plays the game: start a session, look, step, until, end")
    ps = sp.add_subparsers(dest="play_cmd", required=True)
    sess = argparse.ArgumentParser(add_help=False)
    sess.add_argument("--session", help="session id (default: the active one)")

    p = ps.add_parser("start", parents=[common], help="spawn the game frozen at frame 0")
    p.add_argument("goal", nargs="?", help="goal YAML (.ghostlap/goals/<name>.yaml)")
    p.add_argument("--scene", help="start scene (overrides the goal's start)")
    d = p.add_mutually_exclusive_group()
    d.add_argument("--headless", dest="display", action="store_const", const="headless", help="fastest, no screenshots")
    d.add_argument("--offscreen", dest="display", action="store_const", const="offscreen", help="xvfb when available (default)")
    d.add_argument("--window", dest="display", action="store_const", const="window", help="a visible window to watch")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rolling-shots", metavar="EVERY[:KEEP]|off", help="rolling screenshots, e.g. 300:10 (default 600:5)")
    p.set_defaults(play_fn=cmd_start)

    p = ps.add_parser("preflight", parents=[common], help="before the first session: loads? deterministic? raw-key input? (cached)")
    p.add_argument("--scene", help="the start scene to boot (default: the main scene)")
    p.add_argument("--force", action="store_true", help="rerun even if nothing changed")
    p.set_defaults(play_fn=cmd_preflight)

    p = ps.add_parser("look", parents=[common, sess], help="observe; no time passes")
    p.add_argument("--view", action="append", help="views: ui,player,near,map,grid,state,game (default: the profile's)")
    p.add_argument("--shot", action="store_true", help="also save a screenshot of the frozen frame")
    p.add_argument("--shot-width", type=int)
    p.add_argument("--radius", type=float, help="`near` radius in player heights")
    p.add_argument("--map-w", type=int)
    p.add_argument("--map-h", type=int)
    p.set_defaults(play_fn=cmd_look)

    p = ps.add_parser("step", parents=[common, sess], help="advance exactly N frames, then freeze")
    p.add_argument("-n", type=int, default=30)
    _input_flags(p)
    p.set_defaults(play_fn=cmd_step)

    p = ps.add_parser("until", parents=[common, sess], help='step until an expression is true (or "goal")')
    p.add_argument("expr")
    p.add_argument("--max", type=int, default=600)
    _input_flags(p)
    p.set_defaults(play_fn=cmd_until)

    p = ps.add_parser("eval", parents=[common, sess], help="read a value, e.g. node('HUD/Score').text")
    p.add_argument("expr")
    p.add_argument("--write", action="store_true", help="allow calls that change the game (logged as a setup cheat)")
    p.set_defaults(play_fn=cmd_eval)

    p = ps.add_parser("note", parents=[common, sess], help="annotate the current frame")
    p.add_argument("text", nargs="+")
    p.add_argument("--saw", help="the screenshot this note is about (evidence)")
    p.set_defaults(play_fn=cmd_note)

    p = ps.add_parser("status", parents=[common, sess], help="goal, budget, notes")
    p.set_defaults(play_fn=cmd_status)

    p = ps.add_parser("end", parents=[common, sess], help="close the game and record the verdict")
    p.add_argument("--verdict", required=True, choices=["pass", "fail", "blocked"])
    p.add_argument("--why", required=True)
    p.add_argument("--evidence", action="append", help="decisive frames or shots, e.g. f1260")
    p.add_argument("--video", action="store_true", help="also render the captioned video (needs a display and ffmpeg)")
    p.add_argument("--no-replay", action="store_true", help="skip the replay check")
    p.set_defaults(play_fn=cmd_end)

    p = ps.add_parser("replay", parents=[common], help="watch an ended session's final input line again (the exact repro)")
    p.add_argument("session", nargs="?", help="session id (default: the latest)")
    d = p.add_mutually_exclusive_group()
    d.add_argument("--window", dest="display", action="store_const", const="window", help="watch it (default)")
    d.add_argument("--headless", dest="display", action="store_const", const="headless")
    p.add_argument("--fast", action="store_true", help="as fast as possible instead of real time")
    p.set_defaults(play_fn=cmd_replay)

    p = ps.add_parser("video", parents=[common], help="render an ended session as a captioned MP4/GIF (Movie Maker + ffmpeg)")
    p.add_argument("session", nargs="?", help="session id (default: the latest)")
    p.add_argument("--full", action="store_true", help="every frame, not just highlights of a long session")
    p.set_defaults(play_fn=cmd_video)

    p = ps.add_parser("rewind", parents=[common, sess], help="restart and replay the input line to a frame; later inputs become an attempt")
    p.add_argument("--to", type=int, required=True, help="the frame to go back to")
    p.set_defaults(play_fn=cmd_rewind)

    p = ps.add_parser("config", parents=[common, sess], help="show settings, or change them live: shots.rolling.every=300")
    p.add_argument("set", nargs="*", metavar="key=value")
    p.set_defaults(play_fn=cmd_config)

    p = ps.add_parser("log-read", parents=[common], help="record that a session screenshot was read (the plugin's Read hook)")
    p.add_argument("path", nargs="?")
    p.add_argument("--hook", action="store_true", help="read the hook's JSON from stdin; never fails")
    p.set_defaults(play_fn=cmd_log_read)

    p = ps.add_parser("kill", parents=[common, sess], help="kill the session's Godot (--all: every session's)")
    p.add_argument("--all", action="store_true")
    p.set_defaults(play_fn=cmd_kill)

    sp.set_defaults(fn=run)


def run(args) -> int:
    if args.play_fn is cmd_log_read:
        return cmd_log_read(args, None)   # the path names its session; no project lookup
    return args.play_fn(args, find_project(args.project))
