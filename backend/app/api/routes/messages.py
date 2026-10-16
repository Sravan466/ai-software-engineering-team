"""Talk to the crew while it builds (#102).

`POST /messages` takes a message in every state, and never waits on the build: a
question is answered from the build's own record at once (or queued for a model once
the step in hand is done), and a note is kept for the agents it is for. On a build that
stopped to ask for help, a note is Keep trying with the person's words as the next
round's note — the same claim and the same checks as the Try again button.

Nothing here claims a running build. Stop stays the only interrupt.
"""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_project
from app.api.routes.projects import _drive, _refuse_credentials, start_keep_trying
from app.core.constants import GateKind, PipelineStatus
from app.db.base import get_db
from app.db.models import CrewMessage, Project
from app.orchestration import autofix, crew_chat, remediation
from app.schemas.project import MessageCancel, MessageCreate, MessageReread

router = APIRouter(prefix="/api/projects", tags=["messages"])

#: Questions waiting on a model, per build, before another is turned away: each is a
#: model call, and on a local model each waits behind the build's own.
MAX_WAITING = 3

#: One process (the runs are threads in it, as `activity` assumes): counting the questions
#: waiting on a model and keeping a new one happen under this, together.
_counting = threading.Lock()

#: What Keep trying handed back: rounds given, the tracks it restarted, the claim token.
Kept = tuple[int, list[str], str]


def _stuck(project: Project) -> bool:
    return (
        project.status == PipelineStatus.AWAITING_APPROVAL.value
        and project.gate_kind == GateKind.NEEDS_HELP.value
    )


def _something_to_fix(db: Session, project: Project) -> bool:
    """Whether a fix round would read a note: a stuck code or test track, or a security
    one with a serious finding still open. With every finding waived the card says
    "Continue the build" — a note then waits like any other."""
    data = autofix.load(project)
    for name in autofix.stuck(data):
        if name != autofix.SECURITY or remediation.unresolved(db, project, serious=True):
            return True
    return False


def _keep_trying(db: Session, project: Project, kind: str, text: str, *, must: bool = False) -> Optional[Kept]:
    """A note on a stuck build sets it going again with the note. Claimed before the
    message is kept: a refused claim keeps nothing, and a claim that went through is
    committed with the fix loop's state at once, as `/auto-fix/retry` commits it — so a
    message that then fails to save never leaves a claimed build that nothing drives."""
    if not must and (kind != crew_chat.NOTE or not _stuck(project) or not _something_to_fix(db, project)):
        return None
    kept = start_keep_trying(db, project, note=text)
    db.commit()
    return kept


def _waiting_on_model(db: Session, project: Project, besides: str) -> int:
    """Questions already waiting on a model — not counting the one being asked."""
    return int(
        db.execute(
            select(func.count()).select_from(CrewMessage).where(
                CrewMessage.project_id == project.id,
                CrewMessage.author == crew_chat.PERSON,
                CrewMessage.kind == crew_chat.QUESTION,
                CrewMessage.status == crew_chat.QUEUED,
                CrewMessage.id != besides,
            )
        ).scalar()
        or 0
    )


def _question(db: Session, project: Project, asked: CrewMessage) -> bool:
    """Answer `asked` from the record, or queue it for a model. True when a model is to be asked."""
    if crew_chat.is_status(asked.text):
        text, agent = crew_chat.status_answer(project, asked.text)
        crew_chat.reply(db, asked, text, agent)
        asked.status = crew_chat.ANSWERED
        asked.agent = agent
        return False
    if _waiting_on_model(db, project, besides=asked.id) >= MAX_WAITING:
        crew_chat.reply(db, asked, "The crew is still on your last questions. Ask this one once they're answered.")
        asked.status = crew_chat.DECLINED
        asked.agent = None
        return False
    busy = project.status == PipelineStatus.RUNNING.value and not project.stalled
    agent = project.current_phase if busy else None
    crew_chat.reply(db, asked, crew_chat.WAIT_FOR_MODEL if busy else crew_chat.ASK_NOW, agent)
    asked.status = crew_chat.QUEUED
    asked.agent = agent
    return True


