"""Pipeline runner: drives the LangGraph through approval gates and mirrors state to the DB.

The compiled graph is the execution engine and source of truth for *pipeline position*
(via its SQLite checkpointer, keyed by thread_id = project_id). This runner advances it one
phase at a time, persists each phase output as a `PhaseResult`, records usage/debates, and
finalises the project (writing a long-term memory summary) when the graph reaches END.
"""
from __future__ import annotations
from typing import Optional

import threading

from sqlalchemy.orm import Session

from app.agents import get_agent
from app.agents.base import AgentContext
from app.analytics import tracker
from app.core.constants import PHASE_ORDER, PhaseStatus, PipelineStatus, RoutingMode
from app.core.logging import get_logger
from app.db.models import DebateRecord, PhaseResult, Project
from app.memory.store import memory_store
from app.orchestration.graph import graph
from app.orchestration.state import PipelineState
from app.schemas.llm import LLMResponse, Usage

log = get_logger(__name__)

# Serialise access to the (single-connection) checkpointer.
_lock = threading.Lock()


def _config(project_id: str) -> dict:
    return {"configurable": {"thread_id": project_id}}


def _initial_state(project: Project) -> PipelineState:
    return PipelineState(
        project_id=project.id,
        idea=project.idea,
        routing_mode=project.routing_mode,
        preferred_model=project.preferred_model,
        prior_outputs={},
        feedback={},
        debates=[],
    )


