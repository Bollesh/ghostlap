"""`gamefeel scan`: engine extractor first, text fallback, incremental ingest into graph.db."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..graph import ingest
from ..graph.store import SCHEMA_VERSION, Graph
from ..project import GamefeelError, Project, addon_dir, ensure_imported, find_godot, godot_version, run_godot
from .changes import ChangeSet, FileInfo, compute_changes
from .text import extract_file_text, parse_project_text

DEPENDENT_EDGE_KINDS = ("instances", "inherits_scene", "extends", "attaches_script", "uses_resource")


@dataclass
class ScanResult:
    mode: str
    counts: dict
    dependents: int = 0
    full_rebuild: bool = False
    imported: bool = False
    warnings: list[str] = field(default_factory=list)
    file_errors: dict[str, list] = field(default_factory=dict)
    dangling_edges: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__


def scan(project: Project, godot: str | None = None, text_only: bool = False, full: bool = False,
         excludes: list[str] | None = None) -> ScanResult:
    t0 = time.monotonic()
    g = Graph(project.graph_db)
    godot_bin = None if text_only else find_godot(godot)
    warnings: list[str] = []
    if not text_only and godot_bin is None:
        warnings.append("Godot binary not found (set GAMEFEEL_GODOT or put `godot` on PATH) — using text fallback; probes need Godot.")
    gv = godot_version(godot_bin) if godot_bin else ""

    rebuild_reasons = []
    if full:
        rebuild_reasons.append("--full")
    if g.get_meta("schema_version") not in (None, str(SCHEMA_VERSION)):
        rebuild_reasons.append("schema version changed")
    if g.get_meta("gamefeel_version") not in (None, __version__):
        rebuild_reasons.append("gamefeel version changed")
    if godot_bin and g.get_meta("godot_version") not in (None, "", gv):
        rebuild_reasons.append("Godot version changed")
    if rebuild_reasons:
        g.clear()

    known = g.files()
    cs = compute_changes(project.root, known, excludes)

    # renames keep their graph rows; content changes are re-extracted below
    for old, info in cs.renamed:
        g.rename_file(old, info.res)
        prev = known[old]
        g.db.execute("DELETE FROM files WHERE path=?", (old,))
        if prev.get("hash") != info.hash:
            cs.changed.append(info)
        else:
            _record_file(g, info, prev.get("kind"), prev.get("source"), prev.get("errors", 0))

    for res in cs.deleted:
        g.drop_file(res)
        g.db.execute("DELETE FROM files WHERE path=?", (res,))

    changed_res = {c.res for c in cs.changed}
    dependents = _dependents(g, [c.res for c in cs.changed if c.res in known], changed_res)
    disk_unchanged = {r: known[r] for r in cs.unchanged}
    dep_infos = [FileInfo(r, project.root / r.removeprefix("res://"), disk_unchanged[r]["mtime"],
                          disk_unchanged[r]["size"], disk_unchanged[r]["hash"], disk_unchanged[r]["uid"])
                 for r in sorted(dependents) if r in disk_unchanged]
    # files last scanned in text mode get upgraded once Godot is available
    upgrade = [] if not godot_bin else [
        FileInfo(r, project.root / r.removeprefix("res://"), known[r]["mtime"], known[r]["size"], known[r]["hash"], known[r]["uid"])
        for r in cs.unchanged if known[r].get("source") == "text" and r not in dependents
    ]
    to_extract = [c for c in cs.changed if c.res != "res://project.godot"] + dep_infos + upgrade

    mode = "engine" if godot_bin else "text"
    imported = False
    engine_out: dict | None = None
    need_project = (cs.project_changed or bool(rebuild_reasons) or "res://project.godot" not in known
                    or (godot_bin is not None and known.get("res://project.godot", {}).get("source") == "text"))
    if godot_bin and (to_extract or need_project):
        try:
            imported = ensure_imported(godot_bin, project)
            engine_out = _run_engine(godot_bin, project, [f.res for f in to_extract])
        except GamefeelError as e:
            warnings.append(f"engine scan failed, using text fallback: {e}")
            mode = "text"

    # project settings
    if engine_out is not None and (need_project or to_extract):
        ingest.ingest_project(g, engine_out["project"])
        g.db.execute("UPDATE files SET source='engine' WHERE path='res://project.godot'")
    elif need_project:
        ingest.ingest_project(g, parse_project_text((project.root / "project.godot").read_text(errors="replace")))
    for info in cs.changed + cs.rehashed:
        if info.res == "res://project.godot":
            _record_file(g, info, "project", mode, 0)

    file_errors: dict[str, list] = {}
    for info in to_extract:
        rec = _record_for(project, info.res, engine_out)
        ingest.ingest_file(g, info.res, rec)
        errs = rec.get("errors") or []
        if errs:
            file_errors[info.res] = errs[:5]
        _record_file(g, info, rec.get("kind"), rec.get("source"), len(errs))

    for info in cs.rehashed:
        if info.res != "res://project.godot":
            g.db.execute("UPDATE files SET mtime=?, size=? WHERE path=?", (info.mtime, info.size, info.res))

    g.set_meta("schema_version", SCHEMA_VERSION)
    g.set_meta("gamefeel_version", __version__)
    if godot_bin:
        g.set_meta("godot_version", gv)
    g.set_meta("last_scan", time.time())
    g.set_meta("last_scan_mode", mode)
    dangling = g.db.execute(
        "SELECT COUNT(*) FROM edges e WHERE e.kind IN ('instances','attaches_script','inherits_scene','extends') "
        "AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = e.dst) AND e.dst NOT LIKE 'class:%'"
    ).fetchone()[0]
    g.commit()

    counts = cs.counts()
    counts["re_extracted"] = len(to_extract)
    counts["upgraded_to_engine"] = len(upgrade)
    return ScanResult(mode=mode, counts=counts, dependents=len(dep_infos), full_rebuild=bool(rebuild_reasons),
                      imported=imported, warnings=warnings + [f"full rebuild: {', '.join(rebuild_reasons)}"] * bool(rebuild_reasons),
                      file_errors=file_errors, dangling_edges=dangling, seconds=round(time.monotonic() - t0, 2))


def _dependents(g: Graph, modified: list[str], already: set[str]) -> set[str]:
    out: set[str] = set()
    for res in modified:
        owned = [r[0] for r in g.db.execute("SELECT id FROM nodes WHERE file=?", (res,))]
        for nid in owned:
            for kind in DEPENDENT_EDGE_KINDS:
                for row in g.db.execute("SELECT DISTINCT file FROM edges WHERE dst=? AND kind=?", (nid, kind)):
                    f = row[0]
                    if f and f != res and f not in already and f != "res://project.godot":
                        out.add(f)
    return out


def _run_engine(godot: str, project: Project, files: list[str]) -> dict:
    state = project.state
    list_path = state / "scan_files.txt"
    out_path = state / "scan_engine.json"
    list_path.write_text("\n".join(files) + "\n")
    if out_path.exists():
        out_path.unlink()
    script = addon_dir() / "scan" / "scan.gd"
    proc = run_godot(godot, project, ["--script", str(script), "--", f"--out={out_path}", f"--files={list_path}"])
    if not out_path.exists():
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise GamefeelError(f"Godot exited {proc.returncode} without scan output. Last output:\n{tail}")
    try:
        # Godot's JSON.stringify doesn't escape every control character; be lenient
        return json.loads(out_path.read_text(encoding="utf-8", errors="replace"), strict=False)
    except json.JSONDecodeError as e:
        raise GamefeelError(f"unreadable scan output from Godot: {e}") from e


def _record_for(project: Project, res: str, engine_out: dict | None) -> dict:
    text_rec = None
    if engine_out is None or res not in engine_out.get("files", {}):
        return extract_file_text(project.root, res)
    eng = engine_out["files"][res]
    if eng.get("kind") == "script" and res.endswith(".gd"):
        text_rec = extract_file_text(project.root, res)
        return ingest.merge_script_records(eng, text_rec)
    if eng.get("load_failed"):
        text_rec = extract_file_text(project.root, res)
        text_rec["errors"] = list(text_rec.get("errors", [])) + list(eng.get("errors", []))
        text_rec["source"] = "text-fallback"  # engine tried and failed: don't retry until the file changes
        return text_rec
    if eng.get("kind") == "scene":  # keep .tscn line numbers for the agent
        text_rec = extract_file_text(project.root, res)
        lines = {n["path"]: n.get("line") for n in text_rec.get("nodes", [])}
        for n in eng.get("nodes", []):
            n["line"] = lines.get(n["path"])
    return eng


def _record_file(g: Graph, info: FileInfo, kind: str | None, source: str | None, errors: int) -> None:
    g.db.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, hash, uid, kind, source, scanned_at, errors) VALUES (?,?,?,?,?,?,?,?,?)",
        (info.res, info.mtime, info.size, info.hash, info.uid, kind, source, time.time(), errors),
    )


def changes_preview(project: Project) -> ChangeSet:
    g = Graph(project.graph_db)
    return compute_changes(project.root, g.files())
