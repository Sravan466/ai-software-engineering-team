"""Project lifecycle + pipeline control (run / approve / reject)."""
from __future__ import annotations

import io

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_project
from app.core import artifacts
from app.core.config import settings
from app.core.constants import PipelineStatus, RoutingMode
from app.db.base import SessionLocal, get_db
from app.db.models import Project
from app.orchestration.runner import runner
from app.router.base import ProviderError
from app.schemas.project import (
    ApprovalRequest,
    ProjectCreate,
    ProjectOut,
    RunResponse,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    mode = (payload.routing_mode or RoutingMode(settings.default_routing_mode)).value
    project = Project(
        idea=payload.idea,
        name=payload.name,
        routing_mode=mode,
        preferred_model=payload.preferred_model,
        require_approval=(
            payload.require_approval
            if payload.require_approval is not None
            else settings.require_approval
        ),
    )
    db.add(project)
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
def _fail(db: Session, project: Project, exc: Exception) -> None:
    """Mark a project failed and surface a clean 502 to the caller."""
    project.status = PipelineStatus.FAILED.value
    db.commit()
    raise HTTPException(
        status_code=502,
        detail=(
            f"A model provider failed: {exc}. If you're running Local-Only, make sure "
            "Ollama is running and the model is pulled (`ollama pull qwen2.5:7b`)."
        ),
    )


def _run_full_pipeline(project_id: str) -> None:
    """Background task: run every phase without pausing (approvals disabled)."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project:
            runner.run_to_completion(db, project)
    except ProviderError:
        if project:
            project.status = PipelineStatus.FAILED.value
            db.commit()
    finally:
        db.close()


@router.post("/{project_id}/run", response_model=RunResponse)
def run_pipeline(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    if project.status not in (
        PipelineStatus.CREATED.value,
        PipelineStatus.FAILED.value,
    ):
        raise HTTPException(400, f"Project is already '{project.status}'.")

    if project.require_approval:
        try:
            runner.start(db, project)
        except ProviderError as e:
            _fail(db, project, e)
        return RunResponse(
            project_id=project.id,
            status=project.status,
            current_phase=project.current_phase,
            message="First phase complete — awaiting your approval to continue.",
        )

    # No approvals: run the whole pipeline in the background.
    background.add_task(_run_full_pipeline, project.id)
    return RunResponse(
        project_id=project.id,
        status=PipelineStatus.RUNNING.value,
        current_phase=None,
        message="Pipeline running end-to-end (approvals disabled). Poll the project for results.",
    )


@router.post("/{project_id}/approve", response_model=RunResponse)
def approve_phase(
    project: Project = Depends(get_project), db: Session = Depends(get_db)
) -> RunResponse:
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise HTTPException(400, f"Nothing to approve (status '{project.status}').")
    try:
        runner.approve(db, project)
    except ProviderError as e:
        _fail(db, project, e)
    done = project.status == PipelineStatus.COMPLETED.value
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Pipeline complete." if done else "Phase approved — next phase ready for review.",
    )


@router.post("/{project_id}/reject", response_model=RunResponse)
def reject_phase(
    payload: ApprovalRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise HTTPException(400, f"Nothing to reject (status '{project.status}').")
    if not payload.feedback:
        raise HTTPException(400, "Feedback is required when rejecting a phase.")
    try:
        runner.reject(db, project, payload.feedback)
    except ProviderError as e:
        _fail(db, project, e)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Phase regenerated with your feedback — ready for review.",
    )
