"""Shared enums and the canonical pipeline phase ordering."""
from __future__ import annotations
from typing import Optional

from enum import Enum


class RoutingMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    LOCAL_ONLY = "local_only"


class PipelineStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Stopped by the reviewer. Resumable from the last checkpoint.
    CANCELLED = "cancelled"


class PhaseStatus(str, Enum):
    #: The agent is generating right now — no output yet. The row exists so the UI
    #: can show which agent has the work and for how long.
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: The phase was abandoned mid-flight (cancelled, or the server restarted).
    FAILED = "failed"


class Phase(str, Enum):
    """Pipeline phases in execution order. Value == agent key."""

    PRODUCT_MANAGER = "product_manager"
    SYSTEM_DESIGN = "system_design"
    BACKEND_ENGINEER = "backend_engineer"
    FRONTEND_ENGINEER = "frontend_engineer"
    QA_ENGINEER = "qa_engineer"
    SECURITY_ENGINEER = "security_engineer"
    DEVOPS_ENGINEER = "devops_engineer"
    COST_ESTIMATION = "cost_estimation"


# Ordered list driving the LangGraph edges and the UI progress tracker.
PHASE_ORDER: list[Phase] = [
    Phase.PRODUCT_MANAGER,
    Phase.SYSTEM_DESIGN,
    Phase.BACKEND_ENGINEER,
    Phase.FRONTEND_ENGINEER,
    Phase.QA_ENGINEER,
    Phase.SECURITY_ENGINEER,
    Phase.DEVOPS_ENGINEER,
    Phase.COST_ESTIMATION,
]

PHASE_LABELS: dict[str, str] = {
    Phase.PRODUCT_MANAGER.value: "Product Requirements",
    Phase.SYSTEM_DESIGN.value: "Architecture Design",
    Phase.BACKEND_ENGINEER.value: "Backend Code",
    Phase.FRONTEND_ENGINEER.value: "Frontend Code",
    Phase.QA_ENGINEER.value: "Test Cases",
    Phase.SECURITY_ENGINEER.value: "Security Review",
    Phase.DEVOPS_ENGINEER.value: "Deployment Plan",
    Phase.COST_ESTIMATION.value: "Cost Estimation",
}


def next_phase(current: Optional[str]) -> Optional[Phase]:
    """Return the phase after `current`, or the first phase if current is None."""
    if current is None:
        return PHASE_ORDER[0]
    try:
        idx = [p.value for p in PHASE_ORDER].index(current)
    except ValueError:
        return None
    return PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else None