def _note(db: Session, project: Project, asked: CrewMessage, kept: Optional[Kept]) -> None:
    """Keep `asked` for the agents it is for, or say what Keep trying did with it."""
    if kept is not None:
        more, names, _token = kept
        phases = [n[len(autofix.BUILD_PREFIX):] for n in names if n.startswith(autofix.BUILD_PREFIX)]
        agent = phases[0] if phases else project.current_phase
        crew_chat.reply(db, asked, f"Trying again with your note, up to {more} more round{'' if more == 1 else 's'}.", agent)
        asked.status = crew_chat.APPLIED
        asked.agent = agent
        # For this fix round, which reads it (#103): not a standing note for every phase.
        asked.meant_for = []
        asked.applied_to = phases or None
        asked.applied_at = crew_chat.now()
        return
    if project.status == PipelineStatus.COMPLETED.value:
        crew_chat.reply(db, asked, "The build is finished. Say what should change, and the crew edits the app.")
        asked.status = crew_chat.DECLINED
        asked.agent = None
        return
    # Some work is done: the planner reads the note against it once the slot is free (PR 3).
    rows = crew_chat.rows_of(project)
    check = crew_chat.checks_finished_work(db, project, asked.text, rows)
    text, agent, meant_for = crew_chat.note_reply(project, asked.text, check, rows)
    crew_chat.reply(db, asked, text, agent)
    # No phase left to take it and nothing to check: said so, not kept waiting for nobody.
    asked.status = crew_chat.QUEUED if (meant_for or check) else crew_chat.DECLINED
    asked.meant_for = list(meant_for)
    if check:
        crew_chat.start_check(asked)
    else:
        asked.plan = None
    # Who it waits for, as it is read now — a reread's earlier reply named someone else.
    asked.agent = agent


def _settle(
    db: Session, project: Project, asked: CrewMessage, kept: Optional[Kept], background: BackgroundTasks
) -> dict:
    """Answer or keep `asked`, commit, then start whatever it set going."""
    scope = False
    try:
        if asked.kind == crew_chat.QUESTION:
            # Counted and kept under one lock, so two sends at once can't both be the third.
            with _counting:
                ask_model = _question(db, project, asked)
                db.commit()
        else:
            ask_model = False
            _note(db, project, asked, kept)
            db.commit()
            scope = crew_chat.change_state(asked) == crew_chat.CHECKING
        # Read back inside the guard too: whatever fails before the response is sent
        # leaves a claimed build driven all the same.
        out = {
            "message": _one(db, asked),
            "messages": crew_chat.thread(db, project.id, crew_chat.RECENT),
            # What a control's answer carries, so the page's badge moves on the click.
            "status": project.status,
            "current_phase": project.current_phase,
        }
    except Exception:
        db.rollback()
        # A build claimed for Keep trying is driven whether or not its message was kept —
        # and an error response runs no background task, so it starts here.
        if kept is not None:
            threading.Thread(target=_drive, args=(project.id, kept[2]), daemon=True, name="keep-trying").start()
        raise
    if kept is not None:
        background.add_task(_drive, project.id, kept[2])
    if ask_model:
        crew_chat.spawn(crew_chat.ask_model, project.id, asked.id)
    if scope:
        crew_chat.spawn(crew_chat.scope_note, project.id, asked.id)
    return out


@router.post("/{project_id}/messages", status_code=201)
def send_message(
    payload: MessageCreate,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Ask the crew something, or tell it what you want — in any state."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Say something to the crew.")
    # The person's words reach a model: never a key they pasted by mistake.
    _refuse_credentials(text, project)
    kind = crew_chat.NOTE if payload.keep_trying else crew_chat.classify(text)
    kept = _keep_trying(db, project, kind, text, must=payload.keep_trying)
    asked = CrewMessage(
        project_id=project.id,
        author=crew_chat.PERSON,
        text=text,
        kind=kind,
        status=crew_chat.QUEUED,
        created_at=crew_chat.now(),
    )
    db.add(asked)
    db.flush()  # its id, for the reply
    return _settle(db, project, asked, kept, background)


