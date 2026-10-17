"""Parser for Godot's text formats: project.godot (ConfigFile), .tscn and .tres.

Values use Godot's variant text syntax. We parse them into plain Python:
str / int / float / bool / None / list / dict, and constructors into tagged dicts:

    ExtResource("1_ab")   -> {"$ext": "1_ab"}
    SubResource("2_cd")   -> {"$sub": "2_cd"}
    Vector2(1, 2)         -> {"$type": "Vector2", "args": [1, 2]}
    Object(InputEventKey, "keycode": 32, ...) -> {"$object": "InputEventKey", "keycode": 32, ...}

Anything we can't parse is kept as {"$raw": "<text>"} so a bad value never aborts a scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_BARE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./+-")


class VariantParseError(ValueError):
    pass


class _VariantParser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def parse(self) -> Any:
        v = self.value()
        self.ws()
        if self.i != len(self.s):
            raise VariantParseError(f"trailing data at {self.i}: {self.s[self.i:self.i + 20]!r}")
        return v

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise VariantParseError(f"expected {ch!r} at {self.i}")
        self.i += 1

    def value(self) -> Any:
        c = self.peek()
        if c == '"':
            return self.string()
        if c == "&" and self.s[self.i + 1:self.i + 2] == '"':
            self.i += 1
            return self.string()
        if c == "^" and self.s[self.i + 1:self.i + 2] == '"':
            self.i += 1
            return {"$type": "NodePath", "args": [self.string()]}
        if c == "[":
            return self.array()
        if c == "{":
            return self.dict()
        if c == "":
            raise VariantParseError("unexpected end")
        return self.word_or_ctor()

    def string(self) -> str:
        self.expect('"')
        out = []
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\\" and self.i + 1 < len(self.s):
                nxt = self.s[self.i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                self.i += 2
                continue
            if ch == '"':
                self.i += 1
                return "".join(out)
            out.append(ch)
            self.i += 1
        raise VariantParseError("unterminated string")

    def array(self) -> list:
        self.expect("[")
        items = []
        while self.peek() != "]":
            items.append(self.value())
            if self.peek() == ",":
                self.i += 1
        self.expect("]")
        return items

    def dict(self) -> dict:
        self.expect("{")
        out: dict = {}
        while self.peek() != "}":
            k = self.value()
            self.expect(":")
            out[_key(k)] = self.value()
            if self.peek() == ",":
                self.i += 1
        self.expect("}")
        return out

    def word_or_ctor(self) -> Any:
        self.ws()
        start = self.i
        while self.i < len(self.s) and self.s[self.i] in _BARE_CHARS:
            self.i += 1
        word = self.s[start:self.i]
        if not word:
            raise VariantParseError(f"unexpected {self.s[self.i:self.i + 10]!r}")
        if self.peek() == "[":  # typed container: Array[int]([...]) / Dictionary[String, int]({...})
            depth = 0
            while self.i < len(self.s):
                if self.s[self.i] == "[":
                    depth += 1
                elif self.s[self.i] == "]":
                    depth -= 1
                    if depth == 0:
                        self.i += 1
                        break
                self.i += 1
        if self.peek() != "(":
            return _scalar(word)
        self.expect("(")
        if word == "Object":
            self.ws()
            cls_start = self.i
            while self.i < len(self.s) and self.s[self.i] not in ",)":
                self.i += 1
            obj: dict = {"$object": self.s[cls_start:self.i].strip()}
            while self.peek() == ",":
                self.i += 1
                if self.peek() == ")":
                    break
                k = self.value()
                self.expect(":")
                obj[_key(k)] = self.value()
            self.expect(")")
            return obj
        args = []
        while self.peek() != ")":
            args.append(self.value())
            if self.peek() == ",":
                self.i += 1
        self.expect(")")
        if word == "ExtResource":
            return {"$ext": str(args[0]) if args else ""}
        if word == "SubResource":
            return {"$sub": str(args[0]) if args else ""}
        if word in ("Array", "Dictionary") or word.startswith(("Array[", "Dictionary[")):
            return args[0] if args else ([] if word.startswith("Array") else {})
        return {"$type": word.split("[")[0], "args": args}


def _key(k: Any) -> str:
    return k if isinstance(k, str) else str(k)


def _scalar(word: str) -> Any:
    if word == "true":
        return True
    if word == "false":
        return False
    if word in ("null", "nil"):
        return None
    if word in ("inf", "inf_neg", "nan"):
        return {"inf": float("inf"), "inf_neg": float("-inf"), "nan": float("nan")}[word]
    try:
        return int(word)
    except ValueError:
        pass
    try:
        return float(word)
    except ValueError:
        return word


def parse_variant(text: str) -> Any:
    try:
        return _VariantParser(text.strip()).parse()
    except (VariantParseError, IndexError) as e:
        return {"$raw": text.strip(), "$error": str(e)}


@dataclass
class Section:
    tag: str                      # "node", "ext_resource", "input", "application", ...
    attrs: dict = field(default_factory=dict)   # header attributes (resource formats)
    props: dict = field(default_factory=dict)   # key = value lines
    line: int = 0


def _balanced(text: str) -> bool:
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
    return depth <= 0 and not in_str


def _parse_header(body: str) -> tuple[str, dict]:
    """`gd_scene load_steps=3 format=3 uid="uid://x"` -> ("gd_scene", {...})."""
    body = body.strip()
    parts = body.split(None, 1)
    tag = parts[0] if parts else ""
    attrs: dict = {}
    rest = parts[1] if len(parts) > 1 else ""
    i = 0
    while i < len(rest):
        while i < len(rest) and rest[i] in " \t":
            i += 1
        eq = rest.find("=", i)
        if eq < 0:
            break
        key = rest[i:eq].strip()
        j = eq + 1
        # value runs until the next ` key=` at depth 0 (or end)
        depth = 0
        in_str = False
        esc = False
        k = j
        while k < len(rest):
            ch = rest[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch in " \t" and depth == 0:
                break
            k += 1
        attrs[key] = parse_variant(rest[j:k])
        i = k
    return tag, attrs


def parse_godot_text(text: str) -> list[Section]:
    """Parse ConfigFile / tscn / tres text into sections. Tolerant of junk lines."""
    sections: list[Section] = [Section(tag="", line=0)]
    pending_key: str | None = None
    pending_val: list[str] = []
    pending_line = 0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\n")
        if pending_key is not None:
            pending_val.append(line)
            joined = "\n".join(pending_val)
            if _balanced(joined):
                sections[-1].props[pending_key] = parse_variant(joined)
                pending_key = None
                pending_val = []
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped.split(None, 1)[0]:
            tag, attrs = _parse_header(stripped[1:-1])
            sections.append(Section(tag=tag, attrs=attrs, line=lineno))
            continue
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            key = key.strip()
            if _balanced(val):
                sections[-1].props[key] = parse_variant(val)
            else:
                pending_key = key
                pending_val = [val]
                pending_line = lineno
    if pending_key is not None:
        sections[-1].props[pending_key] = {"$raw": "\n".join(pending_val), "$error": f"unbalanced value from line {pending_line}"}
    return sections
