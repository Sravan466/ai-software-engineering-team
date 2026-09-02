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


class PreviewOut(BaseModel):
    project_id: str
    html: Optional[str] = None  # newest revision's document, or None if never generated
    sections: List[SectionOut] = []
    revisions: List[RevisionOut] = []
    has_frontend: bool = False  # whether the Frontend phase has run (richer preview if so)
