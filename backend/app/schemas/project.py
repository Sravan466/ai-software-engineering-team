"""Request/response schemas for the projects & pipeline API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.constants import RoutingMode


class ProjectCreate(BaseModel):
    idea: str = Field(..., min_length=3, description="The product idea to build.")
    name: Optional[str] = None
    routing_mode: Optional[RoutingMode] = None
    preferred_model: Optional[str] = Field(
        None, description="provider:model for Manual mode, e.g. 'anthropic:claude-opus-4-8'."
    )
    require_approval: Optional[bool] = None


class PhaseResultOut(BaseModel):
    id: str
    phase: str
    agent: str
    status: str
    output: dict
    content_md: str
    model_used: Optional[str] = None
    provider_used: Optional[str] = None
    feedback: Optional[str] = None
    created_at: datetime

    # from_attributes for ORM; disable the 'model_' protected namespace (we use model_used).
    model_config = {"from_attributes": True, "protected_namespaces": ()}


class ProjectOut(BaseModel):
    id: str
    idea: str
    name: Optional[str]
    status: str
    current_phase: Optional[str]
    routing_mode: str
    preferred_model: Optional[str]
    require_approval: bool
    created_at: datetime
    updated_at: datetime
    phases: list[PhaseResultOut] = []

    model_config = {"from_attributes": True}


class ApprovalRequest(BaseModel):
    feedback: Optional[str] = Field(
        None, description="Optional guidance, required-ish when rejecting."
    )


class RunResponse(BaseModel):
    project_id: str
    status: str
    current_phase: Optional[str]
    message: str
