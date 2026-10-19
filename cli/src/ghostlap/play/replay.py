"""Replays of a session's final input line (design §4.1, §4.2).

Every replay goes through the bridge the session ran in, so it injects input exactly as the session
did, on the same frames. (The design first planned a `replay` plan type in the v1 feel runner, but
the runner presses actions with `Input.action_press`, which `_process` code never sees as just
pressed.)

    check   `play end` replays the line in the session's display class (headless, or rendered when
            the session rendered) and compares every frame's state hash: `exact`, or `diverged@f`
            with the first field that differs. A fail is confirmed when the replay hits the same
            incident within 2 frames.
    watch   `play replay`: the exact repro, paced to real time in a window.
    video   `play video`: a Movie Maker replay with the caption HUD (hud.gd), then ffmpeg into an
            MP4, a GIF and a contact sheet. Long sessions keep only the windows around notes,
            incidents and the success frame (±3 s) unless --full.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from ..project import GhostlapError
from .bridge_client import Bridge, BridgeError, BridgeHang


def fps(meta: dict) -> int:
    """Frames per second of game time: the --fixed-fps a session runs at, its project's physics
    tick rate (sessions from before it was recorded all ran at 60)."""
    return int(meta.get("fps") or 60)

MATCH_FRAMES = 2       # a replayed incident within this many frames is the same one
HIGHLIGHT_S = 3.0      # seconds kept around each moment in a highlight video
LONG_S = 30.0          # sessions longer than this get a highlight video unless --full
HOLD_S = 2.0           # the final frame (with the verdict) is held this long
GIF_MAX = 8_000_000    # bytes: GitHub READMEs and chat uploads; the GIF steps down until it fits
QUIET = {"every": 0, "keep": 5, "on": [], "width": 960}


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def drive(b: Bridge, inputs: list[dict], cheats: list[dict], to: int, pace_tps: int | None = None) -> dict | None:
    """Step `b` from frame 0 to `to` with the line's inputs; an eval write at frame f runs after
    frame f, as it did in the session. pace_tps: step in small chunks at real time (for watching).
    Returns the last step's result, with `input_lost` from every step (holds the window's focus
    changes released and the bridge pressed again)."""
    res = None
    lost: list[dict] = []
    at = 0
    chunk = max(1, pace_tps // 10) if pace_tps else None
    t0 = time.monotonic()
    for stop in sorted({c["f"] for c in cheats if c["f"] <= to} | {to}):
        while stop > at:
            end = min(stop, at + chunk) if chunk else stop
            res = b.step(end - at, [{"at": e["f"] - at, **{k: v for k, v in e.items() if k != "f"}}
                                    for e in inputs if at < e["f"] <= end])
            lost += res.get("input_lost") or []
            at = end
            if pace_tps:
                lag = t0 + at / pace_tps - time.monotonic()
                if lag > 0:
                    time.sleep(lag)
        for c in cheats:
            if c["f"] == stop:
                b.request("eval", expr=c["eval"], write=True)
    if res is not None:
        res["input_lost"] = lost
    return res


def launch(s, godot: str, run_dir: Path, display: str, shots: dict | None = None,
           extra_args: list[str] | None = None, hud: Path | None = None) -> Bridge:
    """A bridge on the session's start scene, seed and profile, in its own directory (its
    godot.log, progress and bridge.json don't touch the session's)."""
    m = s.meta
    prof = s.dir / "profile.json"
    args = list(extra_args or [])
    b = Bridge.launch(godot, s.project.root, m["scene"], run_dir, seed=m["seed"], display=display, physics_tps=fps(m),
                      profile=json.loads(prof.read_text()) if prof.is_file() else None, shots=shots or QUIET,
                      extra_args=args, hud=hud)
    success = (m.get("goal") or {}).get("success")
    if success:
        b.request("config", goal=success)
    return b


def _fresh(d: Path) -> Path:
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def _run(s, godot: str, run_dir: Path, display: str, pace: bool = False, hold: int = 0, **kw) -> dict:
    """Replay the final line in run_dir; {hashes, samples, incidents, crashed, hung, look}.
    hold: frames to run on with the game tree paused after the line ends (a video's final frame,
    with the verdict, stays on screen; Movie Maker only writes frames that are rendered)."""
    m = s.meta
    inputs = read_jsonl(s.dir / "inputs.jsonl")
    b = launch(s, godot, run_dir, display, **kw)
    out = {"hashes": [], "samples": [], "incidents": [], "crashed": None, "hung": None, "look": None, "input_lost": []}
    tps = fps(m)
    # a session that crashed or hung ends at its last known frame: run on a little to meet it again
    to = m["frame"] + (10 * tps if m.get("status") in ("crashed", "hung") else 0)
    try:
        res = drive(b, inputs, m.get("cheats") or [], to, tps if pace else None)
        out["input_lost"] = (res or {}).get("input_lost") or []
    except BridgeHang as e:
        b.kill()
        out["hung"] = e.frame if e.frame is not None else -1
        return out
    except BridgeError:
        if not b.exited():
            b.kill()
            raise
        out["crashed"] = _progress(run_dir)
        return out
    out["look"] = b.request("look", views=["player"])["result"]
    out["hashes"] = b.hashes()
    out["samples"] = b.samples()
    out["incidents"] = b.request("incidents")["result"]
    if hold:
        b.request("eval", expr="tree.set('paused', true)", write=True)
        b.step(hold)
    if pace:
        time.sleep(HOLD_S)
    b.quit()
    return out


def _progress(run_dir: Path) -> int:
    try:
        return int((run_dir / "progress").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return -1


def first_divergence(a: list[str], b: list[str], upto: int) -> int | None:
    for f in range(upto + 1):
        if f >= len(a) or f >= len(b) or a[f] != b[f]:
            return f
    return None


def _field(live: list[dict], rep: list[dict], f: int) -> str:
    """What differs at frame f, as far as the player samples tell."""
    x = live[f] if f < len(live) else None
    y = rep[f] if f < len(rep) else None
    if x is None or y is None:
        return "the state hash (no player sample for that frame)"
    for k in ("pos", "vel", "floor"):
        if x.get(k) != y.get(k):
            return f"player {k}: {x.get(k)} in the session, {y.get(k)} in the replay"
    return "a node other than the player (the player's pos/vel/floor match)"


def likely_causes(project_state: Path, n: int = 3) -> list[str]:
    """Nondeterminism sources from the cached preflight, if any."""
    try:
        pf = json.loads((project_state / "preflight.json").read_text())
    except (OSError, ValueError):
        return []
    return [f"{x['what']} ({x['at']})" for x in pf.get("nondeterminism", [])[:n]]


def check(s, godot: str, live_hashes: list[str] | None, live_samples: list[dict] | None) -> dict:
    """The replay check (§4.1). Written to <session>/replay.json."""
    display = "headless" if s.meta.get("headless") else "offscreen"
    t0 = time.monotonic()
    r = _run(s, godot, _fresh(s.dir / "replay"), display)
    end = s.meta["frame"]
    out = {"mode": "headless" if display == "headless" else "rendered", "frames": end,
           "seconds": round(time.monotonic() - t0, 1), "incidents": [_slim(i) for i in r["incidents"]]}
    if r["input_lost"]:
        out["input_lost"] = r["input_lost"]
    if r["crashed"] is not None:
        out["crashed_at"] = r["crashed"]
    if r["hung"] is not None:
        out["hung_at"] = r["hung"]
    if live_hashes is None or not r["hashes"]:
        # the session's game or the replay's died: there are no hashes to compare
        out["result"] = "not compared"
        out["why"] = ("the session's game was gone at the end" if live_hashes is None else
                      "the replay " + ("crashed" if r["crashed"] is not None else "hung"))
    else:
        d = first_divergence(live_hashes, r["hashes"], min(end, len(live_hashes) - 1))
        if d is None:
            out["result"] = "exact"
        else:
            out["result"] = "diverged"
            out["diverged_at"] = d
            out["field"] = _field(live_samples or [], r["samples"], d)
            causes = likely_causes(s.project.state)
            if causes:
                out["likely_causes"] = causes
    (s.dir / "replay.json").write_text(json.dumps(out, indent=1))
    return out


def _slim(i: dict) -> dict:
    return {k: i[k] for k in ("f", "type", "class", "why") if k in i}


def confirms(rep: dict, incident: dict) -> bool:
    """Does the replay hit this incident again? Same type within MATCH_FRAMES; a crash or hang
    is confirmed by the replay crashing or hanging too (their frame is only known roughly)."""
    if incident["type"] == "crash":
        return "crashed_at" in rep
    if incident["type"] == "hang":
        return "hung_at" in rep
    return any(i["type"] == incident["type"] and abs(i["f"] - incident["f"]) <= MATCH_FRAMES for i in rep["incidents"])


def watch(s, godot: str, display: str = "window", fast: bool = False) -> dict:
    """`play replay`: the final line again, at real time unless fast; compared with the session."""
    r = _run(s, godot, _fresh(s.dir / "watch"), display, pace=not fast)
    live = [h["hash"] for h in read_jsonl(s.dir / "hashes.jsonl")]
    sampled = {h["f"]: h["hash"] for h in read_jsonl(s.dir / "hashes.jsonl")}
    bad = next((f for f, h in sorted(sampled.items()) if f >= len(r["hashes"]) or r["hashes"][f] != h), None)
    return {"frames": s.meta["frame"], "result": "exact" if live and bad is None else ("diverged" if live else "not compared"),
            "diverged_by": bad, "crashed_at": r["crashed"], "hung_at": r["hung"], "input_lost": r["input_lost"]}


# ------------------------------------------------------------------ video

def _need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise GhostlapError(f"`play video` needs {tool} on PATH")
    return p


def windows(moments: list[int], final: int, tps: int, full: bool) -> list[tuple[int, int]]:
    """Frame ranges to keep: everything for a short session or --full, else ±HIGHLIGHT_S around
    each moment (always including the start and the final frame), merged."""
    if full or final <= LONG_S * tps:
        return [(0, final)]
    pad = int(HIGHLIGHT_S * tps)
    spans = sorted((max(0, f - pad), min(final, f + pad)) for f in {0, final, *moments} if 0 <= f <= final)
    out: list[tuple[int, int]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def video(s, godot: str, hud: dict, moments: list[int], full: bool = False, width: int = 640) -> dict:
    """Render the final line with Movie Maker and the caption HUD; returns the output paths."""
    ffmpeg = _need("ffmpeg")
    m = s.meta
    tps = fps(m)
    run_dir = _fresh(s.dir / "video")
    hud_path = run_dir / "hud.json"
    hud_path.write_text(json.dumps(hud))
    avi = run_dir / "movie.avi"
    r = _run(s, godot, run_dir, "offscreen", hold=int(HOLD_S * tps) + 2, extra_args=["--write-movie", str(avi)], hud=hud_path)
    if not avi.is_file() or avi.stat().st_size == 0:
        raise GhostlapError(f"Godot didn't write a movie; see {run_dir / 'godot.log'}")
    it0 = int((r["look"] or {}).get("it0") or 0)   # movie frames before the start scene's frame 0
    final = m["frame"]
    kept = windows(moments, final, tps, full)
    spans = list(kept)
    spans[-1] = (spans[-1][0], final + int(HOLD_S * tps))   # the paused hold after the final frame
    sel = "+".join(f"between(n\\,{a + it0}\\,{b + it0})" for a, b in spans)
    base = f"select='{sel}',setpts=N/{tps}/TB,fps=30,scale={width}:-2:flags=lanczos"
    run = lambda args: subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(avi), *args],
                                      capture_output=True, text=True, timeout=1800)
    out = {"spans": kept, "frames": final}
    mp4 = s.dir / "playtest.mp4"
    res = run(["-vf", base + ",format=yuv420p", "-c:v", "libx264", "-crf", "23", "-movflags", "+faststart", "-an", str(mp4)])
    if res.returncode != 0:
        raise GhostlapError(f"ffmpeg failed: {res.stderr[-1200:]}")
    out["mp4"] = str(mp4)
    gif = s.dir / "playtest.gif"
    for rate, w, colors in ((20, width, 128), (15, int(width * 0.8), 96), (12, int(width * 0.6), 64), (10, int(width * 0.5), 48)):
        g = base.replace("fps=30", f"fps={rate}").replace(f"scale={width}:", f"scale={w}:")
        res = run(["-filter_complex", g + f",split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", "-loop", "0", str(gif)])
        if res.returncode != 0:
            raise GhostlapError(f"ffmpeg failed: {res.stderr[-1200:]}")
        if gif.stat().st_size <= GIF_MAX:
            break
    out["gif"], out["gif_bytes"] = str(gif), gif.stat().st_size
    sheet = s.dir / "playtest-sheet.png"
    out_frames = sum(b - a + 1 for a, b in spans) / tps * 30
    step = max(1, round(out_frames / 24))   # about 24 frames across the whole video
    res = run(["-vf", base.replace(f"scale={width}", "scale=480") + f",select='not(mod(n\\,{step}))',"
               "tile=4x6:padding=4:color=0x111111", "-frames:v", "1", str(sheet)])
    if res.returncode == 0:
        out["sheet"] = str(sheet)
    live = [h["hash"] for h in read_jsonl(s.dir / "hashes.jsonl")]
    sampled = {h["f"]: h["hash"] for h in read_jsonl(s.dir / "hashes.jsonl")}
    bad = next((f for f, h in sorted(sampled.items()) if f >= len(r["hashes"]) or r["hashes"][f] != h), None)
    out["matches_session"] = bool(live) and bad is None
    if bad is not None:
        out["diverged_by"] = bad
    out["input_lost"] = r["input_lost"]
    avi.unlink(missing_ok=True)   # large; the MP4 is the keeper
    return out
