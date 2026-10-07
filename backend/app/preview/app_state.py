"""What a project remembers about its app preview (#78), on `projects.preview_app`.

The running app itself lives only as long as the backend process does — a sandbox
and the pipes to it. What must outlive that is small, and kept here:

  * `failed` — the last preview build that didn't build, for which Frontend attempt
    and why, so a restart doesn't rebuild something known to fail and the Preview tab
    can say why it shows the sketch;
  * `edit` — the change made on the preview that the crew is applying now, or the last
    one, and whether it landed or was refused (and why);
  * `edits` — undo and redo over those changes. Each change is a Frontend attempt, and
    attempts are never deleted, so undo is "make the attempt before it current again"
    — as a new attempt with its code. `head` is the attempt the stacks were made
    against: once anything else rewrites the frontend (a fix round, a redo from the
    Ship review), the stacks describe a history that is no longer this one's, and
    undo is off until the next change made here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm.attributes import flag_modified

from app.db.models import Project

#: Changes kept for undo. Each is one Frontend attempt, which is already kept anyway.
MAX_UNDO = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(project: Project) -> dict:
    data = project.preview_app if isinstance(project.preview_app, dict) else {}
    return deepcopy(data)


def save(project: Project, data: dict) -> None:
    project.preview_app = data or None
    flag_modified(project, "preview_app")


# ── the last build that failed ───────────────────────────────────────────────
def failed(project: Project) -> Optional[dict]:
    record = load(project).get("failed")
    return record if isinstance(record, dict) else None


def record_failure(project: Project, row_id: str, reason: str, problems: list[dict]) -> None:
    data = load(project)
    data["failed"] = {"row": row_id, "reason": reason, "problems": problems[:8], "at": _now()}
    save(project, data)


def clear_failure(project: Project) -> None:
    data = load(project)
    if data.pop("failed", None) is not None:
        save(project, data)


# ── a change in flight ───────────────────────────────────────────────────────
def begin_edit(project: Project, kind: str, label: str, files: list[str]) -> None:
    data = load(project)
    data["edit"] = {"kind": kind, "label": label, "files": files[:6], "status": "running", "at": _now()}
    save(project, data)


def edit(project: Project) -> Optional[dict]:
    record = load(project).get("edit")
    return record if isinstance(record, dict) else None


def refuse_edit(project: Project, reason: str) -> None:
    data = load(project)
    record = data.get("edit") or {}
    record.update(status="refused", reason=reason, at=_now())
    data["edit"] = record
    save(project, data)


def landed(project: Project, kind: str, before: Optional[str], after: str, theme: Optional[dict] = None) -> None:
    """A change made here became Frontend attempt `after`, replacing `before`.

    `theme` is that attempt's site style, remembered on the project too: a frontend
    the crew rewrites later (a fix round, a redo from the Ship review) takes it over."""
    data = load(project)
    if theme:
        data["theme"] = theme
    else:
        data.pop("theme", None)
    stacks = data.get("edits") if isinstance(data.get("edits"), dict) else {}
    undo = list(stacks.get("undo") or []) if stacks.get("head") == before else []
    redo = list(stacks.get("redo") or []) if stacks.get("head") == before else []
    if kind == "undo" and undo:
        undo.pop()
        if before:
            redo.append(before)
    elif kind == "redo" and redo:
        redo.pop()
        if before:
            undo.append(before)
    else:
        if before:
            undo.append(before)
        redo = []
    data["edits"] = {"head": after, "undo": undo[-MAX_UNDO:], "redo": redo[-MAX_UNDO:]}
    record = data.get("edit") or {}
    record.update(status="landed", row=after, at=_now())
    data["edit"] = record
    save(project, data)


def targets(project: Project, current: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(the attempt undo would bring back, the one redo would) — None when there is
    none, or when the frontend was rewritten since by something other than this."""
    stacks = load(project).get("edits")
    if not isinstance(stacks, dict) or not current or stacks.get("head") != current:
        return None, None
    undo = stacks.get("undo") or []
    redo = stacks.get("redo") or []
    return (undo[-1] if undo else None), (redo[-1] if redo else None)


def theme(project: Project) -> Optional[dict]:
    """The site style last chosen on the app preview, if any."""
    found = load(project).get("theme")
    return found if isinstance(found, dict) else None
