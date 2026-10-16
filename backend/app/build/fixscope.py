"""Which files a fix round edits (#103).

A fix round used to run the code phase again from the start: a new plan, new file
names, new mismatches, every round — so a backend with 3 problems came back with 6,
then 4, then 8, and none of it was the code that had been sent back. A round now
*edits*, and this decides what it edits, from the problems alone:

  * the file each problem is in;
  * the file each problem *names* — the module an import of a missing name points at
    (TypeScript 2305, 2613, 2614, 2724, 1192), so the importer and the exporter it
    disagrees with are decided together;
  * a file an import points at that doesn't exist, written new — but only when no file
    of that name exists anywhere in the build. One that does means the import's path is
    wrong, and the importer is the file to change.

Every other file stays byte for byte. A problem no file can be found for is handed
back as `unlocated`, for the code phase to ask the model where it lives.

Pure functions over paths and messages: no model, no I/O.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.build import layout
from app.build import packages as pkg
from app.build.check import Problem, resolve
from app.build.scaffold import js_imports, platform_owned, py_imports

#: What a written module's name may end in — `auth.service` is a name, not an extension.
_SOURCE = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".json", ".py")
#: Text files an import may want that a model can write as they are named.
_TEXT_ASSETS = (".css", ".scss", ".sass", ".less", ".svg", ".html", ".md", ".txt")
#: Files no model writes: an import of a missing one is the importer's to change.
_BINARY = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp", ".woff", ".woff2", ".ttf",
           ".otf", ".eot", ".mp3", ".mp4", ".webm", ".wav", ".pdf", ".zip")

#: TypeScript names the module it means as `'"./x"'` (#101 keeps the importer's own
#: specifier there, or the build path when it couldn't).
_MODULE = re.compile(r"""'"([^"]+)"'""")
_VIRTUAL = re.compile(r"^/__build__/(?:root/)?")
_MISSING_JS = re.compile(r"no file in this build is at (.+?)\. Write that file")
_MISSING_ALIAS = re.compile(r"imports `([^`]+)`, and no file in this build matches it \(`([^`]+)` points at ([^)]*?)/?\)")
_MISSING_PY = re.compile(r"no module (?:in this build )?is at (\S+?\.py)")
_PACKAGE = re.compile(r"`(@?[\w.\-]+(?:/[\w.\-]+)?)`")
#: A charter violation names its file first: "`backend/x.js` — uses …".
_STACK_PATH = re.compile(r"^\s*`([^`]+)`")


@dataclass
class Scope:
    #: path -> what is shown under it: its own problems, then those of the files that
    #: name it (each keeps the importer's path, so the model reads whose import it is).
    touch: dict[str, list[Problem]] = field(default_factory=dict)
    #: path -> why it is written new: the problems of the files that import it.
    new: dict[str, list[Problem]] = field(default_factory=dict)
    #: (importer, exporter): edited in one call when both fit.
    pairs: list[tuple[str, str]] = field(default_factory=list)
    #: Problems no file could be found for, as they came.
    unlocated: list[dict] = field(default_factory=list)


def stack_path(note: str) -> Optional[str]:
    """The file a stack-charter violation is about, when it names one."""
    found = _STACK_PATH.match(str(note or ""))
    return layout.clean(found.group(1)) if found else None


def find(target: str, tree: Iterable[str]) -> Optional[str]:
    """The file a module path resolves to — `backend/services/todo` -> `…/todo.js` —
    by the compile gate's own rule, or Python's (`x.py`, `x/__init__.py`)."""
    paths = set(tree)
    stem = layout.clean(target)
    found = resolve(stem, paths)
    if found is None:
        found = next((p for p in (f"{stem}.py", f"{stem}/__init__.py") if p in paths), None)
    return found


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base in ("index.js", "index.jsx", "index.ts", "index.tsx", "__init__.py") and "/" in path:
        base = path.rsplit("/", 2)[-2]
    return base.split(".", 1)[0].lower()


def _module_path(path: str) -> str:
    """`backend/app/models/__init__.py` -> `backend/app/models`; `x/index.ts` -> `x`."""
    stem = posixpath.splitext(path)[0]
    for tail in ("/index", "/__init__"):
        if stem.endswith(tail):
            return stem[: -len(tail)]
    return stem


def same_named(target: str, tree: Iterable[str], tail: Optional[str] = None) -> list[str]:
    """Files on the same side of the build that are the module `target` asks for, one
    folder off: the same name, and — given the import's own words, `tail` — ending in
    them, so `./client/api` never means an unrelated `routes/api.js`."""
    side = layout.side_of(target)
    want = _stem(target)
    end = "/" + tail.lower() if tail else None
    return sorted(
        p for p in tree
        if layout.side_of(p) == side and _stem(p) == want and not platform_owned(p)
        and (end is None or ("/" + _module_path(p).lower()).endswith(end))
    )


_SPEC = re.compile(r"imports `([^`]+)`")


def _tail(problem: dict) -> Optional[str]:
    """The import as its file wrote it, without the folders it climbs or its extension:
    `../services/todo_service.js` -> `services/todo_service`, `.models` -> `models`."""
    found = _SPEC.search(str(problem.get("message") or ""))
    if not found:
        return None
    spec = found.group(1).strip()
    if spec.startswith(".") and "/" not in spec and not spec.startswith(("./", "../")):
        spec = spec.lstrip(".").replace(".", "/")  # a Python relative import
    elif "/" not in spec and "." in spec and not spec.startswith("."):
        spec = spec.replace(".", "/")  # a dotted Python module
    parts = [s for s in spec.split("/") if s not in ("", ".", "..")]
    if spec.startswith(("@/", "~/")):
        parts = parts[1:]
    if not parts:
        return None
    parts[-1] = posixpath.splitext(parts[-1])[0] if parts[-1].endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py")) else parts[-1]
    return "/".join(parts)


def _module_of(importer: str, spec: str, tree: set[str]) -> Optional[str]:
    """The file `importer` means by `spec`, as TypeScript printed it."""
    if spec.startswith("."):
        return find(layout.join(posixpath.dirname(importer), spec), tree)
    if spec.startswith(("@/", "~/")):
        # An alias: whichever file on the importer's side ends in the rest of it.
        rest = layout.clean(spec[2:])
        side = layout.side_of(importer)
        hits = [
            p for p in tree
            if layout.side_of(p) == side and posixpath.splitext(p)[0].endswith(("/" + rest, "/" + rest + "/index"))
        ]
        return hits[0] if len(hits) == 1 else None
    # The build's own path, when the checker couldn't say how the file wrote it — on
    # records from before #101, still under the checker's in-memory root.
    return find(_VIRTUAL.sub("", spec), tree)


def _python_missing(problem: dict) -> Optional[str]:
    """A Python relative import's whole module path. The checker names only its first
    segment's file — `services.py` for `.services.todo` — which is the package to make
    a folder of, not the module to write."""
    found = _SPEC.search(str(problem.get("message") or ""))
    path = str(problem.get("path") or "")
    if not found or not path.endswith(".py"):
        return None
    spec = found.group(1)
    level = len(spec) - len(spec.lstrip("."))
    if not level or not spec[level:]:
        return None
    base = posixpath.dirname(path)
    for _ in range(level - 1):
        base = posixpath.dirname(base)
    return layout.join(base, *spec[level:].split(".")) + ".py"


def _missing(problem: dict) -> Optional[str]:
    """The path an import of a file that doesn't exist points at."""
    whole = _python_missing(problem)
    if whole is not None:
        return whole
    message = str(problem.get("message") or "")
    found = _MISSING_JS.search(message)
    if found:
        return layout.clean(found.group(1))
    found = _MISSING_ALIAS.search(message)
    if found:
        spec, prefix, roots = found.groups()
        root = roots.split(",")[0].strip()
        return layout.join(root, spec[len(prefix):]) if root and root != "." else None
    found = _MISSING_PY.search(message)
    return layout.clean(found.group(1)) if found else None


