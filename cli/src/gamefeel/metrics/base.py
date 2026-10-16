"""Recorded probe runs and the metric registry.

A metric is a function `(ProbeData) -> MetricValue` registered with @metric. It reads frames
(one sample per physics frame, recorded *after* the game's physics code ran) and the input
timeline, and returns a value plus evidence (file + frame range) so findings are traceable.

Units: distances are in player-heights (H), times in seconds unless the name ends in _frames.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Run:
    header: dict
    frames: list[dict]
    process: list[list]
    file: Path

    @property
    def name(self) -> str:
        return self.header.get("run", "run")

    @property
    def fps(self) -> float:
        return float(self.header.get("physics_fps", 60))

    @property
    def dims(self) -> str:
        return self.header.get("dims", "2d")

    @property
    def height(self) -> float:
        size = self.header.get("size") or [0, 0]
        return float(size[1]) or (2.0 if self.dims == "3d" else 32.0)

    @property
    def vars(self) -> dict:
        return self.header.get("vars", {})

    @property
    def ok(self) -> bool:
        return not self.header.get("error") and bool(self.frames)

    # ---- per-frame accessors (positive vy = moving up, regardless of 2D/3D axis)
    def vy(self, s: dict) -> float:
        v = s.get("v") or [0, 0, 0]
        return -v[1] if self.dims == "2d" else v[1]

    def y_up(self, s: dict) -> float:
        p = s.get("p") or [0, 0, 0]
        return -p[1] if self.dims == "2d" else p[1]

    def planar(self, s: dict, plane: str) -> float:
        v = s.get("v") or [0, 0, 0]
        if plane == "x":
            return abs(v[0])
        if plane == "xy":
            return math.hypot(v[0], v[1])
        return math.hypot(v[0], v[2] if len(v) > 2 else 0.0)

    def planar_vec(self, s: dict, plane: str) -> tuple[float, float]:
        v = s.get("v") or [0, 0, 0]
        if plane == "x":
            return (v[0], 0.0)
        if plane == "xy":
            return (v[0], v[1])
        return (v[0], v[2] if len(v) > 2 else 0.0)

    def presses(self, action: str | None = None) -> list[tuple[int, str]]:
        """(frame, action) for each frame an action becomes held."""
        out = []
        prev: set[str] = set()
        for s in self.frames:
            cur = set(s.get("in", []))
            for a in sorted(cur - prev):
                if action is None or a == action:
                    out.append((s["f"], a))
            prev = cur
        return out

    def releases(self, action: str | None = None) -> list[tuple[int, str]]:
        out = []
        prev: set[str] = set()
        for s in self.frames:
            cur = set(s.get("in", []))
            for a in sorted(prev - cur):
                if action is None or a == action:
                    out.append((s["f"], a))
            prev = cur
        return out

    def hp_changes(self) -> list[tuple[int, str, float, float]]:
        """(frame, prop, old, new) for tracked props (health etc.)."""
        last: dict[str, float] = {}
        out = []
        for s in self.frames:
            for k, v in (s.get("hp") or {}).items():
                if k in last and v != last[k]:
                    out.append((s["f"], k, last[k], v))
                last[k] = v
        return out


@dataclass
class ProbeData:
    id: str
    spec: dict                       # compiled probe (from plan.json)
    cal: Run | None
    runs: list[Run]
    dir: Path

    @property
    def plane(self) -> str:
        return self.spec.get("plane", "x")

    def ok_runs(self) -> list[Run]:
        return [r for r in self.runs if r.ok]


@dataclass
class MetricValue:
    value: float | None
    unit: str
    details: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    note: str = ""
    spread: float | None = None


@dataclass
class MetricDef:
    name: str
    unit: str
    description: str
    fn: Callable[[ProbeData], MetricValue]
    families: tuple[str, ...] = ()


REGISTRY: dict[str, MetricDef] = {}


def metric(name: str, unit: str, description: str):
    def deco(fn: Callable[[ProbeData], MetricValue]):
        REGISTRY[name] = MetricDef(name, unit, description, fn)
        return fn
    return deco


def none(unit: str, note: str, **details) -> MetricValue:
    return MetricValue(None, unit, details=details, note=note)


def load_run(path: Path) -> Run:
    lines = path.read_text().splitlines()
    header = json.loads(lines[0], strict=False) if lines else {}
    frames, process = [], []
    for line in lines[1:]:
        if not line.strip():
            continue
        d = json.loads(line, strict=False)
        if "process" in d:
            process = d["process"]
        elif not d.get("gone"):
            frames.append(d)
    return Run(header, frames, process, path)


def load_probe_data(run_dir: Path, compiled: dict) -> ProbeData:
    d = run_dir / compiled["id"]
    cal = None
    runs = []
    if (d / "_calibrate.jsonl").is_file():
        cal = load_run(d / "_calibrate.jsonl")
    for r in compiled["runs"]:
        f = d / (_safe(r["name"]) + ".jsonl")
        if f.is_file():
            runs.append(load_run(f))
    return ProbeData(compiled["id"], compiled, cal, runs, d)


def _safe(name: str) -> str:
    # mirrors String.validate_filename() for the characters our run names use
    for ch in ':/\\?*"|%<>':
        name = name.replace(ch, "_")
    return name


def median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def spread(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return (max(xs) - min(xs)) if len(xs) > 1 else None
