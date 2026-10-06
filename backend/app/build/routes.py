"""Which HTTP routes a build serves, and which ones its frontend calls.

Read from the code, never from what an agent said it wrote: a Backend output whose
summary lists `/api/todos` while its router serves `/api/tasks` is exactly the
mismatch this exists to catch. Three readers share it — the hand-off digest (what the
Frontend is told the backend implements), the compile gate (a call nothing serves is
sent back), and the scorecard (how many calls are wired).

Deliberately lenient where it cannot know. A FastAPI router's prefix is often set in
another file (`include_router(x, prefix="/api")`), and an Express router is mounted
with `app.use("/api", router)`, so a route is compared by its *tail*: `/todos/{id}`
serves a call to `/api/todos/7`. A gate that sent correct code back because it could
not follow an include would be worse than no gate.
"""
from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.build import layout

_PY_ROUTE = re.compile(
    r"""@\s*\w+(?:\.\w+)*\.(get|post|put|patch|delete|route|api_route)\(\s*[rbuf]?["']([^"']*)["']""",
    re.IGNORECASE,
)
_PY_PREFIX = re.compile(r"""(\w+)\s*=\s*(?:\w+\.)?(?:APIRouter|Blueprint)\(([^)]*)\)""")
_PY_PREFIX_ARG = re.compile(r"""(?:prefix|url_prefix)\s*=\s*["']([^"']*)["']""")
_JS_ROUTE = re.compile(
    r"""\b(?:app|router|server|api|\w+Router|routes)\.(get|post|put|patch|delete|all)\(\s*['"`]([^'"`]+)['"`]""",
)
_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

#: `fetch("/api/x")`, `fetch(`${API}/x`)`, `axios.get('/x')`, `api.post("/x")`, …
_CALL = re.compile(
    r"""\b(?:fetch|axios(?:\.(?:get|post|put|patch|delete|request))?|"""
    r"""(?:api|client|http|request|instance|apiClient|axiosInstance)\.(?:get|post|put|patch|delete))"""
    r"""\(\s*(['"`])((?:(?!\1).){1,300}?)\1""",
)
_TEMPLATE = re.compile(r"\$\{[^}]*\}")
_PARAM = re.compile(r"^(?:\{[^}]*\}|:[\w]+\??|<[^>]*>|\[[^\]]*\]|\*)$")
#: A segment that takes the rest of the path: `{p:path}`, `<path:p>`, `[...slug]`, `*`.
_REST = re.compile(r"^(?:\{\w+:path\}|<path:\w+>|\[\[?\.\.\.\w+\]\]?|\*|:\w+\*)$")
#: Where a router is mounted, in whatever file does the mounting.
_MOUNT = re.compile(
    r"""(?:include_router|register_blueprint)\([^)]*?(?:url_)?prefix\s*=\s*["']([^"']+)["']"""
    r"""|\.use\(\s*['"`](/[^'"`]*)['"`]\s*,"""
)
_FILE_EXT = re.compile(r"\.[a-z0-9]{1,5}$", re.IGNORECASE)
_METHOD_OPT = re.compile(r"""method\s*:\s*['"`](\w+)['"`]""", re.IGNORECASE)


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    file: str
    line: int = 0

    @property
    def shape(self) -> tuple[str, ...]:
        return segments(self.path)


@dataclass(frozen=True)
class Call:
    path: str
    file: str
    line: int
    method: str = "GET"

    @property
    def shape(self) -> tuple[str, ...]:
        return segments(self.path)


def segments(path: str) -> tuple[str, ...]:
    """`/api/todos/{id}/` -> ('api', 'todos', '*'). Parameters of every spelling are '*',
    a rest-of-path parameter is '**', and a Next.js route group `(auth)` is no segment."""
    path = (path or "").split("?", 1)[0].split("#", 1)[0]
    out = []
    for part in path.strip("/").split("/"):
        if not part or (part.startswith("(") and part.endswith(")")):
            continue
        if "\0" in part and part.strip("\0") and "\0" not in part.strip("\0"):
            # `tasks${qs}`: fixed text with a template on one end is that text — a
            # wildcard here would let a misspelled path hide behind a query string.
            part = part.strip("\0")
        if _REST.match(part):
            out.append("**")
        elif _PARAM.match(part) or "\0" in part:
            out.append("*")
        else:
            out.append(part.lower())
    return tuple(out)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# ── what the backend serves ───────────────────────────────────────────────────
def mounts(files: dict[str, str], side: Optional[str] = layout.BACKEND) -> list[str]:
    """Every prefix a router is mounted at (`include_router(x, prefix="/api")`, `app.use("/api", r)`)."""
    found: dict[str, None] = {}
    for path, content in files.items():
        if (side is not None and layout.side_of(path) != side) or not isinstance(content, str):
            continue
        for m in _MOUNT.finditer(content):
            prefix = (m.group(1) or m.group(2) or "").rstrip("/")
            if prefix:
                found.setdefault(prefix, None)
    return list(found)


