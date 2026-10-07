"""Keep talking after a build (#79): change requests, versions, restore.

`POST /changes` on a finished build starts a change — scoped, made by the agents that
own the code on top of their files, checked and reviewed like a build — and the build is
`running` until it waits for its review. Progress is the same poll as any run.

Every control here claims the build the way `projects.py` does, so a second tab's click
is a clean 409 and a run that was stopped mid-call can't write over what follows.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project
from app.api.routes.projects import (
    _claim,
    _conflict,
    _refuse_credentials,
    _require_models,
    _strand,
)
from app.core.constants import PipelineStatus
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import ChangeRequest, Project, User
from app.orchestration import changes, versions
from app.orchestration.runner import runner
from app.schemas.project import ChangeCreate, DiscardRequest

log = get_logger(__name__)

router = APIRouter(prefix="/api/projects", tags=["changes"])

#: A change can be thrown away from wherever it stopped — but not while it runs.
_DISCARDABLE = {
    PipelineStatus.AWAITING_APPROVAL.value,
    PipelineStatus.FAILED.value,
    PipelineStatus.CANCELLED.value,
    PipelineStatus.PAUSED.value,
}


def _change_of(db: Session, project: Project, change_id: str) -> ChangeRequest:
    found = db.get(ChangeRequest, change_id)
    if found is None or found.project_id != project.id:
        raise HTTPException(404, "There's no such change on this build.")
    return found


def _drive_change(project_id: str, change_id: str, token: str) -> None:
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.change(db, project, change_id, claim_token=token)
    except Exception as e:  # noqa: BLE001 - a crash must not strand the build
        log.exception("Change task crashed for %s", project_id)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


def _drive_discard(project_id: str, change_id: str, reason: str, token: str) -> None:
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        found = db.get(ChangeRequest, change_id)
        if project is not None and found is not None:
            runner.discard_change(db, project, found, reason, claim_token=token)
    except Exception as e:  # noqa: BLE001
        log.exception("Discard task crashed for %s", project_id)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


def _drive_restore(project_id: str, number: int, token: str) -> None:
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        version = versions.by_number(db, project, number) if project is not None else None
        if project is not None and version is not None:
            runner.restore_version(db, project, version, claim_token=token)
    except Exception as e:  # noqa: BLE001
        log.exception("Restore task crashed for %s", project_id)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


# ── changes ───────────────────────────────────────────────────────────────────
@router.post("/{project_id}/changes", status_code=202)
def start_change(
    payload: ChangeCreate,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Ask the crew to change the finished app: "now add login"."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Say what should change.")
    _refuse_credentials(text, project)
    if changes.open_change(db, project) is not None:
        raise HTTPException(
            409, "A change is already being made on this build. Review it, or discard it, first."
        )
    if project.status != PipelineStatus.COMPLETED.value:
        raise (
            _conflict(project, "change")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(409, "A change can be asked for once the build is finished.")
        )
    # The same checks a run makes before it starts: the models are there.
    _require_models(project)
    base = versions.ensure_first(db, project)
    token = _claim(db, project, {PipelineStatus.COMPLETED.value})
    if not token:
        raise _conflict(project, "change")
    found = ChangeRequest(
        project_id=project.id,
        number=changes.next_number(db, project),
        text=text,
        status=changes.OPEN,
        base_version_id=base.id if base is not None else None,
        created_by=user.id,
    )
    db.add(found)
    db.commit()
    background.add_task(_drive_change, project.id, found.id, token)
    return {
        "change": changes.out(project, found),
        "project_id": project.id,
        "status": project.status,
        "current_phase": project.current_phase,
        "message": "The crew is reading your change.",
    }


@router.get("/{project_id}/changes")
def list_changes(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> dict:
    found = changes.all_for(db, project)
    by_id = {v.id: v for v in versions.all_for(db, project)}
    return {"changes": [changes.out(project, c, by_id) for c in found]}


@router.post("/{project_id}/changes/{change_id}/discard", status_code=202)
def discard_change(
    change_id: str,
    background: BackgroundTasks,
    payload: Optional[DiscardRequest] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Throw a change away: the version it was made on is the build again."""
    found = _change_of(db, project, change_id)
    if found.status != changes.OPEN:
        raise HTTPException(409, f"That change is already {found.status}.")
    allowed = set(_DISCARDABLE)
    if project.status == PipelineStatus.RUNNING.value and project.stalled:
        allowed.add(PipelineStatus.RUNNING.value)
    if project.status not in allowed:
        raise (
            HTTPException(409, "The crew is still working on this change. Stop it first, then discard it.")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(409, f"Nothing to discard (status '{project.status}').")
        )
    # Released from whatever it waits on — a database question included: discarding is
    # an answer to every one of them.
    token = _claim(db, project, allowed, answers_question=True)
    if not token:
        raise _conflict(project, "discard")
    reason = (payload.reason if payload and payload.reason else "") or ""
    background.add_task(_drive_discard, project.id, found.id, reason, token)
    return {
        "project_id": project.id,
        "status": project.status,
        "current_phase": project.current_phase,
        "message": "Discarding the change — the version before it is coming back.",
    }


