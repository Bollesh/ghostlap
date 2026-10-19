"""`ghostlap feel clip`: watch it, don't just trust the numbers.

Renders probe runs (same inputs, same arenas) with Godot's Movie Maker and puts two versions
side by side: by default the project as it is now vs. the latest proposal's scratch copy.
Outputs a GIF and MP4 for people and a contact-sheet PNG for agents (an agent can't watch a
video, but it can look at a sheet of frames).

Needs a display (Movie Maker renders for real) and ffmpeg. On a headless Linux box it uses
`xvfb-run` when available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .probes import run as prun
from .project import GhostlapError, Project, addon_dir, ensure_imported, find_godot

# probes worth watching, in order. Sweeps are shown through one telling run each: e.g. coyote
# at k=4 (jump pressed 4 frames after leaving the ledge: no coyote time = falls, with it = jumps)
SHOWCASE = ["run_start_stop", "jump", "coyote", "jump_buffer", "turnaround", "hit_react", "diagonal", "dash",
            "dash_cooldown", "attack", "attack_combo", "turn", "camera_follow", "slope"]
SHOW_RUNS = {"jump": ["hold=1", "hold=40"], "coyote": ["k=4"], "jump_buffer": ["k=4"], "attack_combo": ["k=16"],
             "dash_cooldown": ["k=32"]}

SHORT = {
    "jump.time_to_apex": "apex", "jump.fall_time": "fall", "jump.fall_rise_ratio": "fall/rise", "jump.coyote_frames": "coyote",
    "jump.buffer_frames": "buffer", "jump.height_max": "jump height", "jump.variable": "tap/hold", "move.accel_time": "start",
    "move.decel_time": "stop", "move.turnaround_time": "turn", "impact.hitstop_frames": "hitstop", "impact.feedback": "hit feedback",
    "impact.knockback": "knockback", "move.diagonal_ratio": "diagonal", "dash.distance": "dash", "dash.duration_frames": "dash time",
    "attack.startup_frames": "attack startup", "camera.follow_lag": "camera lag", "move.turn_time": "turn-to-face",
}
UNIT = {"s": "s", "frames": "f", "H": "H", "x": "x", "channels": "", "H/s": "H/s"}


def _need(tool: str, hint: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise GhostlapError(f"`ghostlap feel clip` needs {tool}: {hint}")
    return path


def _display_prefix() -> list[str]:
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        xvfb = shutil.which("xvfb-run")
        if not xvfb:
            raise GhostlapError("rendering needs a display; install xvfb (xvfb-run) for headless machines")
        return [xvfb, "-a", "-s", "-screen 0 1280x720x24"]
    return []


def _showcase(sel: prun.Selection, wanted: list[str] | None) -> list[str]:
    ids = [lp.id for lp in sel.probes]
    if wanted:
        return [w for w in wanted if w in ids]
    return [p for p in SHOWCASE if p in ids]


def render(project: Project, profile: dict, out_avi: Path, probes: list[str] | None = None, godot: str | None = None,
           seconds_cap: float = 40.0) -> dict:
    """Render the showcase probes of `project` into one AVI."""
    godot_bin = find_godot(godot)
    if godot_bin is None:
        raise GhostlapError("clips need Godot 4 on PATH (or GHOSTLAP_GODOT)")
    sel = prun.select(project, profile, only=None)
    chosen = _showcase(sel, probes)
    if not chosen:
        raise GhostlapError(f"none of these probes can run here: {probes or SHOWCASE}")
    sel.probes = [lp for lp in sel.probes if lp.id in chosen]
    sel.probes.sort(key=lambda lp: chosen.index(lp.id))
    tmp = project.state / "clips" / "_runs" / time.strftime("%Y%m%d-%H%M%S")
    tmp.mkdir(parents=True, exist_ok=True)
    plan = prun.build_plan(project, profile, sel, tmp)
    plan["render"] = True
    budget = int(seconds_cap * plan["physics_fps"])
    for c in plan["probes"]:
        if len(c["runs"]) > 1:
            want = SHOW_RUNS.get(c["id"])
            picked = [r for r in c["runs"] if want and r["name"] in want]
            c["runs"] = picked or c["runs"][-1:]
        c["calibrate"] = c["calibrate"] if any("cal_" in str(i.get("at")) for r in c["runs"] for i in r["inputs"]) else None
        for r in c["runs"]:
            r["frames"] = min(int(r["frames"]), 150)
    total = sum(r["frames"] + 30 for c in plan["probes"] for r in c["runs"] + ([c["calibrate"]] if c["calibrate"] else []))
    if total > budget:
        keep, acc = [], 0
        for c in plan["probes"]:
            acc += sum(r["frames"] + 30 for r in c["runs"])
            if acc > budget and keep:
                break
            keep.append(c)
        plan["probes"] = keep
    (tmp / "plan.json").write_text(json.dumps(plan, indent=1))
    ensure_imported(godot_bin, project)
    out_avi.parent.mkdir(parents=True, exist_ok=True)
    # --disable-vsync: a covered window would otherwise wait on the compositor for every frame (~1 fps)
    cmd = _display_prefix() + [godot_bin, "--disable-vsync", "--path", str(project.root), "--write-movie", str(out_avi),
                               "--fixed-fps", str(plan["physics_fps"]), "--script", str(addon_dir() / "probe" / "runner.gd"),
                               "--", f"--plan={tmp / 'plan.json'}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not out_avi.is_file() or out_avi.stat().st_size == 0:
        raise GhostlapError(f"Godot didn't produce a movie (exit {proc.returncode}):\n{(proc.stderr or proc.stdout)[-1500:]}")
    return {"avi": str(out_avi), "probes": [c["id"] for c in plan["probes"]]}


def _font() -> str | None:
    """A regular sans font that has '→' (drawtext shows boxes for missing glyphs)."""
    if not shutil.which("fc-list"):
        return None
    try:
        out = subprocess.run(["fc-list", ":charset=2192:style=Regular", "file", "family"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    files = [ln.split(":")[0] for ln in out.splitlines() if ln.strip()]
    for pref in ("Inter", "NotoSans-", "DejaVuSans.ttf", "LiberationSans", "Arial", "Helvetica"):
        for f in files:
            if pref.lower() in Path(f).name.lower() and not any(x in f.lower() for x in ("mono", "serif", "italic", "bold", "condensed")):
                return f
    return None


def caption_from_proposal(proposal: dict, limit: int = 5) -> str:
    parts = []
    for m in proposal.get("metrics", []):
        if m["severity_before"] in ("warn", "error", "assert_failed") and m["severity_after"] in ("ok", "accepted") \
                and m["metric"] in SHORT and isinstance(m["before"], (int, float)) and isinstance(m["after"], (int, float)):
            u = UNIT.get(m["unit"] or "", "")
            parts.append(f"{SHORT[m['metric']]} {_num(m['before'])}{u} → {_num(m['after'])}{u}")
        if len(parts) >= limit:
            break
    return "     ".join(parts)


def _num(v: float) -> str:
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def compose(a_avi: Path, b_avi: Path | None, labels: tuple[str, str], caption: str, out_base: Path, width: int = 480) -> dict:
    ffmpeg = _need("ffmpeg", "install ffmpeg (with libfreetype for labels)")
    font = _font()
    arrow_ok = font is not None
    if not arrow_ok:
        caption = caption.replace("→", "->")
    ff = f"fontfile='{font}':" if font else ""
    lab = f"drawtext={ff}text='%s':x=12:y=12:fontsize=21:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6"
    inputs = ["-i", str(a_avi)] + (["-i", str(b_avi)] if b_avi else [])
    if b_avi:
        graph = (f"[0:v]fps=30,scale={width}:-2:flags=lanczos,{lab % _esc(labels[0])}[a];"
                 f"[1:v]fps=30,scale={width}:-2:flags=lanczos,{lab % _esc(labels[1])}[b];"
                 f"[a][b]hstack=inputs=2")
    else:
        graph = f"[0:v]fps=30,scale={width}:-2:flags=lanczos,{lab % _esc(labels[0])}"
    if caption:
        graph += (f",pad=iw:ih+36:0:0:color=0x2b2b2b,drawtext={ff}text='{_esc(caption)}':x=(w-text_w)/2:y=h-26:"
                  f"fontsize=15:fontcolor=0xe8e8e8")
    out = {}
    gif = out_base.with_suffix(".gif")
    run = lambda args: subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True, timeout=900)
    # GitHub renders GIFs up to ~10 MB; 3D scenes compress poorly, so step quality down until it fits
    for fps, w, colors in ((30, width, 96), (20, int(width * 0.85), 72), (15, int(width * 0.7), 48)):
        g = graph.replace("fps=30", f"fps={fps}").replace(f"scale={width}:", f"scale={w}:")
        r = run([*inputs, "-filter_complex", g + f",split[s0][s1];[s0]palettegen=max_colors={colors}:stats_mode=diff[p];"
                 "[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", "-loop", "0", str(gif)])
        if r.returncode != 0:
            raise GhostlapError(f"ffmpeg failed: {r.stderr[-1200:]}")
        if gif.stat().st_size <= 8_000_000:
            break
    out["gif"] = str(gif)
    out["gif_bytes"] = gif.stat().st_size
    mp4 = out_base.with_suffix(".mp4")
    r = run([*inputs, "-filter_complex", graph + ",format=yuv420p", "-c:v", "libx264", "-crf", "23", "-movflags", "+faststart", str(mp4)])
    if r.returncode == 0:
        out["mp4"] = str(mp4)
    # contact sheet for agents: bigger frames, no caption, every 0.4 s, two per row
    sheet = out_base.parent / (out_base.name + "-sheet.png")
    side = graph.split(",pad=")[0] if caption else graph
    r = run([*inputs, "-filter_complex", side.replace(f"scale={width}", "scale=640") +
             ",select='not(mod(n\\,12))',tile=2x14:padding=6:color=0x111111", "-frames:v", "1", str(sheet)])
    if r.returncode == 0:
        out["sheet"] = str(sheet)
    return out


def clip(project: Project, profile: dict, against: str | None = "proposal", probes: list[str] | None = None,
         save_as: str | None = None, godot: str | None = None) -> dict:
    """Render the project now; compare with the latest proposal (default), a saved label, or nothing."""
    _need("ffmpeg", "install ffmpeg")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = project.state / "clips"
    base.mkdir(parents=True, exist_ok=True)
    now_avi = base / f"{save_as or 'current'}-{stamp}.avi"
    info = render(project, profile, now_avi, probes, godot)
    if save_as:
        (base / f"{save_as}.avi").unlink(missing_ok=True)
        shutil.copyfile(now_avi, base / f"{save_as}.avi")
    other_avi, labels, caption = None, ("now", ""), ""
    if against == "proposal":
        from . import propose as prop

        try:
            proposal, _ = prop.load(project)
        except GhostlapError:
            proposal = None
        if proposal and not proposal.get("applied") and Path(proposal["work_dir"]).is_dir():
            work = Project(Path(proposal["work_dir"]))
            other_avi = base / f"proposal-{proposal['stamp']}.avi"
            if not other_avi.is_file():
                render(work, profile, other_avi, info["probes"], godot)
            labels, caption = ("now", "proposed"), caption_from_proposal(proposal)
    elif against:
        cand = base / f"{against}.avi"
        if not cand.is_file():
            raise GhostlapError(f"no saved clip '{against}' (save one with `ghostlap feel clip --save-as {against}`)")
        other_avi, labels = cand, (against, "now")
        now_avi, other_avi = other_avi, now_avi   # saved (older) on the left
    out = compose(now_avi, other_avi, labels, caption, base / f"clip-{stamp}")
    return {**out, "probes": info["probes"], "left": labels[0], "right": labels[1] if other_avi else None, "caption": caption}
