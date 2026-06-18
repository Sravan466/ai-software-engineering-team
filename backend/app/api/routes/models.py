"""Model routing introspection — what providers/models are available, and the diagram."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.constants import PHASE_LABELS, PHASE_ORDER
from app.orchestration.graph import mermaid_diagram
from app.router.router import router as model_router

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/status")
def router_status() -> dict:
    """Provider availability + configured defaults + fallback chain."""
    return model_router.status()


@router.get("/pipeline")
def pipeline_shape() -> dict:
    """The phase order, labels, and a Mermaid diagram of the LangGraph pipeline."""
    return {
        "phases": [
            {"key": p.value, "label": PHASE_LABELS.get(p.value, p.value), "order": i}
            for i, p in enumerate(PHASE_ORDER)
        ],
        "mermaid": mermaid_diagram(),
    }
