"""A build's app connectors: at its question after the architecture, or from the project.

    GET    /api/projects/{id}/integrations               every connector the build uses
    PUT    /api/projects/{id}/integrations/{iid}         parse, test, save (account or build)
    POST   /api/projects/{id}/integrations/{iid}/check   the same
    DELETE /api/projects/{id}/integrations/{iid}         drop this build's own key for it
    POST   /api/projects/{id}/integrations/{iid}/later   "add it later", for one
    POST   /api/projects/{id}/integrations/continue      at the question, once all are answered
    POST   /api/projects/{id}/integrations/later         "Add all later": answer the rest, carry on
    POST   /api/projects/{id}/integrations               {add|remove}: change which it uses

A connector already connected in the account's Connectors is linked — read from there
on every use — and never asked about. Keys given here go to the account by default
("save to my Connectors"), so the next build that needs the service doesn't ask; with
that switched off, or as "use a different key for this build", they are this build's
alone and win over the account's.

As with the database, saving never resumes the run: `continue` and `later` do, claimed
the way every other control is.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, object_session

from app.api.deps import get_project
from app.api.routes.connectors import guard_write, require, save_checked
from app.api.routes.projects import _claim, _conflict, _drive, _require_models
from app.build import integrations
from app.core import connectors_store, project_secrets, secretbox
from app.core.constants import PipelineStatus
from app.core.logging import get_logger
from app.db.base import get_db
from app.db.models import Project
from app.orchestration import connectors
from app.orchestration.charter import Charter
from app.orchestration.runner import runner
from app.schemas.project import RunResponse

log = get_logger(__name__)

router = APIRouter(prefix="/api/projects", tags=["integrations"])


class ProjectConnectValues(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    confirm_live: bool = False
    #: Keep it in the account's Connectors for future builds (the default), or for
    #: this build only — which is also how "a different key for this build" is saved.
    save_to_account: bool = True


class ChangeRequest(BaseModel):
    add: Optional[str] = None
    remove: Optional[str] = None


def _reasons(project: Project) -> dict[str, dict]:
    """Why each connector is in the build, worked out the same way the freeze did."""
    from app.core.constants import Phase

    db = object_session(project)
    row = runner.latest_row(db, project, Phase.SYSTEM_DESIGN.value) if db is not None else None
    design = row.output if row is not None else None
    store = connectors_store.for_user(project.owner_id or "")
    choice = project.integrations_choice or {}
    try:
        found = integrations.relevant(
            project.idea or "",
            integrations.design_texts(design),
            store.connected_ids() if project.owner_id else [],
            use=choice.get("use") or (),
            skip=choice.get("skip") or (),
            defaults=store.defaults() if project.owner_id else {},
            recent=store.recent() if project.owner_id else {},
        )
    except Exception:  # noqa: BLE001 - a reason is a nicety, never a failure
        return {}
    return {m.iid: m.as_dict() for m in found}


def _state(project: Project) -> dict:
    """Everything the card and the panel show. Never a value."""
    used = connectors.used(project)
    reasons = _reasons(project) if used else {}
    store = connectors_store.for_user(project.owner_id or "") if project.owner_id else None
    rows = []
    for iid in used:
        found = integrations.get(iid)
        if found is None:
            continue
        status, source = connectors.status_of(project, iid)
        saved: list[dict] = []
        check = None
        mode = None
        models: list[str] = []
        if source == connectors.PROJECT:
            entry = project_secrets.integrations_load(project.owner_id, project.id).get(iid) or {}
            try:
                saved = integrations.hints(found, project_secrets.integration_values(project.owner_id, project.id, iid))
            except secretbox.SecretsLocked:
                saved = [{"name": n, "hint": "(can't be read — save it again)", "side": "server"} for n in entry.get("values") or {}]
            check, mode = entry.get("check"), entry.get("mode")
        elif source == connectors.ACCOUNT and store is not None:
            info = store.public(iid)
            saved, check, mode = info.get("saved") or [], info.get("check"), info.get("mode")
            models = info.get("models") or []
        rows.append(
            {
                **found.as_dict(),
                "status": status,
                "source": source,
                "saved": saved,
                "check": check,
                "mode": mode,
                "models": models,
                "connected": status in ("connected", "unchecked"),
                "reason": (reasons.get(iid) or {}).get("reason"),
                "account_connected": bool(store and store.entry(iid).get("values")),
            }
        )
    return {
        "used": list(used),
        "connectors": rows,
        "at_gate": runner.at_integrations_gate(project),
        "can_change": runner.before_code(project),
        "unanswered": connectors.unanswered(project),
        "not_connected": connectors.not_connected(project),
        "addable": [
            {"id": i.id, "label": i.label}
            for i in integrations.catalog()
            if i.connectable and i.id not in used
        ],
    }


def _require_used(project: Project, iid: str) -> integrations.Integration:
    found = require(iid)
    if iid not in connectors.used(project):
        raise HTTPException(400, f"This build doesn't use {found.label}. Add it to the build first.")
    return found


@router.get("/{project_id}/integrations")
def get_integrations(project: Project = Depends(get_project)) -> dict:
    return _state(project)


def _save(project: Project, iid: str, body: ProjectConnectValues, db: Session) -> dict:
    found = _require_used(project, iid)
    owner = project.owner_id or ""
    _, source = connectors.status_of(project, iid)
    try:
        if body.save_to_account:
            store = connectors_store.for_user(owner)
            saved = store.values(iid) if store.entry(iid).get("values") else {}
        else:
            saved = project_secrets.integration_values(owner, project.id, iid) if source == connectors.PROJECT else {}
    except secretbox.SecretsLocked:
        saved = {}
    outcome = save_checked(found, body.values, saved, body.confirm_live, owner or "install")
    if isinstance(outcome, dict):
        return {**outcome, "state": _state(project)}
    parsed, checked = outcome
    if checked.result.status == integrations.FAILED:
        return {"ok": False, "status": "failed", "check": checked.as_dict(), "state": _state(project)}
    try:
        if body.save_to_account:
            connectors_store.for_user(owner).save(iid, parsed.values, checked, parsed.mode)
            # The account's key is what this build should read now, not an old override.
            project_secrets.remove_integration(owner, project.id, iid)
        else:
            project_secrets.save_integration(
                owner, project.id, iid, parsed.values, connectors_store.check_record(checked), checked.mode or parsed.mode
            )
    except connectors_store.StoreUnreadable as e:
        raise HTTPException(503, str(e))
    except secretbox.SecretsLocked as e:
        raise HTTPException(503, str(e))
    status = dict(project.integrations_status or {})
    status.pop(iid, None)
    project.integrations_status = status or None
    db.commit()
    db.refresh(project)
    return {"ok": True, "status": checked.result.status, "check": checked.as_dict(), "state": _state(project)}


@router.put("/{project_id}/integrations/{iid}")
def put_integration(
    iid: str,
    body: ProjectConnectValues,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    guard_write(request)
    return _save(project, iid, body, db)


@router.post("/{project_id}/integrations/{iid}/check")
def check_integration(
    iid: str,
    body: ProjectConnectValues,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    guard_write(request)
    return _save(project, iid, body, db)


@router.delete("/{project_id}/integrations/{iid}")
def delete_integration(
    iid: str,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Drop this build's own key. It falls back to the account's connection, if any."""
    guard_write(request)
    _require_used(project, iid)
    project_secrets.remove_integration(project.owner_id, project.id, iid)
    if connectors.status_of(project, iid)[0] is None and project.integrations_status is not None:
        # Asked already, and now not connected: the same as having said "later".
        connectors.mark_later(project, [iid])
    db.commit()
    db.refresh(project)
    return _state(project)


