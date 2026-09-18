"""Where each agent's file goes in the build.

A real run wrote the backend's `./main.py`, the frontend's `./pages/_app.js` and QA's
`./tests/main_test.py` all at the root, while DevOps wrote Dockerfiles for `backend/`
and `frontend/` directories that did not exist. Nothing in that archive could be
installed, because nothing agreed where anything was.

The layout is fixed: backend code under `backend/`, frontend code under `frontend/`,
infrastructure at the root where DevOps already put it. Agents are told this, and a
file that ignores it is moved rather than rejected — moving every file of one side by
the same prefix keeps every relative import between them intact.
"""
from __future__ import annotations

import posixpath
import re
from typing import Optional

from app.core.constants import Phase

FRONTEND = "frontend"
BACKEND = "backend"

#: Top-level directories an agent might use for a side, and the canonical name.
_FRONTEND_ROOTS = frozenset({"frontend", "client", "web", "webapp", "ui", "front-end", "front_end"})
_BACKEND_ROOTS = frozenset({"backend", "server", "back-end", "back_end", "api-server"})

_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
#: What makes a JavaScript test a *front-end* test: it renders something.
_FRONTEND_TEST = re.compile(
    r"""@testing-library/(react|dom|user-event)|from\s+['"]react['"]|next/|\.\./components|"""
    r"""render\(|<[A-Z]\w*[\s/>]""",
)


def clean(path: str) -> str:
    """A relative POSIX path with no `./`, no leading slash and no `..` escapes."""
    p = (path or "").strip().replace("\\", "/")
    parts = [s for s in p.split("/") if s not in ("", ".")]
    out: list[str] = []
    for part in parts:
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


def _rooted(path: str, root: str, aliases: frozenset[str], other_root: str, other: frozenset[str]) -> str:
    head, _, rest = path.partition("/")
    if rest and head.lower() in aliases:
        return f"{root}/{rest}"
    if rest and head.lower() in other:
        # An agent writing into the other side's tree on purpose — a backend that
        # ships a static page, say. Kept there, under the canonical name.
        return f"{other_root}/{rest}"
    return f"{root}/{path}"


def side_of_test(path: str, content: str, backend_language: Optional[str]) -> str:
    """Which side a QA file belongs to: Python tests are backend tests; JS ones render."""
    lower = path.lower()
    if lower.endswith(".py"):
        return BACKEND
    if lower.endswith(_JS):
        if lower.endswith((".jsx", ".tsx")) or _FRONTEND_TEST.search(content or ""):
            return FRONTEND
        if backend_language in ("javascript", "typescript"):
            return BACKEND
        return FRONTEND
    return BACKEND if backend_language == "python" else FRONTEND


def place(
    phase: str,
    path: str,
    content: str = "",
    backend_language: Optional[str] = None,
) -> str:
    """Where `path`, as the agent for `phase` wrote it, lives in the build."""
    p = clean(path)
    if not p:
        return p
    if phase == Phase.BACKEND_ENGINEER.value:
        return _rooted(p, BACKEND, _BACKEND_ROOTS, FRONTEND, _FRONTEND_ROOTS)
    if phase == Phase.FRONTEND_ENGINEER.value:
        return _rooted(p, FRONTEND, _FRONTEND_ROOTS, BACKEND, _BACKEND_ROOTS)
    if phase == Phase.QA_ENGINEER.value:
        head = p.partition("/")[0].lower()
        if head in _FRONTEND_ROOTS:
            return _rooted(p, FRONTEND, _FRONTEND_ROOTS, BACKEND, _BACKEND_ROOTS)
        if head in _BACKEND_ROOTS:
            return _rooted(p, BACKEND, _BACKEND_ROOTS, FRONTEND, _FRONTEND_ROOTS)
        side = side_of_test(p, content, backend_language)
        return f"{side}/{p}"
    # DevOps and anything else: infrastructure, where it said.
    return p


def side_of(path: str) -> Optional[str]:
    head = clean(path).partition("/")[0]
    return head if head in (FRONTEND, BACKEND) else None


def relative_to_side(path: str) -> str:
    """`frontend/app/page.tsx` -> `app/page.tsx`."""
    p = clean(path)
    side = side_of(p)
    return p[len(side) + 1:] if side else p


def join(*parts: str) -> str:
    return clean(posixpath.join(*parts))
