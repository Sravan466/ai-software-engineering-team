"""What a phase is doing right now, between "started" and "finished" (#81).

A code phase is a plan call and then a call per file or batch — on a local model,
minutes of work that used to show as one spinner. The phase reports each step here
and `ProjectOut.activity` reads it back: "writing frontend/app/page.tsx (3 of 9)",
and the file list filling in as files land.

The steps it has finished are kept too (#86), so the build page can show them as a
feed — "Planned 9 files", "Ran npm install" — rather than only the one in hand. A
command that ran between two polls, or before a reload, is still there to read.
When the phase stops reporting, its last snapshot is kept, marked `ended`, until
the next phase begins: the runner is still saving the row for a moment after the
agent returns, and a feed that lost every step in that gap would jump backwards.

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
    "testing": "Running the tests",
}

#: A file's state in the list: planned, being written, written and parses, being
#: fixed, written and still broken, or never written.
FILE_STATES = ("planned", "writing", "ok", "fixing", "failed", "missing")

#: Stages whose `detail` is a command: each command is a step of its own.
COMMAND_STAGES = ("building", "testing")
#: Writing and fixing are one step, file to file — the file list carries the rest.
FILE_STAGES = ("writing", "fixing")
#: Finished steps kept per phase. The oldest fall off the front.
TRAIL_MAX = 24
#: The planner's summary, as the page shows it under "Planned 9 files".
NOTE_MAX = 280


def _step(stage: str, detail: str) -> tuple[str, str]:
    """What tells one step from the next: its stage, and which command it runs."""
    return ("files" if stage in FILE_STAGES else stage, detail if stage in COMMAND_STAGES else "")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    return (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(",;:") + "…"


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
    #: The stage that opened the step in hand; empty until the phase names one, so
    #: the default `planning` of a phase that never plans is not mistaken for a step.
    opened: str = ""
    trail: list[dict] = field(default_factory=list)
    #: Steps that fell off the front of the trail, so the page can number lines stably.
    dropped: int = 0
    note: str = ""
    #: The phase stopped reporting; this is its last word, kept until the row settles.
    ended: bool = False

    def file_step(self) -> None:
        """Put the step in hand into the trail, keeping only the latest `TRAIL_MAX`."""
        self.trail.append(self.finished())
        over = len(self.trail) - TRAIL_MAX
        if over > 0:
            del self.trail[:over]
            self.dropped += over

    def finished(self) -> dict:
        """The step in hand, as the trail keeps it once the next one starts."""
        return {
            "stage": self.opened,
            "detail": self.detail if self.opened in COMMAND_STAGES else "",
            "done": self.done,
            "total": self.total,
        }

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
            "trail": list(self.trail),
            "dropped": self.dropped,
            "note": self.note,
            "ended": self.ended,
        }


_lock = threading.Lock()
_board: dict[str, _Activity] = {}
#: What each project's last phase said as it stopped reporting (see the module doc).
_ended: dict[str, _Activity] = {}


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
        _ended.pop(pid, None)


def stage(name: str, *, total: Optional[int] = None, detail: str = "", done: Optional[int] = None) -> None:
    """The phase moved on. A new step files the one before it in the trail."""
    pid = _project()
    if pid is None:
        return
    with _lock:
        found = _board.get(pid)
        if found is None:
            return
        moved = _step(name, detail) != _step(found.stage, found.detail)
        if found.opened and moved:
            found.file_step()
        if moved or not found.opened:
            found.opened = name
        found.stage, found.detail = name, detail
        if total is not None:
            found.total = total
        if done is not None:
            found.done = done


def plan(paths: Iterable[str], per_call: int, note: str = "") -> None:
    """The plan landed: every file it lists, all still to write, and what it's for."""
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
        found.note = _clip(note, NOTE_MAX) if isinstance(note, str) else ""


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
    """The phase stopped reporting: its step in hand is done, and the snapshot is kept."""
    pid = _project()
    if pid is None:
        return
    with _lock:
        found = _board.pop(pid, None)
        if found is None:
            return
        if found.opened:
            found.file_step()
            found.opened = ""
        found.ended = True
        _ended[pid] = found


def clear(project_id: str) -> None:
    """Forget the project's progress, live and ended. A new phase starts clean."""
    with _lock:
        _board.pop(project_id, None)
        _ended.pop(project_id, None)


def get(project_id: str) -> Optional[dict]:
    """The phase reporting now, or None."""
    with _lock:
        found = _board.get(project_id)
        return found.as_dict() if found is not None else None


def latest(project_id: str) -> Optional[dict]:
    """The phase reporting now, or else the last word of the one that just stopped."""
    with _lock:
        found = _board.get(project_id) or _ended.get(project_id)
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
