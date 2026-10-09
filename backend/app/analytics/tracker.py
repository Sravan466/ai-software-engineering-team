"""Usage & cost analytics.

Records one UsageEvent per LLM call and aggregates them for the dashboard:
token usage, estimated $ cost, per-provider/per-model breakdown, fallback rate.
"""
from __future__ import annotations
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import identity
from app.core.constants import PHASE_ORDER, BuildStatus, PhaseStatus, SchemaStatus
from app.db.models import PhaseResult, Project, UsageEvent
from app.router.registry import estimate_cost
from app.schemas.llm import LLMResponse


def record(
    db: Session,
    *,
    response: LLMResponse,
    project_id: Optional[str] = None,
    phase: Optional[str] = None,
    commit: bool = True,
) -> UsageEvent:
    """Record one call. `commit=False` leaves it in the caller's transaction, so a
    phase's row and the calls that produced it are written together or not at all."""
    # Whose call this was: the project's owner, which is who the run was bound to.
    project = db.get(Project, project_id) if project_id else None
    owner_id = project.owner_id if project is not None else identity.current_user_id()
    # None means nobody knows what this model costs, which is emphatically not the
    # same as free. The column keeps 0.0 so every existing sum still works; the flag
    # beside it is what says whether that zero is a price or a gap.
    cost = estimate_cost(
        response.model,
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        provider=response.provider,
        is_local=response.is_local,
    )
    event = UsageEvent(
        owner_id=owner_id,
        project_id=project_id,
        phase=phase,
        provider=response.provider,
        model=response.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        total_tokens=response.usage.total_tokens,
        cost_usd=cost if cost is not None else 0.0,
        cost_known=cost is not None,
        is_local=response.is_local,
        latency_ms=response.latency_ms,
        fallback_used=response.fallback_used,
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    return event


def summary(db: Session, owner_id: str, project_id: Optional[str] = None) -> dict:
    """Aggregate one account's analytics. If project_id is given, scope to that project."""
    base = select(UsageEvent).where(UsageEvent.owner_id == owner_id)
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
    #: Calls that ran on the user's own hardware, as each call recorded it.
    local_calls = sum(1 for e in events if e.is_local)

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
        "local_calls": local_calls,
        "by_provider": by_provider,
        "by_model": by_model,
        #: How many calls the total above could not price, and on which models. When
        #: this is non-empty the total is a lower bound, and the UI says so.
        "unpriced_calls": len(unpriced),
        "unpriced_models": sorted({e.model for e in unpriced}),
    }


def crew(db: Session, owner_id: str) -> dict:
    """Each phase's record across one account's builds: what that agent has done for them.

    Two sources, because neither answers alone. The calls (`UsageEvent`, tagged with
    the phase that made them) say what the work cost; the phase rows say how it went:
    approved, sent back, repaired, whether its code built. An account with no builds
    gets every phase at zero rather than an error, and the page shows dashes.
    """
    keys = [p.value for p in PHASE_ORDER]
    out: dict[str, dict] = {
        k: {
            "builds": 0,
            "calls": 0,
            "tokens": 0,
            "cost_usd": 0.0,
            "unpriced_calls": 0,
            "avg_latency_ms": 0.0,
            "local_calls": 0,
            # Calls that recorded where they ran; the share is over these, not all calls.
            "located_calls": 0,
            "local_share": None,
            "approved": 0,
            "rejected": 0,
            "failed": 0,
            "schema_repaired": 0,
            "schema_invalid": 0,
            "build_ok": 0,
            "build_failed": 0,
        }
        for k in keys
    }

    latency = {k: 0 for k in keys}
    events = db.execute(
        select(UsageEvent).where(UsageEvent.owner_id == owner_id, UsageEvent.phase.in_(keys))
    ).scalars()
    for e in events:
        rec = out[e.phase]
        rec["calls"] += 1
        rec["tokens"] += e.total_tokens
        rec["cost_usd"] = round(rec["cost_usd"] + e.cost_usd, 6)
        latency[e.phase] += e.latency_ms
        if e.cost_known is False:
            rec["unpriced_calls"] += 1
        if e.is_local is not None:
            rec["located_calls"] += 1
            if e.is_local:
                rec["local_calls"] += 1

    rows = db.execute(
        select(PhaseResult)
        .join(Project, Project.id == PhaseResult.project_id)
        .where(Project.owner_id == owner_id, PhaseResult.phase.in_(keys))
    ).scalars()
    touched: dict[str, set] = {k: set() for k in keys}
    for r in rows:
        # A restored version (#79) copies its rows. The copy is the same work, not more.
        if isinstance(r.handoff, dict) and r.handoff.get("restored_row"):
            continue
        rec = out[r.phase]
        touched[r.phase].add(r.project_id)
        if r.status == PhaseStatus.APPROVED.value:
            rec["approved"] += 1
        elif r.status == PhaseStatus.REJECTED.value:
            rec["rejected"] += 1
        elif r.status == PhaseStatus.FAILED.value:
            rec["failed"] += 1
        if r.schema_status == SchemaStatus.REPAIRED.value:
            rec["schema_repaired"] += 1
        elif r.schema_status == SchemaStatus.INVALID.value:
            rec["schema_invalid"] += 1
        if r.build_status == BuildStatus.OK.value:
            rec["build_ok"] += 1
        elif r.build_status == BuildStatus.FAILED.value:
            rec["build_failed"] += 1

    for k, rec in out.items():
        rec["builds"] = len(touched[k])
        if rec["calls"]:
            rec["avg_latency_ms"] = round(latency[k] / rec["calls"], 1)
        if rec["located_calls"]:
            rec["local_share"] = round(rec["local_calls"] / rec["located_calls"], 3)
    return {"phases": out}


def project_count(db: Session, owner_id: str) -> int:
    return db.execute(
        select(func.count()).select_from(UsageEvent).where(UsageEvent.owner_id == owner_id)
    ).scalar_one()