@router.get("/{project_id}/changes/{change_id}/diff")
def change_diff(change_id: str, project: Project = Depends(get_project), db: Session = Depends(get_db)) -> dict:
    """What a change changed, file by file: the version it was made on against the
    build as it is now (an open change) or the version it became (a kept one)."""
    found = _change_of(db, project, change_id)
    base = db.get(versions.Version, found.base_version_id) if found.base_version_id else None
    if base is None:
        raise HTTPException(409, "This change has no version to compare against.")
    if found.status == changes.OPEN:
        after, _ = versions.shipping(db, project, live=True)
    elif found.version_id:
        produced = db.get(versions.Version, found.version_id)
        if produced is None:
            raise HTTPException(409, "The version this change made isn't kept any more.")
        after = versions.assemble(project, produced)
    else:
        raise HTTPException(409, f"This change was {found.status}, so nothing it made is kept to compare.")
    return {"base": base.number, **versions.diff(versions.assemble(project, base), after)}


# ── versions ──────────────────────────────────────────────────────────────────
@router.get("/{project_id}/versions")
def list_versions(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> dict:
    versions.ensure_first(db, project)
    found = versions.all_for(db, project)
    current = versions.current(db, project)
    return {
        "versions": [versions.out(v, project) for v in reversed(found)],
        "current": current.number if current is not None else None,
        "deployed": project.deployed_version,
        "pushed": project.github_pushed_version,
    }


@router.get("/{project_id}/versions/{number}/diff")
def version_diff(number: int, project: Project = Depends(get_project), db: Session = Depends(get_db)) -> dict:
    """What a version changed from the one before it."""
    version = versions.by_number(db, project, number)
    if version is None:
        raise HTTPException(404, f"This build has no version {number}.")
    before = versions.by_number(db, project, number - 1) if number > 1 else None
    empty = {"files": []}
    return {
        "base": before.number if before is not None else None,
        **versions.diff(versions.assemble(project, before) if before else empty, versions.assemble(project, version)),
    }


@router.post("/{project_id}/versions/{number}/restore", status_code=202)
def restore_version(
    number: int,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Bring an earlier version back as the newest: restoring v1 after v3 makes v4."""
    version = versions.by_number(db, project, number)
    if version is None:
        raise HTTPException(404, f"This build has no version {number}.")
    if changes.open_change(db, project) is not None:
        raise HTTPException(409, "A change is being made on this build. Review it, or discard it, first.")
    if project.status != PipelineStatus.COMPLETED.value:
        raise (
            _conflict(project, "restore")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(409, "A version can be restored once the build is finished.")
        )
    current = versions.ensure_first(db, project)
    if current is not None and current.id == version.id:
        raise HTTPException(409, f"v{number} is already the current version.")
    token = _claim(db, project, {PipelineStatus.COMPLETED.value})
    if not token:
        raise _conflict(project, "restore")
    background.add_task(_drive_restore, project.id, number, token)
    return {
        "project_id": project.id,
        "status": project.status,
        "current_phase": project.current_phase,
        "message": f"Restoring v{number} as a new version.",
    }
