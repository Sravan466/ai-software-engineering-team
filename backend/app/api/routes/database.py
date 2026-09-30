"""Connecting a build's database: at the gate after Atlas, or from the project later.

    GET    /api/projects/{id}/database            what it needs, what is saved (hints only)
    POST   /api/projects/{id}/database/check      parse, test, and save when it is usable
    PUT    /api/projects/{id}/database            the same, for the project page
    DELETE /api/projects/{id}/database            remove what is saved
    POST   /api/projects/{id}/database/continue   at the gate, once something is saved
    POST   /api/projects/{id}/database/later      at the gate: "Continue, I'll add it later"

A value goes in and never comes back out: every response carries names and hints —
a host, or `…last4` — and nothing else. Saving does not resume the run, so a person
can fix a failed test and try again; `continue` and `later` are what move it on,
claimed the same way every other control is.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.build import dbconnect
from app.core import project_secrets, secretbox
from app.core.constants import DatabaseStatus, PipelineStatus
from app.core.logging import get_logger
from app.db.base import get_db
from app.db.models import Project
from app.orchestration.charter import Charter
from app.orchestration.runner import runner
from app.router.keycheck import RateLimiter
from app.schemas.project import RunResponse

from app.api.deps import get_project
from app.api.routes.projects import _claim, _conflict, _drive, _require_models
from app.api.routes.settings import _trusted_host

log = get_logger(__name__)

router = APIRouter(prefix="/api/projects", tags=["database"])

#: Same budget as cloud key checks: a check route is otherwise a free way to probe
#: someone's database with guessed passwords, or this server's network with hosts.
limiter = RateLimiter()

#: Statuses that mean something is saved.
_SAVED = {DatabaseStatus.CONNECTED.value, DatabaseStatus.UNCHECKED.value}


class DatabaseValues(BaseModel):
    #: NAME -> value, as pasted. Firebase also accepts `firebaseConfig`.
    values: dict[str, str] = Field(default_factory=dict)
    #: Switch provider (Supabase / Neon / Other Postgres). Must be one the database has.
    provider: Optional[str] = None


def _contract(project: Project, provider: Optional[str] = None) -> dbconnect.Contract:
    charter = Charter.from_dict(project.charter)
    choice = charter.get("database") if charter else None
    if choice is None or choice.token not in dbconnect.NEEDS_CREDENTIALS:
        raise HTTPException(
            400,
            "This build's database needs no connection string — "
            + ("it uses SQLite." if choice is not None else "Atlas hasn't picked one yet."),
        )
    if provider and provider not in dbconnect.PROVIDERS[choice.token]:
        raise HTTPException(422, f"'{provider}' isn't a host for {choice.label}.")
    if provider and provider != charter.database_provider and not runner.at_database_gate(project):
        # The crew has written code that reads the charter's variable names. Values
        # saved under another host's names would leave that code reading nothing.
        current = dbconnect.contract_for(choice.token, charter.database_provider)
        raise HTTPException(
            409,
            f"The crew already built against {current.label if current else 'another host'}'s "
            "variables, so the host can't change now. Redo the architecture to change it.",
        )
    return dbconnect.contract_for(choice.token, provider or charter.database_provider)  # type: ignore[return-value]



def _guard_write(request: Request) -> None:
    if not _trusted_host(request):
        raise HTTPException(
            403,
            "Database credentials can only be changed from an address this backend is "
            "served at (localhost, or BACKEND_PUBLIC_URL).",
        )


def _state(project: Project, want: Optional[str] = None) -> dict:
    """Everything the card and the project panel show. Never a value.

    `want` is a provider the person is looking at before anything is saved for it —
    the fields for Supabase differ from those for Neon.
    """
    charter = Charter.from_dict(project.charter)
    choice = charter.get("database") if charter else None
    needed = bool(choice and choice.token in dbconnect.NEEDS_CREDENTIALS)
    at_gate = runner.at_database_gate(project)
    out: dict = {
        "needed": needed,
        "status": project.database_status,
        "database": choice.token if choice else None,
        "database_label": choice.label if choice else None,
        "at_gate": at_gate,
    }
    if not needed:
        return out
    # The charter's host is the one the code reads. Another is only for looking at,
    # and only while the question is still open.
    provider = charter.database_provider
    if at_gate and want in dbconnect.PROVIDERS[choice.token]:
        provider = want
    contract = dbconnect.contract_for(choice.token, provider)
    saved = project_secrets.summary(project.owner_id, project.id, contract)
    out.update(
        {
            # The stored record's provider is already folded into `contract`; the
            # summary's own `provider` (None when nothing is saved) must not win.
            "saved": saved["saved"],
            "check": saved["check"],
            "provider": contract.provider,
            # A choice only while the build hasn't been written against one yet.
            "providers": [
                {"provider": p, "label": dbconnect.contract_for(choice.token, p).label}
                for p in (dbconnect.PROVIDERS[choice.token] if at_gate else (provider,))
            ],
            "contract": contract.as_dict(),
        }
    )
    return out


@router.get("/{project_id}/database")
def get_database(provider: Optional[str] = None, project: Project = Depends(get_project)) -> dict:
    return _state(project, provider)


def _save(project: Project, body: DatabaseValues, db: Session) -> dict:
    contract = _contract(project, body.provider)
    parsed = dbconnect.parse(contract, body.values)
    if parsed.problems:
        # Nothing touched the network, and nothing was saved.
        return {
            "ok": False,
            "status": "invalid",
            "problems": [p.as_dict() for p in parsed.problems],
            "notices": [n.as_dict() for n in parsed.notices],
            "state": _state(project, contract.provider),
        }
    if not limiter.allow(project.owner_id or "install"):
        raise HTTPException(
            429, "Too many connection tests in a short time. Wait a few minutes and try again."
        )
    # Scrubbed while the test runs: a driver may quote a value back in its error.
    with project_secrets.held(dbconnect.secret_parts(contract, parsed.values)):
        result = dbconnect.check_connection(contract, parsed.values)
    if result.status == dbconnect.FAILED:
        # A value that failed its test is not saved over one that might work, and is
        # kept on the page for the person to fix.
        log.info("Database test failed for %s: %s", project.id, result.reason)
        return {
            "ok": False,
            "status": result.status,
            "check": result.as_dict(),
            "notices": [n.as_dict() for n in parsed.notices],
            "state": _state(project, contract.provider),
        }
    charter = Charter.from_dict(project.charter)
    moved = charter is not None and charter.database_provider != contract.provider
    previous = project_secrets.snapshot(project.owner_id, project.id)
    try:
        if moved:
            # Only reachable at the gate (see `_contract`): the code isn't written
            # yet, so it is told to read the names that were actually saved.
            runner.set_database_provider(project, charter.with_provider(contract.provider))
        project_secrets.save(project.owner_id, project.id, contract, parsed.values, result)
        project.database_status = (
            DatabaseStatus.CONNECTED.value
            if result.status == dbconnect.CONNECTED
            else DatabaseStatus.UNCHECKED.value
        )
        db.commit()
    except Exception as e:
        # All three agree or none changes: the file goes back to what it held, the
        # checkpoint to the charter the row still holds, and the row is rolled back.
        db.rollback()
        try:
            project_secrets.restore(project.owner_id, project.id, previous)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't restore the saved credentials after a failed save on %s", project.id)
        if moved:
            try:
                runner.set_database_provider(project, charter)
                db.rollback()
            except Exception:  # noqa: BLE001
                log.exception("Couldn't restore the charter after a failed save on %s", project.id)
        if isinstance(e, secretbox.SecretsLocked):
            raise HTTPException(503, str(e))
        raise
    db.refresh(project)
    return {
        "ok": True,
        "status": result.status,
        "check": result.as_dict(),
        "notices": [n.as_dict() for n in parsed.notices],
        "state": _state(project),
    }


@router.post("/{project_id}/database/check")
def check_database(
    body: DatabaseValues,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Parse, test, and — when it is usable — save. Does not resume the run."""
    _guard_write(request)
    return _save(project, body, db)


