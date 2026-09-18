"""Visual preview: render a project as a clickable site, and edit it section by section.

A post-pipeline, non-destructive editing loop that sits alongside the phase-approval gates.
Each generate/edit appends a `PreviewRevision`; the newest row is the live preview and
`undo` drops it. Edits are scoped to a single `data-section`, so the model only ever
rewrites one block at a time.

Generating is a build of many model calls, so it does not happen inside the request:
`POST /generate` starts it and returns, and `GET` reports how far it has got. An edit
is one call (two with a repair) and stays synchronous.
"""
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.analytics import tracker
from app.api.deps import get_project
from app.core.constants import RoutingMode
from app.db.base import SessionLocal, get_db
from app.db.models import PreviewRevision, Project
from app.preview import service
from app.preview.document import is_site, site_data
from app.preview.generator import EditRejected, edit_section
from app.preview.html import extract_section, replace_section, route_spans, scan_sections
from app.preview.jobs import Reporter, jobs
from app.router.base import ProviderError
from app.router.router import router as model_router
from app.schemas.llm import LLMResponse
from app.schemas.preview import PreviewEditRequest, PreviewOut

router = APIRouter(prefix="/api/projects", tags=["preview"])

def _provider_hint() -> str:
    """Advice that names the model this install actually uses.

    A literal here told everyone to pull one particular model, so anyone who had
    chosen a different one was handed a fix for a model they were not running. The
    router is asked rather than the settings object, because a choice made in
    Settings lands there first — `.env` is only where the default started.
    """
    model = model_router.default_model("ollama") or "the model you selected"
    return (
        " If you're running Local-Only, make sure Ollama is running and the model is "
        f"pulled (`ollama pull {model}`)."
    )


# ── helpers ──────────────────────────────────────────────────────────────────
def _revisions(db: Session, project_id: str) -> List[PreviewRevision]:
    return (
        db.query(PreviewRevision)
        .filter(PreviewRevision.project_id == project_id)
        .order_by(PreviewRevision.created_at.desc())
        .all()
    )


def _save_revision(
    db: Session,
    project: Project,
    html: str,
    *,
    source: str,
    responses: Optional[List[LLMResponse]] = None,
    section_id: Optional[str] = None,
    instruction: Optional[str] = None,
) -> PreviewRevision:
    last = responses[-1] if responses else None
    row = PreviewRevision(
        project_id=project.id,
        html=html,
        source=source,
        section_id=section_id,
        instruction=instruction,
        model_used=last.model if last else None,
        provider_used=last.provider if last else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    for resp in responses or []:  # fold preview tokens into the project's cost/analytics
        tracker.record(db, response=resp, project_id=project.id, phase="preview")
    return row


def _preview_out(db: Session, project: Project) -> PreviewOut:
    revs = _revisions(db, project.id)
    html = revs[0].html if revs else None
    # An edit does not rebuild the site, so it carries no report of its own; the
    # build it edited is still the one being described.
    report = next((r.report for r in revs if r.report), None) if html and is_site(html) else None
    return PreviewOut(
        project_id=project.id,
        html=html,
        sections=scan_sections(html) if html else [],
        routes=[{"path": r["path"], "title": r["title"]} for r in route_spans(html)] if html else [],
        revisions=revs,
        has_frontend=any(ph.phase == "frontend_engineer" for ph in project.phases),
        report=report,
        job=jobs.visible(project.id),
    )


def _fail(exc: Exception, what: str) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"A model provider failed while {what}: {exc}.{_provider_hint()}",
    )


def _busy(project: Project) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="The mockup is being built right now. Wait for it to finish, then try again.",
    )


def _build_in_background(project_id: str, reporter: Reporter) -> None:
    """The work behind `POST /generate`, on its own session and thread."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None:
            return
        try:
            service.build_and_save(db, project, reporter)
        except ProviderError as e:
            raise RuntimeError(f"A model provider failed while drawing the mockup: {e}.{_provider_hint()}") from e
    finally:
        db.close()


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/{project_id}/preview", response_model=PreviewOut)
def get_preview(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    return _preview_out(db, project)


@router.post("/{project_id}/preview/generate", response_model=PreviewOut, status_code=202)
def generate(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    """Start building the mockup. Returns at once; `GET` reports progress."""
    project_id = project.id
    started = jobs.start(
        project_id, "request", lambda reporter: _build_in_background(project_id, reporter)
    )
    if not started:
        raise _busy(project)
    return _preview_out(db, project)


@router.post("/{project_id}/preview/edit", response_model=PreviewOut)
def edit(
    payload: PreviewEditRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    if jobs.running(project.id):
        raise _busy(project)
    revs = _revisions(db, project.id)
    if not revs:
        raise HTTPException(400, "No preview to edit yet — generate one first.")
    current = revs[0].html
    fragment = extract_section(current, payload.section_id)
    if fragment is None:
        raise HTTPException(400, f"Section '{payload.section_id}' isn't in the current preview.")
    try:
        new_fragment, responses = edit_section(
            fragment,
            payload.instruction,
            mode=RoutingMode(project.routing_mode),
            preferred_model=project.preferred_model,
            site=site_data(current),
            section_id=payload.section_id,
        )
    except ProviderError as e:
        raise _fail(e, "editing the section")
    except EditRejected as e:
        # The edit came back without what makes the section work, twice. Saving it
        # would leave a list that no longer lists or a form that no longer stores,
        # looking exactly as if it did — so the tokens are billed and the change is not.
        for resp in e.responses:
            tracker.record(db, response=resp, project_id=project.id, phase="preview")
        raise HTTPException(
            422,
            "That change would break how this section works, so it wasn't applied: "
            + "; ".join(e.problems[:3])
            + ". Try describing the change differently.",
        )
    if "<" not in new_fragment:
        raise HTTPException(502, "The model didn't return a usable section. Try rephrasing the change.")
    new_html = replace_section(current, payload.section_id, new_fragment)
    _save_revision(
        db,
        project,
        new_html,
        source="edited",
        responses=responses,
        section_id=payload.section_id,
        instruction=payload.instruction,
    )
    return _preview_out(db, project)


@router.post("/{project_id}/preview/undo", response_model=PreviewOut)
def undo(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    if jobs.running(project.id):
        raise _busy(project)
    revs = _revisions(db, project.id)
    if not revs:
        raise HTTPException(400, "Nothing to undo — no preview revisions yet.")
    db.delete(revs[0])
    db.commit()
    return _preview_out(db, project)
