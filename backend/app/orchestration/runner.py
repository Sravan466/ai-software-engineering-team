"""Pipeline runner: drives the LangGraph forward and mirrors its state to the DB.

The compiled graph is the execution engine and the source of truth for *pipeline
position* (via its SQLite checkpointer, keyed by thread_id = project_id). This runner
turns that position into something a person can watch and steer:

  • Every phase is announced *before* it runs — `current_phase`, `phase_started_at`
    and a `PhaseResult` row with status `running` — so exactly one agent is visibly
    working and the UI can show how long it has been at it.
  • A heartbeat thread touches the project every few seconds while a phase generates.
    A `running` project with no heartbeat is a dead run, not a slow one, and the API
    reports it stalled instead of leaving the UI to poll forever.
  • Nothing here blocks an HTTP request. `continue_run` is designed to be handed to a
    background task; the routes commit `running` and return at once.
  • Stop sets a flag the loop checks between phases and after every agent returns, and
    Resume picks the run back up from the last checkpoint.
"""
from __future__ import annotations
from typing import Optional

import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents import get_agent
from app.agents.base import AgentContext
from app.analytics import tracker
from app.core.config import settings
from app.core.constants import PHASE_ORDER, PhaseStatus, PipelineStatus, RoutingMode
from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.db.models import DebateRecord, PhaseResult, Project
from app.memory.store import memory_store
from app.orchestration.graph import graph
from app.orchestration.state import PipelineState
from app.router.base import ProviderError
from app.schemas.llm import LLMResponse, Usage

log = get_logger(__name__)

# Serialise access to the (single-connection) checkpointer.
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


class CancelledRun(Exception):
    """Raised inside the run loop when the reviewer pressed Stop."""