@router.post("/{project_id}/integrations/{iid}/later")
def later_one(
    iid: str,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    _require_used(project, iid)
    connectors.mark_later(project, [iid])
    db.commit()
    db.refresh(project)
    return _state(project)


def _resume(background: BackgroundTasks, db: Session, project: Project, message: str) -> RunResponse:
    _require_models(project)
    if not _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value}, answers_question=True):
        raise _conflict(project, "continue")
    project.gate_kind = None
    project.gate_note = None
    db.commit()
    background.add_task(_drive, project.id)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message=message,
    )


def _require_gate(project: Project) -> None:
    if not runner.at_integrations_gate(project):
        raise (
            _conflict(project, "continue")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, "This build isn't waiting on its services.")
        )


@router.post("/{project_id}/integrations/continue", response_model=RunResponse)
def continue_build(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    _require_gate(project)
    waiting = connectors.unanswered(project)
    if waiting:
        names = ", ".join(integrations.get(i).label for i in waiting)
        raise HTTPException(409, f"{names} still need an answer: connect, or choose Later.")
    return _resume(background, db, project, "Continuing — the crew builds against your services.")


@router.post("/{project_id}/integrations/later", response_model=RunResponse)
def all_later(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """"Add all later": everything unanswered is put off, and the build carries on."""
    _require_gate(project)
    waiting = connectors.unanswered(project)
    if waiting:
        connectors.mark_later(project, waiting)
        db.commit()
    return _resume(background, db, project, "Continuing — connect the services from the project any time.")


@router.post("/{project_id}/integrations")
def change(
    body: ChangeRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Add a connector to the build, or take one out — only before any code exists.

    The code reads the charter's names; changing them after the crew wrote it would
    leave it reading variables nobody set. Redo the architecture to change them later.
    """
    if not body.add and not body.remove:
        raise HTTPException(422, "Say which connector to add or remove.")
    iid = body.add or body.remove
    found = require(iid)  # type: ignore[arg-type]
    if not runner.before_code(project):
        raise HTTPException(
            409,
            "The crew already built against this build's services, so they can't change now. "
            "Redo the architecture to change them.",
        )
    charter = Charter.from_dict(project.charter) or Charter({})
    used = list(charter.integrations)
    choice = dict(project.integrations_choice or {"use": [], "skip": []})
    use, skip = set(choice.get("use") or []), set(choice.get("skip") or [])
    if body.add:
        # One connector per capability: adding Stripe replaces another payments one.
        used = [i for i in used if integrations.get(i).capability != found.capability]
        used.append(found.id)
        use.add(found.id)
        skip.discard(found.id)
    else:
        used = [i for i in used if i != found.id]
        skip.add(found.id)
        use.discard(found.id)
    order = {i.id: n for n, i in enumerate(integrations.catalog())}
    used = sorted(dict.fromkeys(used), key=lambda i: order.get(i, 999))
    before = project.charter
    project.integrations_choice = {"use": sorted(use), "skip": sorted(skip)}
    runner.set_integrations(project, charter.with_integrations(tuple(used)))
    runner.settle_integrations(project, before)
    if runner.at_integrations_gate(project) and not connectors.unanswered(project):
        # Nothing left to ask: the note would name services the build no longer uses.
        project.gate_note = "Every service this build uses is answered. Continue when you're ready."
    db.commit()
    db.refresh(project)
    return _state(project)

