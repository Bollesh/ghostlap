"""Declarative probe files: validation and compilation into the runner's plan.

A probe (YAML):

    probe: coyote                       # id, unique within a run
    title: Coyote time
    description: Walk off a ledge and press jump k frames after leaving the floor.
    dims: [2d]                          # optional: only for these dimensions
    requires: {actions: [jump, right]}  # roles (left/right/up/down/jump/dash/attack) or literal action names
    needs: [hazard]                     # optional extra requirements: hazard, hitbox
    arena: ledge                        # flat | ledge | wall | air | hazard | slope | open
    scene: {path: res://levels/l2.tscn, spawn: SpawnPoints/WallTest}   # instead of arena
    plane: x                            # speed plane for metrics: x (side view) | xy (top-down) | xz (3D)
    settle: auto                        # auto | none
    frames: 150
    calibrate:                          # optional run whose events feed `cal.*` in `at` expressions
      arena: flat                       # optional, defaults to the probe's arena
      frames: 150
      inputs: [{at: 0, action: right}]
    sweep: {var: k, from: 0, to: 12, step: 1}     # or {var: hold, values: [1, 60]}
    inputs:
      - {at: 0, action: right}                    # press and hold until the end
      - {at: "cal.left_floor + k", action: jump, hold: 1}   # press for 1 frame
      - {at: 40, action: right, release: true}
      - {at: 0, action: right, strength: 0.5, hold: 30}
    measure: [jump.coyote_frames]
    assert: {jump.coyote_frames: {min: 4}}

`at` may be an int or an expression over `k` (or the sweep var) and calibration values
`cal.left_floor`, `cal.landed`, `cal.apex`, `cal.top_frame`, `cal.max_speed`, `cal.end`.
`{var}` placeholders are substituted in strings (e.g. action: "{action}", hold: "{hold}").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ARENAS = {"flat", "ledge", "wall", "air", "hazard", "slope", "open"}
ROLES = {"left", "right", "up", "down", "jump", "dash", "attack"}
CAL_KEYS = {"left_floor", "landed", "apex", "top_frame", "max_speed", "max_speed_x", "end", "dir_x", "dir_y", "dir_z"}
TOP_KEYS = {"probe", "title", "description", "dims", "requires", "needs", "arena", "scene", "spawn", "plane",
            "settle", "frames", "calibrate", "sweep", "inputs", "measure", "assert", "notes"}
INPUT_KEYS = {"at", "action", "hold", "release", "strength"}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_EXPR_OK = re.compile(r"^[\w\s.+\-*/()]+$")


@dataclass
class ProbeIssue:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class LoadedProbe:
    id: str
    spec: dict
    source: Path | None = None
    pack: str | None = None
    issues: list[ProbeIssue] = field(default_factory=list)


def load_probe_file(path: Path, pack: str | None = None) -> LoadedProbe:
    try:
        spec = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        return LoadedProbe(id=path.stem, spec={}, source=path, pack=pack, issues=[ProbeIssue(str(path), f"invalid YAML: {e}")])
    if not isinstance(spec, dict):
        return LoadedProbe(id=path.stem, spec={}, source=path, pack=pack, issues=[ProbeIssue(str(path), "probe must be a mapping")])
    return LoadedProbe(id=str(spec.get("probe", path.stem)), spec=spec, source=path, pack=pack)


def validate(spec: dict, metric_names: set[str], known_actions: set[str] | None = None,
             roles: dict[str, str] | None = None) -> list[ProbeIssue]:
    """Schema + semantic checks. known_actions/roles enable the 'does this action exist' checks."""
    issues: list[ProbeIssue] = []

    def bad(path: str, msg: str) -> None:
        issues.append(ProbeIssue(path, msg))

    for k in spec:
        if k not in TOP_KEYS:
            bad(k, f"unknown key (allowed: {', '.join(sorted(TOP_KEYS))})")
    if not isinstance(spec.get("probe"), str) or not re.fullmatch(r"[a-z0-9_]+", spec.get("probe", "")):
        bad("probe", "required; lowercase id like 'dash_into_wall'")
    if "arena" in spec and spec["arena"] not in ARENAS:
        bad("arena", f"must be one of {sorted(ARENAS)}")
    if "arena" in spec and "scene" in spec:
        bad("scene", "use either arena or scene, not both")
    if "scene" in spec:
        sc = spec["scene"]
        if not isinstance(sc, dict) or not str(sc.get("path", "")).startswith("res://"):
            bad("scene", "must be {path: res://..., spawn: NodePath}")
    if "plane" in spec and spec["plane"] not in ("x", "xy", "xz"):
        bad("plane", "must be x, xy or xz")
    if "settle" in spec and spec["settle"] not in ("auto", "none"):
        bad("settle", "must be auto or none")
    frames = spec.get("frames", 180)
    if not isinstance(frames, int) or not (1 <= frames <= 3600):
        bad("frames", "must be an int between 1 and 3600")
    sweep = spec.get("sweep")
    sweep_var = None
    if sweep is not None:
        if not isinstance(sweep, dict) or not isinstance(sweep.get("var"), str):
            bad("sweep", "must be {var: k, from: 0, to: 12} or {var: name, values: [...]}")
        else:
            sweep_var = sweep["var"]
            if "values" not in sweep and not all(isinstance(sweep.get(x), int) for x in ("from", "to")):
                bad("sweep", "needs integer from/to, or values")
            n = len(sweep.get("values", [])) if "values" in sweep else (sweep.get("to", 0) - sweep.get("from", 0)) // max(1, sweep.get("step", 1)) + 1
            if n > 60:
                bad("sweep", f"{n} runs is too many (max 60)")
    variables = {sweep_var or "k"}
    uses_cal = False
    for where, inputs in (("inputs", spec.get("inputs")), ("calibrate.inputs", (spec.get("calibrate") or {}).get("inputs"))):
        if inputs is None:
            if where == "inputs":
                bad("inputs", "required (a list of inputs)")
            continue
        if not isinstance(inputs, list):
            bad(where, "must be a list")
            continue
        for i, inp in enumerate(inputs):
            p = f"{where}[{i}]"
            if not isinstance(inp, dict):
                bad(p, "must be a mapping")
                continue
            for k in inp:
                if k not in INPUT_KEYS:
                    bad(p, f"unknown key '{k}' (allowed: {sorted(INPUT_KEYS)})")
            if "action" not in inp or "at" not in inp:
                bad(p, "needs action and at")
                continue
            at = inp["at"]
            if isinstance(at, str):
                if not _EXPR_OK.match(at):
                    bad(p, f"`at` expression has unsupported characters: {at!r}")
                for ref in re.findall(r"cal\.(\w+)", at):
                    uses_cal = True
                    if ref not in CAL_KEYS:
                        bad(p, f"unknown calibration value cal.{ref} (known: {sorted(CAL_KEYS)})")
                for ident in re.findall(r"(?<![\w.])([a-zA-Z_]\w*)(?![\w(])", re.sub(r"cal\.\w+", "", at)):
                    if ident not in variables:
                        bad(p, f"unknown name '{ident}' in `at` (sweep var is '{sweep_var or 'k'}')")
            elif not isinstance(at, int) or at < 0:
                bad(p, "`at` must be a non-negative int or an expression")
            hold = inp.get("hold")
            if hold is not None and not (isinstance(hold, int) and hold >= 1) and not (isinstance(hold, str) and _PLACEHOLDER.search(hold)):
                bad(p, "`hold` must be an int >= 1 (or a {placeholder})")
            if "strength" in inp and not (isinstance(inp["strength"], (int, float)) and 0 < inp["strength"] <= 1):
                bad(p, "`strength` must be in (0, 1]")
            action = str(inp["action"])
            if roles is not None and known_actions is not None and not _PLACEHOLDER.search(action):
                if action in ROLES and action not in roles:
                    bad(p, f"role '{action}' has no action in profile.actions")
                elif action not in ROLES and action not in known_actions:
                    bad(p, f"action '{action}' is neither a role ({sorted(ROLES)}) nor an InputMap action")
    if uses_cal and not spec.get("calibrate"):
        bad("calibrate", "inputs use cal.* but the probe has no calibrate block")
    measure = spec.get("measure")
    if not isinstance(measure, list) or not measure:
        bad("measure", "required: list of metric names")
    else:
        for m in measure:
            if m not in metric_names:
                bad("measure", f"unknown metric '{m}' (see `gamefeel metrics`)")
    for m, bounds in (spec.get("assert") or {}).items():
        if m not in (measure or []):
            bad(f"assert.{m}", "asserted metric must also be in measure")
        if not isinstance(bounds, dict) or not set(bounds) <= {"min", "max"}:
            bad(f"assert.{m}", "must be {min: x} and/or {max: y}")
    return issues


def _subst(value: Any, vars: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = _PLACEHOLDER.fullmatch(value.strip())
        if m and m.group(1) in vars:
            return vars[m.group(1)]
        return _PLACEHOLDER.sub(lambda mm: str(vars.get(mm.group(1), mm.group(0))), value)
    return value


def _compile_inputs(inputs: list[dict], vars: dict[str, Any], roles: dict[str, str], sweep_var: str) -> list[dict]:
    out = []
    for inp in inputs:
        action = str(_subst(inp["action"], vars))
        action = roles.get(action, action)
        at = _subst(inp["at"], vars)
        if isinstance(at, str):
            at = at.replace("cal.", "cal_")
            if sweep_var != "k":
                at = re.sub(rf"(?<![\w.]){re.escape(sweep_var)}(?![\w(])", "k", at)
        hold = _subst(inp.get("hold"), vars)
        if isinstance(hold, str) and hold.isdigit():
            hold = int(hold)
        if inp.get("release"):
            out.append({"at": at, "action": action, "pressed": False})
            continue
        out.append({"at": at, "action": action, "pressed": True, "strength": float(inp.get("strength", 1.0))})
        if hold is not None:
            rel = at + int(hold) if isinstance(at, int) else f"({at}) + {int(hold)}"
            out.append({"at": rel, "action": action, "pressed": False})
    return out


def compile_probe(spec: dict, roles: dict[str, str], default_plane: str) -> dict:
    sweep = spec.get("sweep")
    sweep_var = sweep["var"] if sweep else "k"
    if sweep is None:
        points = [None]
    elif "values" in sweep:
        points = list(sweep["values"])
    else:
        points = list(range(sweep["from"], sweep["to"] + 1, sweep.get("step", 1)))
    frames = int(spec.get("frames", 180))
    runs = []
    for v in points:
        if isinstance(v, str) and v in ROLES and v not in roles:
            continue  # e.g. input_latency sweeping [right, jump, dash, attack] in a game without dash
        vars = {} if v is None else {sweep_var: v}
        k_val = v if isinstance(v, (int, float)) else (points.index(v) if v is not None else 0)
        name = "run" if v is None else f"{sweep_var}={v}"
        runs.append({"name": name, "vars": {"k": k_val, **({sweep_var: v} if v is not None else {})}, "frames": frames,
                     "inputs": _compile_inputs(spec["inputs"], vars, roles, sweep_var)})
    cal = None
    if spec.get("calibrate"):
        c = spec["calibrate"]
        cal = {"name": "calibrate", "frames": int(c.get("frames", frames)), "vars": {},
               "inputs": _compile_inputs(c.get("inputs", []), {}, roles, sweep_var)}
        if c.get("arena"):
            cal["arena"] = c["arena"]
    out = {
        "id": spec["probe"],
        "arena": spec.get("arena", "flat") if "scene" not in spec else None,
        "scene": spec.get("scene"),
        "spawn": spec.get("spawn"),
        "settle": spec.get("settle", "auto"),
        "plane": spec.get("plane", default_plane),
        "calibrate": cal,
        "runs": runs,
        "measure": spec.get("measure", []),
        "assert": spec.get("assert", {}),
        "title": spec.get("title", spec["probe"]),
    }
    if out["arena"] is None:
        out.pop("arena")
    return out


def requirements_met(spec: dict, profile: dict, dims: str | None) -> tuple[bool, str]:
    if spec.get("dims") and dims and dims not in spec["dims"]:
        return False, f"only for {spec['dims']}"
    roles = profile.get("actions") or {}
    for a in (spec.get("requires") or {}).get("actions", []):
        if a in ROLES and a not in roles:
            return False, f"needs a '{a}' action (profile.actions.{a})"
    for need in spec.get("needs") or []:
        if need == "hazard" and not profile.get("hazard"):
            return False, "needs a hazard scene (profile.hazard)"
        if need == "hitbox" and not profile.get("hitboxes"):
            return False, "needs an attack hitbox (profile.hitboxes)"
    return True, ""
