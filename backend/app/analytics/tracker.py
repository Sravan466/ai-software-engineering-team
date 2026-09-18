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
    # None means nobody knows what this model costs, which is emphatically not the
    # same as free. The column keeps 0.0 so every existing sum still works; the flag
    # beside it is what says whether that zero is a price or a gap.
    cost = estimate_cost(
        response.model,
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        provider=response.provider,
    )
    event = UsageEvent(
        project_id=project_id,
        phase=phase,
        provider=response.provider,
        model=response.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        total_tokens=response.usage.total_tokens,
        cost_usd=cost if cost is not None else 0.0,
        cost_known=cost is not None,
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

    # Calls whose model has no published price. Their `cost_usd` is zero because the
    # column has to hold a number, not because they were free — so the total below is
    # a floor, and saying which models it is missing is what stops "$0.00" reading as
    # "this cost nothing" when it means "nobody priced it".
    unpriced = [e for e in events if e.cost_known is False]

    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for e in events:
        p = by_provider.setdefault(
            e.provider, {"calls": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0}
        )
        p["calls"] += 1
        p["tokens"] += e.total_tokens
        p["cost_usd"] = round(p["cost_usd"] + e.cost_usd, 6)
        if e.cost_known is False:
            p["unpriced_calls"] += 1

        m = by_model.setdefault(
            e.model, {"calls": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0}
        )
        m["calls"] += 1
        m["tokens"] += e.total_tokens
        m["cost_usd"] = round(m["cost_usd"] + e.cost_usd, 6)
        if e.cost_known is False:
            m["unpriced_calls"] += 1

    return {
        "calls": calls,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "avg_latency_ms": avg_latency,
        "fallback_rate": round(fallback_calls / calls, 3) if calls else 0.0,
        "by_provider": by_provider,
        "by_model": by_model,
        #: How many calls the total above could not price, and on which models. When
        #: this is non-empty the total is a lower bound, and the UI says so.
        "unpriced_calls": len(unpriced),
        "unpriced_models": sorted({e.model for e in unpriced}),
    }


def project_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(UsageEvent)).scalar_one()
