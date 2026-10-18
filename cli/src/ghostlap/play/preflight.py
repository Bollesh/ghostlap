"""`ghostlap play preflight` (design §3.0): checks that would otherwise waste a session.

    loads            the start scene boots headless and runs 60 frames without a bug incident
    input_style      gameplay scripts read raw keys/joypad (Input.is_key_pressed, InputEventKey, ...):
                     action injection won't reach them, so play with --key
    nondeterminism   randomize(), unseeded RandomNumberGenerator, Time/OS clocks, threads: a
                     session may not replay (the replay check names these as likely causes)
    render_dependent VisibleOnScreenNotifier/Enabler never fire headless: play and replay rendered
    determinism      two headless boots, 300 frames without input, compared frame by frame
    pause_time_scale get_tree().paused and Engine.time_scale (the bridge's clock handles them)
    audio_position   get_playback_position(): audio feeding back into game state

The code checks are a text scan of the project's scripts and scenes (outside addons/), so they
work before `ghostlap scan`. Results are cached in .ghostlap/preflight.json until a script,
scene or project.godot changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..project import Project, ensure_imported
from .bridge_client import Bridge, BridgeError
from .session import main_scene

CACHE = "preflight.json"
SKIP_DIRS = {".godot", ".ghostlap", ".git", "addons"}

# (check, label, regex) over .gd files
CODE = [
    ("input_style", "Input.is_key_pressed", r"\bInput\.is_key_pressed\s*\("),
    ("input_style", "Input.is_physical_key_pressed", r"\bInput\.is_physical_key_pressed\s*\("),
    ("input_style", "Input.get_joy_axis", r"\bInput\.get_joy_axis\s*\("),
    ("input_style", "InputEventKey handling", r"\bis\s+InputEventKey\b|\bas\s+InputEventKey\b"),
    ("nondeterminism", "randomize()", r"(?<![\w.])randomize\s*\(\s*\)"),
    ("nondeterminism", "RandomNumberGenerator.new() without a seed", r"\bRandomNumberGenerator\.new\s*\("),
    ("nondeterminism", "Time.* clock", r"\bTime\.get_(ticks_msec|ticks_usec|unix_time\w*|datetime\w*|time\w*)\s*\("),
    ("nondeterminism", "OS.get_ticks*", r"\bOS\.get_(ticks_msec|ticks_usec|unix_time)\s*\("),
    ("nondeterminism", "Thread", r"\bThread\.new\s*\("),
    ("render_dependent", "VisibleOnScreenNotifier/Enabler", r"\bVisibleOnScreen(Notifier|Enabler)(2D|3D)\b"),
    ("pause_time_scale", "get_tree().paused", r"\.paused\s*="),
    ("pause_time_scale", "Engine.time_scale", r"\bEngine\.time_scale\b"),
    ("audio_position", "get_playback_position()", r"\bget_playback_position\s*\("),
]
SCENE = [("render_dependent", "VisibleOnScreenNotifier/Enabler node", r'type="VisibleOnScreen(Notifier|Enabler)(2D|3D)"')]

WHY = {
    "input_style": "the game reads raw keys or joypad axes: action injection won't reach that code, play it with --key / --key-hold",
    "nondeterminism": "may make sessions replay differently: the replay check would report diverged and no test could be made",
    "render_dependent": "never fires in a headless session: start rendered (--offscreen, the default), not --headless",
    "pause_time_scale": "the bridge counts frames through pauses and hitstop, so step -n always returns; noted for the report",
    "audio_position": "audio position feeding game state may not replay exactly",
}


def _files(root: Path, exts: tuple[str, ...]) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        if p.suffix in exts and p.is_file():
            out.append(p)
    return sorted(out)


def fingerprint(root: Path) -> str:
    h = hashlib.sha1()
    for p in _files(root, (".gd", ".tscn", ".tres", ".godot", ".cfg")):
        st = p.stat()
        h.update(f"{p.relative_to(root)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def code_findings(root: Path) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}

    def add(check: str, what: str, path: Path, line: int) -> None:
        found.setdefault(check, []).append({"what": what, "at": f"res://{path.relative_to(root).as_posix()}:{line}"})

    for path in _files(root, (".gd",)):
        lines = path.read_text(errors="replace").splitlines()
        seeded = any(re.search(r"\.seed\s*=", ln) for ln in lines)
        for i, ln in enumerate(lines, 1):
            code = ln.split("#", 1)[0]
            for check, what, rx in CODE:
                if re.search(rx, code):
                    if what.startswith("RandomNumberGenerator") and seeded:
                        continue
                    add(check, what, path, i)
    for path in _files(root, (".tscn",)):
        for i, ln in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for check, what, rx in SCENE:
                if re.search(rx, ln):
                    add(check, what, path, i)
    pg = (root / "project.godot").read_text(errors="replace")
    if re.search(r"run_on_separate_thread\s*=\s*true", pg):
        found.setdefault("nondeterminism", []).append({"what": "physics/common/run_on_separate_thread", "at": "project.godot"})
    return found


def _boot(godot: str, project: Project, scene: str, run_dir: Path, frames: int) -> dict:
    b = Bridge.launch(godot, project.root, scene, run_dir, display="headless")
    try:
        load = b.request("incidents")["result"]
        b.step(frames, timeout=120)
        return {"hashes": b.hashes(), "incidents": b.request("incidents")["result"], "load": load}
    finally:
        b.quit()


def run(project: Project, godot: str, scene: str | None = None, force: bool = False) -> dict:
    """The preflight result, from the cache when nothing changed."""
    fp = fingerprint(project.root)
    cache = project.state / CACHE
    if cache.is_file() and not force:
        try:
            old = json.loads(cache.read_text())
            if old.get("fingerprint") == fp and old.get("scene") == (scene or old.get("scene")):
                return {**old, "cached": True}
        except ValueError:
            pass
    ensure_imported(godot, project)
    start = scene or main_scene(project)
    out: dict = {"fingerprint": fp, "scene": start}
    work = project.state / "preflight"
    try:
        a = _boot(godot, project, start, work / "a", 300)
        b = _boot(godot, project, start, work / "b", 300)
    except BridgeError as e:
        out["loads"] = {"ok": False, "why": str(e).splitlines()[0], "log": str(work / "a" / "godot.log")}
        a = b = None
    if a is not None:
        bugs = [i for i in a["incidents"] if i.get("class") == "bug" and i["f"] <= 60]
        out["loads"] = {"ok": not bugs, "incidents": [{"f": i["f"], "type": i["type"], "why": i.get("why", "")} for i in bugs]}
        diff = next((f for f in range(min(len(a["hashes"]), len(b["hashes"]))) if a["hashes"][f] != b["hashes"][f]), None)
        out["determinism"] = {"ok": diff is None, "frames": 300, **({"diverged_at": diff} if diff is not None else {})}
    for check, items in code_findings(project.root).items():
        out[check] = items
    out["fingerprint"] = fingerprint(project.root)   # after the import, which may touch project.godot
    cache.write_text(json.dumps(out, indent=1))
    return {**out, "cached": False}


def warnings(pf: dict) -> list[str]:
    """One line per finding that matters, for `play start` and the skill."""
    out = []
    ld = pf.get("loads") or {}
    if ld and not ld.get("ok"):
        why = ld.get("why") or "; ".join(f"{i['type']} at f={i['f']}: {i['why']}" for i in ld.get("incidents", []))
        out.append(f"the start scene doesn't run cleanly: {why}")
    det = pf.get("determinism") or {}
    if det and not det.get("ok"):
        out.append(f"two identical boots differ from f={det['diverged_at']} with no input: replays won't be exact")
    for check in ("input_style", "render_dependent", "nondeterminism", "audio_position"):
        items = pf.get(check) or []
        if items:
            where = ", ".join(f"{i['what']} ({i['at']})" for i in items[:3]) + (f" +{len(items) - 3} more" if len(items) > 3 else "")
            out.append(f"{check}: {where} — {WHY[check]}")
    return out


def render(pf: dict) -> list[str]:
    ld, det = pf.get("loads") or {}, pf.get("determinism") or {}
    lines = [f"preflight of {pf['scene']}" + (" (cached; --force to rerun)" if pf.get("cached") else "")]
    lines.append(("✅" if ld.get("ok") else "❌") + " loads: " + ("60 frames without a bug incident" if ld.get("ok") else
                 ld.get("why") or "; ".join(f"{i['type']} at f={i['f']}" for i in ld.get("incidents", []))))
    if det:
        lines.append(("✅" if det["ok"] else "⚠️") + " determinism: " + ("two boots match for 300 frames" if det["ok"] else
                     f"two boots differ from f={det['diverged_at']}"))
    for check in ("input_style", "nondeterminism", "render_dependent", "pause_time_scale", "audio_position"):
        items = pf.get(check) or []
        mark = "✅" if not items else ("ℹ️" if check == "pause_time_scale" else "⚠️")
        lines.append(f"{mark} {check}: " + ("none found" if not items else f"{len(items)} — {WHY[check]}"))
        lines += [f"    {i['what']} at {i['at']}" for i in items[:6]]
        if len(items) > 6:
            lines.append(f"    … {len(items) - 6} more")
    return lines


def cached(project: Project) -> dict | None:
    """The cached result when it's still current, else None."""
    p = project.state / CACHE
    try:
        pf = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return pf if pf.get("fingerprint") == fingerprint(project.root) else None

