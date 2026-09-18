"""Where each agent's file goes in the build.

A real run wrote the backend's `./main.py`, the frontend's `./pages/_app.js` and QA's
`./tests/main_test.py` all at the root, while DevOps wrote Dockerfiles for `backend/`
and `frontend/` directories that did not exist. Nothing in that archive could be
installed, because nothing agreed where anything was.

The layout is fixed: backend code under `backend/`, frontend code under `frontend/`,
infrastructure at the root where DevOps already put it. Agents are told this, and a
file that ignores it is moved rather than rejected — moving every file of one phase by
the same prefix keeps every relative import between them intact.

That is why an agent's own top folder (`client/`, `server/`) is only renamed to the
canonical one when *every* file the phase wrote sits under it. `ui/Button.jsx` beside
`pages/index.jsx` is a components folder, not a name for the whole frontend, and
renaming it alone would break `import Button from '../ui/Button'`.
"""
from __future__ import annotations

import posixpath
import re
from typing import Iterable, Optional

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


def common_root(phase: str, paths: Iterable[str]) -> Optional[str]:
    """The agent's own name for its whole side, when every file of the phase uses it."""
    aliases = {
        Phase.BACKEND_ENGINEER.value: _BACKEND_ROOTS - {BACKEND},
        Phase.FRONTEND_ENGINEER.value: _FRONTEND_ROOTS - {FRONTEND},
    }.get(phase)
    if not aliases:
        return None
    heads = set()
    for path in paths:
        head, _, rest = clean(path).partition("/")
        if not rest:
            return None  # a file at the top of the phase: there is no common folder
        heads.add(head.lower())
    if len(heads) == 1:
        head = heads.pop()
        return head if head in aliases else None
    return None


def _rooted(path: str, root: str, other_root: str, strip: Optional[str]) -> str:
    head, _, rest = path.partition("/")
    if rest and head.lower() == root:
        return path
    if rest and head.lower() == other_root:
        # An agent writing into the other side's tree on purpose — a backend that
        # ships a static page, say. Kept there.
        return path
    if rest and strip and head.lower() == strip:
        return f"{root}/{rest}"
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
    strip: Optional[str] = None,
) -> str:
    """Where `path`, as the agent for `phase` wrote it, lives in the build.

    `strip` is the phase's `common_root`: the agent's own name for its whole side,
    renamed to the canonical one. Placing a whole phase goes through `place_all`,
    which works it out.
    """
    p = clean(path)
    if not p:
        return p
    if phase == Phase.BACKEND_ENGINEER.value:
        return _rooted(p, BACKEND, FRONTEND, strip)
    if phase == Phase.FRONTEND_ENGINEER.value:
        return _rooted(p, FRONTEND, BACKEND, strip)
    if phase == Phase.QA_ENGINEER.value:
        head = p.partition("/")[0].lower()
        if head in (FRONTEND, BACKEND) and "/" in p:
            return p
        side = side_of_test(p, content, backend_language)
        return f"{side}/{p}"
    # DevOps and anything else: infrastructure, where it said.
    return p


def place_all(
    phase: str, items: Iterable[tuple], backend_language: Optional[str] = None
) -> list[tuple]:
    """`[(placed, path, content, …rest)]` for every `(path, content, …rest)` of one phase."""
    items = list(items)
    strip = common_root(phase, [item[0] for item in items])
    return [
        (place(phase, item[0], item[1], backend_language, strip), *item) for item in items
    ]


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
