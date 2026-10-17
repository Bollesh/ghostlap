"""Apply value changes to project files: script defaults, scene overrides, project settings.

Used by `ghostlap feel propose` (on a scratch copy) and `ghostlap feel apply` (on the real project).
Only literal values are edited; anything else (an expression, a const reference) is reported
as needing a manual edit rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .scan.godot_text import _parse_header

_NUM = r"[-+]?(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][-+]?\d+)?"
_LITERAL = rf"(?:{_NUM}|true|false)"


@dataclass
class Edit:
    kind: str                 # property | node_property | setting
    file: str                 # res:// path (project.godot for settings)
    property: str             # property name, or setting path for kind=setting
    value: Any
    original: Any = None
    node: str | None = None   # scene node path for .tscn edits

    @property
    def key(self) -> str:
        return f"{self.file}::{self.node or ''}::{self.property}"

    def to_dict(self) -> dict:
        return asdict(self)


def edits_from_suggestion(s: dict) -> Edit | None:
    """A numeric/bool suggestion as an Edit. Text-only suggestions ("multiply by…") return None."""
    v = s.get("proposed")
    if not isinstance(v, (int, float, bool)):
        return None
    if s["kind"] == "property":
        if s.get("scene_node") or str(s.get("file", "")).endswith(".tscn"):
            return Edit("node_property", s["file"], s["property"], v, s.get("current"), node=s.get("scene_node") or ".")
        return Edit("property", s["file"], s["property"], v, s.get("current"))
    if s["kind"] == "node_property":
        return Edit("node_property", s["file"], s["property"], v, s.get("current"), node=s.get("node") or ".")
    if s["kind"] == "setting":
        return Edit("setting", "res://project.godot", s["setting"], v, s.get("current"))
    return None


def format_value(v: Any, like: Any = None) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) or (isinstance(like, int) and not isinstance(like, bool) and float(v).is_integer()):
        return str(int(round(v)))
    text = f"{float(v):.4f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _parse_literal(text: str) -> Any:
    t = text.strip()
    if t in ("true", "false"):
        return t == "true"
    clean = t.replace("_", "")
    try:
        return int(clean) if re.fullmatch(r"[-+]?\d+", clean) else float(clean)
    except ValueError:
        return None


def read_value(root: Path, e: Edit) -> Any:
    """Current literal value of the edit's target in the file (None if missing/not literal)."""
    path = root / e.file.removeprefix("res://")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if e.kind == "property":
        m = _gd_var_re(e.property).search(text)
        return _parse_literal(m.group(2)) if m else None
    if e.kind == "node_property":
        span = _tscn_node_span(text, e.node or ".")
        if span is None:
            return None
        m = re.search(rf"^{re.escape(e.property)} = ({_LITERAL})\s*$", text[span[0]:span[1]], re.M)
        return _parse_literal(m.group(1)) if m else None
    if e.kind == "setting":
        section, key = e.property.split("/", 1)
        span = _ini_section_span(text, section)
        if span is None:
            return None
        m = re.search(rf"^{re.escape(key)}=({_LITERAL})\s*$", text[span[0]:span[1]], re.M)
        return _parse_literal(m.group(1)) if m else None
    return None


def apply_edit(root: Path, e: Edit) -> tuple[bool, str]:
    path = root / e.file.removeprefix("res://")
    if not path.is_file():
        return False, f"{e.file} not found"
    text = path.read_text(encoding="utf-8")
    new_text, msg = _apply_text(text, e)
    if new_text is None:
        return False, msg
    path.write_text(new_text, encoding="utf-8")
    return True, msg


def _gd_var_re(name: str) -> re.Pattern:
    # `@export var speed := 220.0`, `var speed: float = 220.0`, `@export_range(0, 1) var x = 0.5  # c`
    return re.compile(rf"^([^\S\n]*(?:@[^\n]*?\s)?var\s+{re.escape(name)}\b[^=\n]*?(?::=|=)\s*)({_LITERAL})(?=\s*(?:#.*)?$)", re.M)


def _apply_text(text: str, e: Edit) -> tuple[str | None, str]:
    if e.kind == "property":
        rx = _gd_var_re(e.property)
        m = rx.search(text)
        if not m:
            return None, f"`var {e.property}` in {e.file} has no literal default to edit — change it by hand"
        like = _parse_literal(m.group(2))
        return text[:m.start(2)] + format_value(e.value, like) + text[m.end(2):], f"{e.property} = {format_value(e.value, like)}"
    if e.kind == "node_property":
        span = _tscn_node_span(text, e.node or ".")
        if span is None:
            return None, f"node '{e.node}' not found in {e.file}"
        a, b = span
        section = text[a:b]
        rx = re.compile(rf"^{re.escape(e.property)} = ({_LITERAL})\s*$", re.M)
        m = rx.search(section)
        if m:
            like = _parse_literal(m.group(1))
            section = section[:m.start(1)] + format_value(e.value, like) + section[m.end(1):]
        elif re.search(rf"^{re.escape(e.property)} = ", section, re.M):
            return None, f"{e.property} on {e.node} isn't a literal — change it by hand"
        else:
            nl = section.index("\n") + 1 if "\n" in section else len(section)
            section = section[:nl] + f"{e.property} = {format_value(e.value, e.original)}\n" + section[nl:]
        return text[:a] + section + text[b:], f"{e.node}.{e.property} = {format_value(e.value, e.original)}"
    if e.kind == "setting":
        section, key = e.property.split("/", 1)
        span = _ini_section_span(text, section)
        line = f"{key}={format_value(e.value, e.original)}"
        if span is None:
            return text.rstrip("\n") + f"\n\n[{section}]\n\n{line}\n", line
        a, b = span
        sec = text[a:b]
        rx = re.compile(rf"^{re.escape(key)}=.*$", re.M)
        sec = rx.sub(line, sec, count=1) if rx.search(sec) else sec.rstrip("\n") + f"\n{line}\n\n"
        return text[:a] + sec + text[b:], line
    return None, f"unknown edit kind {e.kind}"


def _tscn_node_span(text: str, node_path: str) -> tuple[int, int] | None:
    """Character span of a [node ...] section (header line included) for a scene node path."""
    headers = [m for m in re.finditer(r"^\[(.+)\]\s*$", text, re.M)]
    for i, m in enumerate(headers):
        tag, attrs = _parse_header(m.group(1))
        if tag != "node":
            continue
        parent = attrs.get("parent")
        name = attrs.get("name")
        path = "." if parent is None else (str(name) if parent == "." else f"{parent}/{name}")
        if path == node_path:
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            return m.start(), end
    return None


def _ini_section_span(text: str, section: str) -> tuple[int, int] | None:
    m = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.M)
    if not m:
        return None
    nxt = re.search(r"^\[[^\]]+\]\s*$", text[m.end():], re.M)
    return m.start(), (m.end() + nxt.start()) if nxt else len(text)
