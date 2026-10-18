"""The platform's own wiring fixes (#103): what a model gets wrong between files, mended
without a model where the answer is mechanical.

26 of the 28 problems a stuck 7B backend went round in were one file disagreeing with
another: an import of a name the other file doesn't export, named against default, a
relative path one folder off. The checker that finds them already knows the answer:
the whole tree, and what every module really exports. So before any model round:

  * **a path one folder off** — a relative import that resolves nowhere, while exactly
    one file on the same side ends in the import's own path: the import is pointed at
    it. Python too, for a relative import one package off.
  * **default against named** (TypeScript 2613, 2614, 1192, and a 2305 whose name is the
    module's default) — TypeScript has no code fix for these, but its message names the
    fix and the program knows the module's exports: `import { Y }` of a module whose
    default is Y becomes `import Y`, and `import X` of a module with no default becomes
    `import { X }`. `require` destructuring the same, against `module.exports`.
  * **a misspelt import** (TypeScript 2724, "did you mean 'Z'?"): `import { Y }` becomes
    `import { Z as Y }`, so every use of Y still works — TypeScript's own `fixSpelling`
    renames the import alone and leaves those uses broken.
  * **TypeScript's own code fix** for 2459 (`fixImportNonExportedMember`, a name declared
    and not exported), from an allow-list. Nothing that writes new logic is applied.

Pure functions over the files and the checker's problems; the caller checks the result
again and keeps it only when it leaves fewer problems.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.build import fixscope, layout
from app.build.check import Problem
from app.build.scaffold import js_imports

#: TypeScript's codes for a named import of a default, and a default import of nothing.
_NAMED_OF_DEFAULT = {2614}
_DEFAULT_OF_NOTHING = {2613, 1192}
_NO_MEMBER = {2305}
_SPELLING = {2724}
_DID_YOU_MEAN = re.compile(r"has no exported member named '([^']+)'\. Did you mean '([^']+)'\?")

_SPEC = re.compile(r"imports `([^`]+)`")
_MODULE = re.compile(r"""'"([^"]+)"'""")
_MEMBER = re.compile(r"has no exported member (?:named )?'([^']+)'")
_IDENT = r"[A-Za-z_$][\w$]*"


@dataclass
class Fix:
    """One mechanical edit: which file, what kind, and the line before and after."""

    path: str
    kind: str  # path | import
    note: str

    def as_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "note": self.note}


# ── a path one folder off ────────────────────────────────────────────────────
def _relative(importer: str, target: str, ext: str) -> str:
    """The specifier `importer` should write for `target`: `./x`, `../y/z`, ending in `ext`
    — the extension the import was written with, so TypeScript's NodeNext `./x.js` for
    `x.ts` stays `.js`."""
    stem = fixscope._module_path(target)  # noqa: SLF001 - one rule
    if ext and stem != posixpath.splitext(target)[0]:
        stem = posixpath.splitext(target)[0]  # an index file named by its folder keeps its name
    rel = posixpath.relpath(stem, posixpath.dirname(importer)) + ext
    return rel if rel.startswith("../") else f"./{rel}"


def _python_relative(importer: str, target: str) -> Optional[str]:
    """`from .x import` for `target` as `importer`'s relative module: dots, then the path."""
    module = fixscope._module_path(target)  # noqa: SLF001
    rel = posixpath.relpath(module, posixpath.dirname(importer))
    up = 0
    parts = rel.split("/")
    while parts and parts[0] == "..":
        up += 1
        parts = parts[1:]
    if not parts or any(not re.fullmatch(r"\w+", p) for p in parts):
        return None
    return "." * (up + 1) + ".".join(parts)


def _repoint(code: str, old: str, new: str, python: bool) -> Optional[str]:
    """`code` with every import of `old` importing `new` instead; None when none did."""
    if python:
        pattern = re.compile(rf"(\bfrom\s+){re.escape(old)}(\s+import\b)")
        out, n = pattern.subn(lambda m: m.group(1) + new + m.group(2), code)
    else:
        # Import specifiers only — `from '…'`, `import '…'`, `require('…')`, `import('…')`
        # — never the same text in a path or a message elsewhere in the file.
        pattern = re.compile(
            rf"""(\bfrom\s*|\bimport\s*|\brequire\(\s*|\bimport\(\s*)(['"]){re.escape(old)}\2"""
        )
        out, n = pattern.subn(lambda m: m.group(1) + m.group(2) + new + m.group(2), code)
    return out if n else None


