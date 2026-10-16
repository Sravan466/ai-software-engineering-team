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
from dataclasses import dataclass
from typing import Iterable, Optional

from app.build import fixscope
from app.build.check import Problem

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
        pattern = re.compile(rf"""(['"]){re.escape(old)}\1""")
        out, n = pattern.subn(lambda m: m.group(1) + new + m.group(1), code)
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
def _esm_statement(code: str, spec: str) -> Optional[re.Match]:
    return re.search(
        rf"""\bimport\s+(?!type\b)(?P<clause>[^;'"]*?)\s+from\s+(?P<q>['"]){re.escape(spec)}(?P=q)""",
        code,
        re.DOTALL,
    )


def _cjs_statement(code: str, spec: str) -> Optional[re.Match]:
    return re.search(
        rf"""\b(?P<kw>const|let|var)\s+(?P<lhs>\{{[^}}]*\}}|{_IDENT})\s*=\s*require\(\s*(?P<q>['"]){re.escape(spec)}(?P=q)\s*\)""",
        code,
    )


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
            # `import X` of a module with no default: X by name, or its one export.
            values = list(real.named)
            if default in values:
                new_named = [(default, default)]
            elif len(values) == 1:
                new_named = [(values[0], default)]
            else:
                return None
            clause = _join_clause(None, new_named + named)
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
        text = out[path]
        last = len(text) + 1
        # The same fix asked for twice (two files importing one undeclared export) is one
        # edit; anything else touching a span already edited is left out.
        unique = {(int(e.get("start") or 0), int(e.get("length") or 0), str(e.get("text") or "")) for e in list_}
        for start, length, new in sorted(unique, reverse=True):
            if start < 0 or start + length > last or (start == last and length == 0 and last <= len(text)):
                continue
            text = text[:start] + new + text[start + length:]
            last = start
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


__all__ = ["Fix", "apply_code_fixes", "mend"]
