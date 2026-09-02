"""Request/response schemas for the projects & pipeline API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, BeforeValidator, Field
from typing_extensions import Annotated

from app.core.constants import RoutingMode


def _as_utc(value: object) -> object:
    """Stamp naive timestamps as UTC before they leave the API.

    Every datetime written here is UTC, but SQLite hands back naive values even for
    `DateTime(timezone=True)` columns — so they serialised without an offset, and
    `Date.parse` in the browser reads an offset-less ISO string as *local* time.
    On a UTC+5:30 machine that turned a phase running for 47 seconds into one
    running for 5h31m, and skewed every relative timestamp in the sidebar.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


#: A datetime that always reaches the client with an explicit UTC offset.
UtcDatetime = Annotated[datetime, BeforeValidator(_as_utc)]


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
    created_at: UtcDatetime

    # Timing, so a phase in flight can show elapsed time and a finished one can show
    # what it actually cost in wall-clock and tokens.
    started_at: Optional[UtcDatetime] = None
    completed_at: Optional[UtcDatetime] = None
    total_tokens: int = 0
    latency_ms: int = 0

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
    created_at: UtcDatetime
    updated_at: UtcDatetime
    phases: list[PhaseResultOut] = []

    # ── liveness ─────────────────────────────────────────────────────────────
    phase_started_at: Optional[UtcDatetime] = None
    heartbeat_at: Optional[UtcDatetime] = None
    cancel_requested: bool = False
    last_error: Optional[str] = None
    #: `running` but nothing is driving it — the run died with its process. Computed
    #: server-side so the client never has to guess a threshold.
    stalled: bool = False
    #: Seconds the current phase has been generating (None when idle).
    elapsed_seconds: Optional[float] = None

    model_config = {"from_attributes": True}


class ApprovalRequest(BaseModel):
    feedback: Optional[str] = Field(
        None, description="Optional guidance, required-ish when rejecting."
    )


class StopRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Why the run was stopped.")


class RunResponse(BaseModel):
    project_id: str
    status: str
    current_phase: Optional[str]
    message: str
