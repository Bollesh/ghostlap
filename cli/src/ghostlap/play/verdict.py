"""Verdict rules and the session's artifacts (design §3.5), enforced by `play end`.

The final input line is the one the session ends on: a `rewind --to R` abandons every event
after R. Evidence has to be on the final line, because those are the only frames a replay
reproduces.

    pass     the goal's `success` true on a frame of the final line (machine); without a
             success expression, --evidence naming a shot taken at or after the last input
             and cited with `note --saw` (visual). An eval write makes it `pass (assisted)`.
    fail     a bug incident on the final line, or one of the goal's `fail_if` incidents.
             (Watch-backed contradictions come with `play watch`, item 2.5.) Confirmed when the
             replay check (replay.py) hits the same incident; otherwise `fail (unconfirmed)`.
    blocked  always accepted: budget used up, no route found, a missing capability.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..project import GhostlapError
from . import replay
from .session import Session, fps, read_jsonl

BUG_HINT = "error, crash, hang, out_of_bounds, player_lost"
TAP_FRAMES = 6   # a press shorter than this reads as a tap in the repro steps


class Refused(GhostlapError):
    def __init__(self, msg: str, hint: dict | None = None):
        super().__init__(msg)
        self.hint = hint


# ------------------------------------------------------------------ the final line

def split_line(events: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    """(events on the final line, events of each abandoned attempt). A rewind to R abandons
    every event after R logged before it."""
    final: list[dict] = []
    attempts: list[list[dict]] = []
    for e in events:
        if e["kind"] == "rewind":
            attempts.append([x for x in final if x["f"] > e["to"]])
            final = [x for x in final if x["f"] <= e["to"]]
        final.append(e)
    return final, attempts


def last_input(inputs: list[dict]) -> int:
    return max((e["f"] for e in inputs), default=0)


def _frame_of(ev: str) -> tuple[int, str | None]:
    """`f1260`, `1260`, `shots/f1260.png` or a path to one → (frame, shot file name or None)."""
    m = re.fullmatch(r"f?(\d+)", ev.strip())
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:^|[/\\])f(\d+)\.png$", ev.strip())
    if m:
        return int(m.group(1)), f"f{m.group(1)}.png"
    raise GhostlapError(f"--evidence {ev!r}: use a frame (f1260) or a shot (shots/f1260.png)")


# ------------------------------------------------------------------ the rules

def check(s: Session, verdict: str, evidence: list[str] | None, shoot=None) -> dict:
    """{verdict, basis, items, final_frame, last_input}, or raises Refused. `shoot()` takes a
    fresh shot of the frozen frame for the pre_verdict hint (None: the game is gone)."""
    m = s.meta
    goal = m.get("goal") or {}
    events = read_jsonl(s.dir / "events.jsonl")
    final, _ = split_line(events)
    inputs = read_jsonl(s.dir / "inputs.jsonl")
    end_f, last_in = m["frame"], last_input(inputs)
    cited = {Path(e["saw"]).name: e for e in final if e["kind"] == "note" and e.get("saw")}
    items = []
    for ev in evidence or []:
        f, shot = _frame_of(ev)
        if f > end_f:
            raise Refused(f"--evidence {ev}: f={f} isn't on the final input line, which ends at f={end_f}"
                          + (" (a rewind abandoned it)" if any(a["to"] < f <= a["from"] for a in m.get("attempts") or [])
                             else ""))
        if shot and not (s.dir / "shots" / shot).is_file():
            raise Refused(f"--evidence {ev}: no such shot on the final input line (shots are in {s.dir / 'shots'})")
        items.append({"f": f, "what": "shot" if shot else "frame", **({"shot": f"shots/{shot}"} if shot else {}),
                      **({"note": cited[shot]["text"]} if shot in cited else {})})
    out = {"verdict": verdict, "basis": None, "items": items, "final_frame": end_f, "last_input": last_in}

    if verdict == "pass":
        if goal.get("success"):
            if m.get("goal_met_at") is None:
                raise Refused(f"pass needs the goal's success check true on the final input line, and it never was "
                              f"(`{goal['success']}`). Keep playing (`until goal`), or end with fail or blocked.")
            out["basis"] = "machine"
            items.insert(0, {"f": m["goal_met_at"], "what": "success", "expr": goal["success"]})
        else:
            shots = [i for i in items if i.get("shot")]
            good = [i for i in shots if i["f"] >= last_in and Path(i["shot"]).name in cited]
            if not good:
                raise _pre_verdict(s, shots, cited, last_in, shoot)
            out["basis"] = "visual"
        if m.get("cheats"):
            out["verdict"] = "pass (assisted)"
            out["cheats"] = m["cheats"]
    elif verdict == "fail":
        fail_if = {str(x).split(".", 1)[1] for x in goal.get("fail_if") or [] if str(x).startswith("incident.")}
        bad = [e for e in final if e["kind"] == "incident" and (e.get("class") == "bug" or e["type"] in fail_if)]
        if not bad:
            signals = sorted({e["type"] for e in final if e["kind"] == "incident"})
            raise Refused("fail needs a bug incident on the final input line (" + BUG_HINT + ")"
                          + (" or one of the goal's fail_if incidents" if fail_if else "")
                          + (f"; this line has only signals ({', '.join(signals)}), which aren't bugs by themselves"
                             if signals else "; there are none")
                          + ". A contradiction you can show with watches needs `play watch` (not built yet). "
                            "If the game isn't shown to be wrong, the verdict is blocked.")
        out["basis"] = "incident"
        out["confirmed"] = None   # set by confirm() after the replay check
        for e in bad:
            items.append({"f": e["f"], "what": "incident", "type": e["type"], "why": e.get("why", ""),
                          **({"shot": s.rel(e["shot"])} if e.get("shot") else {})})
    else:
        out["basis"] = "blocked"
    return out


def _pre_verdict(s: Session, shots: list[dict], cited: dict, last_in: int, shoot) -> Refused:
    msg = (f"pass without a success expression needs --evidence naming a screenshot taken at or after the "
           f"last input (f={last_in}) and cited with `play note --saw`")
    if shots:
        early = [i["shot"] for i in shots if i["f"] < last_in]
        uncited = [i["shot"] for i in shots if Path(i["shot"]).name not in cited]
        if early:
            msg += f"; {', '.join(early)} is from before the last input"
        if uncited:
            msg += f"; {', '.join(uncited)} isn't cited"
    if s.meta.get("headless"):
        return Refused(msg + ". A headless session has no screenshots: give the goal a success expression, "
                             "or end with blocked and retry with --offscreen.")
    sh = shoot() if shoot else None
    if not sh or not sh.get("path"):
        return Refused(msg + ".")
    rel = s.rel(sh["path"])
    s._event(s.meta["frame"], "shot_hint", reason="pre_verdict", path=rel, why="pass with no machine check")
    hint = {"reason": "pre_verdict", "path": sh["path"], "why": "pass with no machine check"}
    return Refused(msg + f".\nSHOT HINT pre_verdict: read {sh['path']}. If it shows the goal reached: "
                         f"`ghostlap play note --saw {rel} \"<what it shows>\"`, then "
                         f"`ghostlap play end --verdict pass --why … --evidence {rel}`", hint)


# ------------------------------------------------------------------ artifacts

def confirm(res: dict, rep: dict | None) -> None:
    """A fail is confirmed when the replay hits one of its incidents again."""
    if res["basis"] != "incident":
        return
    if rep is None:
        res["verdict"] = "fail (unconfirmed)"
        return
    hit = [i for i in res["items"] if i["what"] == "incident" and replay.confirms(rep, i)]
    res["confirmed"] = bool(hit)
    if not hit:
        res["verdict"] = "fail (unconfirmed)"


def hud(s: Session, res: dict, why: str) -> tuple[dict, list[int]]:
    """The video's caption data (hud.gd) and the frames worth keeping in a highlight video."""
    m = s.meta
    goal = m.get("goal") or {}
    final, _ = split_line(read_jsonl(s.dir / "events.jsonl"))
    notes = [{"f": e["f"], "text": e["text"]} for e in final if e["kind"] == "note"]
    incs = [{"f": e["f"], "type": e["type"], "why": e.get("why", "")} for e in final if e["kind"] == "incident"]
    data = {"goal": goal.get("goal") or m["scene"], "describe": goal.get("describe", ""), "verdict": res["verdict"],
            "basis": res["basis"], "why": why, "tps": fps(m), "final": res["final_frame"],
            "inputs": read_jsonl(s.dir / "inputs.jsonl"), "notes": notes, "incidents": incs,
            "goal_met_at": m.get("goal_met_at")}
    moments = [n["f"] for n in notes] + [i["f"] for i in incs] + [it["f"] for it in res["items"]]
    if m.get("goal_met_at") is not None:
        moments.append(m["goal_met_at"])
    return data, moments


