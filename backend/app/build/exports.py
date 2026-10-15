"""What a written file really exports (#101).

A code phase's write calls were told what each earlier file exports *from the plan* —
the names the plan call guessed before a line was written. A file planned as
`export default App` and written as `export function App` was still described the
way the plan said, and the next file imported it the wrong way: the default-vs-named
mistake that is most of what a small model gets wrong across files (TypeScript's 2613
and 2614). These are the names taken from the code instead — De-Hallucinator's idea,
and Aider's repo map, in their simplest form.

Two readers, both in-process and cheap enough for every file as it lands:

  * Python — `ast`: top-level functions, classes and assignments, or `__all__` when
    the module declares it;
  * JavaScript and TypeScript — the lexer the placeholder check uses, so a string or
    a comment that says `export` is never read as one.

Once a phase's tree is checked, the TypeScript program the compile gate builds says
the same thing exactly (re-exports followed); `check_js.cjs` returns it, and it
replaces what was read here for every file it covers.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Optional

#: The JavaScript and TypeScript sources a reader here (and the placeholder check) reads.
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_JS_EXT = JS_EXT

@dataclass
class Exports:
    """One module's public names. `default` is the default export's local name, `""`
    for an anonymous one, None for none."""

    default: Optional[str] = None
    named: list[str] = field(default_factory=list)
    #: TypeScript types and interfaces: importable, but not values.
    types: list[str] = field(default_factory=list)
    #: `export * from './x'` the reader could not follow.
    star: list[str] = field(default_factory=list)
    #: Python: every top-level name, or `__all__`.
    python: bool = False

    def empty(self) -> bool:
        return self.default is None and not self.named and not self.types and not self.star

    def render(self) -> str:
        """`default: App · named: useTodos, Todo` — how the next write call reads it."""
        if self.python:
            return f"names: {', '.join(self.named[:16])}" if self.named else "nothing importable"
        parts = []
        if self.default is not None:
            parts.append(f"default: {self.default or '(anonymous)'}")
        if self.named:
            parts.append(f"named: {', '.join(self.named[:14])}")
        if self.types:
            parts.append(f"types: {', '.join(self.types[:8])}")
        if self.star:
            parts.append("everything from " + ", ".join(f"`{s}`" for s in self.star[:3]))
        return " · ".join(parts) if parts else "no exports"

    def labels(self) -> list[str]:
        """The plan's `exports` list, rewritten from the code: `App (default)`, `useTodos`."""
        out = []
        if self.default is not None:
            out.append(f"{self.default} (default)" if self.default else "default")
        out += self.named
        out += [f"{t} (type)" for t in self.types]
        return out[:20]


def read(path: str, content: str) -> Optional[Exports]:
    """The exports of one file, or None for a file no reader here understands. Python
    that doesn't parse gives None (the parser names that, and a guess would mislead);
    JavaScript is read leniently, so a file with one bad line still lists the rest."""
    low = path.lower()
    if low.endswith(".py"):
        return _python(content, package=low.rsplit("/", 1)[-1] == "__init__.py")
    if low.endswith(_JS_EXT) and not low.endswith(".d.ts"):
        return _javascript(content)
    return None


