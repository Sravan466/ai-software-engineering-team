"""Analytics dashboard data: usage, cost, model/agent breakdowns."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import tracker
from app.db.base import get_db
from app.db.models import DebateRecord

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/summary")
def overall_summary(db: Session = Depends(get_db)) -> dict:
    return tracker.summary(db)


@router.get("/projects/{project_id}")
def project_summary(project_id: str, db: Session = Depends(get_db)) -> dict:
    return tracker.summary(db, project_id=project_id)


@router.get("/projects/{project_id}/debates")
def project_debates(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(DebateRecord)
        .where(DebateRecord.project_id == project_id)
        .order_by(DebateRecord.created_at)
    ).scalars()
    return [
        {
            "id": r.id,
            "topic": r.topic,
            "arguments": r.arguments,
            "decision": r.decision,
            "rationale": r.rationale,
            "created_at": r.created_at,
        }
        for r in rows
    ]
