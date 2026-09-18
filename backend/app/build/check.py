"""The compile gate: does the generated code parse, and does everything it uses exist?

"Published results put compilation-feedback loops at a 40–60% reduction in failure
rate versus single-shot" — and the loop only works if the feedback names the file,
the line and the thing that is wrong. This produces exactly that, for the three
failures that stop a build before it runs:

  **syntax**    — the file does not parse (Python's own parser; TypeScript's for
                  JavaScript and TypeScript, JSX included)
  **reference** — a name used that nothing declares or imports, the `<Header />`
                  nobody imported that fails `next build` when the page prerenders
  **import**    — a relative import of a file nobody wrote, or a package the platform
                  cannot put in the manifest

Type errors are deliberately not on the list. They are real, but a build with them
still runs, and failing a 7B model's phase over a mistyped prop would send correct
work back for a reason `next build` itself is configured to ignore.
"""
from __future__ import annotations

import ast
import json
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from app.build import layout, toolchain
from app.build import packages as pkg
from app.build.scaffold import js_imports, py_imports
from app.core.config import settings
from app.core.constants import BuildStatus
from app.core.logging import get_logger

log = get_logger(__name__)

_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_RESOLVE_EXT = ("", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".json", ".css", ".scss")
_CHECKER = Path(__file__).with_name("check_js.cjs")

#: Per file and in total: a repair prompt that lists sixty problems buries the three
#: that cause the other fifty-seven.
_PER_FILE = 5
_TOTAL = 24


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    kind: str  # syntax | reference | import | package
    line: Optional[int] = None

    def text(self) -> str:
        where = f"`{self.path}`" + (f" line {self.line}" if self.line else "")
        return f"{where} — {self.message}"

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "kind": self.kind, "message": self.message}


@dataclass
class BuildCheck:
    status: str = BuildStatus.OK.value
    problems: list[Problem] = field(default_factory=list)
    #: Files that exist and could not be read by any parser here.
    unchecked: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    checked: int = 0

    def messages(self) -> list[str]:
        return [p.text() for p in self.problems]

    def as_list(self) -> list[dict]:
        return [p.as_dict() for p in self.problems]


# ── resolution ───────────────────────────────────────────────────────────────
def _resolves(target: str, tree: set[str]) -> bool:
    for ext in _RESOLVE_EXT:
        if f"{target}{ext}" in tree:
            return True
    for ext in _RESOLVE_EXT[1:6]:
        if f"{target}/index{ext}" in tree:
            return True
    return False


