"""ORM models: projects, phase results, debates, analytics, knowledge docs."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.constants import ApprovalMode
from app.db.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes even for `DateTime(timezone=True)` columns.

    Everything written here is UTC, so re-attach the timezone rather than letting a
    naive/aware comparison raise in the middle of a status check.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    idea: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Pipeline state
    status: Mapped[str] = mapped_column(String(32), default="created")  # see PipelineStatus
    current_phase: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)

    # When the phase named by `current_phase` started generating. Set *before* the
    # agent runs, so the UI can show elapsed time while a phase is in flight.
    phase_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Written every few seconds by the live runner. A `running` project whose
    # heartbeat has gone quiet is stalled — see `stalled` below.
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set by Stop. The runner checks it between phases and after each agent returns.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Why the last run stopped, in words a person can act on.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Routing config chosen for this project
    routing_mode: Mapped[str] = mapped_column(String(16), default="local_only")
    preferred_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    require_approval: Mapped[bool] = mapped_column(default=True)

    # ── review policy (mutable mid-run) ──────────────────────────────────────
    # How often this run stops for a person. Nullable so a database written before
    # the policy existed still reads correctly — see `effective_approval_mode`.
    approval_mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: Projected monthly run cost, in USD, above which Ledger interrupts the build.
    cost_cap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Why the pipeline is currently waiting — set when it parks at a gate, cleared
    # when it moves. The reviewer is shown a different surface for each.
    gate_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    gate_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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

    # ── derived state ────────────────────────────────────────────────────────
    @property
    def effective_approval_mode(self) -> str:
        """The review policy this run actually follows.

        `approval_mode` is nullable on purpose: rows created before it existed have
        only `require_approval`, and those runs were gated at *every* phase. Reading
        them as `checkpoints` would silently drop gates a person was relying on, so
        an unset column keeps the old meaning and only new runs get the new default.
        """
        if self.approval_mode:
            return self.approval_mode
        return (
            ApprovalMode.EVERY_PHASE.value
            if self.require_approval
            else ApprovalMode.UNATTENDED.value
        )

    @property
    def stalled(self) -> bool:
        """True when this run says `running` but nothing is actually driving it.

        Background tasks die with the process, so a `running` row that outlives its
        server is unrecoverable on its own — and used to poll forever. A missing
        heartbeat is proof there is no live runner: only the runner writes one.
        """
        if self.status != "running":
            return False
        beat = _aware(self.heartbeat_at) or _aware(self.phase_started_at)
        if beat is None:
            return True
        return (_now() - beat).total_seconds() > settings.stall_after_seconds

    @property
    def elapsed_seconds(self) -> Optional[float]:
        """Seconds the current phase has been generating, or None when idle."""
        started = _aware(self.phase_started_at)
        if self.status != "running" or started is None:
            return None
        return max(0.0, (_now() - started).total_seconds())


class PhaseResult(Base):
    """Output of a single agent/phase, plus its approval state."""

    __tablename__ = "phase_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    phase: Mapped[str] = mapped_column(String(48), nullable=False)
    agent: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")

    # A row is created the moment the agent starts (status `running`) and filled in
    # when it finishes, so "which agent has the work, since when" is always answerable.
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

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
