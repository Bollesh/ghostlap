"""A play session: the goal, budget, input line and log around one running bridge.

State lives in .ghostlap/play/<id>/ so every `ghostlap play …` call is a fresh process:

    session.json   goal, start, mode, budget and its use, held inputs, verdict
    inputs.jsonl   the input line: {"f": frame the game first sees it, "action"|"key"|"mouse": …, "pressed": …}
    events.jsonl   notes, incidents, shots and shot hints, shot reads, eval writes, refusals, the end
    last_look.json views last shown, so `step` can collapse what didn't change
    bridge.json    port/token/pid of the running Godot (written by bridge.gd)
    shots/         f<frame>.png captures; keep.txt lists the ones rolling rotation must keep
    attempts/<n>.jsonl  inputs a `rewind` took off the line; attempts/<n>/ its shots and godot.log
    hashes.jsonl, state.jsonl, evidence.json, report.md   written by `end` (verdict.py)

.ghostlap/play/active names the session commands act on by default.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..project import GhostlapError, Project
from . import config as cfgmod
from .bridge_client import Bridge, BridgeError, BridgeHang

DEFAULT_BUDGET = {"frames": 36000, "commands": 120, "shots": 15}
# commands that never count against the budget (they're how a session ends or recovers)
FREE = {"status", "end", "kill"}


def play_dir(project: Project) -> Path:
    d = project.state / "play"
    d.mkdir(exist_ok=True)
    return d


def load_goal(path: Path) -> dict:
    try:
        goal = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise GhostlapError(f"cannot read goal {path}: {e}") from e
    if not isinstance(goal, dict) or not goal.get("goal"):
        raise GhostlapError(f"{path}: a goal file needs at least `goal: <name>` (see design §3.1)")
    unknown = set(goal) - {"goal", "describe", "start", "success", "fail_if", "budget", "constraints", "shots"}
    if unknown:
        raise GhostlapError(f"{path}: unknown keys {sorted(unknown)}")
    bad = set(goal.get("budget") or {}) - set(DEFAULT_BUDGET)
    if bad:
        raise GhostlapError(f"{path}: unknown budget keys {sorted(bad)} (use {sorted(DEFAULT_BUDGET)})")
    cfgmod.from_goal(goal.get("shots"), str(path))   # validate now, not at start
    return goal


def main_scene(project: Project) -> str:
    m = re.search(r'run/main_scene="([^"]+)"', (project.root / "project.godot").read_text())
    if not m:
        raise GhostlapError("project.godot has no run/main_scene; give the goal a start: {scene: res://…}")
    return m.group(1)


def log_summary(log: Path, n: int = 15) -> str:
    """The end of godot.log as evidence: errors, the crash signal and the GDScript backtrace,
    without the C++ frames and banners that push them out of a plain tail."""
    try:
        lines = log.read_text(errors="replace").splitlines()[-200:]
    except OSError:
        return "(no log)"
    noise = re.compile(r"^\[\d+\] |^=+$|^-- END OF|^Dumping the backtrace|^Load address|^Engine version|^\s*$")
    return "\n".join([ln for ln in lines if not noise.search(ln)][-n:])


# ------------------------------------------------------------------ inputs

@dataclass
class Tap:
    kind: str   # action | key | mouse
    what: object
    at: int     # step-relative frame of the press (1-based)
    hold: int   # frames held


def _split(values: list[str] | None) -> list[str]:
    return [v.strip() for x in values or [] for v in x.split(",") if v.strip()]


def parse_tap(spec: str, kind: str) -> Tap:
    """`jump`, `jump@5`, `jump@5:12` (press on step frame 5, hold 12 frames)."""
    m = re.fullmatch(r"([^@:]+?)(?:@(\d+))?(?::(\d+))?", spec.strip())
    if not m:
        raise GhostlapError(f"bad --{'press' if kind == 'action' else kind} {spec!r}: use name[@frame][:frames]")
    return Tap(kind, m.group(1), int(m.group(2) or 1), int(m.group(3) or 1))


def parse_click(spec: str) -> Tap:
    """`x,y` or `x,y@5` (press on step frame 5, release the next frame)."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)(?:\s*,\s*(\d))?(?:@(\d+))?\s*", spec)
    if not m:
        raise GhostlapError(f"bad --mouse {spec!r}: use x,y[,button][@frame]")
    return Tap("mouse", ([float(m.group(1)), float(m.group(2))], int(m.group(3) or 1)), int(m.group(4) or 1), 1)


