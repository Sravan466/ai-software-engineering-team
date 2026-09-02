"""Project lifecycle + pipeline control (run / approve / reject / stop / resume).

Every control here is non-blocking. The phase itself runs in a background task and the
request returns as soon as the project has been moved into `running`, because a POST
that holds the connection open for the length of a model call is indistinguishable
from a hang: the client cannot poll, the status badge lies, and a refresh mid-flight
loses the gate entirely.

Each control claims the project with a conditional UPDATE. That single statement is the
idempotency guard — two tabs racing to approve the same phase produce one winner and
one `409`, instead of quietly advancing the pipeline twice on one click's worth of intent.
"""
from __future__ import annotations
from typing import Optional

import io
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_project
from app.core import artifacts
from app.core.config import settings
from app.core.constants import PHASE_ORDER, ApprovalMode, PipelineStatus, RoutingMode
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import Project
from app.orchestration.approval import decide_gate
from app.orchestration.runner import runner
from app.schemas.project import (
    ApprovalRequest,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RedoRequest,
    RunResponse,
    StopRequest,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_approval_mode(payload: ProjectCreate) -> str:
    """The review policy for a new run.

    `require_approval` is still accepted, and still means what it always did — stop
    at every handoff — so an existing caller keeps its behaviour. Everything else
    gets the two-checkpoint default.
    """
    if payload.approval_mode is not None:
        return payload.approval_mode.value
    if payload.require_approval is not None:
        return (
            ApprovalMode.EVERY_PHASE.value
            if payload.require_approval
            else ApprovalMode.UNATTENDED.value
        )
    return (
        ApprovalMode.CHECKPOINTS.value
        if settings.require_approval
        else ApprovalMode.UNATTENDED.value
    )


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    mode = (payload.routing_mode or RoutingMode(settings.default_routing_mode)).value
    approval = _resolve_approval_mode(payload)
    project = Project(
        idea=payload.idea,
        name=payload.name,
        routing_mode=mode,
        preferred_model=payload.preferred_model,
        approval_mode=approval,
        cost_cap_usd=payload.cost_cap_usd,
        # Kept in step with the mode so anything still reading the old flag — a saved
        # query, an older client — never disagrees with the policy actually in force.
        require_approval=approval != ApprovalMode.UNATTENDED.value,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _rederive_gate(db: Session, project: Project) -> None:
    """Re-answer "why are we stopped here?" after the policy changed under a gate.

    A run parked on a single handoff and then switched to two-checkpoint review is
    standing at a Plan review, and should say so. Without this the panel keeps the
    title the old policy gave it until the run moves again.

    A policy that would no longer stop here at all keeps the existing gate: the run
    is parked and something has to release it, so the decision on screen stays real.
    """
    if project.status != PipelineStatus.AWAITING_APPROVAL.value or not project.current_phase:
        return
    # The runner's own definition of "latest", so this cannot re-derive the gate from
    # a different row than the one the loop parked on.
    row = runner.latest_row(db, project, project.current_phase)
    if row is None:
        return
    gate = decide_gate(project, row.phase, row.output)
    if gate is not None:
        project.gate_kind = gate.kind
        project.gate_note = gate.note


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    payload: ProjectUpdate,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> Project:
    """Change how a run is reviewed — including one that is already in flight.

    The policy used to be frozen at create time: a build heading somewhere expensive
    could not be given a gate, and one you had come to trust could not be let off its
    leash without starting over. The runner re-reads these before every handoff, so a
    change lands on the next one rather than at the next restart.
    """
    if payload.approval_mode is not None:
        project.approval_mode = payload.approval_mode.value
        project.require_approval = payload.approval_mode != ApprovalMode.UNATTENDED
    if payload.clear_cost_cap:
        project.cost_cap_usd = None
    elif payload.cost_cap_usd is not None:
        project.cost_cap_usd = payload.cost_cap_usd
    _rederive_gate(db, project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return list(db.execute(select(Project).order_by(Project.created_at.desc())).scalars())


@router.get("/{project_id}", response_model=ProjectOut)
def get_one(project: Project = Depends(get_project)) -> Project:
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    """Delete a project and everything it produced.

    A run may still be mid-phase; the flag tells it to stop touching a row that is
    about to disappear (the runner also treats a vanished project as cancelled).
    """
    project.cancel_requested = True
    db.commit()
    db.delete(project)
    db.commit()


# ── Generated-project artifacts (preview + download) ─────────────────────────
@router.get("/{project_id}/artifacts")
def get_artifacts(project: Project = Depends(get_project)) -> dict:
    """Assembled files + docs + setup steps the agents produced (for Preview/Summary)."""
    assembled = artifacts.assemble(project)
    return {
        "idea": project.idea,
        "name": project.name,
        "status": project.status,
        "readme": artifacts.readme_md(project, assembled),
        **assembled,
    }


@router.get("/{project_id}/download")
def download_project(project: Project = Depends(get_project)):
    """Stream the generated project as a .zip (code + docs + README)."""
    assembled = artifacts.assemble(project)
    data = artifacts.build_zip(project, assembled)
    filename = artifacts.slug(project.name or project.idea) + ".zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Pipeline control ─────────────────────────────────────────────────────────
def _claim(db: Session, project: Project, allowed: set[str]) -> bool:
    """Atomically move the project into `running` — but only from `allowed`.

    Returns False when someone else got there first (a second tab, a double click),
    which is the whole point: the phase is handed to a background task exactly once.
    """
    result = db.execute(
        update(Project)
        .where(Project.id == project.id, Project.status.in_(allowed))
        .values(
            status=PipelineStatus.RUNNING.value,
            cancel_requested=False,
            last_error=None,
            heartbeat_at=_now(),
        )
    )
    db.commit()
    if result.rowcount != 1:
        db.refresh(project)
        return False
    db.refresh(project)
    return True


def _conflict(project: Project, action: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"This build is already '{project.status}' — nothing to {action}. "
            "Another tab or click probably got there first; reload to see where it is."
        ),
    )


def _drive(project_id: str) -> None:
    """Background task: run the pipeline forward until it needs a human again."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.continue_run(db, project)
    except Exception as e:  # noqa: BLE001 - a background crash must not strand the run
        log.exception("Pipeline task crashed for %s", project_id)
        _strand(db, project_id, str(e))
    finally:
        db.close()


def _drive_reject(project_id: str, feedback: str) -> None:
    """Background task: regenerate the current phase with the reviewer's note."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.reject(db, project, feedback)
    except Exception as e:  # noqa: BLE001
        log.exception("Reject task crashed for %s", project_id)
        _strand(db, project_id, str(e))
    finally:
        db.close()


def _drive_redo(project_id: str, phase: str, feedback: str) -> None:
    """Background task: regenerate one named phase and return to the same review."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.redo(db, project, phase, feedback)
    except Exception as e:  # noqa: BLE001
        log.exception("Redo task crashed for %s/%s", project_id, phase)
        _strand(db, project_id, str(e))
    finally:
        db.close()


def _strand(db: Session, project_id: str, message: str) -> None:
    """Last resort: never leave a project `running` with nothing running.

    This runs on the session the crash happened on, whose transaction may already be
    poisoned — so roll back first. Without that the recovery write fails too and the
    project stays `running` forever, which is the dead end this whole change removes.
    """
    try:
        db.rollback()
        project = db.get(Project, project_id)
        if project is not None and project.status == PipelineStatus.RUNNING.value:
            project.status = PipelineStatus.FAILED.value
            project.last_error = message
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


@router.post("/{project_id}/run", response_model=RunResponse)
def run_pipeline(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    if not _claim(db, project, {PipelineStatus.CREATED.value, PipelineStatus.FAILED.value}):
        raise _conflict(project, "start")

    background.add_task(_drive, project.id)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message=(
            "Running — the first agent is generating now."
            if project.require_approval
            else "Running end-to-end (approvals disabled)."
        ),
    )


@router.post("/{project_id}/approve", response_model=RunResponse)
def approve_phase(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "approve")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"Nothing to approve (status '{project.status}').")
        )
    if not _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value}):
        raise _conflict(project, "approve")

    runner.approve_current(db, project)
    background.add_task(_drive, project.id)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Approved — the next agent is starting.",
    )


@router.post("/{project_id}/reject", response_model=RunResponse)
def reject_phase(
    payload: ApprovalRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Send back whichever phase is under review.

    `POST /redo` does this and more — it can name any phase — and is what the UI
    calls. This stays for API callers that already use it; both run the same code.
    """
    if not payload.feedback or not payload.feedback.strip():
        raise HTTPException(400, "Feedback is required when rejecting a phase.")
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "reject")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"Nothing to reject (status '{project.status}').")
        )
    if not _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value}):
        raise _conflict(project, "reject")

    background.add_task(_drive_reject, project.id, payload.feedback.strip())
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Sent back — the agent is regenerating this phase with your note.",
    )


