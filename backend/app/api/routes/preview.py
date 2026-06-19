"""Visual preview: render a project as an editable HTML mockup, section by section.

A post-pipeline, non-destructive editing loop that sits alongside the phase-approval gates.
Each generate/edit appends a `PreviewRevision`; the newest row is the live preview and
`undo` drops it. Edits are scoped to a single `data-section`, so the model only ever
rewrites one block at a time.
"""
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.analytics import tracker
from app.api.deps import get_project
from app.core.constants import RoutingMode
from app.db.base import get_db
from app.db.models import PreviewRevision, Project
from app.preview.generator import edit_section, generate_preview
from app.preview.html import extract_section, replace_section, scan_sections
from app.router.base import ProviderError
from app.schemas.llm import LLMResponse
from app.schemas.preview import PreviewEditRequest, PreviewOut

router = APIRouter(prefix="/api/projects", tags=["preview"])

_PROVIDER_HINT = (
    " If you're running Local-Only, make sure Ollama is running and the model is pulled "
    "(`ollama pull qwen2.5:7b`)."
)


# ── helpers ──────────────────────────────────────────────────────────────────
def _revisions(db: Session, project_id: str) -> List[PreviewRevision]:
    return (
        db.query(PreviewRevision)
        .filter(PreviewRevision.project_id == project_id)
        .order_by(PreviewRevision.created_at.desc())
        .all()
    )


def _build_context(project: Project) -> str:
    """Distil the team's outputs into a short brief for the preview generator."""
    by_phase = {ph.phase: ph.output for ph in project.phases if isinstance(ph.output, dict)}
    parts: List[str] = []

    pm = by_phase.get("product_manager", {})
    if pm.get("product_name"):
        parts.append(f"Product name: {pm['product_name']}")
    feats = pm.get("features") or pm.get("mvp_features") or pm.get("user_stories")
    if feats:
        parts.append(f"Key features: {_short(feats)}")

    fe = by_phase.get("frontend_engineer", {})
    if fe.get("framework"):
        parts.append(f"Frontend framework: {fe['framework']}")
    if fe.get("pages"):
        parts.append(f"Pages: {_short(fe['pages'])}")
    if fe.get("components"):
        parts.append(f"Components: {_short(fe['components'])}")

    ds = by_phase.get("system_design", {})
    if ds.get("tech_stack"):
        parts.append(f"Tech stack: {_short(ds['tech_stack'])}")

    return "\n".join(parts) or "(No detailed design yet — infer a sensible layout from the idea.)"


def _short(value: object, limit: int = 600) -> str:
    """Compact, readable one-liner for a JSON-ish value."""
    import json

    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _save_revision(
    db: Session,
    project: Project,
    html: str,
    *,
    source: str,
    resp: Optional[LLMResponse] = None,
    section_id: Optional[str] = None,
    instruction: Optional[str] = None,
) -> PreviewRevision:
    row = PreviewRevision(
        project_id=project.id,
        html=html,
        source=source,
        section_id=section_id,
        instruction=instruction,
        model_used=resp.model if resp else None,
        provider_used=resp.provider if resp else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if resp:  # fold preview tokens into the project's cost/analytics
        tracker.record(db, response=resp, project_id=project.id, phase="preview")
    return row


def _preview_out(db: Session, project: Project) -> PreviewOut:
    revs = _revisions(db, project.id)
    html = revs[0].html if revs else None
    return PreviewOut(
        project_id=project.id,
        html=html,
        sections=scan_sections(html) if html else [],
        revisions=revs,
        has_frontend=any(ph.phase == "frontend_engineer" for ph in project.phases),
    )


def _fail(exc: Exception, what: str) -> HTTPException:
    return HTTPException(status_code=502, detail=f"A model provider failed while {what}: {exc}.{_PROVIDER_HINT}")


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/{project_id}/preview", response_model=PreviewOut)
def get_preview(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    return _preview_out(db, project)


@router.post("/{project_id}/preview/generate", response_model=PreviewOut)
def generate(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    try:
        html, resp = generate_preview(
            project.idea,
            project.name,
            _build_context(project),
            mode=RoutingMode(project.routing_mode),
            preferred_model=project.preferred_model,
        )
    except ProviderError as e:
        raise _fail(e, "generating the preview")
    if "<" not in html:
        raise HTTPException(502, "The model did not return usable HTML. Try again or switch model.")
    _save_revision(db, project, html, source="generated", resp=resp)
    return _preview_out(db, project)


@router.post("/{project_id}/preview/edit", response_model=PreviewOut)
def edit(
    payload: PreviewEditRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    revs = _revisions(db, project.id)
    if not revs:
        raise HTTPException(400, "No preview to edit yet — generate one first.")
    current = revs[0].html
    fragment = extract_section(current, payload.section_id)
    if fragment is None:
        raise HTTPException(400, f"Section '{payload.section_id}' isn't in the current preview.")
    try:
        new_fragment, resp = edit_section(
            fragment,
            payload.instruction,
            mode=RoutingMode(project.routing_mode),
            preferred_model=project.preferred_model,
        )
    except ProviderError as e:
        raise _fail(e, "editing the section")
    if "<" not in new_fragment:
        raise HTTPException(502, "The model didn't return a usable section. Try rephrasing the change.")
    new_html = replace_section(current, payload.section_id, new_fragment)
    _save_revision(
        db,
        project,
        new_html,
        source="edited",
        resp=resp,
        section_id=payload.section_id,
        instruction=payload.instruction,
    )
    return _preview_out(db, project)


@router.post("/{project_id}/preview/undo", response_model=PreviewOut)
def undo(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    revs = _revisions(db, project.id)
    if not revs:
        raise HTTPException(400, "Nothing to undo — no preview revisions yet.")
    db.delete(revs[0])
    db.commit()
    return _preview_out(db, project)
