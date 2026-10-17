"""Select probes for a confirmed profile, compile them into a plan, run Godot headless."""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..graph.store import Graph
from ..metrics import REGISTRY
from ..packs.loader import Pack, default_plane, expand, load_packs, merged_vars
from ..project import GhostlapError, Project, addon_dir, ensure_imported, find_godot, run_godot
from .schema import LoadedProbe, compile_probe, load_probe_file, requirements_met, validate

TRACK_PROP_RE = re.compile(r"^(health|hp|hit_points|life|lives|current_health|stamina)$", re.I)
_VAR = re.compile(r"\{(\w+)\}")


@dataclass
class Selection:
    probes: list[LoadedProbe] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)


def custom_probe_dir(project: Project) -> Path:
    d = project.state / "probes"
    d.mkdir(exist_ok=True)
    return d


def substitute_vars(spec, vars: dict):
    """Replace {move}/{back}/{ground}-style pack variables anywhere in a probe spec."""
    if isinstance(spec, dict):
        return {k: substitute_vars(v, vars) for k, v in spec.items()}
    if isinstance(spec, list):
        return [substitute_vars(v, vars) for v in spec]
    if isinstance(spec, str):
        m = _VAR.fullmatch(spec)
        if m and m.group(1) in vars:
            return vars[m.group(1)]
        return _VAR.sub(lambda mm: str(vars[mm.group(1)]) if mm.group(1) in vars else mm.group(0), spec)
    return spec


def known_actions(g: Graph) -> set[str]:
    return {n["name"] for n in g.nodes("InputAction")}


def select(project: Project, profile: dict, only: list[str] | None = None, extra_files: list[Path] | None = None) -> Selection:
    packs = load_packs(project.root)
    genres = profile.get("genres") or []
    unknown = [gname for gname in genres if gname not in packs]
    if unknown:
        raise GhostlapError(f"profile.genres has unknown pack(s): {unknown}. Available: {sorted(p for p in packs if packs[p].selectable)}")
    pvars = merged_vars(packs, genres)
    by_id: dict[str, LoadedProbe] = {}
    for pack in expand(packs, genres):
        for path in pack.probes.values():
            lp = load_probe_file(path, pack=pack.name)
            by_id[lp.id] = lp
    files = sorted(custom_probe_dir(project).glob("*.yaml")) + list(extra_files or [])
    for path in files:
        lp = load_probe_file(path, pack="custom")
        by_id[lp.id] = lp
    sel = Selection()
    g = Graph(project.graph_db)
    actions = known_actions(g)
    roles = profile.get("actions") or {}
    for pid, lp in by_id.items():
        if only and pid not in only:
            continue
        if lp.issues:
            sel.invalid.append({"probe": pid, "source": str(lp.source), "issues": [str(i) for i in lp.issues]})
            continue
        lp.spec = substitute_vars(copy.deepcopy(lp.spec), pvars)
        ok, why = requirements_met(lp.spec, profile, profile.get("dims"))
        if not ok:
            sel.skipped.append({"probe": pid, "pack": lp.pack, "reason": why})
            continue
        issues = validate(lp.spec, set(REGISTRY), actions, roles)
        if issues:
            sel.invalid.append({"probe": pid, "source": str(lp.source), "issues": [str(i) for i in issues]})
            continue
        sel.probes.append(lp)
    if only:
        missing = [o for o in only if o not in by_id]
        for m in missing:
            sel.invalid.append({"probe": m, "source": None, "issues": [f"no probe named '{m}' in the selected packs or .ghostlap/probes/"]})
    return sel


def build_plan(project: Project, profile: dict, sel: Selection, out_dir: Path) -> dict:
    g = Graph(project.graph_db)
    packs: dict[str, Pack] = load_packs(project.root)
    genres = profile.get("genres") or []
    plane = default_plane(packs, genres, profile.get("dims"))
    roles = profile.get("actions") or {}
    proj = g.node("project")
    tps = int(((proj or {}).get("props", {}).get("physics") or {}).get("ticks_per_second") or 60)
    player = profile.get("player") or {}
    if not player.get("scene"):
        raise GhostlapError("profile.player.scene is empty — run `ghostlap detect` and confirm the player")
    track = sorted({p["name"] for p in _player_props(g, player) if TRACK_PROP_RE.match(p["name"])} | set(profile.get("track") or []))
    compiled = []
    for lp in sel.probes:
        c = compile_probe(lp.spec, roles, plane)
        c["roles"] = roles
        c["pack"] = lp.pack
        c["source"] = str(lp.source) if lp.source else None
        compiled.append(c)
    return {
        "version": 1,
        "out_dir": str(out_dir),
        "physics_fps": tps,
        "dims": profile.get("dims") or "2d",
        "player": {"scene": player["scene"], "node": player.get("node") or "."},
        "hazard": profile.get("hazard"),
        "hitbox_pattern": "",
        "hitbox_paths": profile.get("hitboxes") or [],
        "track_props": track,
        "roles": roles,
        "genres": genres,
        "style": profile.get("style"),
        "probes": compiled,
        "max_process_frames": 200000,
    }


def _player_props(g: Graph, player: dict) -> list[dict]:
    from ..graph.queries import exports_of, script_of

    out = []
    target = f"{player['scene']}::{player.get('node') or '.'}"
    for s in script_of(g, target):
        out.extend(exports_of(g, s["script"]))
    return out


@dataclass
class RunResult:
    run_dir: Path
    plan: dict
    summary: dict
    selection: Selection
    seconds: float
    stderr_tail: str = ""


def run(project: Project, profile: dict, godot: str | None = None, only: list[str] | None = None,
        extra_files: list[Path] | None = None, timeout: float = 900) -> RunResult:
    godot_bin = find_godot(godot)
    if godot_bin is None:
        raise GhostlapError("probes need Godot 4: put `godot` on PATH or set GHOSTLAP_GODOT")
    sel = select(project, profile, only, extra_files)
    if not sel.probes:
        raise GhostlapError("no runnable probes: " + json.dumps({"skipped": sel.skipped, "invalid": sel.invalid}, indent=1))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = project.state / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = build_plan(project, profile, sel, run_dir)
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1))
    ensure_imported(godot_bin, project)
    t0 = time.monotonic()
    proc = run_godot(godot_bin, project, ["--fixed-fps", str(plan["physics_fps"]), "--script",
                                          str(addon_dir() / "probe" / "runner.gd"), "--", f"--plan={run_dir / 'plan.json'}"],
                     timeout=timeout)
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        tail = (proc.stderr or proc.stdout or "")[-3000:]
        raise GhostlapError(f"probe runner exited {proc.returncode} without a summary. Last output:\n{tail}")
    summary = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"), strict=False)
    (project.state / "runs" / "latest").write_text(stamp)
    (run_dir / "selection.json").write_text(json.dumps({"skipped": sel.skipped, "invalid": sel.invalid}, indent=1))
    return RunResult(run_dir, plan, summary, sel, round(time.monotonic() - t0, 2), (proc.stderr or "")[-2000:])


def latest_run(project: Project) -> Path | None:
    ptr = project.state / "runs" / "latest"
    if ptr.is_file():
        d = project.state / "runs" / ptr.read_text().strip()
        if d.is_dir():
            return d
    return None
