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
from app.core.constants import (
    PHASE_ORDER,
    GateKind,
    Phase,
    PhaseStatus,
    PipelineStatus,
    RoutingMode,
)
from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.db.models import DebateRecord, PhaseResult, PreviewRevision, Project
from app.memory.store import memory_store
from app.orchestration.approval import Gate, decide_gate
from app.orchestration.graph import graph
from app.orchestration.state import PipelineState
from app.preview.generator import build_context, generate_preview
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
        # How many times each phase has been started by *this* call. The loop derives
        # its next move from the graph checkpoint, so a node that returned without
        # producing output and without advancing would be re-run forever — an
        # unbounded retry against a paid model. One retry, then stop and say so.
        attempts: dict[str, int] = {}

        try:
            while True:
                self._raise_if_cancelled(db, project)
                row = self._reconcile_current_phase(db, project)

                if row is not None and row.status == PhaseStatus.PENDING_APPROVAL.value:
                    gate = decide_gate(project, row.phase, row.output)
                    if gate is not None:
                        self._park(db, project, gate)
                        return project
                    # No gate here: this handoff isn't one the policy stops for, so
                    # the reviewer's standing answer for it is "yes".
                    row.status = PhaseStatus.APPROVED.value
                    db.commit()

                started, next_node = self._graph_position(project.id)
                if started and next_node is None:
                    self._finalize(db, project)
                    return project

                phase_key = next_node if started else PHASE_ORDER[0].value
                attempts[phase_key] = attempts.get(phase_key, 0) + 1
                if attempts[phase_key] > 2:
                    self._fail(
                        db,
                        project,
                        f"The {phase_key} phase produced no usable output after "
                        "repeated attempts, so the run was stopped rather than retried "
                        "indefinitely. Resume to try again.",
                    )
                    return project

                self._run_phase(db, project, phase_key)
        except CancelledRun:
            self._settle_cancelled(db, project)
            return project
        except ProviderError as e:
            self._fail(db, project, str(e))
            return project

    def reject(self, db: Session, project: Project, feedback: str) -> Project:
        """Send the phase the reviewer is looking at back to its agent.

        `redo` with the phase filled in. Kept because `POST /reject` is a published
        endpoint with its own tests; the UI drives `POST /redo`, which can address any
        phase. Both are this one code path — do not grow a second.
        """
        return self.redo(db, project, project.current_phase, feedback)

    def redo(
        self, db: Session, project: Project, phase_key: Optional[str], feedback: str
    ) -> Project:
        """Re-run one phase with reviewer feedback, patching the checkpoint in place.

        The graph is *not* advanced: the node's output is replaced where it sits, so
        the pipeline's position is unchanged and the reviewer lands back on the same
        decision with the corrected work in front of them.

        `phase_key` need not be the phase being reviewed. The Ship review shows a
        whole file tree assembled from several phases, and "this file is wrong" has
        to reach the agent that wrote *that* file — otherwise per-file redo is a
        button that sends a note to whoever happens to have finished last.
        """
        if not phase_key:
            return project

        # Where to return to once the agent is done: the decision this redo was
        # requested from, which is not necessarily the phase being re-run.
        gate_phase = project.current_phase or phase_key
        fallback = Gate(project.gate_kind or GateKind.PHASE.value, project.gate_note)
        # Phases built on top of the one being replaced. They are about to be rebuilt,
        # so they are dropped from both the database and the graph's own memory — an
        # agent re-running against the discarded build's outputs is the same defect
        # this rewind exists to fix, one layer down.
        stale = set(self._phases_after(phase_key)) if gate_phase != phase_key else set()

        # The attempt being sent back keeps its place in the history.
        self._mark_phase(db, project, phase_key, PhaseStatus.REJECTED.value, feedback=feedback)
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
                            if k != phase_key and k not in stale
                        },
                        feedback=feedback,
                    )
                    result = agent.run(ctx)

                    from app.orchestration.graph import _serialize_result

                    last_result = _serialize_result(phase_key, agent.title, result)
                    kept = {
                        key: value
                        for key, value in values.get("prior_outputs", {}).items()
                        if key not in stale
                    }
                    graph.update_state(
                        cfg,
                        {
                            "prior_outputs": {**kept, phase_key: result.output},
                            "last_phase": phase_key,
                            "last_result": last_result,
                            "feedback": {**values.get("feedback", {}), phase_key: feedback},
                        },
                        # Attribute the correction to the node that made it, so the
                        # graph resumes at the phase *after* it. Redoing the backend
                        # from the Ship review has to rewind to the backend, not
                        # leave the pipeline sitting at the end.
                        as_node=phase_key,
                    )
        except ProviderError as e:
            self._abandon_row(db, row, "The model provider failed while regenerating.")
            self._fail(db, project, str(e))
            return project

        self._complete_row(db, project, row, last_result)
        if phase_key == Phase.FRONTEND_ENGINEER.value:
            # The front end was rewritten, so the picture of it is of code that no
            # longer exists. `_run_phase`'s hook does not fire on this path.
            if self._clear_generated_mockup(db, project.id):
                self._draw_mockup_later(project.id)
        if stale:
            # Before the cancellation check, not after: a Stop pressed in that window
            # would otherwise leave a build whose backend is new and whose tests,
            # findings and costs describe the code it replaced — and `artifacts.assemble`
            # would hand out both halves in one .zip without a word.
            self._discard_after(db, project, phase_key)

        if self._cancel_requested(db, project):
            self._settle_cancelled(db, project)
            return project

        if gate_phase == phase_key:
            # The reviewer sent back the phase they were looking at. Nothing
            # downstream exists yet, so put them back on the same decision.
            gate_row = self.latest_row(db, project, phase_key)
            gate = decide_gate(project, phase_key, gate_row.output if gate_row else None)
            self._park(db, project, gate or fallback)
            return project

        # Everything built on top of the replaced phase is gone (above); rebuild it.
        # The discard waits until the agent has returned, so a provider failure
        # mid-redo leaves the run exactly as it was rather than with a hole where the
        # downstream phases used to be.
        project.current_phase = phase_key
        db.commit()
        log.info("Rebuilding %s from %s after a redo", project.id, phase_key)
        return self.continue_run(db, project)

    @staticmethod
    def _delete_rows(db: Session, project: Project, rows: list[PhaseResult]) -> None:
        """Delete phase rows through the ORM, and forget them properly.

        A bulk `query(...).delete()` is faster and wrong here. These sessions are
        `expire_on_commit=False` and `Project.phases` cascades `delete-orphan`, so a
        bulk delete leaves the deleted rows sitting in the identity map and in any
        loaded collection — and a later flush can emit an UPDATE against a row that is
        no longer there, which fails the whole run with a `StaleDataError` naming a
        table rather than anything a person could act on. Deleting each instance and
        expiring the collection keeps the session's picture and the database's the
        same. There are never more than eight of these.
        """
        if not rows:
            return
        for row in rows:
            db.delete(row)
        db.expire(project, ["phases"])

    @staticmethod
    def _clear_generated_mockup(db: Session, project_id: str) -> bool:
        """Drop an auto-drawn mockup so a rebuilt front end gets a fresh one.

        Only ever an untouched one. The moment somebody has edited a section, the
        preview is their work rather than a derived picture, and throwing it away to
        redraw something they did not ask for would be worse than showing them a
        mockup they can regenerate themselves.
        """
        revisions = (
            db.query(PreviewRevision).filter(PreviewRevision.project_id == project_id).all()
        )
        if not revisions or any(r.source != "generated" for r in revisions):
            return False
        for revision in revisions:
            db.delete(revision)
        db.commit()
        return True

    @staticmethod
    def _phases_after(phase_key: str) -> list[str]:
        """The phases that run after `phase_key`, in order."""
        order = [p.value for p in PHASE_ORDER]
        try:
            return order[order.index(phase_key) + 1 :]
        except ValueError:
            return []

    def _discard_after(self, db: Session, project: Project, phase_key: str) -> None:
        """Forget every phase that ran after `phase_key` — they are about to re-run."""
        downstream = self._phases_after(phase_key)
        if not downstream:
            return
        self._delete_rows(
            db,
            project,
            db.query(PhaseResult)
            .filter(
                PhaseResult.project_id == project.id,
                PhaseResult.phase.in_(downstream),
            )
            .all(),
        )

        # The mockup is a picture of the front end. If the front end is being rebuilt,
        # the picture is of code that will not exist. Re-running that phase draws a
        # fresh one through `_run_phase`'s hook.
        if Phase.FRONTEND_ENGINEER.value in downstream:
            self._clear_generated_mockup(db, project.id)
        db.commit()

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

        if phase_key == Phase.FRONTEND_ENGINEER.value and last_result:
            self._draw_mockup_later(project.id)

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
        self._delete_rows(
            db,
            project,
            db.query(PhaseResult)
            .filter(
                PhaseResult.project_id == project.id,
                PhaseResult.phase == phase_key,
                PhaseResult.status == PhaseStatus.FAILED.value,
            )
            .all(),
        )

        project.current_phase = phase_key
        project.phase_started_at = now
        project.heartbeat_at = now
        project.status = PipelineStatus.RUNNING.value
        # Moving means the pipeline is no longer parked; nothing is waiting on anyone,
        # and whatever stopped the last attempt has been superseded by this one.
        project.gate_kind = None
        project.gate_note = None
        project.last_error = None

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

    def _park(self, db: Session, project: Project, gate: Gate) -> None:
        """Stop and wait for a person, recording which review to show and why."""
        project.status = PipelineStatus.AWAITING_APPROVAL.value
        project.gate_kind = gate.kind
        project.gate_note = gate.note
        # "Waiting for you" and "here is why the run died" cannot both be true.
        project.last_error = None
        db.commit()
        log.info("Waiting on review: %s (%s)", project.id, gate.kind)

    def _draw_mockup_later(self, project_id: str) -> None:
        """Draw the mockup alongside the pipeline rather than in front of it.

        This is a whole extra model call, and inlining it between a phase and the gate
        it leads to held the reviewer's decision hostage to it — for minutes on a local
        model, with no agent shown as working, because the phase had already finished.
        The pipeline moves on; the picture catches up.
        """
        threading.Thread(
            target=self._draw_mockup,
            args=(project_id,),
            name=f"mockup-{project_id[:8]}",
            daemon=True,
        ).start()

    def _draw_mockup(self, project_id: str) -> None:
        """Render the visual mockup from the Frontend phase's work.

        It used to be a button on a different tab: Prism had already written
        `frontend/`, and the reviewer still had to leave the decision and ask for a
        picture of it. Now it exists by the time anyone reviews the build.

        Strictly best effort, and never on top of existing work — a mockup that
        fails to draw must not fail a build that produced real code, and a
        regenerate would throw away sections the reviewer has already edited.
        """
        db = SessionLocal()
        try:
            project = db.get(Project, project_id)
            if project is None or self._cancel_requested(db, project):
                return
            if (
                db.query(PreviewRevision)
                .filter(PreviewRevision.project_id == project_id)
                .count()
            ):
                return

            html, resp = generate_preview(
                project.idea,
                project.name,
                build_context(project),
                mode=RoutingMode(project.routing_mode),
                preferred_model=project.preferred_model,
            )
            if "<" not in html:
                log.warning("Mockup generation returned no HTML for %s.", project_id)
                return

            db.add(
                PreviewRevision(
                    project_id=project_id,
                    html=html,
                    source="generated",
                    model_used=resp.model,
                    provider_used=resp.provider,
                )
            )
            db.commit()
            tracker.record(db, response=resp, project_id=project_id, phase="preview")
        except Exception as e:  # noqa: BLE001 - the build is the deliverable, not the picture
            log.warning("Mockup generation failed for %s: %s", project_id, e)
        finally:
            db.close()

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
        row = self.latest_row(db, project, project.current_phase)
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
    #: Re-read before every gate decision, because a person can change any of them
    #: while the run is in flight.
    _LIVE_FIELDS = ("cancel_requested", "approval_mode", "require_approval", "cost_cap_usd")

    @staticmethod
    def _cancel_requested(db: Session, project: Project) -> bool:
        """Re-read the reviewer's live settings — Stop and the review policy are both
        written by other requests on other sessions, and this session has the run.

        A project deleted mid-run counts as cancelled: there is nothing left to write to.
        """
        try:
            db.refresh(project, list(PipelineRunner._LIVE_FIELDS))
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
        project.gate_kind = None
        project.gate_note = None
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
    def latest_row(db: Session, project: Project, phase: str) -> Optional[PhaseResult]:
        """The most recent attempt at one phase — a phase re-runs when sent back.

        Public because the PATCH route re-derives a gate from the same row this loop
        parks on; two spellings of "latest" would let those two disagree.
        """
        return (
            db.query(PhaseResult)
            .filter(PhaseResult.project_id == project.id, PhaseResult.phase == phase)
            .order_by(PhaseResult.created_at.desc(), PhaseResult.id.desc())
            .first()
        )

    def _mark_phase(
        self,
        db: Session,
        project: Project,
        phase: Optional[str],
        status: str,
        feedback: Optional[str] = None,
    ) -> Optional[PhaseResult]:
        """Set the status of the most recent row for one named phase."""
        if not phase:
            return None
        row = self.latest_row(db, project, phase)
        if row:
            row.status = status
            if feedback is not None:
                row.feedback = feedback
            db.commit()
        return row

    def _mark_latest(
        self, db: Session, project: Project, status: str, feedback: Optional[str] = None
    ) -> Optional[PhaseResult]:
        """Set the status of the most recent phase row for the current phase."""
        return self._mark_phase(db, project, project.current_phase, status, feedback)

    def approve_current(self, db: Session, project: Project) -> Optional[PhaseResult]:
        """Record the reviewer's yes on the phase they were shown.

        A Plan review covers two phases and a Ship review covers the whole run, so
        the yes lands on every phase still waiting for one — otherwise the phases
        the reviewer approved without being asked twice stay `pending_approval`
        forever and read as unfinished.
        """
        project.gate_kind = None
        project.gate_note = None
        approved = self._mark_latest(db, project, PhaseStatus.APPROVED.value)
        pending = (
            db.query(PhaseResult)
            .filter(
                PhaseResult.project_id == project.id,
                PhaseResult.status == PhaseStatus.PENDING_APPROVAL.value,
            )
            .all()
        )
        for row in pending:
            row.status = PhaseStatus.APPROVED.value
        db.commit()
        return approved

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