def served(files: dict[str, str], side: Optional[str] = layout.BACKEND) -> list[Route]:
    """Every route declared in `files` (placed paths), on one side or everywhere."""
    out: list[Route] = []
    for path, content in files.items():
        if side is not None and layout.side_of(path) != side:
            continue
        if not isinstance(content, str):
            continue
        if path.endswith(".py"):
            out += _python_routes(path, content)
        elif path.endswith(_JS_EXT):
            out += _js_routes(path, content)
    return out


def _python_routes(path: str, content: str) -> list[Route]:
    prefixes: dict[str, str] = {}
    for m in _PY_PREFIX.finditer(content):
        arg = _PY_PREFIX_ARG.search(m.group(2))
        if arg:
            prefixes[m.group(1)] = arg.group(1)
    out = []
    for m in _PY_ROUTE.finditer(content):
        decorator = content[m.start():m.end()]
        owner = re.match(r"@\s*(\w+)", decorator)
        prefix = prefixes.get(owner.group(1), "") if owner else ""
        method = m.group(1).upper()
        if method in ("ROUTE", "API_ROUTE"):
            tail = content[m.end(): m.end() + 120]
            found = re.search(r"methods\s*=\s*\[\s*['\"](\w+)", tail)
            method = found.group(1).upper() if found else "GET"
        out.append(Route(method, _join(prefix, m.group(2)), path, _line_of(content, m.start())))
    return out


def _js_routes(path: str, content: str) -> list[Route]:
    return [
        Route(m.group(1).upper().replace("ALL", "ANY"), m.group(2), path, _line_of(content, m.start()))
        for m in _JS_ROUTE.finditer(content)
        if m.group(2).startswith("/")
    ]


def frontend_api(files: dict[str, str]) -> list[Route]:
    """Routes the frontend serves itself: Next.js `app/api/**/route.*` and `pages/api/**`."""
    out = []
    for path in files:
        if layout.side_of(path) != layout.FRONTEND or not path.endswith(_JS_EXT):
            continue
        rel = layout.relative_to_side(path)
        for root in ("src/app/api/", "app/api/"):
            if rel.startswith(root) and posixpath.basename(rel).split(".")[0] == "route":
                out.append(Route("ANY", "/api/" + posixpath.dirname(rel[len(root):]), path))
        for root in ("src/pages/api/", "pages/api/"):
            if rel.startswith(root):
                stem = rel[len(root):].rsplit(".", 1)[0]
                stem = stem[: -len("/index")] if stem.endswith("/index") else ("" if stem == "index" else stem)
                out.append(Route("ANY", "/api/" + stem, path))
    return out


def _join(prefix: str, path: str) -> str:
    joined = "/" + "/".join(p for p in (prefix.strip("/"), path.strip("/")) if p)
    return joined


# ── what the frontend calls ───────────────────────────────────────────────────
def calls(files: dict[str, str], targets: Optional[Iterable[str]] = None) -> list[Call]:
    """Every same-origin-or-backend call in the frontend files (placed paths).

    An absolute URL to another host is a third-party API, not this build's backend,
    and is skipped. A template's leading `${API_URL}` is the backend's base URL.
    """
    wanted = set(targets) if targets is not None else None
    out: list[Call] = []
    for path, content in files.items():
        if wanted is not None and path not in wanted:
            continue
        if layout.side_of(path) != layout.FRONTEND or not path.endswith(_JS_EXT) or not isinstance(content, str):
            continue
        for m in _CALL.finditer(content):
            raw = m.group(2).strip()
            url = _call_path(raw)
            if url is None:
                continue
            method = "GET"
            head = m.group(0).split("(", 1)[0].lower()
            for verb in ("post", "put", "patch", "delete"):
                if head.endswith("." + verb):
                    method = verb.upper()
            if head.startswith("fetch"):
                opt = _METHOD_OPT.search(content[m.end(): m.end() + 200])
                if opt:
                    method = opt.group(1).upper()
            out.append(Call(url, path, _line_of(content, m.start()), method))
    return out


