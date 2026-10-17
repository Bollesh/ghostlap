"""Genre packs: detection rules, probes (YAML) and reference-range profiles.

Layout of a pack directory:

    <pack>/pack.yaml           name, title, dims, includes, detect rules, default_style, feel words
    <pack>/probes/*.yaml       declarative probes
    <pack>/profiles/<style>.yaml  reference ranges per metric

Built-in packs live next to this file. Extra packs are loaded from <project>/.ghostlap/packs/
and from directories listed in GHOSTLAP_PACKS (os.pathsep-separated); a later pack with the
same name overrides an earlier one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BUILTIN_DIR = Path(__file__).resolve().parent


@dataclass
class Pack:
    name: str
    title: str
    dir: Path
    dims: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    selectable: bool = True
    detect: dict = field(default_factory=dict)
    default_style: str = "default"
    feel: dict = field(default_factory=dict)
    probes: dict[str, Path] = field(default_factory=dict)
    profiles: dict[str, Path] = field(default_factory=dict)
    description: str = ""
    vars: dict = field(default_factory=dict)
    plane: str | None = None


def _load_pack(d: Path) -> Pack | None:
    meta_path = d / "pack.yaml"
    if not meta_path.is_file():
        return None
    meta = yaml.safe_load(meta_path.read_text()) or {}
    probes = {p.stem: p for p in sorted((d / "probes").glob("*.yaml"))} if (d / "probes").is_dir() else {}
    profiles = {p.stem: p for p in sorted((d / "profiles").glob("*.yaml"))} if (d / "profiles").is_dir() else {}
    return Pack(
        name=meta.get("name", d.name),
        title=meta.get("title", d.name),
        dir=d,
        dims=meta.get("dims", []),
        includes=meta.get("includes", []),
        selectable=meta.get("selectable", True),
        detect=meta.get("detect", {}),
        default_style=meta.get("default_style", "default"),
        feel=meta.get("feel", {}),
        probes=probes,
        profiles=profiles,
        description=meta.get("description", ""),
        vars=meta.get("vars", {}) or {},
        plane=meta.get("plane"),
    )


def pack_dirs(project_root: Path | None = None) -> list[Path]:
    dirs = [BUILTIN_DIR]
    for extra in os.environ.get("GHOSTLAP_PACKS", "").split(os.pathsep):
        if extra:
            dirs.append(Path(extra))
    if project_root is not None:
        dirs.append(project_root / ".ghostlap" / "packs")
    return dirs


def load_packs(project_root: Path | None = None) -> dict[str, Pack]:
    packs: dict[str, Pack] = {}
    for base in pack_dirs(project_root):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir():
                p = _load_pack(d)
                if p:
                    packs[p.name] = p
    return packs


def expand(packs: dict[str, Pack], names: list[str]) -> list[Pack]:
    """Selected packs plus everything they include (depth-first, de-duplicated, includes first)."""
    out: list[Pack] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen or n not in packs:
            return
        seen.add(n)
        for inc in packs[n].includes:
            visit(inc)
        out.append(packs[n])

    for n in names:
        visit(n)
    return out


def merged_vars(packs: dict[str, Pack], genres: list[str]) -> dict:
    out: dict = {}
    for pack in expand(packs, genres):
        out.update(pack.vars)
    return out


def merged_feel(packs: dict[str, Pack], genres: list[str]) -> dict:
    out: dict = {}
    for pack in expand(packs, genres):
        out.update(pack.feel)
    return out


def default_plane(packs: dict[str, Pack], genres: list[str], dims: str | None) -> str:
    for g in genres:
        if g in packs and packs[g].plane:
            return packs[g].plane
    return "xz" if dims == "3d" else "x"


def load_ranges(packs: dict[str, Pack], genres: list[str], style: str | None) -> dict[str, dict]:
    """Merge reference ranges. Included packs first; a later (more specific) pack overrides a metric.

    For a metric defined by two *selected* genres (hybrids) the ranges are intersected; if the
    intersection is empty the first selected genre wins and the conflict is recorded.
    """
    merged: dict[str, dict] = {}
    by_genre: dict[str, dict[str, dict]] = {}
    for pack in expand(packs, genres):
        st = style if style in pack.profiles else pack.default_style if pack.default_style in pack.profiles else next(iter(pack.profiles), None)
        if st is None:
            continue
        data = yaml.safe_load(pack.profiles[st].read_text()) or {}
        calibrated = bool(data.get("calibrated", False))
        for metric, spec in (data.get("metrics") or {}).items():
            entry = {**spec, "pack": pack.name, "style": st, "calibrated": calibrated}
            if pack.name in genres:
                by_genre.setdefault(metric, {})[pack.name] = entry
            merged[metric] = entry
    for metric, per in by_genre.items():
        if len(per) < 2:
            continue
        entries = [per[g] for g in genres if g in per]
        good = _intersect([e.get("good") for e in entries])
        warn = _intersect([e.get("warn") for e in entries])
        if good is None:
            merged[metric] = {**entries[0], "conflict": [e["pack"] for e in entries]}
        else:
            merged[metric] = {**entries[0], "good": good, "warn": warn or entries[0].get("warn"),
                              "pack": "+".join(e["pack"] for e in entries)}
    return merged


def _intersect(ranges: list) -> list | None:
    ranges = [r for r in ranges if r]
    if not ranges:
        return None
    lo = max(r[0] for r in ranges)
    hi = min(r[1] for r in ranges)
    return [lo, hi] if lo <= hi else None