# ── Python ───────────────────────────────────────────────────────────────────
def _python(content: str, package: bool = False) -> Optional[Exports]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return None
    names: list[str] = []
    declared: Optional[list[str]] = None
    for node in _top_level(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    if t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        declared = [
                            e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        ]
                    names.append(t.id)
                elif isinstance(t, ast.Tuple):
                    names += [e.id for e in t.elts if isinstance(e, ast.Name)]
        elif isinstance(node, ast.ImportFrom) and (package or node.level) and all(a.name != "*" for a in node.names):
            # A package's `__init__` offers what it imports, relative or absolute; any
            # module offers what it takes from a sibling (`from .models import User`).
            names += [a.asname or a.name for a in node.names]
    public = declared if declared is not None else [n for n in dict.fromkeys(names) if not n.startswith("_")]
    return Exports(named=list(dict.fromkeys(public)), python=True)


def _top_level(body: list) -> list:
    """The module's own statements, including those inside a top-level `if`, `try` or
    `with` — `try: import orjson … except ImportError: …` still defines its names."""
    out = []
    for node in body:
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            for part in ("body", "orelse", "finalbody"):
                out += _top_level(getattr(node, part, None) or [])
            for handler in getattr(node, "handlers", None) or []:
                out += _top_level(handler.body)
        else:
            out.append(node)
    return out


# ── JavaScript and TypeScript ────────────────────────────────────────────────
_IDENT = r"[A-Za-z_$][\w$]*"
_DECL = re.compile(
    rf"\bexport\s+(default\s+)?(?:declare\s+)?(?:async\s+)?(?:abstract\s+)?"
    rf"(function\s*\*\s*|function\s+|class\s+|const\s+enum\s+|const\s+|let\s+|var\s+|enum\s+|interface\s+|type\s+)({_IDENT})"
)
#: Words that follow `class` in an anonymous class, never a name.
_NOT_A_NAME = frozenset({"extends", "implements"})
_DEFAULT_EXPR = re.compile(rf"\bexport\s+default\s+(?:async\s+)?(?:({_IDENT})\s*;?\s*$|(?:function|class)\b|[(\[{{<]|\w)", re.MULTILINE)
_LIST = re.compile(r"\bexport\s+(type\s+)?\{([^}]*)\}\s*(?:from\s*(['\"])([^'\"]+)\3)?")
_STAR = re.compile(r"\bexport\s+\*\s*(?:as\s+(" + _IDENT + r")\s+)?from\s*(['\"])([^'\"]+)\2")
_DESTRUCTURED = re.compile(r"\bexport\s+(?:const|let|var)\s*\{([^}]*)\}\s*=")
_CJS_DEFAULT = re.compile(rf"\bmodule\.exports\s*=\s*({_IDENT})\s*;?\s*$", re.MULTILINE)
_CJS_OBJECT = re.compile(r"\bmodule\.exports\s*=\s*\{([^}]*)\}")
_CJS_NAMED = re.compile(rf"\b(?:module\.)?exports\.({_IDENT})\s*=")


def _javascript(content: str) -> Exports:
    from app.build.placeholders import lex

    # The lexer the placeholder check uses: strings and comments emptied, so neither
    # is ever read as an export; `from '…'` specifiers are read from the original.
    code, _ = lex(content)
    out = Exports()

    def add(name: str, kind: str = "named") -> None:
        bucket = out.types if kind == "type" else out.named
        if name and name not in bucket:
            bucket.append(name)

    for m in _DECL.finditer(code):
        kind = m.group(2).split()[-1].strip("*")
        name = m.group(3)
        if name in _NOT_A_NAME:
            name = ""  # `export default class extends Base`
        if m.group(1):
            out.default = name
        elif name:
            add(name, "type" if kind in ("interface", "type") else "named")
    for m in _DEFAULT_EXPR.finditer(code):
        if out.default is None:
            out.default = m.group(1) or ""
    for m in _LIST.finditer(code):
        is_type = bool(m.group(1))
        for part in m.group(2).split(","):
            part = part.strip()
            if not part:
                continue
            part_type = is_type or part.startswith("type ")
            part = re.sub(r"^type\s+", "", part)
            local, _, alias = part.partition(" as ")
            name = (alias or local).strip()
            if name == "default":
                out.default = local.strip() if local.strip() != "default" else (out.default or "")
            elif re.fullmatch(_IDENT, name):
                add(name, "type" if part_type else "named")
    for m in _STAR.finditer(code):
        spec = content[m.start(3) : m.end(3)]
        if m.group(1):
            add(m.group(1))
        elif spec not in out.star:
            out.star.append(spec)
    for m in _DESTRUCTURED.finditer(code):
        for part in m.group(1).split(","):
            name = part.split(":")[-1].split("=")[0].strip()
            if re.fullmatch(_IDENT, name):
                add(name)
    # CommonJS, for a backend written that way.
    m = _CJS_DEFAULT.search(code)
    if m and out.default is None:
        out.default = m.group(1)
    for m in _CJS_OBJECT.finditer(code):
        for part in m.group(1).split(","):
            name = part.split(":")[0].strip()
            if re.fullmatch(_IDENT, name):
                add(name)
    for m in _CJS_NAMED.finditer(code):
        if m.group(1) == "default":
            out.default = out.default or ""  # `exports.default = X`: a transpiled default
        else:
            add(m.group(1))
    return out


def from_checker(entry: dict) -> Exports:
    """What TypeScript's own program says a module exports (`check_js.cjs`):
    `{default: name | "" | null, named: [...], types: [...]}`."""
    default = entry.get("default")
    return Exports(
        default=None if default is None else str(default),
        named=[str(n) for n in entry.get("named") or []],
        types=[str(n) for n in entry.get("types") or []],
    )