def _call_path(raw: str) -> Optional[str]:
    if re.match(r"^https?://", raw, re.IGNORECASE):
        host = re.match(r"^https?://([^/]+)", raw, re.IGNORECASE)
        if not host or not re.match(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$", host.group(1)):
            return None
        raw = raw[host.end():]
    # A leading `${API_URL}` / `${base}` is the backend's address.
    raw = re.sub(r"^\$\{[^}]*\}", "", raw)
    raw = _TEMPLATE.sub("\0", raw).split("?", 1)[0]
    if not raw.startswith("/") or raw.startswith("//"):
        return None
    shape = segments(raw)
    if not shape or all(s == "*" for s in shape):
        return None
    if _FILE_EXT.search(shape[-1]):
        return None  # a static file (`/data.json`, `/logo.svg`), not an API call
    return "/" + "/".join("{param}" if s == "*" else s for s in shape)


# ── matching ──────────────────────────────────────────────────────────────────
def _same(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Equal, segment by segment, with '*' matching one segment and '**' the rest."""
    if "**" in b:
        i = b.index("**")
        return len(a) >= i and _same(a[:i], b[:i])
    if "**" in a:
        return _same(b, a)
    return len(a) == len(b) and all(x == "*" or y == "*" or x == y for x, y in zip(a, b))


def _tail_match(call: tuple[str, ...], route: tuple[str, ...]) -> bool:
    """A route serves a call when one is the other's tail.

    The route's prefix may be set where it is mounted (`/todos` serving `/api/todos`),
    and the call's base URL may already carry it (`${API}/todos` with `API=…/api`).
    At least one fixed segment has to line up, so a bare `/{id}` matches nothing.
    """
    if not route or not call:
        return False
    if "**" in route:
        head = route[: route.index("**")]
        return any(_same(call[i:i + len(head)], head) for i in range(0, len(call) - len(head) + 1)) and bool(head)
    short, long_ = (route, call) if len(route) <= len(call) else (call, route)
    if not any(s not in ("*", "**") for s in short):
        return len(route) == len(call) and _same(call, route)
    return _same(long_[len(long_) - len(short):], short)


def _expanded(routes: Iterable[Route], prefixes: Iterable[str]) -> list[Route]:
    """Each route as declared, and as mounted under every prefix the backend uses."""
    out = list(routes)
    for r in list(out):
        for prefix in prefixes:
            if not r.path.startswith(prefix + "/") and r.path != prefix:
                out.append(Route(r.method, _join(prefix, r.path), r.file, r.line))
    return out


def is_served(call: Call, routes: Iterable[Route]) -> bool:
    return any(_tail_match(call.shape, r.shape) for r in routes)


def nearest(path: str, candidates: Iterable[str]) -> Optional[str]:
    """The candidate path most like `path`, compared on their parameter-blind shapes."""
    pool = {"/" + "/".join(segments(c)): c for c in candidates if segments(c)}
    if not pool:
        return None
    key = "/" + "/".join(segments(path))
    found = difflib.get_close_matches(key, list(pool), n=1, cutoff=0.0)
    return pool[found[0]] if found else None


def unserved_calls(files: dict[str, str], targets: Iterable[str]) -> list[tuple[Call, Optional[str]]]:
    """Frontend calls in `targets` that no backend route (or frontend API route) serves.

    Empty when the build has no backend routes at all: with nothing to compare
    against, every call would be "unserved", and that says nothing about the call.
    """
    backend = _expanded(served(files, layout.BACKEND), mounts(files, layout.BACKEND))
    if not backend:
        return []
    known = backend + frontend_api(files)
    out = []
    for call in calls(files, targets):
        if not is_served(call, known):
            out.append((call, nearest(call.path, [r.path for r in backend])))
    return out


def _number_slip(a: str, b: str) -> bool:
    """`todo`/`todos`, `category`/`categories`: the same name, singular for plural."""
    def stem(w: str) -> str:
        if w.endswith("ies"):
            return w[:-3] + "y"
        if w.endswith(("ses", "xes", "zes", "ches", "shes")):
            return w[:-2]
        return w[:-1] if w.endswith("s") and not w.endswith("ss") else w

    return a != b and stem(a) == stem(b)


def off_registry(
    routes: Iterable[Route], registry_paths: Iterable[str], cutoff: float = 0.8
) -> list[tuple[Route, str]]:
    """Backend routes that are a near miss of a registry path — a rename, not an addition.

    Near miss means the same number of segments with exactly one fixed segment that is
    the registry's name in the other number (`todo` beside `todos`). A route the
    registry does not list (`/health`, `/api/status` beside `/api/stats`, a nested
    `/api/tasks/{id}/tags`) is an addition, and is fine.
    """
    paths = [p for p in registry_paths if segments(p)]
    if not paths:
        return []
    out = []
    for route in routes:
        shape = route.shape
        if not shape or any(_tail_match(segments(p), shape) for p in paths):
            continue
        for p in paths:
            want = segments(p)
            # Compared on the registry path's tail: a prefix set elsewhere is not a rename.
            if len(want) < len(shape):
                continue
            want = want[len(want) - len(shape):]
            diff = [(a, b) for a, b in zip(shape, want) if not (a == b or "*" in (a, b))]
            if len(diff) == 1 and _number_slip(*diff[0]):
                out.append((route, p))
                break
    return out


_ENV_PY = re.compile(r"""os\.(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Z][A-Z0-9_]*)["']""")
_ENV_JS = re.compile(r"""\bprocess\.env\.([A-Z][A-Z0-9_]*)|\bprocess\.env\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\]""")


def env_names(files: dict[str, str]) -> list[str]:
    """The environment variables the code reads, in first-seen order."""
    seen: dict[str, None] = {}
    for path, content in files.items():
        if not isinstance(content, str):
            continue
        if path.endswith(".py"):
            for m in _ENV_PY.finditer(content):
                seen.setdefault(m.group(1), None)
        elif path.endswith(_JS_EXT):
            for m in _ENV_JS.finditer(content):
                seen.setdefault(m.group(1) or m.group(2), None)
    return list(seen)