@router.post("/{project_id}/redo", response_model=RunResponse)
def redo_phase(
    payload: RedoRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Send one phase back to its agent without leaving the review you are in.

    The Ship review shows a file tree assembled from four phases, so "this file is
    wrong" has to reach whoever wrote that file. Reject can only ever address the
    phase on screen; this addresses the one named.
    """
    if payload.phase not in {p.value for p in PHASE_ORDER}:
        raise HTTPException(400, f"'{payload.phase}' is not a phase of this pipeline.")
    if not payload.feedback.strip():
        raise HTTPException(400, "Say what to change — an agent cannot act on blank feedback.")
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "redo")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"Nothing to redo (status '{project.status}').")
        )
    if not any(ph.phase == payload.phase and ph.output for ph in project.phases):
        raise HTTPException(
            400, f"The {payload.phase} phase hasn't produced anything to redo yet."
        )
    if not _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value}):
        raise _conflict(project, "redo")

    background.add_task(_drive_redo, project.id, payload.phase, payload.feedback.strip())
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Sent back — that agent is redoing its work with your note.",
    )


@router.post("/{project_id}/stop", response_model=RunResponse)
def stop_pipeline(
    payload: Optional[StopRequest] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Stop a run. The current model call finishes, then the pipeline halts.

    Marked `cancelled` immediately so the UI is never stuck watching a run it has
    already abandoned — and everything produced so far is kept for the resume.
    """
    if project.status not in (
        PipelineStatus.RUNNING.value,
        PipelineStatus.AWAITING_APPROVAL.value,
    ):
        raise HTTPException(400, f"This build isn't running (status '{project.status}').")

    reason = (payload.reason if payload and payload.reason else None) or (
        "Stopped by you. Resume picks up from the last approved phase."
    )
    runner.stop(db, project, reason)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Stopped. Anything already generated is kept — resume when you're ready.",
    )


@router.post("/{project_id}/resume", response_model=RunResponse)
def resume_pipeline(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Pick a stopped, failed or stalled run back up from its last checkpoint."""
    resumable = {
        PipelineStatus.CANCELLED.value,
        PipelineStatus.FAILED.value,
        PipelineStatus.CREATED.value,
    }
    # A live `running` project is doing fine; only a stalled one may be taken over.
    if project.status == PipelineStatus.RUNNING.value and project.stalled:
        resumable.add(PipelineStatus.RUNNING.value)
    if project.status not in resumable:
        still_running = project.status == PipelineStatus.RUNNING.value
        raise HTTPException(
            400,
            f"Nothing to resume (status '{project.status}')."
            + (" This build is still running — stop it first." if still_running else ""),
        )

    runner.prepare_resume(db, project)
    if not _claim(db, project, resumable):
        raise _conflict(project, "resume")

    background.add_task(_drive, project.id)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Resumed from the last checkpoint.",
    )