def _event(kind: str, what, pressed: bool) -> dict:
    if kind == "mouse":
        pos, btn = what
        return {"mouse": pos, "button": btn, "pressed": pressed}
    return {kind: what, "pressed": pressed}


def _hkey(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# ------------------------------------------------------------------ shot hints

HINT_ORDER = ["first_look", "scene_changed", "repeat_death", "expectation_missed", "ui_without_text", "unclassified_nearby"]
UNKNOWN_GAP = 300   # frames between unclassified_nearby hints, unless the scene changed


def unknown_keys(views: dict) -> list[str]:
    """Things the text state couldn't classify: `?` cells in map/grid (keyed by world cell, so the
    same thing seen from elsewhere isn't new) and `near` entries of kind unknown."""
    out = []
    for name, v in views.items():
        if name == "near" and isinstance(v, list):
            out += [f"near:{e['name']}" for e in v if isinstance(e, dict) and e.get("kind") == "unknown"]
        elif isinstance(v, dict) and isinstance(v.get("rows"), list):
            cell = v.get("cell_px") or [1, 1]
            origin = v.get("origin") or [0, 0]
            ocell = v.get("origin_cell")
            for y, row in enumerate(v["rows"]):
                for x, ch in enumerate(row):
                    if ch != "?":
                        continue
                    if ocell is not None:
                        out.append(f"{name}:{ocell[0] + x},{ocell[1] + y}")
                    elif len(cell) == 2 and len(origin) == 2:
                        out.append(f"{name}:{round(origin[0] / cell[0]) + x},{round(origin[1] / cell[1]) + y}")
    return out


# ------------------------------------------------------------------ session

class Session:
    def __init__(self, project: Project, sid: str):
        self.project = project
        self.id = sid
        self.dir = play_dir(project) / sid
        self.meta: dict = json.loads((self.dir / "session.json").read_text())

    # ---------------------------------------------------------- lifecycle

    @classmethod
    def start(cls, project: Project, godot: str, *, goal: dict | None, scene: str | None, display: str,
              seed: int, profile: dict | None, budget: dict | None = None,
              flags: dict | None = None) -> tuple["Session", dict]:
        """flags: settings from `play start` options (config keys, e.g. from --rolling-shots)."""
        active = cls.active(project, required=False)
        if active and active.alive():
            raise GhostlapError(f"session {active.id} is still running — `ghostlap play end` or `ghostlap play kill` it first")
        if active and active.meta.get("status") == "running":
            active.mark_dead("found dead when the next session started")
        goal = goal or {}
        start = goal.get("start") or {}
        if scene:
            start = {"scene": scene}
        if not start:
            raise GhostlapError("nothing to start: give a goal file with start: {scene: …} or --scene res://…")
        start_scene = main_scene(project) if start.get("main") else start.get("scene")
        if not start_scene:
            raise GhostlapError("goal start needs scene: res://… or main: true")
        base = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^A-Za-z0-9_-]+", "_", str(goal.get("goal") or "adhoc"))
        sid, n = base, 1
        while (play_dir(project) / sid).exists():
            n += 1
            sid = f"{base}-{n}"
        d = play_dir(project) / sid
        d.mkdir(parents=True)
        bud = {**DEFAULT_BUDGET, **(goal.get("budget") or {}), **(budget or {})}
        cfg = cfgmod.resolve([(cfgmod.CFG_FILE, cfgmod.from_cfg(project.root)), ("goal", cfgmod.from_goal(goal.get("shots"))),
                              ("play start", flags or {})])
        meta = {"id": sid, "goal": goal, "scene": start_scene, "display": display, "seed": seed, "created": time.time(),
                "frame": 0, "held": [], "pending": [], "budget": bud,
                "used": {"frames": 0, "commands": 1, "shots": 0, "bytes": 0}, "goal_met_at": None,
                "cheats": [], "status": "running", "config": cfg,
                "hints": {"unknown": [], "unknown_at": None, "scene": None}}
        (d / "session.json").write_text(json.dumps(meta, indent=1))
        (play_dir(project) / "active").write_text(sid)
        s = cls(project, sid)
        try:
            bridge = s._boot(godot, profile, cfgmod.bridge_shots(cfg))
        except (BridgeError, GhostlapError):
            s.meta["status"] = "failed_to_start"
            s.save()
            (play_dir(project) / "active").unlink(missing_ok=True)
            raise
        s.meta.update({"godot": bridge.info.get("godot"), "physics_tps": bridge.info.get("physics_tps"),
                       "headless": bool(bridge.info.get("headless"))})
        if s.meta["headless"]:
            s.log("shots_unavailable", why="headless: no renderer, so no screenshots or shot hints")
        # errors while the scene loaded (frame 0)
        s.meta["load_incidents"] = bridge.request("incidents")["result"]
        for inc in s.meta["load_incidents"]:
            s.incident(inc)
        s.save()
        return s, s.look_raw()

    def _boot(self, godot: str, profile: dict | None, shots: dict, first: bool = True) -> Bridge:
        """Launch Godot on the session's start scene, frozen at frame 0, with the goal check on.
        first: the session's first launch (a rewind's relaunch doesn't log the goal error again)."""
        m = self.meta
        bridge = Bridge.launch(godot, self.project.root, m["scene"], self.dir, seed=m["seed"], display=m["display"],
                               profile=profile, shots=shots)
        success = (m.get("goal") or {}).get("success")
        if success:
            try:
                res = bridge.request("config", goal=success)["result"]
            except BridgeError as e:
                bridge.quit()
                raise GhostlapError(f"the goal's success expression doesn't compile: {e}") from e
            if res.get("goal_error") and first:
                self.log("goal_error", error=res["goal_error"])
        return bridge

    @classmethod
    def active(cls, project: Project, sid: str | None = None, required: bool = True) -> "Session | None":
        if sid is None:
            pointer = play_dir(project) / "active"
            sid = pointer.read_text().strip() if pointer.is_file() else None
        if not sid or not (play_dir(project) / sid / "session.json").is_file():
            if required:
                raise GhostlapError("no active play session — `ghostlap play start <goal.yaml>`")
            return None
        return cls(project, sid)

    def save(self) -> None:
        tmp = self.dir / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=1))
        tmp.replace(self.dir / "session.json")

    @property
    def bridge(self) -> Bridge:
        return Bridge.attach(self.dir)

    def alive(self) -> bool:
        try:
            return self.meta.get("status") == "running" and self.bridge.alive()
        except BridgeError:
            return False

    def mark_dead(self, error: str) -> str:
        """The Godot process is gone: an idle timeout (the bridge quit by itself) or a crash."""
        try:
            idle = "idle limit reached" in (self.dir / "godot.log").read_text(errors="replace")
        except OSError:
            idle = False
        self.meta["status"] = "abandoned" if idle else "crashed"
        self.log(self.meta["status"], error=error)
        if not idle:
            self.incident({"f": self.meta.get("frame", 0), "type": "crash", "class": "bug",
                           "why": "the Godot process died", "log_tail": log_summary(self.dir / "godot.log")})
        self.save()
        if idle:
            return (f"the session sat idle for 30 minutes and its game closed; end it with "
                    f"`ghostlap play end --verdict blocked --why abandoned`, then start a new one")
        return (f"CRASH: Godot is gone: {error}\n{log_summary(self.dir / 'godot.log', 8)}\n"
                f"log: {self.dir / 'godot.log'} — end the session with `ghostlap play end --verdict fail --why …`")

    def incident(self, inc: dict) -> None:
        counts = self.meta.setdefault("incidents", {})
        counts[inc["type"]] = counts.get(inc["type"], 0) + 1
        with open(self.dir / "events.jsonl", "a") as f:
            f.write(json.dumps({"f": inc["f"], **{k: v for k, v in inc.items() if k != "f"}, "kind": "incident"}) + "\n")

    def log(self, kind: str, **data) -> None:
        with open(self.dir / "events.jsonl", "a") as f:
            f.write(json.dumps({"f": self.meta.get("frame", 0), "kind": kind, **data}) + "\n")

    # ---------------------------------------------------------- budget

    def count(self, cmd: str) -> None:
        if cmd not in FREE:
            self.meta["used"]["commands"] += 1

    def frames_left(self) -> int:
        return self.meta["budget"]["frames"] - self.meta["used"]["frames"]

    def check_can_step(self, frames: int | None) -> None:
        """Stepping is refused once any budget is used up; look/note/end still work."""
        u, b = self.meta["used"], self.meta["budget"]
        why = None
        if u["frames"] >= b["frames"]:
            why = f"the frame budget is used up ({b['frames']} frames)"
        elif u["commands"] > b["commands"]:
            why = f"the command budget is used up ({b['commands']} commands)"
        elif frames is not None and frames > self.frames_left():
            why = f"only {self.frames_left()} frames of budget left (asked for {frames})"
        if why:
            self.log("refused", why=why)
            self.save()
            raise GhostlapError(f"{why}. Only look, note, status and `end --verdict blocked|fail` are accepted now.")

    def take_shot(self) -> None:
        if self.meta["used"]["shots"] >= self.meta["budget"]["shots"]:
            raise GhostlapError(f"the screenshot budget is used up ({self.meta['budget']['shots']}); look without --shot")
        self.meta["used"]["shots"] += 1

    # ---------------------------------------------------------- observe

    def look_raw(self, views: list[str] | None = None, shot: bool = False, **opts) -> dict:
        """Look with the session's look.* settings for whatever isn't given."""
        cfg = self.meta.get("config") or {}
        views = views or cfgmod.value(cfg, "look.views")
        for opt, key in (("radius", "look.radius"), ("map_w", "look.map_w"), ("map_h", "look.map_h")):
            opts.setdefault(opt, cfgmod.value(cfg, key))
        res = self.bridge.look(views=views, shot=shot, **opts)
        self._sync(res)
        return res

    def config(self) -> dict:
        return self.meta.get("config") or cfgmod.resolve([])

    def _sync(self, res: dict) -> None:
        self.meta["frame"] = res["f"]
        if res.get("goal_met_at", -1) >= 0 and self.meta.get("goal_met_at") is None:
            self.meta["goal_met_at"] = res["goal_met_at"]
            self.log("goal_met", at=res["goal_met_at"])
        for sh in res.get("new_shots") or []:
            self._event(sh["f"], "shot", path=self.rel(sh["path"]), why=sh["why"])
        for ui in res.get("new_ui") or []:
            self._event(ui["f"], "ui_shown", **{k: (self.rel(v) if k == "shot" else v) for k, v in ui.items() if k != "f"})

    def rel(self, path: str) -> str:
        p = Path(path)
        return str(p.relative_to(self.dir)) if p.is_relative_to(self.dir) else str(p)

    def _event(self, f: int, kind: str, **data) -> None:
        with open(self.dir / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"f": f, "kind": kind, **data}) + "\n")

    def pin_shot(self, path: Path) -> None:
        """Rolling rotation never deletes a shot that was cited or read."""
        keep = self.dir / "shots" / "keep.txt"
        if keep.parent.is_dir():
            with open(keep, "a") as fh:
                fh.write(Path(path).name + "\n")

    # ---------------------------------------------------------- shot hints (design §3.3.1)

    def shot_hint(self, responses: list[dict], views: dict | None = None, until: dict | None = None) -> dict | None:
        """The one screenshot Claude should read before acting, when the text state is likely
        wrong or incomplete: {reason, path, why, also}. `responses` are this command's bridge
        results (a step and the look after it); `views` the full (uncollapsed) views shown."""
        if self.meta.get("headless"):
            return None
        h = self.meta.setdefault("hints", {"unknown": [], "unknown_at": None, "scene": None})
        found: list[tuple[str, str | None, str]] = []
        shots = [sh for r in responses for sh in r.get("new_shots") or []]

        def shot_for(trigger: str) -> str | None:
            return next((sh["path"] for sh in reversed(shots) if trigger in sh["why"]), None)

        last = responses[-1]
        if last["f"] == 0 and h.get("scene") is None:
            found.append(("first_look", shot_for("start"), "the first frame: see the art style and layout once"))
        elif h.get("scene") not in (None, last.get("scene")):
            found.append(("scene_changed", shot_for("scene_change"), f"now in {last.get('scene')}"))
        scene_changed = h.get("scene") != last.get("scene")
        h["scene"] = last.get("scene")
        for r in responses:
            for inc in r.get("incidents") or []:
                if inc["type"] == "death" and "repeat_of" in inc:
                    found.append(("repeat_death", inc.get("shot"), f"died again where it died at f={inc['repeat_of']}"))
                elif inc["type"] == "stuck":
                    found.append(("expectation_missed", inc.get("shot"), "stuck: movement held but the player isn't moving"))
            for ui in r.get("new_ui") or []:
                if ui.get("textless"):
                    found.append(("ui_without_text", ui.get("shot"), f"{ui['node']} appeared with no readable text"))
        if until and until.get("reason") == "max":
            found.append(("expectation_missed", None, "until reached --max without its condition"))
        unknown = [k for k in unknown_keys(views or {}) if k not in h["unknown"]]
        if unknown and (scene_changed or h.get("unknown_at") is None or last["f"] - h["unknown_at"] >= UNKNOWN_GAP):
            h["unknown"] = sorted(set(h["unknown"]) | set(unknown))[-500:]
            h["unknown_at"] = last["f"]
            found.append(("unclassified_nearby", None, f"{len(unknown)} unclassified thing(s) nearby: " + ", ".join(unknown[:4])))
        if not found:
            return None
        found.sort(key=lambda x: HINT_ORDER.index(x[0]))
        reason, path, why = found[0]
        if path is None:
            sh = self.bridge.shot(why="hint")
            self._sync_shot(sh)
            path = sh.get("path")
            if path is None:
                return None
        hint = {"reason": reason, "path": path, "why": why}
        also = sorted({r for r, _, _ in found[1:] if r != reason}, key=HINT_ORDER.index)
        if also:
            hint["also"] = also
        self._event(last["f"], "shot_hint", reason=reason, path=self.rel(path), why=why)
        return hint

    def _sync_shot(self, sh: dict) -> None:
        for x in sh.get("new_shots") or []:
            self._event(x["f"], "shot", path=self.rel(x["path"]), why=x["why"])

    def collapse(self, views: dict) -> dict:
        """Replace views identical to the last ones shown with "unchanged"."""
        p = self.dir / "last_look.json"
        prev = json.loads(p.read_text()) if p.is_file() else {}
        p.write_text(json.dumps(views))
        return {k: ("unchanged" if prev.get(k) == v else v) for k, v in views.items()}

    def remember(self, views: dict) -> None:
        (self.dir / "last_look.json").write_text(json.dumps(views))

    # ---------------------------------------------------------- stepping

    def plan_inputs(self, frames: int, *, hold: list[str], key_hold: list[str], press: list[str], keys: list[str],
                    release: list[str], mouse: list[str], keep: bool) -> tuple[list[dict], list[str], list[dict]]:
        """Bridge inputs (step-relative) for one stepping command, the held set afterwards, and
        the taps ({kind, what, at, up}); a tap released after the command ends carries its
        release over to the next one.

        Holds are declarative: the actions in --hold / --key-hold are held for the whole command,
        and anything held before but not listed is released on its first frame (--keep keeps them).
        """
        want = [_hkey("action", a) for a in _split(hold)] + [_hkey("key", k) for k in _split(key_hold)]
        before = list(self.meta.get("held") or [])
        if keep:
            want = before + [w for w in want if w not in before]
        for r in _split(release):
            want = [w for w in want if w not in (_hkey("action", r), _hkey("key", r))]
        taps = [parse_tap(p, "action") for p in _split(press)] + [parse_tap(k, "key") for k in _split(keys)] \
            + [parse_click(m) for m in mouse or []]
        for t in taps:
            if t.kind != "mouse" and _hkey(t.kind, t.what) in want:
                raise GhostlapError(f"{t.what} is both held and pressed; pick one")
            if not 1 <= t.at <= frames:
                raise GhostlapError(f"press of {t.what} at frame {t.at} is outside this command (1..{frames})")
        ev: list[dict] = []
        # releases carried from the previous command land on frame 1, unless now held
        for p in self.meta.get("pending") or []:
            if _hkey(p["kind"], p["what"]) not in want:
                ev.append({"at": 1, **_event(p["kind"], p["what"], False)})
        for h in before:
            if h not in want:
                kind, name = h.split(":", 1)
                ev.append({"at": 1, **_event(kind, name, False)})
        for h in want:
            if h not in before:
                kind, name = h.split(":", 1)
                ev.append({"at": 1, **_event(kind, name, True)})
        tap_list = []
        for t in taps:
            ev.append({"at": t.at, **_event(t.kind, t.what, True)})
            up = t.at + t.hold
            if up <= frames:
                ev.append({"at": up, **_event(t.kind, t.what, False)})
            tap_list.append({"kind": t.kind, "what": t.what, "at": t.at, "up": up})
        return ev, want, tap_list

    # ---------------------------------------------------------- rewind (design §3.5)

    def rewind(self, godot: str, to: int) -> dict:
        """Restart the game and replay the input line (and eval writes) up to frame `to`, then
        freeze there. Inputs after it move to attempts/<n>.jsonl, and the session goes on from
        `to` on a new line. Returns {look, attempt, diverged_at}: diverged_at is the first frame
        whose state hash differs from the first run (None: identical)."""
        m = self.meta
        if not 0 <= to <= m["frame"]:
            raise GhostlapError(f"--to {to}: rewind goes back, to a frame from 0 to {m['frame']}")
        old = self.bridge
        before = old.hashes()
        old.quit()
        n = len(m.get("attempts") or []) + 1
        adir = self.dir / "attempts" / str(n)
        adir.mkdir(parents=True)
        # the abandoned branch: its inputs, eval writes, shots and log
        line = read_jsonl(self.dir / "inputs.jsonl")
        kept, dropped = [e for e in line if e["f"] <= to], [e for e in line if e["f"] > to]
        write_jsonl(self.dir / "attempts" / f"{n}.jsonl", dropped)
        write_jsonl(self.dir / "inputs.jsonl", kept)
        cheats = [c for c in m.get("cheats") or [] if c["f"] <= to]
        for p in (self.dir / "shots").glob("f*.png"):
            if int(p.stem[1:]) > to:
                (adir / "shots").mkdir(exist_ok=True)
                p.replace(adir / "shots" / p.name)
        if (self.dir / "godot.log").is_file():
            (self.dir / "godot.log").replace(adir / "godot.log")
        m["attempts"] = (m.get("attempts") or []) + [{"n": n, "from": m["frame"], "to": to, "inputs": len(dropped),
                                                       "cheats": len(m.get("cheats") or []) - len(cheats)}]
        m["cheats"] = cheats
        prof = self.dir / "profile.json"
        profile = json.loads(prof.read_text()) if prof.is_file() else None
        # replay without captures: those frames' shots are already on disk
        quiet = {**cfgmod.bridge_shots(self.config()), "every": 0, "on": []}
        m["status"] = "running"
        b = self._boot(godot, profile, quiet, first=False)
        # an eval write at frame f ran after frame f, before the next one: replay it there
        at = 0
        for stop in sorted({c["f"] for c in cheats} | {to}):
            if stop > at:
                b.step(stop - at, [{"at": e["f"] - at, **{k: v for k, v in e.items() if k != "f"}}
                                   for e in kept if at < e["f"] <= stop])
                at = stop
            for c in cheats:
                if c["f"] == stop:
                    b.request("eval", expr=c["eval"], write=True)
        b.request("config", shots=cfgmod.bridge_shots(self.config()))
        after = b.hashes()
        diverged = next((f for f in range(to + 1) if f >= len(before) or f >= len(after) or before[f] != after[f]), None)
        # what was held at `to`
        down: dict[str, dict] = {}
        for e in kept:
            k = _hkey("action", e["action"]) if "action" in e else _hkey("key", e["key"]) if "key" in e \
                else f"mouse:{e.get('button', 1)}"
            if e["pressed"]:
                down[k] = e
            else:
                down.pop(k, None)
        m["held"] = [k for k in down if not k.startswith("mouse:")]
        m["pending"] = [{"kind": "mouse", "what": [e["mouse"], e.get("button", 1)]} for k, e in down.items()
                        if k.startswith("mouse:")]
        if m.get("goal_met_at") is not None and m["goal_met_at"] > to:
            m["goal_met_at"] = None
        m["hints"] = {**(m.get("hints") or {}), "scene": None}
        (self.dir / "last_look.json").unlink(missing_ok=True)
        look = self.look_raw()
        m["hints"]["scene"] = look.get("scene")
        self.log("rewind", to=to, attempt=n, diverged_at=diverged)
        self.save()
        return {"look": look, "attempt": m["attempts"][-1], "diverged_at": diverged}

    def run(self, cmd: str, frames: int, inputs: list[dict], held: list[str], taps: list[dict],
            expr: str | None = None) -> dict:
        start = self.meta["frame"]
        b = self.bridge
        try:
            res = b.until(expr, frames, inputs) if cmd == "until" else b.step(frames, inputs)
        except BridgeHang as e:
            b.kill()
            self.meta["status"] = "hung"
            self.incident({"f": e.frame if e.frame is not None else start, "type": "hang", "class": "bug", "why": str(e),
                           "log_tail": log_summary(self.dir / "godot.log")})
            self.save()
            raise GhostlapError(f"HANG during {cmd} from frame {start}: {e}. Godot was killed; the session is over "
                                f"(`ghostlap play end --verdict fail --why …`). log: {self.dir / 'godot.log'}") from e
        except BridgeError as e:
            if b.exited():
                raise GhostlapError(f"during {cmd} from frame {start}: " + self.mark_dead(str(e))) from e
            raise
        ran = res["f"] - start
        # the input line is what the game actually saw: an until that stopped early drops the rest
        with open(self.dir / "inputs.jsonl", "a") as f:
            for e in inputs:
                if e["at"] <= ran:
                    f.write(json.dumps({"f": start + e["at"], **{k: v for k, v in e.items() if k != "at"}}) + "\n")
        self.meta["held"] = held
        # pressed but not yet released: the release lands on the next command's first frame
        self.meta["pending"] = [{"kind": t["kind"], "what": t["what"]} for t in taps if t["at"] <= ran < t["up"]]
        self.meta["used"]["frames"] += ran
        for inc in res.get("incidents") or []:
            self.incident(inc)
        self._sync(res)
        return res
