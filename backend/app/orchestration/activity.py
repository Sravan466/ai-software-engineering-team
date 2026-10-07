"""What a phase is doing right now, between "started" and "finished" (#81).

A code phase is a plan call and then a call per file or batch — on a local model,
minutes of work that used to show as one spinner. The phase reports each step here
and `ProjectOut.activity` reads it back: "writing frontend/app/page.tsx (3 of 9)",
and the file list filling in as files land.

Process-local, like the mockup's progress (`preview/jobs.py`): the run is a thread in
this process, so is every request that reads it, and a restart that loses the run
loses its progress with it. Everything here is a no-op outside a build.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.router import inflight

#: What each step is called on the page.
STAGES = {
    "planning": "Planning the files",
    "writing": "Writing",
    "fixing": "Fixing",
    "checking": "Checking the build",
    "building": "Building and starting it",
}

#: A file's state in the list: planned, being written, written and parses, being
#: fixed, written and still broken, or never written.
FILE_STATES = ("planned", "writing", "ok", "fixing", "failed", "missing")


@dataclass
class _Activity:
    phase: str
    stage: str = "planning"
    done: int = 0
    total: int = 0
    detail: str = ""
    files: dict[str, str] = field(default_factory=dict)
    per_call: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "stage": self.stage,
            "label": STAGES.get(self.stage, self.stage),
            "done": self.done,
            "total": self.total,
            "detail": self.detail,
            "per_call": self.per_call,
            "files": [{"path": p, "state": s} for p, s in self.files.items()],
            "elapsed_s": int(time.time() - self.started_at),
        }


_lock = threading.Lock()
_board: dict[str, _Activity] = {}


def _project() -> Optional[str]:
    build = inflight.current()
    return build["id"] if build else None


def begin(phase: str) -> None:
    """A phase in this build has started reporting. Replaces whatever was there."""
    pid = _project()
    if pid is None:
        return
    with _lock:
        _board[pid] = _Activity(phase=phase)


def stage(name: str, *, total: Optional[int] = None, detail: str = "", done: Optional[int] = None) -> None:
    _update(stage=name, total=total, detail=detail, done=done)


def plan(paths: Iterable[str], per_call: int) -> None:
    """The plan landed: every file it lists, all still to write."""
    pid = _project()
    if pid is None:
        return
    with _lock:
        found = _board.get(pid)
        if found is None:
            return
        found.files = {p: "planned" for p in paths}
        found.total = len(found.files)
        found.per_call = per_call


def file(path: str, state: str) -> None:
    """One file changed state; a file the plan never listed is added at the end."""
    pid = _project()
    if pid is None:
        return
    with _lock:
        found = _board.get(pid)
        if found is None:
            return
        found.files[path] = state
        found.total = max(found.total, len(found.files))
        found.done = sum(1 for s in found.files.values() if s in ("ok", "failed"))


def per_call(n: int) -> None:
    _update(per_call=n)


def end() -> None:
    pid = _project()
    if pid is not None:
        clear(pid)


def clear(project_id: str) -> None:
    with _lock:
        _board.pop(project_id, None)


def get(project_id: str) -> Optional[dict]:
    with _lock:
        found = _board.get(project_id)
        return found.as_dict() if found is not None else None


def _update(**changes) -> None:
    pid = _project()
    if pid is None:
        return
    with _lock:
        found = _board.get(pid)
        if found is None:
            return
        for key, value in changes.items():
            if value is not None:
                setattr(found, key, value)
