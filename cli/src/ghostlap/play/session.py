"""A play session: the goal, budget, input line and log around one running bridge.

State lives in .ghostlap/play/<id>/ so every `ghostlap play …` call is a fresh process:

    session.json   goal, start, mode, budget and its use, held inputs, verdict
    inputs.jsonl   the input line: {"f": frame the game first sees it, "action"|"key"|"mouse": …, "pressed": …}
    events.jsonl   notes, incidents, eval writes, refusals, the end
    last_look.json views last shown, so `step` can collapse what didn't change
    bridge.json    port/token/pid of the running Godot (written by bridge.gd)

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
    unknown = set(goal) - {"goal", "describe", "start", "success", "fail_if", "budget", "constraints"}
    if unknown:
        raise GhostlapError(f"{path}: unknown keys {sorted(unknown)}")
    bad = set(goal.get("budget") or {}) - set(DEFAULT_BUDGET)
    if bad:
        raise GhostlapError(f"{path}: unknown budget keys {sorted(bad)} (use {sorted(DEFAULT_BUDGET)})")
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
              seed: int, profile: dict | None, budget: dict | None = None) -> tuple["Session", dict]:
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
        meta = {"id": sid, "goal": goal, "scene": start_scene, "display": display, "seed": seed, "created": time.time(),
                "frame": 0, "held": [], "pending": [], "budget": bud,
                "used": {"frames": 0, "commands": 1, "shots": 0, "bytes": 0}, "goal_met_at": None,
                "cheats": [], "status": "running"}
        (d / "session.json").write_text(json.dumps(meta, indent=1))
        (play_dir(project) / "active").write_text(sid)
        try:
            bridge = Bridge.launch(godot, project.root, start_scene, d, seed=seed, display=display, profile=profile)
        except BridgeError:
            meta["status"] = "failed_to_start"
            (d / "session.json").write_text(json.dumps(meta, indent=1))
            (play_dir(project) / "active").unlink(missing_ok=True)
            raise
        s = cls(project, sid)
        s.meta.update({"godot": bridge.info.get("godot"), "physics_tps": bridge.info.get("physics_tps")})
        if goal.get("success"):
            try:
                cfg = bridge.request("config", goal=goal["success"])["result"]
            except BridgeError as e:
                bridge.quit()
                s.meta["status"] = "failed_to_start"
                s.save()
                (play_dir(project) / "active").unlink(missing_ok=True)
                raise GhostlapError(f"the goal's success expression doesn't compile: {e}") from e
            if cfg.get("goal_error"):
                s.log("goal_error", error=cfg["goal_error"])
        # errors while the scene loaded (frame 0)
        s.meta["load_incidents"] = bridge.request("incidents")["result"]
        for inc in s.meta["load_incidents"]:
            s.incident(inc)
        s.save()
        return s, s.look_raw()

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
            f.write(json.dumps({"f": inc["f"], "kind": "incident", **{k: v for k, v in inc.items() if k != "f"}}) + "\n")

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
        res = self.bridge.look(views=views, shot=shot, **opts)
        self._sync(res)
        return res

    def _sync(self, res: dict) -> None:
        self.meta["frame"] = res["f"]
        if res.get("goal_met_at", -1) >= 0 and self.meta.get("goal_met_at") is None:
            self.meta["goal_met_at"] = res["goal_met_at"]
            self.log("goal_met", at=res["goal_met_at"])

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