def write(s: Session, res: dict, why: str, samples: list[dict] | None, rep: dict | None = None) -> None:
    """evidence.json, state.jsonl (when the game was still running) and report.md."""
    if samples is not None:
        with open(s.dir / "state.jsonl", "w") as fh:
            for f, smp in enumerate(samples):
                if smp:
                    fh.write(json.dumps({"f": f, **smp}) + "\n")
    state = {}
    if (s.dir / "state.jsonl").is_file():
        state = {r["f"]: r for r in read_jsonl(s.dir / "state.jsonl")}
    for it in res["items"]:
        if it["f"] in state:
            it["state"] = {k: v for k, v in state[it["f"]].items() if k != "f"}
        if it.get("shot"):
            s.pin_shot(Path(it["shot"]))
    m = s.meta
    ev = {"session": s.id, "goal": (m.get("goal") or {}).get("goal"), "verdict": res["verdict"], "why": why,
          "basis": res["basis"], "final_line": {"ends_at": res["final_frame"], "last_input": res["last_input"],
                                                "inputs": len(read_jsonl(s.dir / "inputs.jsonl"))},
          "evidence": res["items"]}
    if "confirmed" in res:
        ev["confirmed"] = res["confirmed"]
    if rep is not None:
        ev["replay"] = rep
    if res.get("cheats"):
        ev["cheats"] = res["cheats"]
    (s.dir / "evidence.json").write_text(json.dumps(ev, indent=1))
    (s.dir / "report.md").write_text(report(s, res, why, rep))


