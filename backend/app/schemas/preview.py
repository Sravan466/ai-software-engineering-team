"""Request/response schemas for the visual-preview API."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.project import UtcDatetime


class PreviewEditRequest(BaseModel):
    section_id: Optional[str] = Field(None, min_length=1, description="data-section id of the block to edit.")
    #: One element, by its `data-oid`. When it is a section's own element this is the
    #: same as `section_id`; otherwise the model rewrites only that element.
    oid: Optional[str] = Field(None, min_length=1, max_length=32)
    instruction: str = Field(..., min_length=1, max_length=2000, description="Plain-language change to apply.")


class PatchOp(BaseModel):
    """One change the server can make without a model."""

    oid: str = Field(..., min_length=1, max_length=32)
    kind: Literal["text", "classes", "attr"]
    text: Optional[str] = Field(None, max_length=5000)
    add: List[str] = Field(default_factory=list, max_length=40)
    remove: List[str] = Field(default_factory=list, max_length=80)
    name: Optional[str] = Field(None, max_length=32)
    value: Optional[str] = Field(None, max_length=2000)


class PreviewPatchRequest(BaseModel):
    ops: List[PatchOp] = Field(..., min_length=1, max_length=200)
    #: A few words for the history, e.g. "Heading: text, size".
    summary: Optional[str] = Field(None, max_length=200)


class PreviewThemeRequest(BaseModel):
    primary: Optional[str] = Field(None, max_length=16)
    accent: Optional[str] = Field(None, max_length=16)
    tint: Optional[str] = Field(None, max_length=16)
    font_pair: Optional[str] = Field(None, max_length=32)
    radius: Optional[str] = Field(None, max_length=16)
    shadow: Optional[str] = Field(None, max_length=16)
    density: Optional[str] = Field(None, max_length=16)


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

    parent_id: Optional[str] = None

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
    #: The revision on screen. Undo and redo move it; nothing is deleted.
    head_id: Optional[str] = None
    can_undo: bool = False
    can_redo: bool = False
    #: The site's recorded design tokens and the choices Site style offers. None for a
    #: single-page mockup drawn before sites, which cannot be restyled in place.
    theme: Optional[dict] = None
