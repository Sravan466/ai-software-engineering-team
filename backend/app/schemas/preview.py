"""Request/response schemas for the visual-preview API."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.project import UtcDatetime


class PreviewEditRequest(BaseModel):
    section_id: str = Field(..., min_length=1, description="data-section id of the block to edit.")
    instruction: str = Field(..., min_length=1, description="Plain-language change to apply.")


class RevisionOut(BaseModel):
    id: str
    source: str
    section_id: Optional[str] = None
    instruction: Optional[str] = None
    model_used: Optional[str] = None
    provider_used: Optional[str] = None
    # Stamped UTC like every other timestamp this API returns. Without it SQLite's
    # naive value serialises with no offset, the browser reads it as *local* time,
    # and on a UTC+5:30 machine a mockup drawn two minutes ago looks five hours older
    # than the phase it depicts — which is exactly how the Ship review came to warn
    # that a fresh picture was out of date.
    created_at: UtcDatetime

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class SectionOut(BaseModel):
    id: str
    label: str
    #: The page this section is on; None for the shared header/footer, and for every
    #: section of a single-page mockup drawn before sites existed.
    route: Optional[str] = None
    kind: Optional[str] = None


class RouteOut(BaseModel):
    path: str
    title: str


class JobOut(BaseModel):
    """A mockup build in flight — or one that failed, and why."""

    stage: str
    label: str
    done: int = 0
    total: int = 0
    detail: str = ""
    error: Optional[str] = None
    running: bool = True
    origin: str = "request"
    elapsed_s: int = 0


class PreviewOut(BaseModel):
    project_id: str
    html: Optional[str] = None  # newest revision's document, or None if never generated
    sections: List[SectionOut] = []
    #: The mockup's pages, in order. Empty for a single-page mockup.
    routes: List[RouteOut] = []
    revisions: List[RevisionOut] = []
    has_frontend: bool = False  # whether the Frontend phase has run (richer preview if so)
    #: What building the current mockup found — pages, sections, checks — taken from
    #: the newest revision that has one (an edit inherits the build it edited).
    report: Optional[dict] = None
    #: A build that is running now, or the last one that failed.
    job: Optional[JobOut] = None