def _importers_of(name: str, own: dict[str, str]) -> list[str]:
    """This phase's files that import the package `name`."""
    out = []
    for path, code in own.items():
        if path.endswith(".py"):
            if any((module or "").split(".")[0] == name for module, level in py_imports(code) if not level):
                out.append(path)
        elif any(pkg.npm_package(spec) == name for spec in js_imports(code)):
            out.append(path)
    return out


def _is_package(target: str, tree: set[str]) -> bool:
    """A Python import's first missing segment that is really a package folder: the
    checker names `models.py` for `from .models.user import User` when `models/` is
    there and `user.py` isn't — a `models.py` beside it would shadow the package."""
    if not target.endswith(".py"):
        return False
    folder = target[: -len(".py")] + "/"
    return any(p.startswith(folder) for p in tree)


def _mentions(text: str, path: str) -> bool:
    """`path` as a whole path in `text`: `index.ts` is not in `src/index.tsx`."""
    return bool(path) and re.search(rf"(?:(?<=\./)|(?<![\w./-])){re.escape(path)}(?![\w-]|\.\w)", text) is not None


def _as_problem(p: dict, path: str) -> Problem:
    line = p.get("line")
    return Problem(path, str(p.get("message") or "has a problem"), str(p.get("kind") or "build"),
                   int(line) if isinstance(line, int) or str(line or "").isdigit() else None)


