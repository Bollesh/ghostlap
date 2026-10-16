"""SQLite graph store (.gamefeel/graph.db).

Every node and edge is owned by one file (`file` column) so re-scanning a file is
"delete what it owned, insert the new records". Shared nodes (actions, groups, engine
classes) have file = NULL and are upserted.

Node ids are readable on purpose — the agent sees them in query output:
    scene:res://player.tscn          node:res://player.tscn::Camera2D
    script:res://player.gd           func:res://player.gd::_physics_process
    prop:res://player.gd::speed      signal:res://player.gd::died
    action:jump   group:player   class:CharacterBody2D   autoload:Game
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, hash TEXT,
                                  uid TEXT, kind TEXT, source TEXT, scanned_at REAL, errors INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT, file TEXT,
                                  props JSON, source TEXT, confidence REAL);
CREATE TABLE IF NOT EXISTS edges (src TEXT, dst TEXT, kind TEXT, file TEXT, props JSON, source TEXT,
                                  PRIMARY KEY (src, dst, kind));
CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, value JSON, evidence JSON, source TEXT,
                                  confidence REAL, confirmed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS nodes_file ON nodes(file);
CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS edges_dst  ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS edges_file ON edges(file);
CREATE INDEX IF NOT EXISTS files_uid  ON files(uid);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(id UNINDEXED, name, path);
"""


class Graph:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS)
            self.has_fts = True
        except sqlite3.OperationalError:  # sqlite built without FTS5
            self.has_fts = False

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))

    # ------------------------------------------------------------ write
    def clear(self) -> None:
        for t in ("files", "nodes", "edges", "facts"):
            self.db.execute(f"DELETE FROM {t}")
        if self.has_fts:
            self.db.execute("DELETE FROM nodes_fts")

    def drop_file(self, file: str) -> None:
        if self.has_fts:
            self.db.execute("DELETE FROM nodes_fts WHERE id IN (SELECT id FROM nodes WHERE file=?)", (file,))
        self.db.execute("DELETE FROM nodes WHERE file=?", (file,))
        self.db.execute("DELETE FROM edges WHERE file=?", (file,))

    def add_node(self, id: str, kind: str, name: str | None, path: str | None, file: str | None,
                 props: dict | None = None, source: str = "engine", confidence: float = 1.0) -> None:
        if file is None:  # shared node: keep first, merge props
            row = self.db.execute("SELECT props FROM nodes WHERE id=?", (id,)).fetchone()
            if row:
                if props:
                    merged = {**json.loads(row[0] or "{}"), **props}
                    self.db.execute("UPDATE nodes SET props=? WHERE id=?", (json.dumps(merged), id))
                return
        self.db.execute(
            "INSERT OR REPLACE INTO nodes(id, kind, name, path, file, props, source, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (id, kind, name, path, file, json.dumps(props or {}), source, confidence),
        )
        if self.has_fts:
            self.db.execute("DELETE FROM nodes_fts WHERE id=?", (id,))
            self.db.execute("INSERT INTO nodes_fts(id, name, path) VALUES (?,?,?)", (id, name or "", path or ""))

    def add_edge(self, src: str, dst: str, kind: str, file: str | None, props: dict | None = None,
                 source: str = "engine") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO edges(src, dst, kind, file, props, source) VALUES (?,?,?,?,?,?)",
            (src, dst, kind, file, json.dumps(props or {}), source),
        )

    def rename_file(self, old: str, new: str) -> None:
        """Keep a renamed file's graph rows: rewrite ids/paths that embed the old res path."""
        for col_sql in (
            "UPDATE nodes SET id = replace(id, ?, ?), path = replace(path, ?, ?), file = ? WHERE file = ?",
        ):
            self.db.execute(col_sql, (old, new, old, new, new, old))
        self.db.execute("UPDATE edges SET src = replace(src, ?, ?) WHERE src LIKE ?", (old, new, f"%{old}%"))
        self.db.execute("UPDATE edges SET dst = replace(dst, ?, ?) WHERE dst LIKE ?", (old, new, f"%{old}%"))
        self.db.execute("UPDATE edges SET file = ? WHERE file = ?", (new, old))
        if self.has_fts:
            self.db.execute("UPDATE nodes_fts SET id = replace(id, ?, ?), path = replace(path, ?, ?) WHERE id LIKE ?",
                            (old, new, old, new, f"%{old}%"))

    def commit(self) -> None:
        self.db.commit()

    # ------------------------------------------------------------ read
    def node(self, id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
        return _row(row) if row else None

    def nodes(self, kind: str | None = None, where: str = "", params: Iterable = ()) -> list[dict]:
        sql = "SELECT * FROM nodes"
        conds, args = [], []
        if kind:
            conds.append("kind=?")
            args.append(kind)
        if where:
            conds.append(where)
            args.extend(params)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return [_row(r) for r in self.db.execute(sql, args)]

    def out(self, src: str, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.db.execute("SELECT * FROM edges WHERE src=? AND kind=?", (src, kind))
        else:
            rows = self.db.execute("SELECT * FROM edges WHERE src=?", (src,))
        return [_row(r) for r in rows]

    def inc(self, dst: str, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.db.execute("SELECT * FROM edges WHERE dst=? AND kind=?", (dst, kind))
        else:
            rows = self.db.execute("SELECT * FROM edges WHERE dst=?", (dst,))
        return [_row(r) for r in rows]

    def edges(self, kind: str) -> list[dict]:
        return [_row(r) for r in self.db.execute("SELECT * FROM edges WHERE kind=?", (kind,))]

    def files(self) -> dict[str, dict]:
        return {r["path"]: dict(r) for r in self.db.execute("SELECT * FROM files")}

    def search(self, text: str, limit: int = 20) -> list[dict]:
        if self.has_fts:
            q = " ".join(f'"{t}"*' for t in text.replace('"', " ").split() if t)
            try:
                rows = self.db.execute(
                    "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.id = f.id WHERE nodes_fts MATCH ? LIMIT ?",
                    (q, limit),
                ).fetchall()
                return [_row(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{text}%"
        rows = self.db.execute("SELECT * FROM nodes WHERE name LIKE ? OR path LIKE ? LIMIT ?", (like, like, limit))
        return [_row(r) for r in rows]

    def sql(self, query: str) -> list[dict]:
        """Read-only escape hatch."""
        uri = f"file:{self.path}?mode=ro"
        ro = sqlite3.connect(uri, uri=True)
        ro.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in ro.execute(query).fetchmany(500)]
        finally:
            ro.close()


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("props", "value", "evidence"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d
