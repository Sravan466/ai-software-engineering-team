"""Request/response schemas for the visual-preview API."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.project import UtcDatetime


#: Which preview a change is for (#78): the running app — the change is made to the
#: code — or the sketch, whose HTML is all there is to change.
Target = Literal["sketch", "app"]


class PreviewEditRequest(BaseModel):
    section_id: Optional[str] = Field(None, min_length=1, description="data-section id of the block to edit.")
    #: One element, by its `data-oid` — or, on the app, its `data-src` address
    #: (`frontend/components/Navbar.tsx:12:5`). When it is a section's own element this
    #: is the same as `section_id`; otherwise the model rewrites only that element.
    oid: Optional[str] = Field(None, min_length=1, max_length=300)
    instruction: str = Field(..., min_length=1, max_length=2000, description="Plain-language change to apply.")
    target: Target = "sketch"
    #: The Frontend attempt the app on screen was built from: a change aimed at code
    #: that has since moved on is refused rather than applied in the wrong place.
    built_from: Optional[str] = Field(None, max_length=32)


class PatchOp(BaseModel):
    """One change the server can make without a model."""

    oid: str = Field(..., min_length=1, max_length=300)
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
    target: Target = "sketch"
    built_from: Optional[str] = Field(None, max_length=32)


class PreviewThemeRequest(BaseModel):
    primary: Optional[str] = Field(None, max_length=16)
    accent: Optional[str] = Field(None, max_length=16)
    tint: Optional[str] = Field(None, max_length=16)
    font_pair: Optional[str] = Field(None, max_length=32)
    radius: Optional[str] = Field(None, max_length=16)
    shadow: Optional[str] = Field(None, max_length=16)
    density: Optional[str] = Field(None, max_length=16)
    target: Target = "sketch"
    built_from: Optional[str] = Field(None, max_length=32)


class AppStartRequest(BaseModel):
    #: Build again an attempt whose last preview build failed.
    retry: bool = False


class AppEditOut(BaseModel):
    """A change made on the app preview: being applied, applied, or refused."""

    kind: str
    label: str = ""
    files: List[str] = []
    #: running | landed | refused
    status: str
    reason: Optional[str] = None
    at: Optional[str] = None
    #: Whether the crew is still applying it — the build runs until it is checked.
    active: bool = False


class AppOut(BaseModel):
    """The generated app, running (#78) — or why it isn't."""

    #: none (no frontend yet) | unavailable (can't run here) | idle | starting |
    #: running | waiting (the crew is changing the frontend) | failed
    status: str
    #: Where the frame loads it: an origin of its own. Only while it runs.
    url: Optional[str] = None
    built_from: Optional[str] = None
    built_at: Optional[str] = None
    current_from: Optional[str] = None
    #: Running an older attempt than the current one — a newer one is starting.
    stale: bool = False
    step: Optional[str] = None
    reason: Optional[str] = None
    problems: List[dict] = []
    stack: Optional[str] = None
    routes: List["RouteOut"] = []
    #: {status: none|installing|starting|up|down, why}
    backend: dict = {}
    #: Elements traced to their place in the code; why none are, when none are.
    traced: int = 0
    trace_note: Optional[str] = None
    ttl_seconds: int = 0
    #: A failed start: the code's fault (it didn't build) or the sandbox's (it couldn't run).
    fault: Optional[str] = None
    editing: Optional[AppEditOut] = None
    can_edit: bool = False
    edit_block: Optional[str] = None
    can_undo: bool = False
    can_redo: bool = False
    theme: Optional[dict] = None


class LocateOut(BaseModel):
    """Where an element on the app comes from, and what can change it here."""

    path: str
    line: int
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    tag: Optional[str] = None
    #: Written by the platform (or another phase), so the crew's Frontend can't change it.
    owner: Optional[str] = None
    text: dict = {}
    classes: dict = {}
    why: Optional[str] = None


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
    #: What the Preview tab shows (#78): the running app, or the sketch — and why.
    source: Optional[Target] = None
    source_note: Optional[str] = None
    app: Optional[AppOut] = None
    #: The Frontend attempt the sketch was drawn from, and whether that is older code
    #: than the current attempt.
    sketch_built_from: Optional[str] = None
    sketch_stale: bool = False


AppOut.model_rebuild()
