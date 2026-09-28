"""A mockup's revisions as a history with a head: undo and redo move it, nothing is deleted.

Undo used to delete the newest revision. That made redo impossible, and at one
revision it deleted the build itself. Now each revision names the one it was made
from (`parent_id`), and the live one is whichever was most recently made head
(`head_at`). Undo makes the parent head; redo makes the newest child head. The first
revision has no parent, so undo can never reach past the original build.

Rows written before the pointer existed carry neither column. Their history was a
straight line, so a revision without a `parent_id` descends from the one before it,
and with no head recorded anywhere the newest revision is live, as it always was.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.models import PreviewRevision, _now


def revisions(db: Session, project_id: str) -> List[PreviewRevision]:
    """Every revision, newest first."""
    return (
        db.query(PreviewRevision)
        .filter(PreviewRevision.project_id == project_id)
        .order_by(PreviewRevision.created_at.desc())
        .all()
    )


def _stamp(row: PreviewRevision):
    value = row.head_at
    if value is None:
        return None
    # SQLite hands back naive datetimes; they are all UTC, so compare them as such.
    return value.replace(tzinfo=None)


def head(revs: List[PreviewRevision]) -> Optional[PreviewRevision]:
    if not revs:
        return None
    marked = [r for r in revs if r.head_at is not None]
    if not marked:
        return revs[0]
    return max(marked, key=_stamp)


def parent(row: PreviewRevision, revs: List[PreviewRevision]) -> Optional[PreviewRevision]:
    if row.parent_id:
        return next((r for r in revs if r.id == row.parent_id), None)
    older = [r for r in revs if _created(r) < _created(row)]
    return max(older, key=_created) if older else None


def _created(row: PreviewRevision):
    return row.created_at.replace(tzinfo=None)


def children(row: PreviewRevision, revs: List[PreviewRevision]) -> List[PreviewRevision]:
    return [r for r in revs if r.id != row.id and (p := parent(r, revs)) is not None and p.id == row.id]


def redo_target(row: PreviewRevision, revs: List[PreviewRevision]) -> Optional[PreviewRevision]:
    kids = children(row, revs)
    return max(kids, key=_created) if kids else None


def lineage(row: Optional[PreviewRevision], revs: List[PreviewRevision]) -> List[PreviewRevision]:
    """The head and every revision it descends from, newest first."""
    out: List[PreviewRevision] = []
    seen = set()
    while row is not None and row.id not in seen:
        out.append(row)
        seen.add(row.id)
        row = parent(row, revs)
    return out


def make_head(db: Session, row: PreviewRevision) -> None:
    row.head_at = _now()
    db.commit()


def new_row(db: Session, project_id: str, **fields) -> PreviewRevision:
    """A revision made from the current head, and made head itself."""
    current = head(revisions(db, project_id))
    row = PreviewRevision(
        project_id=project_id,
        parent_id=current.id if current else None,
        head_at=_now(),
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
