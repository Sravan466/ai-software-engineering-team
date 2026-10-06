"""Which driver a build belongs to (#41).

Stop marks a build cancelled without being able to take back a model call already in
flight, and Resume claims it straight back. A flag anyone can clear could not tell
the two drivers apart: the first one's call returned, read `cancel_requested=False`
— cleared by the resume — and carried on beside the second. Both completed the same
row (recording its usage twice) and both advanced the checkpoint.

So every claim mints a token (`projects.run_token`), and the driver it was handed to
holds it. Three things enforce it:

  • **Every database write.** A driver's session carries its claim in `Session.info`,
    and `before_flush` compare-and-sets the token in the same transaction as the
    write. A superseded driver's commit raises `Superseded` and writes nothing. The
    check and the write are one transaction, so this holds across processes too — on
    SQLite the check's UPDATE takes the write lock until the commit, and a claim from
    anywhere waits behind it.
  • **The checkpoint.** The graph node checks the token once its agent returns, and
    raises before LangGraph writes the step; `redo` checks before it patches. That
    one is a read, then a write to a different file, so a claim landing in between
    still lets one step through — which the next point catches.
  • **Between phases.** The loop checks where it checks for Stop, so a superseded
    driver does not start another model call.

A driver that finds itself superseded stops without a word: the build is someone
else's now, and anything it wrote — a status, an error — would be about a run that
no longer exists.
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import event, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.models import Project

#: The key a driver's session carries its claim under: `(project_id, token)`.
_INFO_KEY = "run_claim"

#: The same claim, for code that has no session to hand — the graph node, the
#: heartbeat. LangGraph runs a node in a copy of the caller's context.
_current: contextvars.ContextVar[Optional[tuple[str, Optional[str]]]] = contextvars.ContextVar(
    "run_claim", default=None
)


class Superseded(Exception):
    """This driver's claim on the build was taken by a newer one, or the build is gone."""


def new_token() -> str:
    return uuid.uuid4().hex


def current() -> Optional[tuple[str, Optional[str]]]:
    """`(project_id, token)` held by the driver running in this context, if any."""
    return _current.get()


def held_by(db: Session) -> Optional[tuple[str, Optional[str]]]:
    return db.info.get(_INFO_KEY)


_projects = Project.__table__


def token_is(token: Optional[str]):
    col = _projects.c.run_token
    return col.is_(None) if token is None else col == token


def holds(conn: Connection, project_id: str, token: Optional[str]) -> bool:
    """Compare-and-set: True while `token` is still this build's claim.

    An UPDATE rather than a SELECT so that, inside a transaction, it takes the write
    lock: nothing can claim the build between this and the commit it guards.
    """
    result = conn.execute(
        update(_projects)
        .where(_projects.c.id == project_id, token_is(token))
        .values(run_token=_projects.c.run_token)
    )
    return result.rowcount == 1


@contextmanager
def holding(db: Session, project: Project, token: Optional[str]) -> Iterator[bool]:
    """Hold `token` for `project` on `db` for the block. Yields True when this call
    took the claim, False when an enclosing call already holds it."""
    if held_by(db) is not None:
        yield False
        return
    claim = (project.id, token)
    db.info[_INFO_KEY] = claim
    ctx = _current.set(claim)
    try:
        yield True
    finally:
        _current.reset(ctx)
        db.info.pop(_INFO_KEY, None)


def check() -> None:
    """Raise `Superseded` if the claim held in this context is no longer current.

    For code without the driver's session — the graph node. Reads on a session of its
    own, so it sees a claim committed a moment ago by another request.
    """
    claim = current()
    if claim is None:
        return
    from app.db.base import SessionLocal

    project_id, token = claim
    with SessionLocal() as db:
        found = db.execute(select(Project.run_token).where(Project.id == project_id)).first()
    if found is None or found[0] != token:
        raise Superseded(project_id)


def between_calls() -> None:
    """For a phase that makes many model calls (#81): stop before the next one when the
    build is no longer this driver's, or the person pressed Stop.

    Stop cancels the call in flight, but a Stop landing *between* two calls has nothing
    to cancel — without this the next file's call would start anyway. Raises
    `Superseded` for a lost claim, and `RequestCancelled` for a Stop (or a deleted
    build), which the run settles exactly as a cancelled call.
    """
    check()
    from app.router import inflight
    from app.router.base import RequestCancelled

    held = current()
    build = inflight.current()
    project_id = held[0] if held is not None else (build or {}).get("id")
    if not project_id:
        return
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        found = db.execute(select(Project.cancel_requested).where(Project.id == project_id)).first()
    if found is None or found[0]:
        raise RequestCancelled()


@event.listens_for(Session, "before_flush")
def _guard_writes(session: Session, _flush_context, _instances) -> None:
    claim = session.info.get(_INFO_KEY)
    if claim is None or not (session.new or session.dirty or session.deleted):
        return
    project_id, token = claim
    if not holds(session.connection(), project_id, token):
        raise Superseded(project_id)