@router.get("/{project_id}/messages")
def list_messages(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> dict:
    return {"messages": crew_chat.thread(db, project.id)}


@router.post("/{project_id}/messages/{message_id}/reread")
def reread_message(
    message_id: str,
    payload: MessageReread,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """"That was a question" (or a note): read the message the other way, and answer it so."""
    with crew_chat.lock_for(project.id):
        return _reread(message_id, payload, background, project, db)


def _reread(message_id: str, payload: MessageReread, background: BackgroundTasks, project: Project, db: Session) -> dict:
    asked = db.get(CrewMessage, message_id)
    if asked is None or asked.project_id != project.id or asked.author != crew_chat.PERSON:
        raise HTTPException(404, "There's no such message on this build.")
    if crew_chat.change_state(asked) in (crew_chat.SCHEDULED_CHANGE, crew_chat.APPLIED_CHANGE):
        raise HTTPException(409, "The crew is already acting on this note.")
    if asked.kind == payload.kind:
        raise HTTPException(409, f"It's already read as a {payload.kind}.")
    newest = db.execute(
        select(CrewMessage.id)
        .where(CrewMessage.project_id == project.id, CrewMessage.author == crew_chat.PERSON)
        .order_by(CrewMessage.created_at.desc())
        .limit(1)
    ).scalar()
    if newest != asked.id:
        raise HTTPException(409, "Only your newest message can be read again. Send this one again instead.")
    if asked.kind == crew_chat.QUESTION and asked.status == crew_chat.QUEUED:
        raise HTTPException(409, "The crew is still answering this. Wait for its answer, then say if it was a note.")
    if asked.status not in (crew_chat.ANSWERED, crew_chat.QUEUED, crew_chat.DECLINED):
        raise HTTPException(409, "The crew has already acted on this note.")
    kept = _keep_trying(db, project, payload.kind, asked.text)
    asked.kind = payload.kind
    if asked.kind == crew_chat.QUESTION:
        asked.meant_for = None  # a question is for no phase
        asked.plan = None  # nor read against finished work
    return _settle(db, project, asked, kept, background)


@router.post("/{project_id}/messages/{message_id}/cancel")
def cancel_message(
    message_id: str,
    payload: Optional[MessageCancel] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Take a note back while it waits — or, with `change`, only the change it makes to
    finished work, until that starts (#102 PR 3): the note still reaches the phases to come."""
    only_change = bool(payload and payload.change)
    with crew_chat.lock_for(project.id):
        asked = db.get(CrewMessage, message_id)
        if asked is None or asked.project_id != project.id or asked.author != crew_chat.PERSON:
            raise HTTPException(404, "There's no such message on this build.")
        if asked.kind != crew_chat.NOTE:
            raise HTTPException(409, "Only a note can be taken back.")
        db.refresh(asked)
        state = crew_chat.change_state(asked)
        if state == crew_chat.APPLIED_CHANGE:
            raise HTTPException(409, "The change has already started.")
        if only_change:
            if state != crew_chat.SCHEDULED_CHANGE:
                raise HTTPException(409, "There's no change waiting to cancel.")
            crew_chat.resolve(asked, crew_chat.CANCELLED_CHANGE)
            crew_chat.reply(db, asked, "Cancelled the change. Nothing finished was changed.", asked.agent)
        else:
            if asked.status != crew_chat.QUEUED:
                raise HTTPException(
                    409, "An agent has already taken this note." if asked.status == crew_chat.APPLIED else f"It's already {asked.status}."
                )
            asked.status = crew_chat.CANCELLED
            if state in (crew_chat.SCHEDULED_CHANGE, crew_chat.CHECKING):
                asked.plan = {**(asked.plan or {}), "state": crew_chat.CANCELLED_CHANGE}
            crew_chat.reply(db, asked, "Taken back. No agent gets this note now, and nothing finished changes for it.", asked.agent)
        db.commit()
    return {
        "message": _one(db, asked),
        "messages": crew_chat.thread(db, project.id, crew_chat.RECENT),
        "status": project.status,
        "current_phase": project.current_phase,
    }


def _one(db: Session, asked: CrewMessage) -> dict:
    replies = db.execute(
        select(CrewMessage).where(CrewMessage.reply_to == asked.id).order_by(CrewMessage.created_at)
    ).scalars().all()
    return crew_chat.out(asked, replies, crew_chat.run_began(db, asked.project_id))
