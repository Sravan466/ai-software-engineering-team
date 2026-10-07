"""The app preview's map back to the code (#78): which file and line drew an element,
and how a change made on the preview becomes a change to that file.

The preview build tags every element the frontend renders with
`data-src="frontend/components/Navbar.tsx:12:5"` — where its opening tag starts in the
file as the crew wrote it. The tags exist only in the preview build; the archive,
the deploy and the repository get the code untouched.

The reading is TypeScript's parser, run with node the way the compile gate runs it
(`jsx_src.cjs`); nothing is evaluated. When node or the parser isn't there, elements
simply can't be mapped, and every change on the app preview says so.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.build import toolchain
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_TOOL = Path(__file__).with_name("jsx_src.cjs")

#: `frontend/components/Navbar.tsx:12:5`
_ADDRESS = re.compile(r"^(?P<path>[^\s:\"'<>]+\.[cm]?[jt]sx?):(?P<line>\d+):(?P<col>\d+)$")


class Unavailable(Exception):
    """Node or its parser isn't here, so the code can't be read."""


@dataclass(frozen=True)
class Address:
    path: str
    line: int
    col: int

    @property
    def text(self) -> str:
        return f"{self.path}:{self.line}:{self.col}"


def parse(value: Optional[str]) -> Optional[Address]:
    """`path:line:col`, or None when it isn't one."""
    m = _ADDRESS.match((value or "").strip())
    if not m:
        return None
    path = m.group("path")
    if ".." in path.split("/") or path.startswith("/"):
        return None
    return Address(path, int(m.group("line")), int(m.group("col")))


def available() -> tuple[bool, Optional[str]]:
    if toolchain.node() is None:
        return False, "Node.js isn't installed on the computer running the backend."
    if toolchain.typescript() is None:
        return False, toolchain.unavailable_reason() or "The JavaScript parser isn't installed."
    return True, None


def _run(request: dict) -> dict:
    ts_path = toolchain.typescript()
    node = toolchain.node()
    if not ts_path or not node:
        raise Unavailable(toolchain.unavailable_reason() or "No JavaScript parser is available.")
    try:
        result = subprocess.run(
            [node, str(_TOOL)],
            input=json.dumps({**request, "typescript": ts_path}),
            capture_output=True,
            text=True,
            timeout=max(settings.build_check_timeout_seconds, 10),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise Unavailable(f"Reading the code failed: {e}") from e
    if result.returncode != 0:
        log.warning("jsx_src crashed: %s", result.stderr[-400:])
        raise Unavailable("Reading the code failed.")
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        raise Unavailable("Reading the code returned something unreadable.") from e
    if data.get("error"):
        raise Unavailable(str(data["error"]))
    return data


def tag(files: dict[str, str]) -> tuple[dict[str, str], int]:
    """Every renderable element tagged with its address. (files, how many tagged)."""
    marked = {p: c for p, c in files.items() if re.search(r"\.(tsx|jsx|js|mjs)$", p)}
    if not marked:
        return dict(files), 0
    data = _run({"op": "tag", "files": marked})
    out = dict(files)
    out.update({p: c for p, c in (data.get("files") or {}).items() if isinstance(c, str)})
    return out, int(data.get("tagged") or 0)


def locate(address: Address, content: str) -> dict:
    """What is at `address`: its tag, its lines, and whether its words and classes
    are written plainly enough to change here."""
    return _run({"op": "locate", "path": address.path, "content": content, "line": address.line, "col": address.col})


def edit(files: dict[str, str], ops: list[dict]) -> tuple[dict[str, str], list[dict]]:
    """Apply `ops` (each with `path`, `line`, `col`, `kind`, …) to `files`.

    Returns (every file, changed or not; what was refused: [{index, reason}])."""
    data = _run({"op": "edit", "files": files, "ops": ops})
    return dict(data.get("files") or files), list(data.get("refused") or [])