def load(s: Session) -> tuple[dict, str, dict | None]:
    """(res, why, replay check) of an ended session, from its evidence.json."""
    p = s.dir / "evidence.json"
    if not p.is_file():
        raise GhostlapError(f"session {s.id} has no evidence.json: end it first (`ghostlap play end --verdict …`)")
    ev = json.loads(p.read_text())
    res = {"verdict": ev["verdict"], "basis": ev["basis"], "items": ev["evidence"], "final_frame": ev["final_line"]["ends_at"],
           "last_input": ev["final_line"]["last_input"]}
    for k in ("confirmed", "cheats"):
        if k in ev:
            res[k] = ev[k]
    return res, ev["why"], ev.get("replay")


def repro_steps(inputs: list[dict], tps: int) -> list[str]:
    """The input line as phrases: `hold move_right 0.02–2.00 s`, `tap jump at 1.40 s`."""
    def t(f: int) -> str:
        return f"{f / tps:.2f}"

    def name_of(e: dict) -> str:
        return e.get("action") or f"key {e.get('key')}"

    # a release and a press of the same input on one frame (a hold the bridge pressed again after
    # a focus change) is one continuous hold
    skip = set()
    for i in range(len(inputs) - 1):
        a, b = inputs[i], inputs[i + 1]
        if "mouse" not in a and not a["pressed"] and b.get("pressed") and a["f"] == b["f"] and name_of(a) == name_of(b):
            skip |= {i, i + 1}
    down: dict[str, int] = {}
    spans = []
    for i, e in enumerate(inputs):
        if i in skip:
            continue
        if "mouse" in e:
            if e["pressed"]:
                x, y = e["mouse"]
                spans.append((e["f"], f"click at ({x:g}, {y:g}) at {t(e['f'])} s"))
            continue
        name = name_of(e)
        if e["pressed"]:
            down[name] = e["f"]
        elif name in down:
            a = down.pop(name)
            spans.append((a, f"tap {name} at {t(a)} s" if e["f"] - a < TAP_FRAMES else f"hold {name} {t(a)}–{t(e['f'])} s"))
    for name, a in down.items():
        spans.append((a, f"hold {name} from {t(a)} s to the end"))
    return [p for _, p in sorted(spans, key=lambda x: x[0])]


