"""Build a project's mockup and keep it — shared by the Preview tab and the pipeline.

Two things draw a mockup: a person pressing Generate, and the Frontend phase finishing.
They used to be two copies of the same four steps; they are now one, with the one
difference between them — the pipeline must not keep a picture of a front end that
was replaced while it was drawing — passed in as a question to ask before saving.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from app.analytics import tracker
from app.core.constants import RoutingMode
from app.db.models import PreviewRevision, Project
from app.preview.brief import site_brief
from app.preview.generator import BuildResult, build_site
from app.preview.jobs import Reporter
from app.router.base import ProviderError
from app.schemas.llm import LLMResponse


def build_for(project: Project, reporter: Optional[Reporter] = None) -> BuildResult:
    return build_site(
        site_brief(project),
        mode=RoutingMode(project.routing_mode),
        preferred_model=project.preferred_model,
        progress=reporter,
    )


def bill(db: Session, project: Project, responses: Iterable[LLMResponse]) -> None:
    """One usage event per call — whether or not the build it was part of was kept."""
    for resp in responses:
        tracker.record(db, response=resp, project_id=project.id, phase="preview")


def save(db: Session, project: Project, result: BuildResult) -> PreviewRevision:
    """Keep a finished build as the newest revision, and bill every call it made."""
    row = PreviewRevision(
        project_id=project.id,
        html=result.html,
        source="generated",
        model_used=result.report.get("model"),
        provider_used=result.report.get("provider"),
        report=result.report,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    bill(db, project, result.responses)
    return row


def build_and_save(
    db: Session,
    project: Project,
    reporter: Optional[Reporter] = None,
    *,
    still_wanted: Callable[[Session, Project], bool] = lambda _db, _project: True,
) -> Optional[PreviewRevision]:
    """Build, then save only if `still_wanted` says the picture is still of something.

    Every call is billed on every path out. A build thrown away because the front end
    was replaced mid-draw, or cut short by a provider failure, spent real tokens —
    and those are the most expensive builds to leave out of a project's cost.
    """
    try:
        result = build_for(project, reporter)
    except ProviderError as e:
        bill(db, project, getattr(e, "responses", []))
        raise
    db.refresh(project)
    if not still_wanted(db, project):
        bill(db, project, result.responses)
        return None
    return save(db, project, result)
