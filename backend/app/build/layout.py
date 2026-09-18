"""Where each agent's file goes in the build.

A real run wrote the backend's `./main.py`, the frontend's `./pages/_app.js` and QA's
`./tests/main_test.py` all at the root, while DevOps wrote Dockerfiles for `backend/`
and `frontend/` directories that did not exist. Nothing in that archive could be
installed, because nothing agreed where anything was.

The layout is fixed: backend code under `backend/`, frontend code under `frontend/`,
infrastructure at the root where DevOps already put it. Agents are told this, and a
file that ignores it is moved rather than rejected — moving every file of one phase by
the same prefix keeps every relative import between them intact.

How an agent's own name for its side (`client/`, `server/`) is renamed to the
canonical one depends on how that language imports — see `renamed_roots`.
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
_CODE = _JS + (".py", ".vue", ".svelte")
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


def renamed_roots(phase: str, paths: Iterable[str]) -> frozenset[str]:
    """The agent's own names for its side that this phase's files are moved out of.

    Two languages, two rules, because they import differently:

      **Python** imports by package from the side's root, so a `server/` folder *is*
      the backend root wherever it appears — `from app.main import app` in a test
      only resolves once `server/app/` is `backend/app/`. Every file under a side
      alias is moved out of it.

      **JavaScript** imports by relative path, so files only keep their imports if
      they move together. An alias folder is renamed only when every code file of
      the phase sits under that one folder — a top-level `main.jsx` importing
      `./ui/Button` means `ui/` is a components folder, not a name for the frontend.
      Non-code files (a README, `docs/`, `public/`) and files written into a
      canonical tree do not import anything by path and do not vote.
    """
    aliases = {
        Phase.BACKEND_ENGINEER.value: _BACKEND_ROOTS - {BACKEND},
        Phase.FRONTEND_ENGINEER.value: _FRONTEND_ROOTS - {FRONTEND},
    }.get(phase)
    if not aliases:
        return frozenset()
    code = []
    for path in paths:
        p = clean(path)
        if p.lower().endswith(_CODE):
            code.append(p)
    python = sum(1 for p in code if p.lower().endswith(".py"))
    if python and python * 2 >= len(code):
        return frozenset(
            p.partition("/")[0].lower()
            for p in code
            if "/" in p and p.partition("/")[0].lower() in aliases
        )
    heads = set()
    for p in code:
        head, _, rest = p.partition("/")
        if not rest:
            return frozenset()  # top-level code: nothing sits under one folder
        if head.lower() in (FRONTEND, BACKEND):
            continue
        heads.add(head.lower())
    if len(heads) == 1 and next(iter(heads)) in aliases:
        return frozenset(heads)
    return frozenset()


def _rooted(path: str, root: str, other_root: str, renamed: frozenset) -> str:
    head, _, rest = path.partition("/")
    if rest and head.lower() == root:
        return f"{root}/{rest}"  # `Frontend/` is still the frontend
    if rest and head.lower() == other_root:
        # An agent writing into the other side's tree on purpose — a backend that
        # ships a static page, say. Kept there.
        return f"{other_root}/{rest}"
    if rest and head.lower() in renamed:
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
    strip: object = None,
) -> str:
    """Where `path`, as the agent for `phase` wrote it, lives in the build.

    `strip` is what the phase's side was renamed from (`renamed_roots`). For QA it is
    `{side: names}` for the Backend and Frontend phases, because a test under
    `client/` sits beside code that was moved out of `client/` and has to follow it.
    Placing whole phases goes through `Placer`, which works all of this out.
    """
    p = clean(path)
    if not p:
        return p
    renamed = strip if isinstance(strip, frozenset) else frozenset()
    if phase == Phase.BACKEND_ENGINEER.value:
        return _rooted(p, BACKEND, FRONTEND, renamed)
    if phase == Phase.FRONTEND_ENGINEER.value:
        return _rooted(p, FRONTEND, BACKEND, renamed)
    if phase == Phase.QA_ENGINEER.value:
        head, _, rest = p.partition("/")
        lowered = head.lower()
        if rest and lowered in (FRONTEND, BACKEND):
            return f"{lowered}/{rest}"
        by_side = strip if isinstance(strip, dict) else {}
        for side, names in by_side.items():
            if rest and lowered in names:
                return f"{side}/{rest}"
        side = side_of_test(p, content, backend_language)
        return f"{side}/{p}"
    # DevOps and anything else: infrastructure, where it said.
    return p


class Placer:
    """Places a build phase by phase, remembering what each code side was renamed from.

    One per build, fed the phases in the order they ran — QA's tests follow the
    folders the Backend and Frontend phases were actually placed in.
    """

    def __init__(self, backend_language: Optional[str] = None) -> None:
        self.backend_language = backend_language
        self.renamed: dict[str, frozenset] = {}

    def place_all(self, phase: str, items: Iterable[tuple]) -> list[tuple]:
        """`[(placed, path, content, …rest)]` for every `(path, content, …rest)`."""
        items = list(items)
        if phase == Phase.QA_ENGINEER.value:
            strip: object = {side: names for side, names in self.renamed.items() if names}
        else:
            strip = renamed_roots(phase, [item[0] for item in items])
            if phase == Phase.BACKEND_ENGINEER.value:
                self.renamed[BACKEND] = strip
            elif phase == Phase.FRONTEND_ENGINEER.value:
                self.renamed[FRONTEND] = strip
        return [
            (place(phase, item[0], item[1], self.backend_language, strip), *item)
            for item in items
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
