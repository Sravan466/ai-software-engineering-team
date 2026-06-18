"""Usage & cost analytics.

Records one UsageEvent per LLM call and aggregates them for the dashboard:
token usage, estimated $ cost, per-provider/per-model breakdown, fallback rate.
"""
from __future__ import annotations
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import UsageEvent
from app.router.registry import estimate_cost
from app.schemas.llm import LLMResponse


def record(
    db: Session,
    *,
    response: LLMResponse,
    project_id: Optional[str] = None,
    phase: Optional[str] = None,
) -> UsageEvent:
    cost = estimate_cost(
        response.model, response.usage.prompt_tokens, response.usage.completion_tokens
    )
    event = UsageEvent(
        project_id=project_id,
        phase=phase,
        provider=response.provider,
        model=response.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        total_tokens=response.usage.total_tokens,
        cost_usd=cost,
        latency_ms=response.latency_ms,
        fallback_used=response.fallback_used,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def summary(db: Session, project_id: Optional[str] = None) -> dict:
    """Aggregate analytics. If project_id is given, scope to that project."""
    base = select(UsageEvent)
    if project_id:
        base = base.where(UsageEvent.project_id == project_id)
    events = db.execute(base).scalars().all()

    total_tokens = sum(e.total_tokens for e in events)
    total_cost = sum(e.cost_usd for e in events)
    calls = len(events)
    fallback_calls = sum(1 for e in events if e.fallback_used)
    avg_latency = round(sum(e.latency_ms for e in events) / calls, 1) if calls else 0.0

    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for e in events:
        p = by_provider.setdefault(e.provider, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
        p["calls"] += 1
        p["tokens"] += e.total_tokens
        p["cost_usd"] = round(p["cost_usd"] + e.cost_usd, 6)

        m = by_model.setdefault(e.model, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
        m["calls"] += 1
        m["tokens"] += e.total_tokens
        m["cost_usd"] = round(m["cost_usd"] + e.cost_usd, 6)

    return {
        "calls": calls,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "avg_latency_ms": avg_latency,
        "fallback_rate": round(fallback_calls / calls, 3) if calls else 0.0,
        "by_provider": by_provider,
        "by_model": by_model,
    }


def project_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(UsageEvent)).scalar_one()