def _jsonc(text: str) -> Optional[dict]:
    """A tsconfig as data: JSON with the comments and trailing commas tsc allows."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        data = json.loads(cleaned)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _compiler_options(files: dict[str, str], path: str, seen: frozenset = frozenset()) -> dict:
    """A config's compilerOptions with `extends` followed, as tsc merges them.

    Relative paths in an extended config (`baseUrl`, `paths` targets) are resolved
    against the config that declares them, and carried as absolute tree paths.
    """
    data = _jsonc(files.get(path) or "")
    if data is None or path in seen:
        return {}
    here = posixpath.dirname(path)
    merged: dict = {}
    parent = data.get("extends")
    if isinstance(parent, str) and parent.startswith("."):
        target = layout.join(here, parent)
        if not target.endswith(".json"):
            target += ".json"
        merged.update(_compiler_options(files, target, seen | {path}))
    options = data.get("compilerOptions") or {}
    if isinstance(options, dict):
        if isinstance(options.get("baseUrl"), str):
            merged["baseUrl"] = layout.join(here, options["baseUrl"])
        if isinstance(options.get("paths"), dict):
            merged["paths"] = options["paths"]
            # Where `paths` resolve from when no `baseUrl` is set anywhere in the chain:
            # the config that declared them. A `baseUrl` — including one set later by a
            # config extending this one — takes over, as it does for tsc.
            merged["pathsDir"] = here
    return merged


def aliases_for(files: dict[str, str], side: Optional[str]) -> dict[str, list[str]]:
    """`{"@/": ["frontend/src"]}` — the import aliases one side's config defines.

    Read from the config in the tree rather than guessed, so the check resolves an
    alias the way `next build` / `vite build` will: an alias the project does not
    configure is an import that will not resolve, whatever directory it names. Targets
    are tree paths, resolved against `baseUrl` (or the config itself when there is
    none), with `extends` followed. Longest alias first, as TypeScript matches them.
    """
    if not side:
        return {}
    for name in ("tsconfig.json", "jsconfig.json"):
        options = _compiler_options(files, f"{side}/{name}")
        paths = options.get("paths")
        if not isinstance(paths, dict):
            continue  # a config without paths does not hide one that has them
        base = options.get("baseUrl") or options.get("pathsDir") or side
        out: dict[str, list[str]] = {}
        for key, targets in paths.items():
            if not (isinstance(key, str) and key.endswith("*") and isinstance(targets, list)):
                continue
            out[key[:-1]] = [
                layout.join(base, str(t)[:-1]) for t in targets if isinstance(t, str) and t.endswith("*")
            ]
        return dict(sorted(out.items(), key=lambda kv: len(kv[0]), reverse=True))
    return {}


def _check_js_imports(path: str, content: str, tree: set[str], aliases: dict[str, list[str]]) -> list[Problem]:
    problems: list[Problem] = []
    side = layout.side_of(path)
    base = posixpath.dirname(path)
    for spec in dict.fromkeys(js_imports(content)):
        if spec.startswith("."):
            target = layout.join(base, spec)
            if not _resolves(target, tree):
                problems.append(
                    Problem(path, f"imports `{spec}`, and no file in this build is at {target}. "
                            "Write that file, or import one that exists.", "import")
                )
            continue
        prefix = next((p for p in aliases if spec.startswith(p)), None)
        if prefix is None and spec.startswith(("@/", "~/")):
            problems.append(
                Problem(path, f"imports `{spec}`, but this build defines no `{spec[:2]}` alias. "
                        "Use a relative import.", "import")
            )
            continue
        if prefix is not None:
            rel = spec[len(prefix):]
            roots = aliases[prefix]
            if not any(_resolves(layout.join(root, rel), tree) for root in roots):
                where = ", ".join(roots) or "."
                problems.append(
                    Problem(path, f"imports `{spec}`, and no file in this build matches it "
                            f"(`{prefix}` points at {where}/).", "import")
                )
            continue
        if spec.startswith("/") or pkg.is_node_builtin(spec):
            continue
        name = pkg.npm_package(spec)
        if name and pkg.npm_version(name) is None:
            problems.append(
                Problem(
                    path,
                    f"imports `{name}`, which is not a package the platform can install. "
                    "Use one of the available packages, or write it as a file in this build.",
                    "package",
                )
            )
    return problems


def _python_roots(path: str) -> list[str]:
    """Directories a Python file's absolute imports may resolve from."""
    side = layout.side_of(path)
    roots = [side] if side else [""]
    here = posixpath.dirname(path)
    while here and here not in roots:
        roots.append(here)
        if here == side:
            break
        here = posixpath.dirname(here)
    return roots


def _module_exists(root: str, dotted: str, tree: set[str], dirs: set[str]) -> bool:
    top = dotted.split(".")[0]
    stem = f"{root}/{top}" if root else top
    return f"{stem}.py" in tree or stem in dirs