@contextmanager
def _heartbeat(project_id: str):
    """Touch `heartbeat_at` every few seconds for as long as the block runs.

    Runs on its own session: the caller's is busy inside a multi-minute agent call,
    and a heartbeat that only lands after the phase finishes proves nothing.
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(settings.heartbeat_interval_seconds):
            db = SessionLocal()
            try:
                project = db.get(Project, project_id)
                if project is None:
                    return
                project.heartbeat_at = _now()
                db.commit()
            except Exception as e:  # noqa: BLE001 - a missed beat must not kill the run
                log.debug("Heartbeat write failed for %s: %s", project_id, e)
            finally:
                db.close()

    thread = threading.Thread(target=beat, name=f"heartbeat-{project_id[:8]}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()


class PipelineRunner:
    # ── the driving loop ──────────────────────────────────────────────────────
    def continue_run(self, db: Session, project: Project) -> Project:
        """Drive the pipeline forward from wherever it is until it needs a human.

        One loop covers every entry point — first run, an approval, a resume after a
        stop or a crash — because they are all the same question: *what does the
        graph's checkpoint say happens next?* In approval mode the loop parks at the
        first finished-but-unapproved phase; with approvals off it runs to the end.

        Safe to hand to a background task: it owns `project.status` for the whole
        run and leaves it in a terminal state (or `cancelled`) on every path out.
        """
        try:
            while True:
                self._raise_if_cancelled(db, project)
                row = self._reconcile_current_phase(db, project)

                if row is not None and row.status == PhaseStatus.PENDING_APPROVAL.value:
                    if project.require_approval:
                        project.status = PipelineStatus.AWAITING_APPROVAL.value
                        db.commit()
                        return project
                    # Approvals are off — the reviewer's standing answer is "yes".
                    row.status = PhaseStatus.APPROVED.value
                    db.commit()

                started, next_node = self._graph_position(project.id)
                if started and next_node is None:
                    self._finalize(db, project)
                    return project

                self._run_phase(db, project, next_node if started else PHASE_ORDER[0].value)
        except CancelledRun:
            self._settle_cancelled(db, project)
            return project
        except ProviderError as e:
            self._fail(db, project, str(e))
            return project

    def reject(self, db: Session, project: Project, feedback: str) -> Project:
        """Re-run the current phase with reviewer feedback, patching the checkpoint.

        The graph is *not* advanced: the same node's output is replaced in place, so
        the next approval continues from the corrected version.
        """
        phase_key = project.current_phase
        if not phase_key:
            return project

        # The attempt being sent back keeps its place in the history.
        self._mark_latest(db, project, PhaseStatus.REJECTED.value, feedback=feedback)
        row = self._begin_phase(db, project, phase_key)

        try:
            with _heartbeat(project.id):
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
                            k: v
                            for k, v in values.get("prior_outputs", {}).items()
                            if k != phase_key
                        },
                        feedback=feedback,
                    )
                    result = agent.run(ctx)

                    from app.orchestration.graph import _serialize_result

                    last_result = _serialize_result(phase_key, agent.title, result)
                    graph.update_state(
                        cfg,
                        {
                            "prior_outputs": {
                                **values.get("prior_outputs", {}),
                                phase_key: result.output,
                            },
                            "last_phase": phase_key,
                            "last_result": last_result,
                            "feedback": {**values.get("feedback", {}), phase_key: feedback},
                        },
                    )
        except ProviderError as e:
            self._abandon_row(db, row, "The model provider failed while regenerating.")
            self._fail(db, project, str(e))
            return project

        self._complete_row(db, project, row, last_result)
        if self._cancel_requested(db, project):
            self._settle_cancelled(db, project)
            return project

        project.status = PipelineStatus.AWAITING_APPROVAL.value
        db.commit()
        return project

    # ── stop / resume ─────────────────────────────────────────────────────────
    def stop(self, db: Session, project: Project, reason: str) -> Project:
        """Ask the run to stop, and say so immediately.

        The in-flight model call cannot be interrupted, but the reviewer's decision
        does not have to wait for it: the project is marked `cancelled` now, and the
        loop honours the flag the moment the agent returns.
        """
        project.cancel_requested = True
        project.status = PipelineStatus.CANCELLED.value
        project.last_error = reason
        db.commit()
        return project

    def prepare_resume(self, db: Session, project: Project) -> None:
        """Clear the stop flag so the run can be claimed again.

        Tidying up what the dead run left behind (a `PhaseResult` stuck at `running`)
        is `continue_run`'s first act, and it needs the checkpointer lock to do it —
        which a request handler must never wait on.
        """
        project.cancel_requested = False
        project.last_error = None
        project.heartbeat_at = _now()
        db.commit()

    # ── one phase ─────────────────────────────────────────────────────────────
    def _run_phase(self, db: Session, project: Project, phase_key: str) -> None:
        """Announce a phase, run exactly that phase, then record what it produced."""
        row = self._begin_phase(db, project, phase_key)
        started, _ = self._graph_position(project.id)

        try:
            with _heartbeat(project.id):
                with _lock:
                    state = graph.invoke(
                        None if started else _initial_state(project), _config(project.id)
                    )
        except ProviderError:
            self._abandon_row(db, row, "The model provider failed during this phase.")
            raise

        last_result = state.get("last_result")
        if last_result:
            self._complete_row(db, project, row, last_result)
        else:
            self._abandon_row(db, row, "The agent produced no output.")

        self._persist_new_debates(db, project, state.get("debates", []))
        self._raise_if_cancelled(db, project)

    def _begin_phase(self, db: Session, project: Project, phase_key: str) -> PhaseResult:
        """Mark a phase as *starting* — before a single token is generated.

        This is what makes progress visible: `current_phase` used to be written only
        once an agent finished, so the whole first phase rendered as eight queued
        nodes and no elapsed time.
        """
        now = _now()

        # A previous attempt that failed produced nothing, so keeping it would only
        # stack identical dead rows every time someone resumes. A *rejected* attempt
        # is different — that one generated real output the reviewer turned down, and
        # that history is worth keeping.
        db.query(PhaseResult).filter(
            PhaseResult.project_id == project.id,
            PhaseResult.phase == phase_key,
            PhaseResult.status == PhaseStatus.FAILED.value,
        ).delete(synchronize_session=False)

        project.current_phase = phase_key
        project.phase_started_at = now
        project.heartbeat_at = now
        project.status = PipelineStatus.RUNNING.value

        row = PhaseResult(
            project_id=project.id,
            phase=phase_key,
            agent=get_agent(phase_key).title,
            status=PhaseStatus.RUNNING.value,
            output={},
            content_md="",
            started_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def _complete_row(
        self, db: Session, project: Project, row: PhaseResult, lr: dict
    ) -> PhaseResult:
        usage = lr.get("usage") or {}
        row.status = PhaseStatus.PENDING_APPROVAL.value
        row.agent = lr["agent"]
        row.output = lr["output"]
        row.content_md = lr["content_md"]
        row.model_used = lr.get("model_used")
        row.provider_used = lr.get("provider_used")
        row.latency_ms = int(lr.get("latency_ms") or 0)
        row.total_tokens = int(usage.get("total_tokens") or 0)
        row.completed_at = _now()
        project.heartbeat_at = row.completed_at
        db.commit()
        self._record_usage(db, project, lr)
        return row

    def _abandon_row(self, db: Session, row: PhaseResult, reason: str) -> None:
        row.status = PhaseStatus.FAILED.value
        row.feedback = reason
        row.completed_at = _now()
        db.commit()

    # ── graph position ────────────────────────────────────────────────────────
    @staticmethod
    def _graph_position(project_id: str) -> tuple[bool, Optional[str]]:
        """(has_checkpoint, next_node) for this project's thread.

        `next_node` is None once the graph has reached END. Reading the checkpoint
        rather than the DB keeps the runner honest about where execution actually is,
        even if a crash left the two disagreeing.
        """
        with _lock:
            snapshot = graph.get_state(_config(project_id))
        started = bool(snapshot.created_at) or bool(snapshot.values)
        next_node = snapshot.next[0] if snapshot.next else None
        return started, next_node

    def _reconcile_current_phase(
        self, db: Session, project: Project
    ) -> Optional[PhaseResult]:
        """Return the latest row for the current phase, healing a dead `running` one.

        Background tasks die with their process, so a row left at `running` means an
        interrupted run. Two cases: the graph checkpointed the phase's output before
        dying (salvage it) or it did not (drop the row so the phase runs again).
        """
        if not project.current_phase:
            return None
        row = self._latest_row(db, project, project.current_phase)
        if row is None or row.status != PhaseStatus.RUNNING.value:
            return row

        with _lock:
            values = dict(graph.get_state(_config(project.id)).values)
        salvaged = values.get("last_result")
        if salvaged and salvaged.get("phase") == row.phase:
            log.info("Recovered checkpointed output for %s/%s", project.id, row.phase)
            return self._complete_row(db, project, row, salvaged)

        db.delete(row)
        # Rewind to the last phase that actually produced something, so the loop's
        # "what's next" question gets the truth.
        db.commit()
        previous = (
            db.query(PhaseResult)
            .filter(PhaseResult.project_id == project.id)
            .order_by(PhaseResult.created_at.desc())
            .first()
        )
        project.current_phase = previous.phase if previous else None
        project.phase_started_at = None
        db.commit()
        return previous

    # ── cancellation ──────────────────────────────────────────────────────────
    @staticmethod
    def _cancel_requested(db: Session, project: Project) -> bool:
        """Re-read the flag — Stop is set by a different request on a different session.

        A project deleted mid-run counts as cancelled: there is nothing left to write to.
        """
        try:
            db.refresh(project, ["cancel_requested"])
        except Exception:  # noqa: BLE001 - the row is gone (deleted from the UI)
            return True
        return bool(project.cancel_requested)

    def _raise_if_cancelled(self, db: Session, project: Project) -> None:
        if self._cancel_requested(db, project):
            raise CancelledRun()

    def _settle_cancelled(self, db: Session, project: Project) -> None:
        project.status = PipelineStatus.CANCELLED.value
        project.cancel_requested = False
        if not project.last_error:
            project.last_error = "Stopped by you. Resume picks up from the last approved phase."
        db.commit()
        log.info("Run cancelled: %s (at %s)", project.id, project.current_phase)

    def _fail(self, db: Session, project: Project, message: str) -> None:
        project.status = PipelineStatus.FAILED.value
        project.last_error = message
        db.commit()
        log.warning("Run failed: %s — %s", project.id, message)

    # ── persistence helpers ───────────────────────────────────────────────────
    def _finalize(self, db: Session, project: Project) -> None:
        """Mark the project complete and write a long-term memory summary."""
        try:
            with _lock:
                values = dict(graph.get_state(_config(project.id)).values)
        except Exception:  # noqa: BLE001
            values = {}
        project.status = PipelineStatus.COMPLETED.value
        project.current_phase = None
        project.phase_started_at = None
        project.last_error = None
        db.commit()
        self._write_memory(project, values)

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

    @staticmethod
    def _latest_row(db: Session, project: Project, phase: str) -> Optional[PhaseResult]:
        return (
            db.query(PhaseResult)
            .filter(PhaseResult.project_id == project.id, PhaseResult.phase == phase)
            .order_by(PhaseResult.created_at.desc(), PhaseResult.id.desc())
            .first()
        )

    def _mark_latest(
        self, db: Session, project: Project, status: str, feedback: Optional[str] = None
    ) -> Optional[PhaseResult]:
        """Set the status of the most recent phase row for the current phase."""
        if not project.current_phase:
            return None
        row = self._latest_row(db, project, project.current_phase)
        if row:
            row.status = status
            if feedback is not None:
                row.feedback = feedback
            db.commit()
        return row

    def approve_current(self, db: Session, project: Project) -> Optional[PhaseResult]:
        """Record the reviewer's yes on the phase they were shown."""
        return self._mark_latest(db, project, PhaseStatus.APPROVED.value)

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