class PipelineRunner:
    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self, db: Session, project: Project, *, pause: bool = True) -> Project:
        """Kick off the pipeline (runs the first phase). Pauses for approval unless
        ``pause`` is False (auto-run keeps the project RUNNING instead)."""
        project.status = PipelineStatus.RUNNING.value
        db.commit()
        with _lock:
            state = graph.invoke(_initial_state(project), _config(project.id))
        self._persist_step(db, project, state, pause=pause)
        return project

    def advance(self, db: Session, project: Project, *, pause: bool = True) -> Project:
        """Resume after an approval — run the next phase. Pauses for approval unless
        ``pause`` is False (auto-run keeps the project RUNNING instead)."""
        with _lock:
            state = graph.invoke(None, _config(project.id))
        self._persist_step(db, project, state, pause=pause)
        return project

    def run_to_completion(self, db: Session, project: Project) -> Project:
        """Used when approvals are disabled: auto-approve every phase to the end.

        The project stays RUNNING (never AWAITING_APPROVAL) for the whole background
        run, so the UI shows a live pipeline rather than a phantom approval gate it
        would then fail to approve. Driven by phase position, not status.
        """
        self.start(db, project, pause=False)
        while project.current_phase and project.current_phase != PHASE_ORDER[-1].value:
            prev = project.current_phase
            self._mark_latest(db, project, PhaseStatus.APPROVED.value)
            self.advance(db, project, pause=False)
            if project.current_phase == prev:
                break  # graph produced no new phase — avoid spinning
        # Approve the final phase and finalise -> COMPLETED.
        self._mark_latest(db, project, PhaseStatus.APPROVED.value)
        self._finalize(db, project)
        return project

    # ── approvals ───────────────────────────────────────────────────────────
    def approve(self, db: Session, project: Project) -> Project:
        """Approve the current phase. If it's the final phase, finalise the project;
        otherwise advance to the next phase (which pauses for its own approval)."""
        self._mark_latest(db, project, PhaseStatus.APPROVED.value)
        if project.current_phase == PHASE_ORDER[-1].value:
            self._finalize(db, project)
            return project
        return self.advance(db, project)

    def reject(self, db: Session, project: Project, feedback: str) -> Project:
        """Re-run the current phase with reviewer feedback, patching the checkpoint."""
        phase_key = project.current_phase
        if not phase_key:
            return project

        cfg = _config(project.id)
        with _lock:
            snapshot = graph.get_state(cfg)
            values: PipelineState = dict(snapshot.values)  # type: ignore[assignment]

            agent = get_agent(phase_key)
            ctx = AgentContext(
                idea=values["idea"],
                routing_mode=RoutingMode(values.get("routing_mode", "local_only")),
                preferred_model=values.get("preferred_model"),
                prior_outputs={
                    k: v for k, v in values.get("prior_outputs", {}).items() if k != phase_key
                },
                feedback=feedback,
            )
            result = agent.run(ctx)

            new_outputs = {**values.get("prior_outputs", {}), phase_key: result.output}
            from app.orchestration.graph import _serialize_result

            last_result = _serialize_result(phase_key, agent.title, result)
            graph.update_state(
                cfg,
                {
                    "prior_outputs": new_outputs,
                    "last_phase": phase_key,
                    "last_result": last_result,
                    "feedback": {**values.get("feedback", {}), phase_key: feedback},
                },
            )

        # Overwrite the pending phase row with the corrected output.
        self._mark_latest(db, project, PhaseStatus.REJECTED.value, feedback=feedback)
        self._save_phase_result(db, project, last_result, status=PhaseStatus.PENDING_APPROVAL.value)
        self._record_usage(db, project, last_result)
        project.status = PipelineStatus.AWAITING_APPROVAL.value
        db.commit()
        return project

    # ── persistence helpers ───────────────────────────────────────────────────
    def _persist_step(
        self, db: Session, project: Project, state: dict, *, pause: bool = True
    ) -> None:
        """Persist the phase the graph just produced.

        When ``pause`` is True it then awaits human approval; when False (auto-run) the
        project stays RUNNING so the UI never sees a between-phase approval gate.
        Completion is NOT inferred from the graph being exhausted — every phase, including
        the last, must be explicitly approved (see `approve` -> `_finalize`).
        """
        last_result = state.get("last_result")
        if last_result:
            self._save_phase_result(
                db, project, last_result, status=PhaseStatus.PENDING_APPROVAL.value
            )
            self._record_usage(db, project, last_result)
            project.current_phase = last_result["phase"]

        self._persist_new_debates(db, project, state.get("debates", []))
        project.status = (
            PipelineStatus.AWAITING_APPROVAL.value if pause else PipelineStatus.RUNNING.value
        )
        db.commit()

    def _finalize(self, db: Session, project: Project) -> None:
        """Mark the project complete and write a long-term memory summary."""
        try:
            values = dict(graph.get_state(_config(project.id)).values)
        except Exception:  # noqa: BLE001
            values = {}
        project.status = PipelineStatus.COMPLETED.value
        project.current_phase = None
        db.commit()
        self._write_memory(project, values)

    def _save_phase_result(
        self, db: Session, project: Project, lr: dict, status: str
    ) -> PhaseResult:
        row = PhaseResult(
            project_id=project.id,
            phase=lr["phase"],
            agent=lr["agent"],
            status=status,
            output=lr["output"],
            content_md=lr["content_md"],
            model_used=lr.get("model_used"),
            provider_used=lr.get("provider_used"),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def _record_usage(self, db: Session, project: Project, lr: dict) -> None:
        usage = lr.get("usage") or {}
        resp = LLMResponse(
            text="",
            provider=lr.get("provider_used") or "unknown",
            model=lr.get("model_used") or "unknown",
            usage=Usage(**usage) if usage else Usage(),
            latency_ms=lr.get("latency_ms", 0),
            fallback_used=lr.get("fallback_used", False),
        )
        tracker.record(db, response=resp, project_id=project.id, phase=lr["phase"])

    def _persist_new_debates(self, db: Session, project: Project, debates: list[dict]) -> None:
        existing = (
            db.query(DebateRecord).filter(DebateRecord.project_id == project.id).count()
        )
        for record in debates[existing:]:
            db.add(
                DebateRecord(
                    project_id=project.id,
                    topic=record.get("topic", ""),
                    arguments=record.get("arguments", []),
                    decision=record.get("decision", ""),
                    rationale=record.get("rationale", ""),
                )
            )
            # Attribute the debate's tokens in analytics too.
            usage = record.get("_usage")
            if usage:
                resp = LLMResponse(
                    text="",
                    provider=record.get("_provider", "unknown"),
                    model=record.get("_model", "unknown"),
                    usage=Usage(**usage),
                )
                tracker.record(db, response=resp, project_id=project.id, phase="debate")
        db.commit()

    def _mark_latest(
        self, db: Session, project: Project, status: str, feedback: Optional[str] = None
    ) -> None:
        """Set the status of the most recent phase row for the current phase."""
        row = (
            db.query(PhaseResult)
            .filter(
                PhaseResult.project_id == project.id,
                PhaseResult.phase == project.current_phase,
            )
            .order_by(PhaseResult.created_at.desc())
            .first()
        )
        if row:
            row.status = status
            if feedback is not None:
                row.feedback = feedback
            db.commit()

    def _write_memory(self, project: Project, state: dict) -> None:
        outputs = state.get("prior_outputs", {})
        design = outputs.get("system_design", {})
        security = outputs.get("security_engineer", {})
        cost = outputs.get("cost_estimation", {})
        summary = (
            f"Tech stack: {design.get('tech_stack')}\n"
            f"Security posture: {security.get('overall_posture')}\n"
            f"Estimated timeline (weeks): {cost.get('estimated_timeline_weeks')}\n"
            f"Monthly cost (USD): {cost.get('total_monthly_low_usd')}–{cost.get('total_monthly_high_usd')}"
        )
        try:
            memory_store.remember(project.id, project.idea, summary)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to write long-term memory: %s", e)


runner = PipelineRunner()
