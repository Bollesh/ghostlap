"""Source-level facts from GDScript files, via the tree-sitter GDScript grammar.

Used in both scan modes: the engine extractor gives exact structure (types, inheritance,
exported properties), and this pass adds what reflection can't see — line numbers, which
input actions each function reads, what it assigns, what it emits and calls.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

INPUT_CALLS = {
    "is_action_pressed", "is_action_just_pressed", "is_action_just_released", "is_action_released",
    "get_action_strength", "get_action_raw_strength", "get_axis", "get_vector", "is_action",
    "action_press", "action_release",
}


@lru_cache(maxsize=1)
def _parser():
    import tree_sitter as ts
    import tree_sitter_gdscript as tsg

    return ts.Parser(ts.Language(tsg.language()))


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _strip_str(s: str) -> str | None:
    m = re.fullmatch(r'[&^]?"((?:[^"\\]|\\.)*)"|[&^]?\'((?:[^\'\\]|\\.)*)\'', s.strip())
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


_NUM_RE = re.compile(r"[+-]?(\d[\d_]*\.?[\d_]*|\.\d[\d_]*)([eE][+-]?\d+)?")


def literal_value(text: str | None):
    """A GDScript literal as a Python value (numbers, bools, strings); None if it isn't one."""
    if text is None:
        return None
    t = text.strip()
    if t in ("true", "false"):
        return t == "true"
    if _NUM_RE.fullmatch(t):
        clean = t.replace("_", "")
        try:
            return int(clean) if re.fullmatch(r"[+-]?\d+", clean) else float(clean)
        except ValueError:
            return None
    s = _strip_str(t)
    return s


def _string_args(args_node) -> list[str | None]:
    out: list[str | None] = []
    if args_node is None:
        return out
    for c in args_node.named_children:
        out.append(_strip_str(_text(c)))
    return out


def _line(node) -> int:
    return node.start_point[0] + 1


def _child_of_type(node, *types):
    for c in node.children:
        if c.type in types:
            return c
    return None


def parse_gdscript(source: str) -> dict[str, Any]:
    """Return a script record (without engine-only fields)."""
    tree = _parser().parse(source.encode("utf-8"))
    root = tree.root_node
    rec: dict[str, Any] = {
        "kind": "script",
        "language": "gdscript",
        "class_name": None,
        "extends": None,
        "tool": False,
        "methods": [],
        "signals": [],
        "properties": [],
        "constants": {},
        "inner_classes": [],
        "facts": {"reads_input": [], "emits": [], "assigns": [], "calls": [], "uses": []},
        "errors": [],
    }
    if root.has_error:
        rec["errors"].append({"kind": "parse", "message": "tree-sitter reported syntax errors", "line": _first_error_line(root)})
    rec["_lines"] = source.splitlines()
    _walk_class_body(root, rec, prefix="")
    rec.pop("_lines", None)
    return rec


def _first_error_line(node) -> int | None:
    if node.type == "ERROR" or node.is_missing:
        return _line(node)
    for c in node.children:
        if c.has_error:
            r = _first_error_line(c)
            if r:
                return r
    return None


def _walk_class_body(body, rec: dict, prefix: str) -> None:
    for node in body.named_children:
        t = node.type
        if t == "class_name_statement" and not prefix:
            name = node.child_by_field_name("name") or _child_of_type(node, "name")
            rec["class_name"] = _text(name) if name else None
        elif t == "extends_statement" and not prefix:
            target = node.named_children[0] if node.named_children else None
            if target is not None:
                raw = _text(target)
                rec["extends"] = _strip_str(raw) if target.type == "string" else raw
        elif t == "annotation" and _text(node) == "@tool" and not prefix:
            rec["tool"] = True
        elif t == "variable_statement":
            _variable(node, rec, prefix)
        elif t == "const_statement":
            name = node.child_by_field_name("name") or _child_of_type(node, "name")
            value = node.child_by_field_name("value") or (node.named_children[-1] if node.named_children else None)
            if name is not None:
                rec["constants"][prefix + _text(name)] = {"value": _text(value) if value is not None else None, "line": _line(node)}
        elif t == "signal_statement":
            name = node.child_by_field_name("name") or _child_of_type(node, "name")
            if name is not None:
                rec["signals"].append({"name": prefix + _text(name), "line": _line(node)})
        elif t == "function_definition":
            _function(node, rec, prefix)
        elif t == "class_definition":
            name = node.child_by_field_name("name") or _child_of_type(node, "name")
            cname = _text(name) if name else "?"
            rec["inner_classes"].append({"name": prefix + cname, "line": _line(node)})
            inner_body = node.child_by_field_name("body") or _child_of_type(node, "class_body", "body")
            if inner_body is not None:
                _walk_class_body(inner_body, rec, prefix=prefix + cname + ".")


def _variable(node, rec: dict, prefix: str) -> None:
    name = node.child_by_field_name("name")
    if name is None:
        return
    annotations = []
    ann = _child_of_type(node, "annotations")
    if ann is not None:
        annotations = [_text(a).split("(")[0] for a in ann.named_children]
    type_node = node.child_by_field_name("type")
    value = node.child_by_field_name("value")
    default_text = _text(value) if value is not None else None
    lit = literal_value(default_text)
    rec["properties"].append({
        "name": prefix + _text(name),
        "line": _line(node),
        "type": _text(type_node) if type_node is not None and type_node.type == "type" else None,
        "default": lit if lit is not None else default_text,
        "default_text": default_text,
        "exported": any(a.startswith("@export") for a in annotations),
        "annotations": annotations,
    })