def _check_python(path: str, content: str, tree: set[str], dirs: set[str]) -> list[Problem]:
    try:
        ast.parse(content, filename=path)
    except SyntaxError as e:
        detail = (e.msg or "invalid syntax").rstrip(".")
        snippet = (e.text or "").strip()
        hint = f" near `{snippet[:80]}`" if snippet else ""
        return [Problem(path, f"does not parse: {detail}{hint}", "syntax", e.lineno)]
    except ValueError as e:  # e.g. null bytes
        return [Problem(path, f"does not parse: {e}", "syntax")]

    problems: list[Problem] = []
    roots = _python_roots(path)
    here = posixpath.dirname(path)
    for module, level in dict.fromkeys(py_imports(content)):
        if level:
            base = here
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            if module and not _module_exists(base, module, tree, dirs):
                problems.append(
                    Problem(path, f"imports `{'.' * level}{module}`, and no module is at "
                            f"{base}/{module.split('.')[0]}.py.", "import")
                )
            continue
        if not module or pkg.is_stdlib(module):
            continue
        if any(_module_exists(root, module, tree, dirs) for root in roots):
            continue
        if pkg.pip_requirement(module):
            continue
        problems.append(
            Problem(
                path,
                f"imports `{module.split('.')[0]}`, which is neither a module in this build nor "
                "a package the platform can install.",
                "package",
            )
        )
    return problems


# ── the JavaScript parser ────────────────────────────────────────────────────
def _paths_for(files: dict[str, str], side: str) -> dict:
    """The same aliases, in the form TypeScript's resolver takes them: relative to the
    side's own root, which is `baseUrl` in the checker's virtual tree."""
    prefix_len = len(side) + 1
    return {
        f"{prefix}*": [f"{root[prefix_len:]}/*" if len(root) > len(side) else "*" for root in roots]
        for prefix, roots in aliases_for(files, None if side == "root" else side).items()
    }


