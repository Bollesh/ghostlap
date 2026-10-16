"""Locating the Godot project, the Godot binary, the addon scripts, and running Godot."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

STATE_DIR = ".gamefeel"


class GamefeelError(RuntimeError):
    """An error with a message meant for the user (and the agent)."""


@dataclass
class Project:
    root: Path

    @property
    def state(self) -> Path:
        d = self.root / STATE_DIR
        d.mkdir(exist_ok=True)
        gi = d / ".gitignore"
        if not gi.exists():
            gi.write_text("# gamefeel working files (graph, probe runs). Commit profile.yaml if you like.\n*\n!profile.yaml\n!.gitignore\n")
        return d

    @property
    def graph_db(self) -> Path:
        return self.state / "graph.db"

    @property
    def profile_path(self) -> Path:
        return self.state / "profile.yaml"


def find_project(start: str | Path | None = None) -> Project:
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / "project.godot").is_file():
            return Project(d)
    raise GamefeelError(f"no project.godot found in {p} or its parents — run gamefeel inside a Godot project or pass --project")


def find_godot(explicit: str | None = None) -> str | None:
    for cand in (explicit, os.environ.get("GAMEFEEL_GODOT"), "godot", "godot4"):
        if not cand:
            continue
        found = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if found:
            return found
    return None


@lru_cache(maxsize=8)
def godot_version(godot: str) -> str:
    try:
        out = subprocess.run([godot, "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def addon_dir() -> Path:
    """The gamefeel GDScript runtime: packaged copy first, then the repo checkout."""
    here = Path(__file__).resolve().parent
    for cand in (here / "_addon", here.parents[2] / "addons" / "gamefeel"):
        if (cand / "scan" / "scan.gd").is_file():
            return cand
    raise GamefeelError("gamefeel's GDScript runtime (addons/gamefeel) was not found next to the package")


def run_godot(godot: str, project: Project, args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    cmd = [godot, "--headless", "--path", str(project.root), *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise GamefeelError(f"Godot timed out after {timeout:.0f}s: {' '.join(cmd)}") from e


def ensure_imported(godot: str, project: Project) -> bool:
    """Godot needs .godot/ (import cache) before scenes load. Returns True if an import ran."""
    if (project.root / ".godot").is_dir():
        return False
    proc = run_godot(godot, project, ["--import"], timeout=900)
    if not (project.root / ".godot").is_dir():
        raise GamefeelError("`godot --import` did not create .godot/:\n" + proc.stderr[-2000:])
    return True
