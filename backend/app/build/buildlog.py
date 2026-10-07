"""What a real build said, as problems the fix loop can act on (#75).

`npm install`, `next build`, `vite build`, `tsc`, a Python import and a Node boot each
fail in their own words. A repair prompt that pastes them whole buries the one line
that matters, so each is read for the file, the line and the message, in the shape
the compile gate already uses (`check.Problem`):

  * `next build` / `tsc`: `./app/page.tsx:7:10` + `Type error: …`, `Module not found`,
    SWC's `x Unexpected token`, `path(line,col): error TS2322: …`, and a page that
    crashes while it is prerendered;
  * Vite / esbuild / Rollup: `src/App.tsx:3:7: ERROR: …`, `✘ [ERROR] …`, `x (3:9): …`;
  * a Python traceback: the deepest frame inside the project, and the exception;
  * a Node crash: `Error: …` and the first stack frame inside the project;
  * npm `ETARGET` / `E404` / `ERESOLVE` and pip's "no matching distribution": a
    `package` problem naming the package, not a wall of text.

Output nothing here recognises becomes one problem on the side's manifest carrying the
last 30 lines, so a failure is never silently dropped. The compile gate's caps apply:
five problems per file, twenty-four in all.
"""
from __future__ import annotations

import posixpath
import re
from typing import Iterable, Optional

from app.build.check import Problem

_PER_FILE = 5
_TOTAL = 24
_TAIL_LINES = 30