def _function(node, rec: dict, prefix: str) -> None:
    name_node = node.child_by_field_name("name")
    fname = prefix + (_text(name_node) if name_node else "?")
    params = node.child_by_field_name("parameters")
    rec["methods"].append({
        "name": fname,
        "line": _line(node),
        "end_line": node.end_point[0] + 1,
        "args": [_text(p).split(":")[0].split("=")[0].strip() for p in params.named_children] if params else [],
    })
    body = node.child_by_field_name("body")
    if body is None:
        return
    facts = rec["facts"]
    seen_calls: set[str] = set()
    used: set[str] = set()

    stack = [body]
    while stack:
        n = stack.pop()
        t = n.type
        if t in ("call", "attribute_call"):
            ident = n.named_children[0] if n.named_children else None
            cname = _text(ident) if ident is not None and ident.type == "identifier" else None
            args = n.child_by_field_name("arguments")
            receiver = None
            if t == "attribute_call" and n.parent is not None and n.parent.type == "attribute":
                first = n.parent.named_children[0]
                if first is not n:
                    receiver = _text(first)
            if cname:
                if cname in INPUT_CALLS:
                    strs = _string_args(args)
                    actions = [s for s in strs if s]
                    if actions:
                        facts["reads_input"].append({
                            "func": fname, "call": cname, "receiver": receiver,
                            "actions": actions, "line": _line(n),
                        })
                if cname == "emit" and receiver:
                    facts["emits"].append({"func": fname, "signal": receiver.split(".")[-1], "line": _line(n)})
                elif cname == "emit_signal":
                    strs = _string_args(args)
                    if strs and strs[0]:
                        facts["emits"].append({"func": fname, "signal": strs[0], "line": _line(n)})
                key = (receiver + "." if receiver else "") + cname
                if key not in seen_calls:
                    seen_calls.add(key)
                    facts["calls"].append({"func": fname, "call": key, "line": _line(n)})
        elif t in ("assignment", "augmented_assignment"):
            left = n.child_by_field_name("left") or (n.named_children[0] if n.named_children else None)
            right = n.child_by_field_name("right") or (n.named_children[-1] if len(n.named_children) > 1 else None)
            op_node = next((c for c in n.children if not c.is_named), None)
            if left is not None:
                facts["assigns"].append({
                    "func": fname, "target": _text(left), "op": _text(op_node) if op_node else "=",
                    "value": _text(right)[:160] if right is not None else None, "line": _line(n),
                })
        elif t == "identifier":
            used.add(_text(n))
        stack.extend(reversed(n.children))
    if node.has_error or body.has_error:
        _regex_supplement(rec["_lines"], node.start_point[0] + 1, fname, facts, seen_calls, used)
    for ident in sorted(used):
        facts["uses"].append({"func": fname, "name": ident})


_CALL_RE = re.compile(r"(?:(\b[\w.]+)\.)?\b(\w+)\s*\(([^()]*)\)")
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][\w.\[\]]*)\s*(=|\+=|-=|\*=|/=)\s*(.+?)\s*$")
_STR_RE = re.compile(r'[&^]?"((?:[^"\\]|\\.)*)"')


def _regex_supplement(lines: list[str], func_line: int, fname: str, facts: dict, seen_calls: set[str], used: set[str]) -> None:
    """tree-sitter's error recovery can flatten a broken function body into an ERROR node (and
    push its tail to module level). Recover input reads, emits, calls and assignments line by
    line, from the `func` line to the next unindented declaration, so one syntax error doesn't
    hide what the function does."""
    end = len(lines)
    for j in range(func_line, len(lines)):  # func_line is 1-based, so this starts after it
        ln = lines[j]
        if ln.strip() and not ln[0].isspace() and not ln.lstrip().startswith("#"):
            end = j
            break
    known_reads = {(r["line"], r["call"]) for r in facts["reads_input"]}
    known_assigns = {(a["line"], a["target"]) for a in facts["assigns"]}
    for i in range(func_line, end):
        line = lines[i]
        lineno = i + 1
        code = line.split("#", 1)[0]
        for m in _CALL_RE.finditer(code):
            receiver, cname, args = m.group(1), m.group(2), m.group(3)
            if cname in INPUT_CALLS and (lineno, cname) not in known_reads:
                actions = _STR_RE.findall(args)
                if actions:
                    facts["reads_input"].append({"func": fname, "call": cname, "receiver": receiver,
                                                 "actions": actions, "line": lineno, "recovered": True})
            if cname == "emit" and receiver:
                facts["emits"].append({"func": fname, "signal": receiver.split(".")[-1], "line": lineno})
            key = (receiver + "." if receiver else "") + cname
            if key not in seen_calls:
                seen_calls.add(key)
                facts["calls"].append({"func": fname, "call": key, "line": lineno})
        # also catch calls whose closing paren is the syntax error, e.g. `move_and_slide(`
        for m in re.finditer(r"\b(\w+)\s*\(", code):
            if m.group(1) not in seen_calls and m.group(1) not in ("if", "elif", "while", "for", "func"):
                seen_calls.add(m.group(1))
                facts["calls"].append({"func": fname, "call": m.group(1), "line": lineno})
        am = _ASSIGN_RE.match(code)
        if am and (lineno, am.group(1)) not in known_assigns and not code.strip().startswith(("var ", "const ")):
            facts["assigns"].append({"func": fname, "target": am.group(1), "op": am.group(2),
                                     "value": am.group(3)[:160], "line": lineno, "recovered": True})
        used.update(re.findall(r"\b[A-Za-z_]\w*\b", code))
