"""ORM models: projects, phase results, debates, analytics, knowledge docs."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    idea: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Pipeline state
    status: Mapped[str] = mapped_column(String(32), default="created")  # see PipelineStatus
    current_phase: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)

    # Routing config chosen for this project
    routing_mode: Mapped[str] = mapped_column(String(16), default="local_only")
    preferred_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    require_approval: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    phases: Mapped[list["PhaseResult"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="PhaseResult.created_at"
    )
    preview_revisions: Mapped[list["PreviewRevision"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PreviewRevision.created_at",
    )


class PhaseResult(Base):
    """Output of a single agent/phase, plus its approval state."""

    __tablename__ = "phase_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    phase: Mapped[str] = mapped_column(String(48), nullable=False)
    agent: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")

    # Structured output the agent produced (dict) + the human-readable markdown.
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    content_md: Mapped[str] = mapped_column(Text, default="")

    # Which model actually produced it (after routing/fallback).
    model_used: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_used: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Human feedback when rejected.
    feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="phases")


class PreviewRevision(Base):
    """One version of the project's visual HTML preview (a self-contained mockup).

    Revisions form an append-only history per project: the newest row is the live
    preview, and `undo` simply drops the latest row. `source` records how it came to be
    (a full regenerate vs a single-section edit); `section_id`/`instruction` capture which
    section a user edited and what they asked for.
    """

    __tablename__ = "preview_revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    html: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="generated")  # generated | edited

    # Set only for single-section edits.
    section_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    instruction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Which model produced this revision (after routing/fallback).
    model_used: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_used: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="preview_revisions")


class DebateRecord(Base):
    """A recorded debate between agents and the platform's verdict."""

    __tablename__ = "debates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(256))
    arguments: Mapped[list] = mapped_column(JSON, default=list)  # [{agent, position, rationale}]
    decision: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageEvent(Base):
    """One LLM call's token usage and estimated cost — powers the analytics dashboard."""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    phase: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    fallback_used: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class KnowledgeDoc(Base):
    """Metadata for an uploaded RAG document (chunks live in ChromaDB)."""

    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