#: Where a build puts the project — the sandbox's `/work`, Vercel's `/vercel/path0`.
#: Paths in tool output are made relative to it.
_ROOT = re.compile(r"^(?:/work|/vercel/path\d+)(?:/|$)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SRC_EXT = r"(?:tsx?|jsx?|mjs|cjs|mts|cts|vue|svelte|css|scss|json|py)"

# ── environment, not code ────────────────────────────────────────────────────
#: A service the sandbox does not have — a database, a cache, the network. An app that
#: cannot reach its database in a box with no network is not broken, and sending it
#: back for that would ask the crew to fix the sandbox.
_ENVIRONMENTAL = re.compile(
    r"ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ENETUNREACH|getaddrinfo|Connection refused|"
    r"could not connect to server|Name or service not known|Temporary failure in name resolution|"
    r"ServerSelectionTimeoutError|MongooseServerSelectionError|MongoNetworkError|Network is unreachable|"
    r"connect ETIMEDOUT|Can't connect to MySQL|redis\.exceptions\.ConnectionError|"
    r"Failed to fetch font|Failed to download `.*` from Google Fonts|"
    r"psycopg2?\.OperationalError|sqlalchemy\.exc\.OperationalError: \(psycopg",
)


def environmental(output: str) -> Optional[str]:
    """The line saying a step failed for want of a service, or None."""
    for line in clean(output).splitlines():
        if _ENVIRONMENTAL.search(line):
            return line.strip()[:240]
    return None


def clean(output: str) -> str:
    return _ANSI.sub("", output or "").replace("\r\n", "\n").replace("\r", "\n")


def rel(path: str) -> str:
    """A path as the project knows it: no `/work/`, no `./`."""
    p = _ROOT.sub("", path.strip().strip("'\"`"), count=1)
    while p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(p) if p else p


def _ours(path: str) -> bool:
    """Whether a path in a trace is the project's own file, not a dependency's."""
    p = path.strip()
    if not p or p.startswith(("node:", "internal/", "<")):
        return False
    if p.startswith("/") and not _ROOT.match(p):
        return False
    r = rel(p)
    return bool(r) and not r.startswith(("node_modules/", ".deps/", ".next/", "dist/", "../")) and "/node_modules/" not in r and "site-packages" not in r


def _short(message: str, limit: int = 400) -> str:
    text = " ".join(message.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── npm / pip ────────────────────────────────────────────────────────────────
_NPM = r"npm (?:ERR!|error)"
_NPM_NOTARGET = re.compile(rf"{_NPM} notarget No matching version found for (?P<spec>\S+?)\.?$", re.M)
_NPM_404 = re.compile(rf"{_NPM} 404\s+'(?P<spec>[^']+)' is not in (?:the npm|this) registry", re.M)
_NPM_404_GET = re.compile(rf"{_NPM} 404 Not Found - GET \S+/(?P<name>(?:@[^/\s]+/)?[^/\s]+?)(?: - |\s|$)", re.M)
_NPM_PEER = re.compile(rf"{_NPM} (?:peer|Could not resolve dependency:\s*\n{_NPM} peer) (?P<spec>\S+) from (?P<by>\S+)", re.M)
_NPM_CODE = re.compile(rf"{_NPM} code (?P<code>E[A-Z0-9]+)", re.M)
_PIP_NONE = re.compile(r"ERROR: No matching distribution found for (?P<spec>\S+)")
_PIP_VERSION = re.compile(r"ERROR: Could not find a version that satisfies the requirement (?P<spec>\S+)")
_PIP_WHEEL = re.compile(r"Failed to build (?:installable wheels for some pyproject\.toml based projects )?\(?(?P<names>[\w.\- ,]+)\)?")


def _pkg_name(spec: str) -> str:
    spec = spec.strip().strip("'\"")
    if spec.startswith("@"):
        scope, _, rest = spec[1:].partition("/")
        return "@" + scope + "/" + re.split(r"@", rest, maxsplit=1)[0]
    return re.split(r"[@=<>!~ \[;]", spec, maxsplit=1)[0]


def npm_problems(output: str, manifest: str = "package.json") -> list[Problem]:
    text = clean(output)
    out: list[Problem] = []
    for m in _NPM_NOTARGET.finditer(text):
        name = _pkg_name(m.group("spec"))
        out.append(Problem(manifest, f"npm has no version of `{name}` matching {m.group('spec')}. "
                                     f"Use a different package, or none — the platform installs only what the code imports.", "package"))
    for m in _NPM_404.finditer(text):
        name = _pkg_name(m.group("spec"))
        out.append(Problem(manifest, f"`{name}` is not a package on npm. Import a real package, or write the code yourself.", "package"))
    if not out:
        for m in _NPM_404_GET.finditer(text):
            name = m.group("name").replace("%2f", "/").replace("%2F", "/")
            out.append(Problem(manifest, f"`{name}` is not a package on npm. Import a real package, or write the code yourself.", "package"))
    codes = {m.group("code") for m in _NPM_CODE.finditer(text)}
    if "ERESOLVE" in codes:
        peers = list(dict.fromkeys(f"{m.group('by')} needs {m.group('spec')}" for m in _NPM_PEER.finditer(text)))
        detail = f": {'; '.join(peers[:3])}" if peers else ""
        names = [_pkg_name(m.group("by")) for m in _NPM_PEER.finditer(text)]
        lead = f"`{names[0]}`" if names else "A package"
        out.append(Problem(manifest, f"{lead} conflicts with the other packages' versions{detail}. "
                                     "Drop the package, or use one that works with this stack's React and Next versions.", "package"))
    return _dedupe(out)


def pip_problems(output: str, manifest: str = "requirements.txt") -> list[Problem]:
    text = clean(output)
    out: list[Problem] = []
    seen: set[str] = set()
    for rx in (_PIP_NONE, _PIP_VERSION):
        for m in rx.finditer(text):
            name = _pkg_name(m.group("spec"))
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(Problem(manifest, f"pip can't install `{name}` ({m.group('spec')}): no matching version. "
                                         "Import a package that exists, or none.", "package"))
    for m in _PIP_WHEEL.finditer(text):
        for name in re.split(r"[,\s]+", m.group("names")):
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(Problem(manifest, f"`{name}` failed to build from source here. Use a package with ready-made wheels.", "package"))
    return out


# ── next build / tsc / vite ──────────────────────────────────────────────────
_LOC_HEAD = re.compile(rf"^(?:\./)?(?P<path>[\w@~.\[\]()+\-/ ]+?\.{_SRC_EXT})(?::(?P<line>\d+)(?::(?P<col>\d+))?)?\s*$")
_TSC = re.compile(rf"^(?P<path>[\w@~.\[\]()+\-/]+?\.{_SRC_EXT})\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>TS\d+):\s*(?P<msg>.+)$")
_ESBUILD = re.compile(rf"^(?:/work/|/vercel/path\d+/)?(?P<path>[\w@~.\[\]()+\-/]+?\.{_SRC_EXT}):(?P<line>\d+):(?P<col>\d+):\s*ERROR:\s*(?P<msg>.+)$")
_ESBUILD_X = re.compile(r"^\s*✘\s*\[ERROR\]\s*(?P<msg>.+)$")
_ESBUILD_LOC = re.compile(rf"^\s+(?P<path>[\w@~.\[\]()+\-/]+?\.{_SRC_EXT}):(?P<line>\d+):(?P<col>\d+):\s*$")
_ROLLUP_RESOLVE = re.compile(r'Rollup failed to resolve import "(?P<spec>[^"]+)" from "(?P<path>[^"]+)"')
_ROLLUP_AT = re.compile(rf"^(?:error during build:\s*)?(?P<path>/?[\w@~.\[\]()+\-/]+?\.{_SRC_EXT}) \((?P<line>\d+):(?P<col>\d+)\): (?P<msg>.+)$")
_MESSAGE = re.compile(r"^\s*(?:(?:Type error|Module not found|Syntax error|SyntaxError|TypeError|ReferenceError|Error)\b:?.*|x .+)$")
_SWC_X = re.compile(r"^\s*(?:×|x)\s+(?P<msg>.+)$")
_PRERENDER = re.compile(r'Error occurred prerendering page "(?P<route>[^"]+)"')
_COLLECT = re.compile(r"Failed to collect page data for (?P<route>\S+)")
_JS_ERROR = re.compile(r"^(?:Uncaught\s+)?(?P<msg>(?:[A-Z]\w*)?Error(?: \[[A-Z_]+\])?:\s*.+)$")
_JS_FRAME = re.compile(r"\(?(?P<path>/work/[^\s():]+):(?P<line>\d+):(?P<col>\d+)\)?")


def _swc_line(snippet: list[str]) -> Optional[int]:
    """The line SWC's code frame points at: the numbered line just above the `^^^`."""
    last: Optional[int] = None
    for line in snippet:
        num = re.match(r"^\s*(\d+)\s*\|", line)
        if num:
            last = int(num.group(1))
        elif re.match(r"^\s*[:·]\s+\^+", line) and last is not None:
            return last
    return None


def _kind_for(message: str) -> str:
    m = message.lower()
    if "type error" in m or re.search(r"\bts\d{4}\b", m) or "is not assignable" in m:
        return "type"
    if "module not found" in m or "can't resolve" in m or "cannot find module" in m or "failed to resolve import" in m:
        return "import"
    if "syntax" in m or "unexpected token" in m or "expected" in m and "but found" in m:
        return "syntax"
    return "build"


def _page_for(route: str, files: Iterable[str]) -> Optional[str]:
    """The file that renders `route` in an app- or pages-router project."""
    route = route.split("?")[0].rstrip("/") or "/"
    seg = "" if route == "/" else route.lstrip("/")
    names = list(files)
    candidates = []
    for base in ("app", "src/app"):
        stem = f"{base}/{seg}/page" if seg else f"{base}/page"
        candidates += [f"{stem}.{e}" for e in ("tsx", "jsx", "js", "ts")]
        rstem = f"{base}/{seg}/route" if seg else f"{base}/route"
        candidates += [f"{rstem}.{e}" for e in ("ts", "js")]
    for base in ("pages", "src/pages"):
        stem = f"{base}/{seg or 'index'}"
        candidates += [f"{stem}.{e}" for e in ("tsx", "jsx", "js", "ts")]
        candidates += [f"{stem}/index.{e}" for e in ("tsx", "jsx", "js", "ts")]
    return next((c for c in candidates if c in names), None)


def js_build_problems(output: str, files: Iterable[str] = ()) -> list[Problem]:
    """`next build`, `vite build`, `tsc` and esbuild, read for file, line and message."""
    text = clean(output)
    lines = text.splitlines()
    known = set(files)
    out: list[Problem] = []

    def add(path: str, message: str, line: Optional[str | int] = None, kind: Optional[str] = None) -> None:
        p = rel(path)
        if not p or not _ours(path):
            return
        out.append(Problem(p, _short(message), kind or _kind_for(message), int(line) if line else None))

    for i, raw in enumerate(lines):
        line = raw.rstrip()
        m = _TSC.match(line.strip())
        if m:
            add(m.group("path"), f"{m.group('code')}: {m.group('msg')}", m.group("line"), "type")
            continue
        m = _ESBUILD.match(line.strip())
        if m:
            add(m.group("path"), m.group("msg"), m.group("line"))
            continue
        m = _ESBUILD_X.match(line)
        if m:
            # The location follows a blank line or two later: `    src/App.tsx:3:7:`.
            for nxt in lines[i + 1 : i + 5]:
                loc = _ESBUILD_LOC.match(nxt)
                if loc:
                    add(loc.group("path"), m.group("msg"), loc.group("line"))
                    break
            continue
        m = _ROLLUP_RESOLVE.search(line)
        if m:
            add(m.group("path"), f"imports `{m.group('spec')}`, which doesn't resolve to a file or an installed package.", None, "import")
            continue
        m = _ROLLUP_AT.match(line.strip())
        if m:
            add(m.group("path"), m.group("msg"), m.group("line"))
            continue
        m = _LOC_HEAD.match(line.strip()) if line.strip().startswith(("./", "/work/")) or (line.strip() and line.strip() in known) else None
        if m and (line.strip().startswith(("./", "/work/")) or line.strip() in known):
            # `next build`: a location on its own line, the message under it.
            message, at = None, m.group("line")
            for j, nxt in enumerate(lines[i + 1 : i + 8], start=i + 1):
                s = nxt.strip()
                if not s:
                    continue
                if s in ("Error:", "Error: "):
                    continue
                swc = _SWC_X.match(nxt)
                if swc:
                    message = swc.group("msg")
                    at = at or _swc_line(lines[j + 1 : j + 16])
                    break
                if _MESSAGE.match(s):
                    message = s
                    break
                if _LOC_HEAD.match(s) or s.startswith(("Import trace", "https://", ">")):
                    break
            if message:
                add(m.group("path"), message, at)
            continue
        m = _PRERENDER.search(line) or _COLLECT.search(line)
        if m:
            if environmental("\n".join(lines[i + 1 : i + 30])):
                # A page that fetches while it is prerendered, in a sandbox with no
                # network: not the code's fault — on Vercel the request goes out.
                continue
            route = m.group("route")
            page = _page_for(route, known) or ("app/page.tsx" if route == "/" and "app/page.tsx" in known else None)
            cause = next((l.strip() for l in lines[i + 1 : i + 12] if _JS_ERROR.match(l.strip())), "")
            where = f" {cause}" if cause else ""
            target = page or next((p for p in ("app/layout.tsx", "app/layout.jsx", "pages/_app.tsx", "pages/_app.jsx") if p in known), None)
            if target:
                out.append(Problem(target, _short(f"crashes when Next.js renders {route} at build time:{where}"), "runtime"))
    return _dedupe(out)


# ── a process that crashed ───────────────────────────────────────────────────
_PY_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)')
_PY_EXC = re.compile(r"^(?P<msg>(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Warning)\b.*)$")
_PYTEST_AT = re.compile(r"^(?P<path>[\w./\-]+\.py):(?P<line>\d+): in ")
_PYTEST_E = re.compile(r"^E\s+(?P<msg>.+)$")


def python_problems(output: str) -> list[Problem]:
    """Each traceback's deepest frame inside the project, with its exception."""
    lines = clean(output).splitlines()
    out: list[Problem] = []
    frame: Optional[tuple[str, int]] = None
    in_tb = False
    for line in lines:
        if line.startswith("Traceback (most recent call last)"):
            in_tb, frame = True, None
            continue
        m = _PY_FRAME.match(line)
        if m and in_tb:
            if _ours(m.group("path")):
                frame = (rel(m.group("path")), int(m.group("line")))
            continue
        if in_tb and not line.startswith((" ", "\t")) and line.strip():
            exc = _PY_EXC.match(line.strip())
            if exc and frame:
                out.append(Problem(frame[0], _short(exc.group("msg")), "runtime", frame[1]))
            in_tb = False
            frame = None
    # pytest's own format: `tests/test_x.py:3: in <module>` … `E   ImportError: …`
    at: Optional[tuple[str, int]] = None
    for line in lines:
        m = _PYTEST_AT.match(line)
        if m and _ours(m.group("path")):
            at = (rel(m.group("path")), int(m.group("line")))
            continue
        m = _PYTEST_E.match(line)
        if m and at and _PY_EXC.match(m.group("msg").strip()):
            out.append(Problem(at[0], _short(m.group("msg")), "runtime", at[1]))
            at = None
    return _dedupe(out)


def node_problems(output: str) -> list[Problem]:
    """A Node process that threw: the error, at its first frame in the project."""
    lines = clean(output).splitlines()
    out: list[Problem] = []
    for i, line in enumerate(lines):
        m = _JS_ERROR.match(line.strip())
        if not m:
            continue
        message = m.group("msg")
        where: Optional[tuple[str, int]] = None
        imported = re.search(r"imported from (/work/\S+)", message)
        if imported and _ours(imported.group(1)):
            where = (rel(imported.group(1)), 0)
        for nxt in lines[i + 1 : i + 30]:
            if where:
                break
            f = _JS_FRAME.search(nxt)
            if f and _ours(f.group("path")):
                where = (rel(f.group("path")), int(f.group("line")))
        if where is None:
            # Node prints the throwing line above the error: `/work/server.js:3`.
            for prev in reversed(lines[max(0, i - 6) : i]):
                f = re.match(r"^(?P<path>/work/[^\s:]+):(?P<line>\d+)$", prev.strip())
                if f and _ours(f.group("path")):
                    where = (rel(f.group("path")), int(f.group("line")))
                    break
        if where:
            out.append(Problem(where[0], _short(message), "runtime", where[1] or None))
    return _dedupe(out)


# ── the fallback, and the caps ───────────────────────────────────────────────
def tail_problem(output: str, manifest: str, what: str) -> Problem:
    lines = [l.rstrip() for l in clean(output).splitlines() if l.strip()]
    tail = "\n".join(lines[-_TAIL_LINES:]) or "(no output)"
    return Problem(manifest, f"{what} failed. Its last lines:\n{tail}", "build")


def _dedupe(problems: list[Problem]) -> list[Problem]:
    seen: set[tuple] = set()
    out = []
    for p in problems:
        key = (p.path, p.line, p.message)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def capped(problems: list[Problem]) -> list[Problem]:
    """Five per file, twenty-four in all — the compile gate's caps."""
    per: dict[str, int] = {}
    out: list[Problem] = []
    for p in _dedupe(problems):
        if per.get(p.path, 0) >= _PER_FILE:
            continue
        per[p.path] = per.get(p.path, 0) + 1
        out.append(p)
        if len(out) >= _TOTAL:
            break
    return out


def prefixed(problems: list[Problem], side: str) -> list[Problem]:
    """Paths relative to a side's folder, made tree paths: `app/page.tsx` → `frontend/app/page.tsx`."""
    if not side:
        return problems
    return [Problem(f"{side}/{p.path}" if not p.path.startswith(f"{side}/") else p.path, p.message, p.kind, p.line, p.step)
            for p in problems]


def vercel_problems(lines: list[str], files: Iterable[str], manifest: str = "package.json") -> list[Problem]:
    """A failed Vercel build's log, read like the sandbox's own `next build`."""
    text = "\n".join(lines)
    found = npm_problems(text, manifest) or js_build_problems(text, files)
    return capped(found) or ([tail_problem(text, manifest, "The build on Vercel")] if text.strip() else [])