def scope(problems: Iterable[dict], own: dict[str, str], tree: Iterable[str]) -> Scope:
    """What a fix round edits. `problems` are `{path, line, kind, message, step}` as the
    compile gate and the real build report them; `own` is this phase's files (placed
    path -> code); `tree` every path the build has, scaffold included."""
    everything = set(tree) | set(own)
    out = Scope()

    def touch(path: str, problem: Problem) -> None:
        shown = out.touch.setdefault(path, [])
        if problem not in shown:
            shown.append(problem)

    for p in problems:
        path = layout.clean(str(p.get("path") or ""))
        kind = str(p.get("kind") or "")
        if kind == "stack" and not path:
            path = stack_path(str(p.get("message") or "")) or ""
        if path and path in own:
            problem = _as_problem(p, path)
            target = _missing(p)
            others = [o for o in same_named(target, everything, _tail(p)) if o != path] if target else []
            if others:
                # The file exists, one folder off: the importer's path is what's wrong.
                problem = Problem(problem.path, f"{problem.message} (This build has {', '.join(f'`{o}`' for o in others[:3])}.)",
                                  problem.kind, problem.line)
            elif target and not _is_package(target, everything) and not target.lower().endswith(_BINARY):
                # Nothing of that name anywhere: the file the import wants is written —
                # and that answers the import, so the importer isn't asked about it.
                named_as_is = target.endswith(_SOURCE) or target.lower().endswith(_TEXT_ASSETS)
                new = target if named_as_is else target + posixpath.splitext(path)[1]
                out.new.setdefault(new, []).append(problem)
                continue
            touch(path, problem)
            # The file the problem names, decided in the same call when it fits.
            module = next(iter(_MODULE.findall(str(p.get("message") or ""))), None)
            exporter = _module_of(path, module, everything) if module else None
            if exporter and exporter != path and exporter in own:
                touch(exporter, problem)
                if (path, exporter) not in out.pairs:
                    out.pairs.append((path, exporter))
            continue
        if path and kind == "missing" and not platform_owned(path):
            # Planned and never written: the plan asked for it, so it is written now.
            out.new.setdefault(path, []).append(_as_problem(p, path))
            continue
        if path and platform_owned(path) and kind == "package":
            # A package that won't install is the platform's manifest's problem only in
            # name: the files that import it are what change.
            name = next(iter(_PACKAGE.findall(str(p.get("message") or ""))), None)
            users = _importers_of(name, own) if name else []
            for user in users:
                touch(user, _as_problem(p, user))
            if users:
                continue
        # Last: a file of this phase the message mentions by its whole path.
        message = str(p.get("message") or "")
        named = [f for f in own if _mentions(message, f) or _mentions(message, layout.relative_to_side(f))]
        if named:
            for f in named:
                touch(f, _as_problem(p, f))
            continue
        out.unlocated.append(dict(p))
    # A file written new is not also an edit.
    for path in list(out.new):
        if path in own:
            for problem in out.new.pop(path):
                touch(path, problem)
    return out


def named(path: str, message: str, tree: Iterable[str]) -> Optional[str]:
    """The file of `tree` a problem in `path` names as the module it imports from."""
    module = next(iter(_MODULE.findall(message or "")), None)
    return _module_of(path, module, set(tree)) if module else None


def imports_of(path: str, code: str, own: Iterable[str]) -> list[str]:
    """The files of `own` that `path` imports by a relative path — whose code a fix
    call shows beside it, where it fits."""
    paths = set(own)
    here = posixpath.dirname(path)
    found: list[str] = []
    if path.endswith(".py"):
        for module, level in py_imports(code):
            if not level:
                continue
            base = here
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            target = find(layout.join(base, *(module or "").split(".")), paths) if module else None
            if target and target != path and target not in found:
                found.append(target)
        return found
    for spec in js_imports(code):
        if spec.startswith("."):
            target = find(layout.join(here, spec), paths)
            if target and target != path and target not in found:
                found.append(target)
    return found


__all__ = ["Scope", "find", "imports_of", "named", "same_named", "scope", "stack_path"]