def report(s: Session, res: dict, why: str, rep: dict | None = None, video: dict | None = None) -> str:
    m = s.meta
    goal = m.get("goal") or {}
    tps = fps(m)
    events = read_jsonl(s.dir / "events.jsonl")
    final, abandoned = split_line(events)
    inputs = read_jsonl(s.dir / "inputs.jsonl")
    basis = {"machine": f"machine-checked: `{goal.get('success')}` true at f={m.get('goal_met_at')}",
             "visual": "visual: a cited screenshot after the last input",
             "incident": "a bug incident on the final input line, " + (
                 "confirmed: the replay hit it again" if res.get("confirmed") else
                 "unconfirmed: the replay didn't hit it again" if res.get("confirmed") is False else
                 "unconfirmed: no replay check ran"),
             "blocked": "not a game bug: the session couldn't decide"}[res["basis"]]
    out = [f"# Playtest: {goal.get('goal') or m['scene']} — {res['verdict'].upper()}", ""]
    if goal.get("describe"):
        out += [f"> {goal['describe']}", ""]
    out += [f"**Verdict:** {res['verdict']} ({basis})  ",
            f"**Why:** {why}  ",
            f"**Session:** `{s.id}` · {m['scene']} · Godot {m.get('godot', '?')} · {tps} tps · seed {m['seed']} · "
            f"{m['display']} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(m['created']))}", ""]

    if video and video.get("gif"):
        out += [f"![playtest]({Path(video['gif']).name})", ""]
    out += ["## Evidence", ""]
    if not res["items"]:
        out.append("None given.")
    for it in res["items"]:
        line = f"- **f={it['f']}** ({it['f'] / tps:.2f} s) {it['what']}"
        if it.get("expr"):
            line += f": `{it['expr']}` became true"
        if it.get("type"):
            line += f": {it['type']} — {it.get('why', '')}"
        if it.get("note"):
            line += f": {it['note']}"
        if it.get("state"):
            line += f" · player {json.dumps(it['state'], separators=(',', ':'))}"
        out.append(line)
        if it.get("shot"):
            out.append(f"\n  ![f{it['f']}]({it['shot']})")
    if res.get("cheats"):
        out += ["", "Setup cheats on the final line (why it's assisted): "
                + "; ".join(f"f={c['f']} `{c['eval']}`" for c in res["cheats"])]

    out += ["", "## Repro", ""]
    steps = repro_steps(inputs, tps)
    end = f"the line ends at f={res['final_frame']} ({res['final_frame'] / tps:.2f} s)"
    if steps:
        out += [f"{i}. {p}" for i, p in enumerate(steps + [end], 1)]
    else:
        out.append(f"No inputs: the game ran by itself; {end}.")
    out += ["",
            f"Exact script: `inputs.jsonl` ({len(inputs)} event{'s' if len(inputs) != 1 else ''}, frame-exact at --fixed-fps {tps}). "
            f"Watch it: `ghostlap play replay {s.id}` (real time, in a window)."]
    out += ["", "## Replay check", ""]
    if rep is None:
        out.append("Not run.")
    else:
        line = f"**{rep['result']}** · {rep['mode']} replay of {rep['frames']} frames in {rep['seconds']} s"
        if rep["result"] == "exact":
            line += ": every frame's state hash matches the session, so this run can become a test."
        elif rep["result"] == "diverged":
            line += (f": the first different frame is f={rep['diverged_at']} ({rep['field']}). "
                     "The game isn't deterministic from these inputs, so no test can be made from it.")
            if rep.get("likely_causes"):
                line += " Likely causes (preflight): " + "; ".join(rep["likely_causes"])
        else:
            line += f": {rep.get('why', '')}"
        out.append(line)
        if video:
            out.append(f"\nVideo: `{Path(video['mp4']).name}`, `{Path(video['gif']).name}`"
                       + (f", contact sheet `{Path(video['sheet']).name}`" if video.get("sheet") else ""))

    incs = [e for e in final if e["kind"] == "incident"]
    out += ["", "## Incidents", ""]
    out += [f"- f={e['f']} **{e['type']}** ({e.get('class')}): {e.get('why', '')}" for e in incs] or ["None on the final line."]

    if abandoned:
        out += ["", "## Attempts", ""]
        for a, evs in zip(m.get("attempts") or [], abandoned):
            kinds = sorted({e["type"] for e in evs if e["kind"] == "incident"})
            notes = [e["text"] for e in evs if e["kind"] == "note"]
            out.append(f"- attempt {a['n']}: played on to f={a['from']} ({a['inputs']} inputs), rewound to f={a['to']}"
                       + (f"; incidents: {', '.join(kinds)}" if kinds else "")
                       + (f"; notes: {' / '.join(notes)}" if notes else "")
                       + f" (`attempts/{a['n']}.jsonl`)")

    out += ["", "## Timeline", ""]
    for e in final:
        k = e["kind"]
        if k == "note":
            out.append(f"- f={e['f']} note: {e['text']}" + (f" (saw `{e['saw']}`)" if e.get("saw") else ""))
        elif k == "goal_met":
            out.append(f"- f={e.get('at', e['f'])} goal met")
        elif k == "incident":
            out.append(f"- f={e['f']} incident: {e['type']}")
        elif k == "rewind":
            out.append(f"- rewound to f={e['to']} (attempt {e['attempt']} abandoned)"
                       + (f", replay diverged at f={e['diverged_at']}" if e.get("diverged_at") is not None else ""))
        elif k == "write":
            out.append(f"- f={e['f']} eval write: `{e['expr']}`")
        elif k == "ui_shown":
            out.append(f"- f={e['f']} UI: {e['node']}" + (f" ({e['text']})" if e.get("text") else ""))

    u, b = m["used"], m["budget"]
    shots = {e["path"] for e in events if e["kind"] == "shot"}
    reads = [e["reason"] for e in events if e["kind"] == "shot_read"]
    out += ["", "## Budget", "",
            f"- frames {u['frames']}/{b['frames']}, commands {u['commands']}/{b['commands']}, "
            f"requested shots {u['shots']}/{b['shots']}, ~{u['bytes'] // 4} tokens returned",
            f"- screenshots: {len(shots)} captured, {len(reads)} read"
            + (" (" + ", ".join(f"{r} ×{reads.count(r)}" for r in sorted(set(reads))) + ")" if reads else "")]
    return "\n".join(out) + "\n"