def _path_fix(problem: Problem, files: dict[str, str], tree: set[str]) -> Optional[tuple[str, str, str, str]]:
    """(file, new code, note, the module it now points at) for an import one folder
    off, or None."""
    found = _SPEC.search(problem.message)
    target = fixscope._missing({"message": problem.message})  # noqa: SLF001
    code = files.get(problem.path)
    if not found or not target or code is None:
        return None
    spec = found.group(1)
    python = problem.path.endswith(".py")
    if python and not spec.startswith("."):
        return None  # relative imports only: an absolute one is a package question
    if not python and not spec.startswith("."):
        return None
    tail = fixscope._tail({"message": problem.message})  # noqa: SLF001
    candidates = [c for c in fixscope.same_named(target, tree, tail) if c != problem.path]
    if len(candidates) != 1:
        return None
    hit = candidates[0]
    if python:
        new = _python_relative(problem.path, hit)
    else:
        ext = posixpath.splitext(spec)[1]
        new = _relative(problem.path, hit, ext if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs") else "")
    if not new or new == spec:
        return None
    changed = _repoint(code, spec, new, python)
    if changed is None:
        return None
    return problem.path, changed, f"`{spec}` → `{new}`", hit


# ── default against named ────────────────────────────────────────────────────
def _esm_re(spec: str) -> re.Pattern:
    # The clause never runs across another `import`: `// we import x` in a comment
    # above must not swallow the real statement under it.
    return re.compile(
        rf"""\bimport\s+(?!type\b)(?P<clause>(?:(?!\bimport\b)[^;'"])*?)\s+from\s+(?P<q>['"]){re.escape(spec)}(?P=q)""",
        re.DOTALL,
    )


def _cjs_re(spec: str) -> re.Pattern:
    return re.compile(
        rf"""\b(?P<kw>const|let|var)\s+(?P<lhs>\{{[^}}]*\}}|{_IDENT})\s*=\s*require\(\s*(?P<q>['"]){re.escape(spec)}(?P=q)\s*\)"""
    )


def _esm_statement(code: str, spec: str) -> Optional[re.Match]:
    return _esm_re(spec).search(code)


def _cjs_statement(code: str, spec: str) -> Optional[re.Match]:
    return _cjs_re(spec).search(code)


def _split_clause(clause: str) -> tuple[Optional[str], list[tuple[str, str]], bool]:
    """(default name, [(imported, local)], ok) of an ESM import clause."""
    clause = clause.strip()
    if clause.startswith("*"):
        return None, [], False
    default: Optional[str] = None
    named: list[tuple[str, str]] = []
    brace = clause.find("{")
    head = clause[:brace] if brace >= 0 else clause
    head = head.strip().rstrip(",").strip()
    if head:
        if not re.fullmatch(_IDENT, head):
            return None, [], False
        default = head
    if brace >= 0:
        inner = clause[brace + 1 : clause.rfind("}")]
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            if part.startswith("type "):
                return None, [], False
            m = re.fullmatch(rf"({_IDENT})(?:\s+as\s+({_IDENT}))?", part)
            if not m:
                return None, [], False
            named.append((m.group(1), m.group(2) or m.group(1)))
    return default, named, True


def _join_clause(default: Optional[str], named: list[tuple[str, str]]) -> str:
    inner = ", ".join(a if a == b else f"{a} as {b}" for a, b in named)
    if default and named:
        return f"{default}, {{ {inner} }}"
    if default:
        return default
    return f"{{ {inner} }}"


def _cjs_named(lhs: str) -> Optional[list[tuple[str, str]]]:
    inner = lhs.strip()[1:-1]
    out = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(rf"({_IDENT})(?:\s*:\s*({_IDENT}))?", part)
        if not m:
            return None
        out.append((m.group(1), m.group(2) or m.group(1)))
    return out


def _spelling_fix(problem: Problem, files: dict[str, str]) -> Optional[tuple[str, str, str]]:
    """`import { getTodo }` of a module exporting `getTodos` (2724): imported under the
    name the code already uses — `{ getTodos as getTodo }` — so nothing else changes."""
    code = files.get(problem.path)
    found = _MODULE.search(problem.message)
    names = _DID_YOU_MEAN.search(problem.message)
    if code is None or not found or not names:
        return None
    spec, (wrong, right) = found.group(1), names.groups()
    esm = _esm_statement(code, spec)
    if esm is not None:
        default, named, ok = _split_clause(esm.group("clause"))
        if not ok or not any(a == wrong for a, _b in named):
            return None
        clause = _join_clause(default, [(right, b) if a == wrong else (a, b) for a, b in named])
        start, end = esm.span("clause")
        return problem.path, code[:start] + clause + code[end:], f"`{wrong}` → `{right} as {wrong}` from `{spec}`"
    cjs = _cjs_statement(code, spec)
    if cjs is not None and cjs.group("lhs").startswith("{"):
        named = _cjs_named(cjs.group("lhs"))
        if not named or not any(a == wrong for a, _b in named):
            return None
        lhs = "{ " + ", ".join(f"{right if a == wrong else a}: {b}" if (a == wrong or a != b) else a for a, b in named) + " }"
        start, end = cjs.span("lhs")
        return problem.path, code[:start] + lhs + code[end:], f"`{wrong}` → `{right}: {wrong}` from `{spec}`"
    return None


def _import_fix(problem: Problem, files: dict[str, str], exports: dict) -> Optional[tuple[str, str, str]]:
    """(file, new code, note) for a default/named mismatch the module's exports settle."""
    code_no = problem.code
    if code_no in _SPELLING:
        return _spelling_fix(problem, files)
    if code_no not in _NAMED_OF_DEFAULT | _DEFAULT_OF_NOTHING | _NO_MEMBER:
        return None
    code = files.get(problem.path)
    found = _MODULE.search(problem.message)
    if code is None or not found:
        return None
    spec = found.group(1)
    module = fixscope.named(problem.path, problem.message, files.keys())
    real = exports.get(module) if module else None
    if real is None or getattr(real, "python", False):
        return None
    member = _MEMBER.search(problem.message)
    name = member.group(1) if member else None

    esm = _esm_statement(code, spec)
    if esm is not None:
        default, named, ok = _split_clause(esm.group("clause"))
        if not ok:
            return None
        if code_no in _DEFAULT_OF_NOTHING and default:
            # `import X` of a module with no default, that exports X by name. Never an
            # unrelated export under X's name: the error would go and the bug stay.
            if default not in real.named:
                return None
            clause = _join_clause(None, [(default, default)] + named)
        elif name and real.default is not None and name not in real.named:
            # `import { Y }` of a module whose default is Y (2614, or 2305 by its name).
            if code_no in _NO_MEMBER and real.default != name:
                return None
            if default:
                return None  # it already imports a default
            pair = next((p for p in named if p[0] == name), None)
            if pair is None:
                return None
            clause = _join_clause(pair[1], [p for p in named if p[0] != name])
        else:
            return None
        start, end = esm.span("clause")
        changed = code[:start] + clause + code[end:]
        return problem.path, changed, f"`import {esm.group('clause').strip()}` → `import {clause}` from `{spec}`"

    cjs = _cjs_statement(code, spec)
    if cjs is not None and name and cjs.group("lhs").startswith("{"):
        named = _cjs_named(cjs.group("lhs"))
        # `const { Y } = require(x)` of a module whose module.exports is Y itself.
        if named is None or len(named) != 1 or named[0][0] != name or real.default is None or name in real.named:
            return None
        if code_no in _NO_MEMBER and real.default != name:
            return None
        start, end = cjs.span("lhs")
        changed = code[:start] + named[0][1] + code[end:]
        return problem.path, changed, f"`{{ {name} }} = require('{spec}')` → `{named[0][1]} = require('{spec}')`"
    return None


# ── TypeScript's own code fixes ──────────────────────────────────────────────
def apply_code_fixes(files: dict[str, str], fixes: Iterable[dict]) -> tuple[dict[str, str], list[Fix]]:
    """Apply TypeScript's code fixes (`check.code_fixes`) to `files`, the phase's own:
    each file's edits from the end backwards, an edit overlapping one already made
    skipped. A fix that would touch a file that isn't the phase's is left alone."""
    out = dict(files)
    made: list[Fix] = []
    edits: dict[str, list[dict]] = {}
    notes: dict[str, str] = {}
    for fix in fixes:
        changes = fix.get("changes") or []
        if any(str(c.get("path")) not in out for c in changes):
            continue
        for change in changes:
            path = str(change.get("path"))
            edits.setdefault(path, []).extend(change.get("edits") or [])
            notes.setdefault(path, str(fix.get("description") or fix.get("fix") or "code fix"))
    for path, list_ in edits.items():
        # TypeScript counts offsets in UTF-16 code units: edited as those, an emoji before
        # the edit can't shift it into the middle of a word.
        units = out[path].encode("utf-16-le")
        size = len(units) // 2
        last = size + 1
        # The same fix asked for twice (two files importing one undeclared export) is one
        # edit; anything else touching a span already edited is left out.
        unique = {(int(e.get("start") or 0), int(e.get("length") or 0), str(e.get("text") or "")) for e in list_}
        for start, length, new in sorted(unique, reverse=True):
            if start < 0 or start + length > last or (start == last and length == 0 and last <= size):
                continue
            units = units[: start * 2] + new.encode("utf-16-le") + units[(start + length) * 2 :]
            last = start
        text = units.decode("utf-16-le")
        if text != out[path]:
            out[path] = text
            made.append(Fix(path, "import", notes[path]))
    return out, made


def mend(problems: Iterable[Problem], files: dict[str, str], tree: Iterable[str], exports: dict) -> tuple[dict[str, str], list[Fix]]:
    """`files` (this phase's, placed path -> code) with every mechanical wiring fix the
    problems allow made, and the fixes. `tree` is every path the build has; `exports`
    what each module really exports (the checker's own answer)."""
    everything = set(tree) | set(files)
    out = dict(files)
    made: list[Fix] = []
    for p in problems:
        if p.path not in out:
            continue
        if p.kind == "import":
            found = _path_fix(p, out, everything)
            if found is not None:
                path, code, note, _target = found
            kind = "path"
        else:
            found = _import_fix(p, out, exports)
            if found is not None:
                path, code, note = found
            kind = "import"
        if found is None:
            continue
        if code != out[path]:
            out[path] = code
            made.append(Fix(path, kind, note))
    return out, made


# ── what an importer takes from a module (#119) ─────────────────────────────
@dataclass
class Wanted:
    """What one file's imports of a module take from it: the lines as written, the
    default it binds (by its local name), the names it takes by name — which the module
    is held to — and the names it only reads off the whole module, which are asked for
    and never checked, since no reader here can say the module lacks them."""

    importer: str
    line: str
    #: The local name a default import binds — or, for CommonJS, the name the whole
    #: module is bound to where it is used as a value or called.
    default: Optional[str] = None
    named: list[str] = field(default_factory=list)
    #: Read off the whole module (`db.query`, `require('./x').y`, `from . import anim`).
    loose: list[str] = field(default_factory=list)
    #: The import is `require`, so a default is `module.exports` itself.
    commonjs: bool = False

    def labels(self) -> list[str]:
        """As the plan lists exports: `default`, then the names. The importer's own name
        for the default is its local name, not the module's."""
        return (["default"] if self.default else []) + list(dict.fromkeys(self.named + self.loose))


def importers_of(target: str, files: dict[str, str]) -> list[tuple[str, str]]:
    """(importer, the specifier it wrote) for every JavaScript file of `files` whose
    relative or `@/` import resolves to `target`, by the compile gate's own rule."""
    out: list[tuple[str, str]] = []
    for path, code in files.items():
        if path == target or path.endswith(".py"):
            continue
        for spec in dict.fromkeys(js_imports(code)):
            if spec.startswith((".", "@/", "~/")) and fixscope._module_of(path, spec, {target}) == target:  # noqa: SLF001
                out.append((path, spec))
    return out


def wanted_by(target: str, files: dict[str, str]) -> list[Wanted]:
    """What every file of `files` takes from `target`, one `Wanted` per importer."""
    out = [w for path, spec in importers_of(target, files) for w in [wanted(path, files[path], spec)] if w is not None]
    for path, code in files.items():
        if path.endswith(".py") and path != target:
            w = _python_takes(path, code, target)
            if w is not None:
                out.append(w)
    return out


def wanted(importer: str, code: str, spec: str) -> Optional[Wanted]:
    """What `importer`'s imports of `spec` take, read from its own import lines — every
    statement of that module, merged — or None when no line is one the readers here
    understand. A line inside a comment or a string doesn't count."""
    if importer.endswith(".py"):
        return None  # Python is read by `wanted_by`, from the resolved module
    from app.build.placeholders import lex

    bare, _ = lex(code)  # strings and comments emptied: `x.y` in a comment isn't a use

    def live(m: re.Match) -> bool:
        # The lexer keeps code where it is and blanks comments: a statement whose
        # keyword survived is code, not `// import { old } from './a'`.
        return bare[m.start() : m.start() + 3] == code[m.start() : m.start() + 3]

    out = Wanted(importer, "")
    lines: list[str] = []

    def take(m: re.Match, default: Optional[str] = None, named=(), loose=(), commonjs: bool = False) -> None:
        lines.append(_line(code, m.start(), m.end()))
        out.default = out.default or default
        out.named += [n for n in named if n not in out.named]
        out.loose += [n for n in loose if n not in out.loose]
        out.commonjs = out.commonjs or commonjs

    for m in _esm_re(spec).finditer(code):
        if not live(m):
            continue
        clause = m.group("clause").strip()
        star = re.match(rf"\*\s+as\s+({_IDENT})$", clause)
        if star:
            take(m, named=_members(bare, star.group(1)))  # a namespace's names are named exports
            continue
        # `{ type Opts, fadeIn }`: a type part is no value to export, and no reason to
        # drop the names beside it.
        clause = re.sub(rf"(?<=[{{,])\s*type\s+{_IDENT}(?:\s+as\s+{_IDENT})?\s*(?:,|(?=\}}))", "", clause)
        default, named, ok = _split_clause(clause)
        if ok:
            take(m, default=default, named=[a for a, _b in named])
        else:
            take(m)
    for m in _cjs_re(spec).finditer(code):
        if not live(m):
            continue
        lhs = m.group("lhs")
        if lhs.startswith("{"):
            take(m, named=_destructure(lhs[1:-1]), commonjs=True)
            continue
        members = _members(bare, lhs)
        members += [n for n in _destructured(bare, lhs) if n not in members]
        called = re.search(rf"(?<![\w$.]){re.escape(lhs)}\s*\(|\bnew\s+{re.escape(lhs)}\b", bare)
        # Used only as a value (`app.use('/todos', todos)`), the module is that value.
        take(m, default=lhs if called or not members else None, loose=members, commonjs=True)
    for m in re.finditer(rf"""\brequire\(\s*(['"]){re.escape(spec)}\1\s*\)\.({_IDENT})""", code):
        if live(m):
            take(m, loose=[m.group(2)], commonjs=True)
    if not lines:
        return None
    out.line = " ".join(dict.fromkeys(lines))[:400]
    return out


def unexported(wants: Iterable[Wanted], real, code: str) -> list[str]:
    """What `wants` take by name that `real` (`exports.Exports` of `code`) doesn't
    export: each missing name, and `default` for an ES default nobody exports.

    Only for a module the reader sees whole: ES `export`s, `exports.x =`, and a
    `module.exports = { … }` literal. Nothing is called missing in one whose exports are
    built some other way — `export * from`, a name, a call, a `require`, a spread,
    `Object.assign` — nor a CommonJS module's own value, nor what is read off it: a
    file is asked for those, never judged on them."""
    if real is None or real.star or _opaque(code):
        return []
    have = {*real.named, *real.types, *(["default"] if real.default is not None else [])}
    # Node and bundlers hand an ES default import a CommonJS module's whole value.
    interop = _commonjs(code)
    missing: list[str] = []
    for want in wants:
        if want.default and not want.commonjs and real.default is None and not interop and "default" not in missing:
            missing.append("default")
        missing += [n for n in want.named if n not in have and n not in missing]
    return missing


def _commonjs(code: str) -> bool:
    from app.build.placeholders import lex

    return re.search(r"\b(?:module\.)?exports\s*(?:\.|=(?!=))", lex(code)[0]) is not None


def _opaque(code: str) -> bool:
    """Whether a file's CommonJS exports are more than a reader here can see: assigned
    as a name, a call, a `require`, `new`, a function or class; an object with a spread;
    or built with `Object.assign`. Only an object literal is seen whole."""
    from app.build.exports import object_parts
    from app.build.placeholders import lex

    bare, _ = lex(code)
    if re.search(r"\bObject\.assign\(\s*(?:module\.)?exports\b", bare):
        return True
    for m in re.finditer(r"\bmodule\.exports\s*=(?!=)\s*", bare):
        if not bare[m.end():].startswith("{"):
            return True
        if any(part.strip().startswith("...") for part in object_parts(bare, m.end())):
            return True  # a spread: names the reader can't list
    return False


def _python_takes(importer: str, code: str, target: str) -> Optional[Wanted]:
    """What a Python file takes from `target`: `from <it> import a, b` (held to), and
    `from . import it` or `import x.it` (the module itself — its attributes asked for,
    not checked). Relative imports from the importer's package, absolute ones from the
    side's root (`from app.services.anim import fade` in `backend/`)."""
    import ast

    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    root = layout.side_of(importer) or ""
    here = posixpath.dirname(importer)
    out = Wanted(importer, "")
    lines: list[str] = []

    def dotted(node) -> str:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        return ".".join([node.id] + parts[::-1]) if isinstance(node, ast.Name) else ""

    def attrs(local: str) -> list[str]:
        # `anim.fade` for `from . import anim`; `app.services.anim.fade` for a dotted import.
        return list(dict.fromkeys(
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and dotted(n.value) == local
        ))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = here
            for _ in range((node.level or 0) - 1):
                base = posixpath.dirname(base)
            base = base if node.level else root
            module = layout.join(base, *node.module.split(".")) if node.module else base
            if node.module and fixscope.find(module, {target}) == target:
                out.named += [a.name for a in node.names if a.name != "*" and a.name not in out.named]
                lines.append(ast.get_source_segment(code, node) or "")
                continue
            for alias in node.names:
                if fixscope.find(layout.join(module, alias.name), {target}) == target:
                    out.loose += [n for n in attrs(alias.asname or alias.name) if n not in out.loose]
                    lines.append(ast.get_source_segment(code, node) or "")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if fixscope.find(layout.join(root, *alias.name.split(".")), {target}) == target:
                    local = alias.asname or alias.name
                    out.loose += [n for n in attrs(local) if n not in out.loose]
                    lines.append(ast.get_source_segment(code, node) or "")
    if not lines:
        return None
    out.line = " ".join(dict.fromkeys(l for l in lines if l))[:400]
    return out


def _destructure(inner: str) -> list[str]:
    """The names a destructuring pattern takes: `a, b: c, d = 1` -> a, b, d."""
    out: list[str] = []
    for part in inner.split(","):
        m = re.match(rf"\s*({_IDENT})", part)
        if m and not part.strip().startswith("...") and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _destructured(code: str, name: str) -> list[str]:
    """The names `code` destructures off `name`: `const { port } = config` -> `port`."""
    out: list[str] = []
    for inner in re.findall(rf"\{{([^{{}}]*)\}}\s*=\s*{re.escape(name)}\b(?!\s*\.)", code):
        out += [n for n in _destructure(inner) if n not in out]
    return out


def _members(code: str, name: str) -> list[str]:
    """The names `code` reads off `name`: `anim.fadeIn(…)` -> `fadeIn`."""
    return list(dict.fromkeys(re.findall(rf"(?<![\w$.]){re.escape(name)}\s*\.\s*({_IDENT})", code)))


def _line(code: str, start: int, end: int) -> str:
    """The statement as written, on one line: an import split over lines reads whole."""
    head = code.rfind("\n", 0, start) + 1
    tail = code.find("\n", end)
    return " ".join(code[head : len(code) if tail < 0 else tail].split())[:300]


__all__ = ["Fix", "Wanted", "apply_code_fixes", "importers_of", "mend", "unexported", "wanted", "wanted_by"]