@router.put("/{project_id}/database")
def put_database(
    body: DatabaseValues,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    _guard_write(request)
    return _save(project, body, db)


@router.delete("/{project_id}/database")
def delete_database(
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    _guard_write(request)
    project_secrets.remove(project.owner_id, project.id)
    if project.database_status in _SAVED:
        # Asked already, and now not connected: the same as having said "later".
        project.database_status = DatabaseStatus.LATER.value
    db.commit()
    db.refresh(project)
    return _state(project)


def _require_gate(project: Project, action: str) -> None:
    if not runner.at_database_gate(project):
        raise (
            _conflict(project, action)
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, "This build isn't waiting on its database.")
        )


def _resume(
    background: BackgroundTasks,
    db: Session,
    project: Project,
    message: str,
    status: Optional[str] = None,
) -> RunResponse:
    _require_models(project)
    if not _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value}, answers_database=True):
        raise _conflict(project, "continue")
    # After the claim, so a second tab that lost the race records nothing.
    if status is not None:
        project.database_status = status
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


@router.post("/{project_id}/database/continue", response_model=RunResponse)
def continue_with_database(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Carry on with the database that was just saved."""
    _require_gate(project, "continue")
    if project.database_status not in (DatabaseStatus.CONNECTED.value, DatabaseStatus.UNCHECKED.value):
        raise HTTPException(
            409,
            "Nothing is saved yet. Test and save the connection first, or continue and add it later.",
        )
    return _resume(background, db, project, "Continuing — the crew builds against your database.")


@router.post("/{project_id}/database/later", response_model=RunResponse)
def add_database_later(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """"Continue, I'll add it later": the build carries on exactly as it would have."""
    _require_gate(project, "continue")
    saved = project.database_status in (DatabaseStatus.CONNECTED.value, DatabaseStatus.UNCHECKED.value)
    return _resume(
        background,
        db,
        project,
        "Continuing — connect the database from the project any time.",
        None if saved else DatabaseStatus.LATER.value,
    )
