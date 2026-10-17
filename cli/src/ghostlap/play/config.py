"""Session settings and where each value came from (design §3.3.1).

The most specific source wins: `play config` during the session, then `play start` flags, then
the goal's `shots:`, then the project's ghostlap.cfg, then the defaults.

    ghostlap.cfg                     goal.yaml
    [shots]                          shots:
    rolling_every = 600                rolling: {every: 300, keep: 10}
    rolling_keep = 5                   on: [incident, death, success]
    on = start, incident, death        width: 960
    width = 960
    [look]
    radius = 12
    views = player, near, map
"""

from __future__ import annotations

import configparser
from pathlib import Path

from ..project import GhostlapError

TRIGGERS = ["start", "scene_change", "incident", "death", "success", "ui"]

# key -> (default, meaning)
KEYS: dict[str, tuple[object, str]] = {
    "shots.rolling.every": (600, "frames between rolling captures (0: off)"),
    "shots.rolling.keep": (5, "rolling captures kept (all: every one; 0: rolling off)"),
    "shots.on": (TRIGGERS, "events that capture: " + ", ".join(TRIGGERS)),
    "shots.width": (960, "width of saved shots in px (wider costs more tokens to read)"),
    "look.radius": (12.0, "`near` radius in player heights"),
    "look.map_w": (32, "`map` width in cells"),
    "look.map_h": (12, "`map` height in cells"),
    "look.views": (None, "default look views (none: the profile's)"),
}
CFG_FILE = "ghostlap.cfg"


def _list(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("a list")
    return [str(x).strip() for x in raw if str(x).strip()]


def _int(raw, lo: int, hi: int) -> int:
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"a whole number from {lo} to {hi}") from None
    if not lo <= v <= hi:
        raise ValueError(f"a whole number from {lo} to {hi}")
    return v


def parse(key: str, raw):
    """A setting's value from a string (cfg, command line) or YAML value; raises GhostlapError."""
    if key not in KEYS:
        raise GhostlapError(f"unknown setting {key!r} (settings: {', '.join(KEYS)})")
    try:
        if key == "shots.rolling.every":
            return _int(raw, 0, 10**7)
        if key == "shots.rolling.keep":
            return "all" if str(raw).strip().lower() == "all" else _int(raw, 0, 10**5)
        if key == "shots.on":
            if isinstance(raw, str) and raw.strip().lower() in ("all", "none", ""):
                return list(TRIGGERS) if raw.strip().lower() == "all" else []
            on = _list(raw)
            bad = [t for t in on if t not in TRIGGERS]
            if bad:
                raise ValueError(f"triggers from {', '.join(TRIGGERS)} (or all / none), not {', '.join(bad)}")
            return [t for t in TRIGGERS if t in on]
        if key == "shots.width":
            return _int(raw, 64, 4096)
        if key == "look.radius":
            try:
                v = float(str(raw).strip())
            except ValueError:
                raise ValueError("a number of player heights") from None
            if v <= 0:
                raise ValueError("a positive number of player heights")
            return v
        if key in ("look.map_w", "look.map_h"):
            return _int(raw, 3, 200)
        if key == "look.views":
            if isinstance(raw, str) and raw.strip().lower() in ("", "none", "profile"):
                return None
            return _list(raw)
    except ValueError as e:
        raise GhostlapError(f"{key} must be {e}") from None
    raise AssertionError(key)


def from_cfg(root: Path) -> dict:
    """[section] name = value from <project>/ghostlap.cfg (`rolling_every` is shots.rolling.every)."""
    path = Path(root) / CFG_FILE
    if not path.is_file():
        return {}
    cp = configparser.ConfigParser()
    try:
        cp.read(path)
    except configparser.Error as e:
        raise GhostlapError(f"{path}: {e}") from e
    out = {}
    for sec in cp.sections():
        for name, raw in cp.items(sec):
            key = f"{sec}.{name.replace('_', '.')}" if sec == "shots" else f"{sec}.{name}"
            if key not in KEYS:
                raise GhostlapError(f"{path}: unknown setting [{sec}] {name}")
            out[key] = parse(key, raw)
    return out


def from_goal(shots, where: str = "goal") -> dict:
    """The goal's `shots:` block, flattened."""
    if shots is None:
        return {}
    if not isinstance(shots, dict):
        raise GhostlapError(f"{where}: shots must be a mapping like {{rolling: {{every: 300, keep: 10}}, on: [incident]}}")
    out = {}
    for k, v in shots.items():
        if k is True:
            k = "on"   # YAML 1.1 (PyYAML) reads a bare `on:` key as true
        if k == "rolling":
            if not isinstance(v, dict):
                raise GhostlapError(f"{where}: shots.rolling must be {{every: N, keep: N}}")
            for rk, rv in v.items():
                out[f"shots.rolling.{rk}"] = rv
        else:
            out[f"shots.{k}"] = v
    try:
        return {k: parse(k, v) for k, v in out.items()}
    except GhostlapError as e:
        raise GhostlapError(f"{where}: {e}") from None


def rolling_flag(spec: str) -> dict:
    """`--rolling-shots 300:10` (every:keep), `300`, or `off`."""
    s = spec.strip().lower()
    if s == "off":
        return {"shots.rolling.every": 0}
    every, _, keep = s.partition(":")
    out = {"shots.rolling.every": parse("shots.rolling.every", every)}
    if keep:
        out["shots.rolling.keep"] = parse("shots.rolling.keep", keep)
    return out


def resolve(layers: list[tuple[str, dict]]) -> dict:
    """{key: {"value", "source"}}; later layers win."""
    out = {k: {"value": d, "source": "default"} for k, (d, _) in KEYS.items()}
    for source, values in layers:
        for k, v in values.items():
            out[k] = {"value": v, "source": source}
    return out


def value(cfg: dict, key: str):
    e = cfg.get(key)
    return e["value"] if e else KEYS[key][0]


def bridge_shots(cfg: dict) -> dict:
    keep = value(cfg, "shots.rolling.keep")
    return {"every": value(cfg, "shots.rolling.every"), "keep": -1 if keep == "all" else keep,
            "on": value(cfg, "shots.on"), "width": value(cfg, "shots.width")}


def describe(cfg: dict) -> list[str]:
    lines = []
    for k, (_, meaning) in KEYS.items():
        v = value(cfg, k)
        shown = ", ".join(v) if isinstance(v, list) else ("none" if v is None else v)
        lines.append(f"{k} = {shown}  ({cfg.get(k, {}).get('source', 'default')}) · {meaning}")
    return lines
