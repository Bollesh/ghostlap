"""Incremental change detection against the last scan (the `files` table), not git.

1. walk the project (skipping .godot/, *.import, .gdignore'd folders, .gamefeel/, excludes)
2. mtime + size unchanged          -> unchanged (no read)
3. changed metadata, same hash     -> rehashed (no re-parse)
4. different hash / new file       -> changed
5. in table but missing on disk    -> deleted
6. deleted + added with same uid (Godot 4.4+ .uid sidecars / scene headers) or same hash -> renamed
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SCANNED_EXT = {".gd", ".cs", ".tscn", ".scn", ".tres", ".res"}
SKIP_DIRS = {".godot", ".gamefeel", ".git", ".import", ".venv", "node_modules", "__pycache__"}
_UID_RE = re.compile(rb'uid="(uid://[a-z0-9]+)"')


@dataclass
class FileInfo:
    res: str
    path: Path
    mtime: float
    size: int
    hash: str | None = None
    uid: str | None = None


@dataclass
class ChangeSet:
    unchanged: list[str] = field(default_factory=list)
    rehashed: list[FileInfo] = field(default_factory=list)
    changed: list[FileInfo] = field(default_factory=list)   # new or modified
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, FileInfo]] = field(default_factory=list)  # (old res, new info)
    project_changed: bool = False

    def counts(self) -> dict[str, int]:
        return {
            "unchanged": len(self.unchanged), "rehashed": len(self.rehashed),
            "changed": len(self.changed), "deleted": len(self.deleted), "renamed": len(self.renamed),
        }


def file_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_uid(path: Path) -> str | None:
    side = path.with_name(path.name + ".uid")
    if side.is_file():
        try:
            return side.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    if path.suffix in (".tscn", ".tres"):
        try:
            with path.open("rb") as f:
                m = _UID_RE.search(f.readline())
            return m.group(1).decode() if m else None
        except OSError:
            return None
    return None


def _excluded(rel: str, excludes: list[str]) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in excludes)


def walk_project(root: Path, excludes: list[str] | None = None) -> dict[str, FileInfo]:
    excludes = [e.strip("/") for e in (excludes or []) if e.strip("/")]
    out: dict[str, FileInfo] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_dir = "" if d == root else d.relative_to(root).as_posix()
        if rel_dir and ((d / ".gdignore").exists() or _excluded(rel_dir, excludes)):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(n for n in dirnames if n not in SKIP_DIRS and not n.startswith("."))
        for fn in filenames:
            p = d / fn
            if p.suffix.lower() not in SCANNED_EXT:
                continue
            rel = p.relative_to(root).as_posix()
            if _excluded(rel, excludes):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out["res://" + rel] = FileInfo("res://" + rel, p, st.st_mtime, st.st_size)
    return out


def compute_changes(root: Path, known: dict[str, dict], excludes: list[str] | None = None) -> ChangeSet:
    cs = ChangeSet()
    disk = walk_project(root, excludes)
    for res, info in disk.items():
        prev = known.get(res)
        if prev and prev["mtime"] == info.mtime and prev["size"] == info.size:
            cs.unchanged.append(res)
            continue
        info.hash = file_hash(info.path)
        info.uid = file_uid(info.path)
        if prev and prev["hash"] == info.hash:
            cs.rehashed.append(info)
        else:
            cs.changed.append(info)
    project_res = "res://project.godot"
    pg = root / "project.godot"
    prev = known.get(project_res)
    st = pg.stat()
    if not prev or prev["mtime"] != st.st_mtime or prev["size"] != st.st_size:
        info = FileInfo(project_res, pg, st.st_mtime, st.st_size, file_hash(pg))
        if prev and prev["hash"] == info.hash:
            cs.rehashed.append(info)
        else:
            cs.project_changed = True
            cs.changed.append(info)

    gone = [r for r in known if r not in disk and r != project_res]
    new = {c.res: c for c in cs.changed if c.res not in known and c.res != project_res}
    by_uid = {c.uid: c for c in new.values() if c.uid}
    by_hash = {c.hash: c for c in new.values()}
    for old in gone:
        prev = known[old]
        match = by_uid.get(prev.get("uid")) if prev.get("uid") else None
        if match is None:
            match = by_hash.get(prev.get("hash"))
        if match is not None and match.res in new:
            cs.renamed.append((old, match))
            new.pop(match.res)
            cs.changed = [c for c in cs.changed if c.res != match.res]
        else:
            cs.deleted.append(old)
    return cs