def _run_js(groups: list[dict]) -> tuple[Optional[list[dict]], Optional[str]]:
    """(diagnostics, reason-if-not-run)."""
    ts_path = toolchain.typescript()
    node = toolchain.node()
    if not ts_path or not node:
        return None, toolchain.unavailable_reason() or "No JavaScript parser is available."
    payload = json.dumps({"typescript": ts_path, "groups": groups})
    try:
        result = subprocess.run(
            [node, str(_CHECKER)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=max(settings.build_check_timeout_seconds, 10),
        )
    except subprocess.TimeoutExpired:
        return None, "The JavaScript check took too long and was stopped."
    except OSError as e:
        return None, f"The JavaScript check could not start: {e}"
    if result.returncode != 0:
        log.warning("JS check crashed: %s", result.stderr[-400:])
        return None, "The JavaScript check crashed."
    try:
        return json.loads(result.stdout).get("diagnostics", []), None
    except ValueError:
        return None, "The JavaScript check returned something unreadable."


def _js_problem(d: dict) -> Problem:
    message = d.get("message") or "does not parse"
    if d.get("syntactic"):
        return Problem(d["path"], f"does not parse: {message}", "syntax", d.get("line"))
    name = re.match(r"Cannot find name '([^']+)'", message)
    if name:
        message = (
            f"uses `{name.group(1)}`, which is never imported or declared in this file. "
            "Import it or define it."
        )
    return Problem(d["path"], message, "reference", d.get("line"))


# ── the check ────────────────────────────────────────────────────────────────
def check_tree(files: dict[str, str], report_on: Iterable[str]) -> BuildCheck:
    """Check `report_on` (placed paths) against the whole tree `files`."""
    tree = set(files)
    dirs = {posixpath.dirname(p) for p in tree}
    # Every ancestor, so `backend/app` counts as a package when only
    # `backend/app/api/routes.py` exists.
    for d in list(dirs):
        while d:
            dirs.add(d)
            d = posixpath.dirname(d)
    targets = [p for p in dict.fromkeys(report_on) if p in files]
    out = BuildCheck()
    problems: list[Problem] = []
    js_targets: list[str] = []

    for path in targets:
        content = files[path]
        if path.endswith(".py"):
            out.checked += 1
            problems += _check_python(path, content, tree, dirs)
        elif path.endswith(".json"):
            out.checked += 1
            try:
                json.loads(content)
            except ValueError as e:
                problems.append(Problem(path, f"is not valid JSON: {e.msg}", "syntax", e.lineno))
        elif path.endswith(_JS_EXT):
            out.checked += 1
            problems += _check_js_imports(path, content, tree, aliases_for(files, layout.side_of(path)))
            js_targets.append(path)

    if js_targets:
        groups = []
        for side in sorted({layout.side_of(p) or "root" for p in js_targets}):
            prefix = "" if side == "root" else f"{side}/"
            side_files = {
                p[len(prefix):]: c
                for p, c in files.items()
                if p.endswith(_JS_EXT) and (p.startswith(prefix) if prefix else layout.side_of(p) is None)
            }
            groups.append(
                {
                    "name": side,
                    "files": side_files,
                    "report": [p[len(prefix):] for p in js_targets if (layout.side_of(p) or "root") == side],
                    "paths": _paths_for(files, side),
                }
            )
        diagnostics, reason = _run_js(groups)
        if diagnostics is None:
            out.unchecked = js_targets
            out.reason = reason
        else:
            for d in diagnostics:
                if d.get("crashed") or not d.get("path"):
                    continue
                side = d.get("group")
                full = d["path"] if side == "root" else f"{side}/{d['path']}"
                problems.append(_js_problem({**d, "path": full}))

    # Worst first within a file (a syntax error makes the rest noise), capped.
    per_file: dict[str, list[Problem]] = {}
    order = {"syntax": 0, "import": 1, "package": 2, "reference": 3}
    for p in sorted(problems, key=lambda p: (order.get(p.kind, 9), p.line or 0)):
        bucket = per_file.setdefault(p.path, [])
        if p.kind != "syntax" and any(q.kind == "syntax" for q in bucket):
            continue
        # One line per missing name: `fireEvent` used on six lines is one fix.
        if any(q.message == p.message for q in bucket):
            continue
        if len(bucket) < _PER_FILE:
            bucket.append(p)
    flat = [p for path in targets for p in per_file.get(path, [])]
    out.problems = flat[:_TOTAL]
    if out.problems:
        out.status = BuildStatus.FAILED.value
    elif out.unchecked:
        out.status = BuildStatus.UNCHECKED.value
    return out


# ── the tree a phase is checked in ───────────────────────────────────────────
def phase_tree(
    prior_outputs: dict,
    phase_key: str,
    output: dict,
    charter=None,
) -> tuple[dict[str, str], list[str]]:
    """(every placed file the build has so far, the paths this phase wrote).

    A phase is checked in the context of what exists: its imports may point at files
    an earlier phase wrote, and at the scaffold the platform will add — a layout
    importing `./globals.css` is fine when the platform is about to write it.
    """
    from app.build.scaffold import build as scaffold_build, platform_owned, superseded
    from app.core.artifacts import iter_files
    from app.core.constants import PHASE_ORDER

    backend_language = charter.get("language").token if charter is not None and charter.get("language") else None
    files: dict[str, str] = {}
    placer = layout.Placer(backend_language)
    order = [p.value for p in PHASE_ORDER]
    for key in order:
        if key == phase_key:
            break
        for placed, _path, content, _lang in placer.place_all(key, iter_files(prior_outputs.get(key) or {})):
            files[placed] = content
    mine: list[str] = []
    for placed, _path, content, _lang in placer.place_all(
        phase_key, iter_files(output if isinstance(output, dict) else {})
    ):
        files[placed] = content
        mine.append(placed)

    design = prior_outputs.get("system_design") if isinstance(prior_outputs, dict) else None
    scaffold = scaffold_build(files, charter, design if isinstance(design, dict) else None)
    # What the scaffold writes replaces the agent's copy, here as in the archive — the
    # check has to resolve imports against the config that will actually ship.
    written = scaffold.paths()
    for path in [p for p in files if platform_owned(p) and superseded(p, written)]:
        del files[path]
    for f in scaffold.files:
        files[f.path] = f.content
    return files, mine


def check_phase(prior_outputs: dict, phase_key: str, output: dict, charter=None) -> BuildCheck:
    files, mine = phase_tree(prior_outputs, phase_key, output, charter)
    # Platform-owned files an agent wrote anyway are replaced, not checked.
    from app.build.scaffold import platform_owned

    return check_tree(files, [p for p in mine if not platform_owned(p)])
