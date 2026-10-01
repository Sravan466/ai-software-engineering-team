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

import functools
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents import get_agent
from app.agents.base import AgentContext
from app.analytics import tracker
from app.build import dbconnect, integrations
from app.core import artifacts, identity, project_secrets
from app.core.config import settings
from app.core.constants import (
    PHASE_ORDER,
    DatabaseStatus,
    FindingStatus,
    GateKind,
    Phase,
    PhaseStatus,
    PipelineStatus,
    RoutingMode,
    BuildStatus,
    SchemaStatus,
    StackStatus,
)
from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.db.models import DebateRecord, PhaseResult, PreviewRevision, Project
from app.preview import history as preview_history
from app.memory.store import memory_store
from app.orchestration import autofix, connectors, remediation
from app.orchestration.approval import Gate, decide_gate
from app.orchestration.charter import Charter, binding_on
from app.orchestration.graph import connectors_note, graph, gather_skills
from app.orchestration.state import PipelineState
from app.preview import service as mockup
from app.preview.jobs import jobs as mockup_jobs
from app.router import inflight
from app.router.base import ComputerDisconnected, ProviderError, RequestCancelled
from app.schemas.llm import LLMResponse, Usage

log = get_logger(__name__)

# ── one lock per build, not one for the whole process ────────────────────────
#
# This used to be a single `threading.Lock()` held around `graph.invoke` — which is
# the model call. One slow generation therefore stalled every other build in the
# process: eight agents, one at a time, globally.
#
# The lock is now per project, and it still covers the model call: a phase is a
# read-modify-write on that build's checkpoint (`invoke` reads the state, generates,
# writes the next one; `redo` reads a snapshot, generates, patches it), and holding
# it across the pair is what stops one writer's patch landing on top of another's.
# What changed is who waits: another writer of the *same* build, rather than every
# build in the process.
#
# What it does not do is stop a build having two drivers. Stop marks a build
# cancelled without interrupting the call in flight, Resume clears the flag and
# claims it straight back, and the first driver — no longer seeing the flag — carries
# on beside the second. The lock makes their checkpoint writes take turns; it does
# not make the second one wait for the first to *finish*, and the two can both
# advance the run. That race is older than this lock (the process-wide one allowed it
# too); closing it needs a claim each driver holds and checks, not a lock.
#
# The SQLite connection itself does not need this. `SqliteSaver` holds its own lock
# around every cursor it opens, so writes from two builds cannot interleave on it.
#
# What this does not cover: the database rows around a phase (`_complete_row`,
# `_delete_rows`) are written outside it, exactly as they were under the old lock,
# and it is a lock in *this process*. Several workers would each have their own and
# would share `checkpoints.sqlite` unguarded — the status claim in the routes is the
# only thing that spans processes, and a multi-worker deployment needs a
# checkpointer that is built for one.
#
# Weak values, so a lock is collected once nothing holds it: a `with` block keeps its
# own reference alive for as long as it is held, so two threads asking at the same
# time still get the same object, and the table cannot grow with every project the
# process has ever seen.
_locks: "weakref.WeakValueDictionary[str, threading.RLock]" = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


def _checkpoint_lock(project_id: str) -> threading.RLock:
    """The lock guarding one project's checkpoint reads and writes."""
    with _locks_guard:
        lock = _locks.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _locks[project_id] = lock
        return lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config(project_id: str) -> dict:
    return {"configurable": {"thread_id": project_id}}


def _skills_for(values: dict, phase_key: str, prior_outputs: dict) -> tuple:
    """The skills a redone phase gets — chosen the same way a first run chooses them.

    The same function the graph node uses, handed the outputs this redo is keeping,
    so a rewind is not the one path through the pipeline where an agent works from a
    different library than the run it is repairing.
    """
    return gather_skills({**values, "prior_outputs": prior_outputs}, phase_key)


def _initial_state(project: Project) -> PipelineState:
    return PipelineState(
        project_id=project.id,
        idea=project.idea,
        routing_mode=project.routing_mode,
        preferred_model=project.preferred_model,
        prior_outputs={},
        feedback={},
        debates=[],
        charter={},
        # Mirrored in from the project row so every node reads one answer, including
        # a run resumed in a new process. Set when the build is created, because a
        # pin is a statement about the work this build is about to do.
        skill_overrides=project.skill_overrides or {},
        integrations_choice=project.integrations_choice or {},
    )


def _is_fix_note(note: object) -> bool:
    """Whether a phase's standing note was written by the fix loop, not a person."""
    text = str(note or "")
    return text.startswith(remediation.FIX_NOTE_PREFIX) or text.startswith(
        autofix.CODE_NOTE_PREFIX
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


def _as_owner(method):
    """Run a runner entry point for the project's owner.

    Whoever called it — a request, a background task, a test's thread — the run
    spends its owner's keys and models, and searches its owner's documents. Bound
    here rather than by every caller, so no way of starting a run can forget.
    """

    @functools.wraps(method)
    def bound(self, db: Session, project: Project, *args, **kwargs):
        # And names the build, so every model call it makes can be cancelled by
        # Stop, and a user's computer can say what it's answering.
        # A new build has no name yet; its idea says which one it is.
        first_line = ((project.idea or "").strip().splitlines() or [""])[0]
        label = project.name or first_line[:60] or None
        with identity.acting_as(project.owner_id), inflight.building(project.id, label):
            return method(self, db, project, *args, **kwargs)

    return bound


class PipelineRunner:
    # ── the driving loop ──────────────────────────────────────────────────────
    @_as_owner
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
                    # Serious problems are the crew's to fix, before anyone is asked
                    # anything: a severe finding goes back to whoever wrote the file,
                    # code that does not compile or contradicts the stack goes back to
                    # its phase. Either this sends it back (and the run continues from
                    # the rebuild), parks the build as "needs help", or finds nothing.
                    if self._auto_fix(db, project, row):
                        return project
                    gate = self.gate_for(project, row)
                    if gate is not None:
                        self._park(db, project, gate)
                        return project
                    # No gate here: this handoff isn't one the policy stops for, so
                    # the reviewer's standing answer for it is "yes".
                    row.status = PhaseStatus.APPROVED.value
                    db.commit()

                started, next_node = self._graph_position(project.id)
                if started and next_node is None:
                    # Never "complete" over a serious finding nobody fixed or waived —
                    # in any review mode, unattended included.
                    # Through the loop rather than straight to "needs help", so there is
                    # always a round to show and a Keep trying that does something — a
                    # build from before the loop can reach here with findings open.
                    if remediation.unresolved(db, project, serious=True) and self._remediate(
                        db, project
                    ):
                        return project
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

                # What the phase found wrong with itself is dealt with at the top of
                # the next pass, where the finished row is read back.
                self._run_phase(db, project, phase_key)
        except (CancelledRun, RequestCancelled):
            self._settle_cancelled(db, project)
            return project
        except ComputerDisconnected as e:
            self._pause(db, project, e)
            return project
        except ProviderError as e:
            if self._cancel_requested(db, project):
                # Stop landed while the call was failing; the Stop is what happened.
                self._settle_cancelled(db, project)
            else:
                self._fail(db, project, str(e))
            return project

    # ── the crew fixing its own serious problems ──────────────────────────────
    def gate_for(self, project: Project, row: PhaseResult) -> Optional[Gate]:
        """`decide_gate` for a finished row, minus what a person already accepted."""
        if row.phase == Phase.SYSTEM_DESIGN.value:
            # Before any review policy: only the person can hand over a database's
            # credentials, so this is asked in unattended mode too, and ahead of the
            # Plan review, which still parks once it is answered.
            asked = self.database_question(project) or self.integrations_question(project)
            if asked is not None:
                return asked
        data = autofix.load(project)
        stack = None if autofix.accepted(data, autofix.build_track(row.phase)) else row.stack_note
        return decide_gate(
            project,
            row.phase,
            row.output,
            row.schema_status,
            stack,
            self.build_problems(project),
        )

    @staticmethod
    def database_question(project: Project) -> Optional[Gate]:
        """The "connect your database" gate, while it is still unanswered."""
        if project.database_status is not None:
            return None
        charter = Charter.from_dict(project.charter)
        choice = charter.get("database") if charter else None
        if choice is None or choice.token not in dbconnect.NEEDS_CREDENTIALS:
            return None
        contract = dbconnect.contract_for(choice.token, charter.database_provider)
        # "PostgreSQL on Supabase", but never "PostgreSQL on Postgres".
        on = f" on {contract.label}" if contract and contract.provider != "generic" and contract.label != choice.label else ""
        return Gate(
            GateKind.DATABASE.value,
            f"Atlas picked {choice.label}{on}. Connect it now, or continue and add it later.",
        )

    @staticmethod
    def integrations_question(project: Project) -> Optional[Gate]:
        """The "connect your services" gate, for connectors nobody has answered.

        One already connected in the account's Connectors is linked, not asked about:
        when every connector the design uses is connected, there is no stop at all.
        """
        waiting = connectors.unanswered(project)
        if not waiting:
            return None
        labels = [integrations.get(i).label for i in waiting if integrations.get(i)]
        named = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" and {labels[-1]}"
        it = "it" if len(labels) == 1 else "them"
        return Gate(
            GateKind.INTEGRATIONS.value,
            f"The design uses {named}. Connect {it} now so the crew builds against {it}, or "
            "continue and add it later.",
        )

    @staticmethod
    def at_integrations_gate(project: Project) -> bool:
        return (
            project.status == PipelineStatus.AWAITING_APPROVAL.value
            and project.gate_kind == GateKind.INTEGRATIONS.value
        )

    @classmethod
    def at_question_gate(cls, project: Project) -> bool:
        """Parked on a question only the person can answer: the database or the services."""
        return cls.at_database_gate(project) or cls.at_integrations_gate(project)

    @staticmethod
    def before_code(project: Project) -> bool:
        """Parked right after the architecture, before any code was written — the only
        time the set of connectors (and so the names the code reads) can change."""
        return (
            project.status == PipelineStatus.AWAITING_APPROVAL.value
            and project.current_phase == Phase.SYSTEM_DESIGN.value
        )

    @staticmethod
    def set_integrations(project: Project, charter: Charter) -> None:
        """Re-point the charter at another set of connectors, before any code exists.

        The same write as `set_database_provider`: to the checkpoint the agents read
        their charter from, attributed to System Design so the run still resumes at
        the Backend Engineer. The caller commits.
        """
        data = charter.as_dict()
        with _checkpoint_lock(project.id):
            graph.update_state(_config(project.id), {"charter": data}, as_node=Phase.SYSTEM_DESIGN.value)
        project.charter = data
        log.info("Connectors for %s set to %s", project.id, ", ".join(charter.integrations) or "none")

    @staticmethod
    def settle_integrations(project: Project, before: object) -> None:
        """After the charter is (re)frozen: a connector the build no longer uses has
        its answer and its project keys removed. The account's connection stays."""
        old = Charter.from_dict(before)
        new = Charter.from_dict(project.charter)
        dropped = [i for i in (old.integrations if old else ()) if i not in (new.integrations if new else ())]
        stale = [i for i in (project.integrations_status or {}) if i not in (new.integrations if new else ())]
        gone = list(dict.fromkeys(dropped + stale))
        if gone:
            connectors.forget(project, gone)

    @staticmethod
    def set_database_provider(project: Project, charter: Charter) -> None:
        """Re-point the charter at another host's variables, at the database gate.

        The person said "it's on Supabase" where Atlas wrote plain Postgres, before
        any code exists. Written to the checkpoint the agents read their charter
        from as well as to the row, attributed to System Design exactly as a redo
        of it is, so the run still resumes at the Backend Engineer. The caller commits.
        """
        data = charter.as_dict()
        with _checkpoint_lock(project.id):
            graph.update_state(_config(project.id), {"charter": data}, as_node=Phase.SYSTEM_DESIGN.value)
        project.charter = data
        log.info("Database host for %s set to %s; the crew reads %s", project.id, charter.database_provider, ", ".join(charter.env))

    @staticmethod
    def at_database_gate(project: Project) -> bool:
        """Parked on the database question. The one definition every route uses."""
        return (
            project.status == PipelineStatus.AWAITING_APPROVAL.value
            and project.gate_kind == GateKind.DATABASE.value
        )

    @classmethod
    def settle_database(cls, project: Project, before: object) -> None:
        """After the charter is (re)frozen: ask again if the database changed.

        Credentials saved for MongoDB are no use to a build that now uses Postgres,
        so they are removed rather than kept beside a database they don't open. A
        database that needs nothing — SQLite, or none — is recorded as `none`.
        """
        def token(data: object) -> Optional[str]:
            charter = Charter.from_dict(data)
            choice = charter.get("database") if charter else None
            return choice.token if choice else None

        old, new = token(before), token(project.charter)
        if new not in dbconnect.NEEDS_CREDENTIALS:
            if old in dbconnect.NEEDS_CREDENTIALS and project.owner_id:
                project_secrets.remove(project.owner_id, project.id)
            project.database_status = DatabaseStatus.NONE.value
            return
        if project.database_status == DatabaseStatus.NONE.value:
            # It needed nothing before, and it does now: that is a new question.
            project.database_status = None
        elif old is not None and old != new:
            if project.owner_id:
                project_secrets.remove(project.owner_id, project.id)
            project.database_status = None
        elif project.owner_id:
            saved = project_secrets.load(project.owner_id, project.id).get("provider")
            charter = Charter.from_dict(project.charter)
            previous = Charter.from_dict(before)
            said_before = previous.frozen_provider if previous else None
            said_now = charter.frozen_provider if charter else None
            if (
                charter is not None
                and saved in dbconnect.PROVIDERS.get(new or "", ())
                and saved != charter.database_provider
            ):
                if said_now in (said_before, dbconnect.default_provider(new)):
                    # Atlas said what it said before (or named no host): the host the
                    # person chose and saved values under stands, or the code would
                    # read names that were never saved.
                    cls.set_database_provider(project, charter.with_provider(saved))
                else:
                    # The redo named a different host than Atlas did before — which
                    # is how the host is changed after the gate. What was saved is
                    # for the old one.
                    project_secrets.remove(project.owner_id, project.id)
                    project.database_status = None

    @staticmethod
    def build_problems(project: Project) -> list[dict]:
        """Compile problems still outstanding, less the phases a person moved past
        on the record — asking again at the Ship review would ask twice."""
        data = autofix.load(project)
        return [
            p
            for p in artifacts.build_problems(project)
            if not autofix.accepted(data, autofix.build_track(str(p.get("phase") or "")))
        ]

    def _auto_fix(self, db: Session, project: Project, row: PhaseResult) -> bool:
        """Send a finished phase's serious problems back to be fixed. True if it
        took the run over — by sending work back, or by parking as needs-help."""
        if row.phase == Phase.SECURITY_ENGINEER.value:
            return self._remediate(db, project)
        track = autofix.load(project)["tracks"].get(autofix.build_track(row.phase))
        # Also when the phase came back clean but a fix round is waiting on it: that
        # round has to be closed as a success, and the track settled for next time.
        if (
            row.stack_note
            or row.build_status == BuildStatus.FAILED.value
            or (track is not None and autofix.open_round(track) is not None)
        ):
            return self._fix_code(db, project, row)
        return False

    def _fix_code(self, db: Session, project: Project, row: PhaseResult) -> bool:
        """Re-run a phase whose code does not compile or contradicts the stack.

        The phase already had its own repair round inside `agent.run`; this is the
        loop around it, with the same stopping rule as security findings. Re-run in
        place: nothing after this phase exists yet.
        """
        name = autofix.build_track(row.phase)
        data = autofix.load(project)
        t = autofix.track(data, name)
        problems = autofix.code_problems(row)
        keys = [p["key"] for p in problems]
        if autofix.accepted(data, name):
            if autofix.covers(t, keys):
                return False
            # Broken differently from what a person accepted: a new problem.
            t["accepted"] = None
            autofix.settle(t)
        autofix.close_round(t, keys)
        if not problems:
            autofix.settle(t)
            autofix.save(project, data)
            db.commit()
            return False

        step = autofix.next_step(t)
        if step != "fix":
            autofix.stop(t, step, keys)
            autofix.save(project, data)
            db.commit()
            self._park(db, project, Gate(GateKind.NEEDS_HELP.value, self._help_note(project)))
            return True

        n = len(t["rounds"]) + 1
        k = n - int(t.get("episode_start") or 0)
        strategy = remediation.strategy_for(k)
        autofix.start_round(t, strategy, [row.phase], problems)
        autofix.save(project, data)
        db.commit()
        log.info(
            "Fixing %d code problem(s) in %s on %s (round %d, %s).",
            len(problems), row.phase, project.id, n, strategy,
        )
        snippets = {}
        if strategy != remediation.STRATEGY_GUIDED:
            for p in problems:
                code = remediation.snippet(
                    project,
                    remediation.Finding(
                        key=p["key"], title=p["title"], severity="", category="",
                        location=p.get("where") or "", recommendation="", owner_phase=row.phase,
                    ),
                    row.phase,
                )
                if code:
                    snippets[p["key"]] = code
        self.redo(
            db,
            project,
            row.phase,
            autofix.code_note(problems, k, snippets, self._standing_fix_note(data, row.phase)),
            escalate=(row.phase,) if strategy == remediation.STRATEGY_STRONGER else (),
            continue_after=True,
            fix_track=name,
        )
        return True

    def _remediate(self, db: Session, project: Project) -> bool:
        """Send serious findings back to the agents that own them. True if it fired.

        The mechanism is the one that already exists: `redo` re-runs one phase and
        rewinds everything built on top of it — which, when the phase being rewound
        is upstream of Warden, *is* a fix followed by a re-audit.

        Every owner in one round. The earliest phase is re-run with its findings, and
        every later owner's findings travel with the rewind as that phase's own note,
        so one rebuild fixes them all — sending only the first owner's findings left
        the others rebuilt without being told, and counted as a round used.

        Bounded by `auto_fix_max_rounds`, and stopped sooner when a round fixes
        nothing. What survives parks the build as "needs help" — never at the
        send-back-or-waive gate, which is for small findings, and never "complete".
        """
        row = self.latest_row(db, project, Phase.SECURITY_ENGINEER.value)
        if row is None:
            return False

        data = autofix.load(project)
        t = autofix.track(data, autofix.SECURITY)
        outstanding = remediation.unresolved(db, project, serious=True)
        fixed = autofix.close_round(t, [f.finding_key for f in outstanding])
        if fixed:
            last = t["rounds"][-1]
            for record in self._dispositions(db, project, fixed):
                if record.status == FindingStatus.FIXED.value:
                    record.fixed_round = last["n"]
        if not outstanding:
            autofix.settle(t)
            autofix.save(project, data)
            db.commit()
            return False

        step = autofix.next_step(t)
        if step != "fix":
            autofix.stop(t, step, [f.finding_key for f in outstanding])
            autofix.save(project, data)
            db.commit()
            log.info(
                "%d serious finding(s) left on %s (%s) — asking for help.",
                len(outstanding), project.id, step,
            )
            self._park(db, project, Gate(GateKind.NEEDS_HELP.value, self._help_note(project)))
            return True

        findings = [
            remediation.Finding(
                key=f.finding_key,
                title=f.title,
                severity=f.severity,
                category=f.category,
                location=f.location,
                recommendation=f.recommendation,
                owner_phase=f.owner_phase,
            )
            for f in outstanding
        ]
        order = [p.value for p in PHASE_ORDER]
        by_owner: dict[str, list[remediation.Finding]] = {}
        for f in findings:
            by_owner.setdefault(remediation.route_owner(f), []).append(f)
        owners = sorted(by_owner, key=order.index)

        n = len(t["rounds"]) + 1
        k = n - int(t.get("episode_start") or 0)
        strategy = remediation.strategy_for(k)
        notes = {
            phase: remediation.fix_instruction(
                items, strategy=strategy, project=project, owner=phase, round_number=k
            )
            for phase, items in by_owner.items()
        }
        record = autofix.start_round(
            t,
            strategy,
            owners,
            [
                {
                    "key": f.key,
                    "title": f.title,
                    "severity": f.severity,
                    "where": f.location or None,
                    "phase": remediation.route_owner(f),
                    "kind": "security",
                }
                for f in findings
            ],
        )
        # Kept with the round, so a compile fix inside it can carry the security fix
        # along — the phase's own note is overwritten by the first such fix.
        record["notes"] = notes
        for finding in outstanding:
            finding.status = FindingStatus.FIX_REQUESTED.value
        project.remediation_rounds = n
        autofix.save(project, data)
        db.commit()

        log.info(
            "Sending %d serious finding(s) back to %s on %s (round %d, %s).",
            len(findings), ", ".join(owners), project.id, n, strategy,
        )
        first, rest = owners[0], owners[1:]
        self.redo(
            db,
            project,
            first,
            notes[first],
            extra_feedback={phase: notes[phase] for phase in rest},
            escalate=tuple(owners) if strategy == remediation.STRATEGY_STRONGER else (),
            continue_after=True,
            fix_track=autofix.SECURITY,
        )
        return True

    @staticmethod
    def _standing_fix_note(data: dict, phase: str) -> Optional[str]:
        """The security fix note `phase` is working to, while that round is open."""
        live = autofix.open_round(autofix.track(data, autofix.SECURITY))
        if live is None:
            return None
        return (live.get("notes") or {}).get(phase)

    def _abandon_round(self, db: Session, project: Project, name: str) -> None:
        """Forget the one fix round whose redo never generated, so it isn't judged."""
        data = autofix.load(project)
        if name in data["tracks"] and autofix.abandon_open_round(data["tracks"][name]):
            project.remediation_rounds = len(autofix.track(data, autofix.SECURITY)["rounds"])
            autofix.save(project, data)
            db.commit()

    @staticmethod
    def _dispositions(db: Session, project: Project, keys: list[str]) -> list:
        from app.db.models import SecurityDisposition

        return (
            db.query(SecurityDisposition)
            .filter(
                SecurityDisposition.project_id == project.id,
                SecurityDisposition.finding_key.in_(keys),
            )
            .all()
        )

    @staticmethod
    def _help_note(project: Project) -> str:
        """One line saying what the crew could not fix, and why it stopped."""
        data = autofix.load(project)
        parts: list[str] = []
        for name, t in data["tracks"].items():
            if not t.get("stopped") or t.get("accepted"):
                continue
            rounds = len(t.get("rounds") or [])
            left = int(t["stopped"].get("left") or 0)
            if name == autofix.SECURITY:
                what = f"{left} serious security finding{'' if left == 1 else 's'}"
            else:
                phase = name[len(autofix.BUILD_PREFIX):]
                try:
                    title = get_agent(phase).title
                except Exception:  # noqa: BLE001
                    title = phase
                what = f"{left} code problem{'' if left == 1 else 's'} in {title}'s work"
            if t["stopped"]["reason"] == autofix.STOP_NO_PROGRESS:
                why = "the last round fixed none of them"
            elif rounds:
                why = f"after {rounds} fix round{'' if rounds == 1 else 's'}"
            else:
                why = "automatic fixing is switched off"
            parts.append(f"The crew couldn't fix {what} — {why}.")
        return " ".join(parts) or (
            "A serious security finding is still open and was neither fixed nor waived."
        )

    @_as_owner
    def reject(self, db: Session, project: Project, feedback: str) -> Project:
        """Send the phase the reviewer is looking at back to its agent.

        `redo` with the phase filled in. Kept because `POST /reject` is a published
        endpoint with its own tests; the UI drives `POST /redo`, which can address any
        phase. Both are this one code path — do not grow a second.
        """
        return self.redo(db, project, project.current_phase, feedback)

    @_as_owner
    def redo(
        self,
        db: Session,
        project: Project,
        phase_key: Optional[str],
        feedback: str,
        extra_feedback: Optional[dict] = None,
        escalate: tuple = (),
        continue_after: bool = False,
        fix_track: Optional[str] = None,
    ) -> Project:
        """Re-run one phase with reviewer feedback, patching the checkpoint in place.

        The graph is *not* advanced: the node's output is replaced where it sits, so
        the pipeline's position is unchanged and the reviewer lands back on the same
        decision with the corrected work in front of them.

        `phase_key` need not be the phase being reviewed. The Ship review shows a
        whole file tree assembled from several phases, and "this file is wrong" has
        to reach the agent that wrote *that* file — otherwise per-file redo is a
        button that sends a note to whoever happens to have finished last.

        The fix loop adds three things. `extra_feedback` is a note for each later
        phase the rewind rebuilds, so one rewind fixes every owner's findings.
        `escalate` names the phases that should run on the most capable model the
        router has. `continue_after` carries on with the run instead of parking on
        the same decision — the crew fixing its own work is not a person's redo.
        """
        if not phase_key:
            return project

        if not continue_after:
            # A person acting on a build the crew gave up on starts a new episode: the
            # verdict on the crew's last round is not a verdict on this.
            data = autofix.load(project)
            stuck = autofix.stuck(data)
            if stuck:
                for name in stuck:
                    autofix.settle(data["tracks"][name])
                autofix.save(project, data)
                db.commit()

        # Where to return to once the agent is done: the decision this redo was
        # requested from, which is not necessarily the phase being re-run.
        gate_phase = project.current_phase or phase_key
        fallback = Gate(project.gate_kind or GateKind.PHASE.value, project.gate_note)
        # Phases built on top of the one being replaced. They are about to be rebuilt,
        # so they are dropped from both the database and the graph's own memory — an
        # agent re-running against the discarded build's outputs is the same defect
        # this rewind exists to fix, one layer down.
        stale = set(self._phases_after(phase_key)) if gate_phase != phase_key else set()
        #: Set only when the architecture itself is rewritten. Written to the project
        #: row after the checkpoint patch lands, so the two cannot disagree about
        #: which stack this build is being held to.
        charter_update: Optional[dict] = None

        # The attempt being sent back keeps its place in the history. What it was
        # before is remembered, because rejecting it is only correct once something
        # has replaced it — see the failure path below.
        superseded = self.latest_row(db, project, phase_key)
        was = superseded.status if superseded is not None else None
        # Its note too. `_mark_phase` is about to overwrite `feedback` with this
        # redo's, and if the attempt being superseded was itself a rejection, that
        # note is the record of why — restoring the row without it would put the
        # attempt back and drop the reason it was sent back in the first place.
        was_feedback = superseded.feedback if superseded is not None else None
        self._mark_phase(db, project, phase_key, PhaseStatus.REJECTED.value, feedback=feedback)
        row = self._begin_phase(db, project, phase_key)

        try:
            with _heartbeat(project.id):
                cfg = _config(project.id)
                with _checkpoint_lock(project.id):
                    snapshot = graph.get_state(cfg)
                    values: PipelineState = dict(snapshot.values)  # type: ignore[assignment]

                    agent = get_agent(phase_key)
                    # The phases this redo drops are dropped from the scoring too:
                    # a procedure chosen because of something a phase that no longer
                    # exists wrote is a procedure chosen from a build that no longer
                    # exists.
                    kept_outputs = {
                        k: v
                        for k, v in values.get("prior_outputs", {}).items()
                        if k != phase_key and k not in stale
                    }
                    ctx = AgentContext(
                        idea=values["idea"],
                        routing_mode=RoutingMode(values.get("routing_mode", "local_only")),
                        preferred_model=values.get("preferred_model"),
                        prior_outputs=kept_outputs,
                        skills=_skills_for(values, phase_key, kept_outputs),
                        feedback=feedback,
                        # Held to the same stack the rest of the build uses. Without
                        # this a redo is the one path through the pipeline where an
                        # agent is free to change database, which is precisely the
                        # divergence the charter exists to prevent — and the phases
                        # rebuilt behind it would inherit the disagreement. System
                        # Design is the exception, and `binding_on` is where that
                        # exception lives rather than here.
                        charter=binding_on(phase_key, values.get("charter")),
                        escalate=phase_key in escalate,
                        # The architect is told what the person has connected, on a
                        # redo exactly as on the first run. Names only.
                        extra_context=(
                            connectors_note() if phase_key == Phase.SYSTEM_DESIGN.value else ""
                        ),
                    )
                    # Inside the lock, model call and all. This is a read-modify-write:
                    # the patch below is built from the snapshot above, so a write to this
                    # checkpoint in between would be silently overwritten by it. The lock
                    # keeps every other writer of *this* build out until the patch lands;
                    # it is per project, so it holds up nothing else. It does not stop a
                    # second driver existing — see `_checkpoint_lock`.
                    result = agent.run(ctx)

                    from app.orchestration.graph import _last_debate, _serialize_result, build_integrations
                    from app.orchestration.charter import freeze

                    last_result = _serialize_result(phase_key, agent.title, result)
                    kept = {
                        key: value
                        for key, value in values.get("prior_outputs", {}).items()
                        if key not in stale
                    }
                    # A fix note belongs to the round that wrote it. A phase this
                    # rewind rebuilds without a note of its own this time must not be
                    # handed the last round's findings again — it may have fixed them.
                    notes = {
                        k: v
                        for k, v in (values.get("feedback") or {}).items()
                        if not (k in stale and _is_fix_note(v))
                    }
                    notes[phase_key] = feedback
                    notes.update(extra_feedback or {})
                    patch = {
                        "prior_outputs": {**kept, phase_key: result.output},
                        "last_phase": phase_key,
                        "last_result": last_result,
                        "feedback": notes,
                        # This round's escalations — plus, for a fix in place, the
                        # ones an open round still owes the phases after this.
                        "escalate": sorted(
                            (set(escalate) | (set(values.get("escalate") or []) if not stale else set()))
                            - {phase_key}
                        ),
                    }
                    if phase_key == Phase.SYSTEM_DESIGN.value:
                        # The architecture was rewritten, so the charter frozen from
                        # the old one describes a build that no longer exists. Every
                        # phase after this is about to re-run against the new one.
                        rewritten = freeze(
                            result.output, _last_debate(values), build_integrations(values, result.output)
                        )
                        patch["charter"] = rewritten.as_dict() if rewritten else {}
                        charter_update = patch["charter"]
                    graph.update_state(
                        cfg,
                        patch,
                        # Attribute the correction to the node that made it, so the
                        # graph resumes at the phase *after* it. Redoing the backend
                        # from the Ship review has to rewind to the backend, not
                        # leave the pipeline sitting at the end.
                        as_node=phase_key,
                    )
        except ProviderError as e:
            if fix_track:
                self._abandon_round(db, project, fix_track)
            # Put the previous attempt back. Both `rejected` and `failed` count as
            # superseded when the archive is assembled, so leaving them that way
            # leaves this phase with *no* current attempt: its files vanish from the
            # `.zip`, and because the graph is still positioned past it the run can
            # be resumed to `completed` with the backend simply missing. The old
            # work is the best thing anyone has until new work replaces it.
            if superseded is not None and was is not None:
                superseded.status = was
                superseded.feedback = was_feedback
                # And drop the attempt that never generated, rather than leaving it
                # `failed` beside the restored one. `_reconcile_current_phase`
                # salvages a checkpointed `last_result` into a failed row whose phase
                # matches — which at a per-phase gate it does — so keeping both left
                # the phase with two live `pending_approval` rows for one piece of
                # work, doubling its history and the progress count drawn from it.
                # It produced nothing; `project.last_error` carries why.
                self._delete_rows(db, project, [row])
                db.commit()
                log.info(
                    "Restored the previous %s attempt on %s — its replacement never "
                    "generated, and dropping both would leave the build without it.",
                    phase_key,
                    project.id,
                )
            else:
                self._abandon_row(
                    db, row, "The model provider failed while regenerating."
                )
            if isinstance(e, RequestCancelled) or self._cancel_requested(db, project):
                self._settle_cancelled(db, project)
            elif isinstance(e, ComputerDisconnected):
                # The earlier attempt was put back above, so resuming parks on it
                # again — and it can be sent back once the computer is here.
                self._pause(db, project, e)
            else:
                self._fail(db, project, str(e))
            return project

        self._complete_row(db, project, row, last_result)
        if charter_update is not None:
            before = project.charter
            project.charter = charter_update or None
            self.settle_database(project, before)
            self.settle_integrations(project, before)
            db.commit()
            log.info("Stack charter re-frozen for %s after redoing the architecture", project.id)
        if phase_key == Phase.FRONTEND_ENGINEER.value:
            # The front end was rewritten, so the picture of it is of code that no
            # longer exists. `_run_phase`'s hook does not fire on this path.
            if self._clear_generated_mockup(db, project.id):
                self._draw_mockup_later(project.id, row.id)
        if stale:
            # Before the cancellation check, not after: a Stop pressed in that window
            # would otherwise leave a build whose backend is new and whose tests,
            # findings and costs describe the code it replaced — and `artifacts.assemble`
            # would hand out both halves in one .zip without a word.
            self._discard_after(db, project, phase_key)

        if self._cancel_requested(db, project):
            self._settle_cancelled(db, project, rewinding=bool(stale))
            return project

        if gate_phase == phase_key:
            if continue_after:
                # The crew re-ran its own phase to fix it. The loop reads the new
                # attempt back and decides: another round, a gate, or onwards.
                return self.continue_run(db, project)
            # The reviewer sent back the phase they were looking at. Nothing
            # downstream exists yet, so put them back on the same decision — once
            # anything serious in the new attempt has been dealt with.
            gate_row = self.latest_row(db, project, phase_key)
            if gate_row is not None and self._auto_fix(db, project, gate_row):
                return project
            gate = self.gate_for(project, gate_row) if gate_row is not None else None
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
    def _has_mockup(db: Session, project_id: str) -> bool:
        return bool(
            db.query(PreviewRevision).filter(PreviewRevision.project_id == project_id).count()
        )

    @staticmethod
    def _clear_generated_mockup(db: Session, project_id: str) -> bool:
        """Make room for a fresh mockup. False means someone's own work is in the way.

        Returns True when there is nothing to clear as well as when it cleared
        something — the caller is asking "may I draw?", and *no mockup at all* is the
        clearest yes there is. Conflating that with the one real no (a preview a
        person has edited) let a redo skip scheduling its redraw, and an already
        in-flight draw of the replaced front end became the mockup by default.
        """
        revisions = preview_history.revisions(db, project_id)
        # Only the version on screen and what it descends from count as someone's
        # work. An edit they undid is off that line: it was deleted outright before
        # undo became a pointer, and it must not block a redraw now that it is kept.
        live = preview_history.lineage(preview_history.head(revisions), revisions)
        if any(r.source != "generated" for r in live):
            return False
        for revision in revisions:
            db.delete(revision)
        if revisions:
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
        project.paused_device_id = None
        db.commit()
        # Now interrupt whatever is generating, so the runtime stops too — not only
        # the build's bookkeeping.
        stopped = inflight.cancel(project.id)
        if stopped:
            log.info("Stop interrupted %d model call(s) in flight for %s.", stopped, project.id)
        return project

    def prepare_resume(self, db: Session, project: Project) -> None:
        """Clear the stop flag so the run can be claimed again.

        Tidying up what the dead run left behind (a `PhaseResult` stuck at `running`)
        is `continue_run`'s first act, and it needs the checkpointer lock to do it —
        which a request handler must never wait on.
        """
        project.cancel_requested = False
        project.last_error = None
        project.paused_device_id = None
        project.heartbeat_at = _now()
        db.commit()

    # ── one phase ─────────────────────────────────────────────────────────────
    def _run_phase(self, db: Session, project: Project, phase_key: str) -> None:
        """Announce a phase, run exactly that phase, then record what it produced."""
        row = self._begin_phase(db, project, phase_key)
        started, _ = self._graph_position(project.id)

        try:
            with _heartbeat(project.id):
                # This build's checkpoint gets one writer at a time; every other build
                # in the process generates alongside it. See `_checkpoint_lock`.
                with _checkpoint_lock(project.id):
                    state = graph.invoke(
                        None if started else _initial_state(project), _config(project.id)
                    )
        except ComputerDisconnected:
            # Dropped rather than kept as failed: the phase runs again, from the
            # start, when the computer is back.
            self._abandon_row(db, row, "Paused: the computer running this model disconnected.")
            raise
        except ProviderError:
            self._abandon_row(db, row, "The model provider failed during this phase.")
            raise

        last_result = state.get("last_result")
        if last_result:
            self._complete_row(db, project, row, last_result)
        else:
            self._abandon_row(db, row, "The agent produced no output.")

        self._persist_new_debates(db, project, state.get("debates", []))
        if phase_key == Phase.SYSTEM_DESIGN.value:
            # Mirrored out of the graph's own state rather than re-derived here, so
            # there is exactly one charter and the row cannot drift from the
            # checkpoint the agents are actually reading.
            before = project.charter
            project.charter = state.get("charter") or None
            self.settle_database(project, before)
            self.settle_integrations(project, before)
            db.commit()
        self._raise_if_cancelled(db, project)

        if phase_key == Phase.FRONTEND_ENGINEER.value and last_result:
            self._draw_mockup_later(project.id, row.id)

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
        row.is_local = lr.get("is_local")
        row.latency_ms = int(lr.get("latency_ms") or 0)
        row.total_tokens = int(usage.get("total_tokens") or 0)
        row.schema_status = lr.get("schema_status")
        row.schema_note = lr.get("schema_note")
        # Two facts, recorded separately. A phase can match its declared shape
        # perfectly and still be written against the wrong database, and a reviewer
        # told only the first would read a green badge over a build that ships two
        # incompatible halves.
        violations = lr.get("stack_violations") or []
        row.stack_status = (
            StackStatus.VIOLATED.value if violations else StackStatus.OK.value
        )
        row.stack_note = violations or None
        row.build_status = lr.get("build_status")
        row.build_note = lr.get("build_problems") or None
        # Provenance, not a verdict: which procedures this deliverable was written
        # with. Empty stays empty rather than becoming null — "this phase was
        # offered skills and none fitted" is a different fact from "this row was
        # written before skills existed", and the review panel says which.
        row.skills_used = lr.get("skills_used") if "skills_used" in lr else None
        row.completed_at = _now()
        project.heartbeat_at = row.completed_at
        db.commit()
        self._record_usage(db, project, lr)
        if row.phase == Phase.SECURITY_ENGINEER.value:
            # Every audit, including the re-audit after a fix — which is the one that
            # decides whether the fix took. Here rather than in `_run_phase` because a
            # redo of this phase lands here too, and a waiver that only survived one
            # of the two paths would be a waiver the reviewer is asked about again.
            remediation.sync_dispositions(
                db,
                project,
                row.output,
                # A report that missed its declared shape yields no findings at all.
                # Reconciling against it would read as "they all went away" and
                # clear a critical finding because nobody could parse the report
                # that raised it. The UNCHECKED gate already stops that run.
                readable=row.schema_status != SchemaStatus.INVALID.value,
            )
        return row

    def _park(self, db: Session, project: Project, gate: Gate) -> None:
        """Stop and wait for a person, recording which review to show and why.

        Re-reads the stop flag first. A Stop landing between the caller's check and
        this write would otherwise be undone — the run coming back as a live decision
        panel, with the reason it was stopped cleared along with it.
        """
        if self._cancel_requested(db, project):
            self._settle_cancelled(db, project)
            return

        project.status = PipelineStatus.AWAITING_APPROVAL.value
        project.gate_kind = gate.kind
        project.gate_note = gate.note
        # "Waiting for you" and "here is why the run died" cannot both be true.
        project.last_error = None
        db.commit()
        log.info("Waiting on review: %s (%s)", project.id, gate.kind)

    def _draw_mockup_later(self, project_id: str, source_row_id: str) -> None:
        """Draw the mockup alongside the pipeline rather than in front of it.

        This is a whole extra model call, and inlining it between a phase and the gate
        it leads to held the reviewer's decision hostage to it — for minutes on a local
        model, with no agent shown as working, because the phase had already finished.
        The pipeline moves on; the picture catches up.

        `source_row_id` is the Frontend attempt being drawn. A redo can replace that
        attempt while this thread is still generating, and without something to check
        at the end, whichever request happened to finish last would decide what the
        Ship review shows.
        """
        threading.Thread(
            # Carries the run's account: the draw spends the same owner's models.
            target=identity.carry(self._draw_mockup),
            args=(project_id, source_row_id),
            name=f"mockup-{project_id[:8]}",
            daemon=True,
        ).start()

    def _wait_for_idle(self, project_id: str, timeout: float = 900.0) -> bool:
        """Hold until the pipeline stops generating. False means don't bother drawing.

        Moving the mockup off the critical path does not help on a local runtime,
        where there is exactly one model: a draw running beside the next agent just
        doubles that agent's visible elapsed time and charges the stall to the wrong
        phase. Waiting for a gate — or for the end — spends the model on idle time
        instead. Bounded, because an unattended run is `running` throughout.
        """
        deadline = time.monotonic() + timeout
        while True:
            db = SessionLocal()
            try:
                project = db.get(Project, project_id)
                if project is None:
                    return False
                status = project.status
            finally:
                db.close()
            if status in (
                PipelineStatus.AWAITING_APPROVAL.value,
                PipelineStatus.COMPLETED.value,
            ):
                return True
            if status != PipelineStatus.RUNNING.value:
                return False  # cancelled or failed — don't spend a model call on it
            if time.monotonic() >= deadline:
                return True  # an unattended run never parks; draw rather than never
            time.sleep(2.0)

    def _draw_mockup(self, project_id: str, source_row_id: str) -> None:
        """Render the visual mockup from the Frontend phase's work.

        It used to be a button on a different tab: Prism had already written
        `frontend/`, and the reviewer still had to leave the decision and ask for a
        picture of it. Now it exists by the time anyone reviews the build.

        Strictly best effort, and never on top of existing work — a mockup that
        fails to draw must not fail a build that produced real code, and a
        regenerate would throw away sections the reviewer has already edited.

        Registered with the same job registry the Preview tab reads once it starts: a
        site is many calls, and "building section 7 of 18" or "failed, here is why"
        are things the reviewer can see instead of an empty tab. Claimed only after
        the pipeline goes idle, not when queued — a redo schedules a fresh draw while
        the old one may still be waiting, and the old one holding the claim would
        lock out the only draw that is still of anything.
        """
        if not self._wait_for_idle(project_id):
            return

        reporter = mockup_jobs.claim(project_id, "pipeline")
        if reporter is None:
            # Someone is already drawing — a person pressed Generate, or an earlier
            # draw got here first. Let it land, then see whether one is still needed.
            mockup_jobs.wait(project_id, timeout=3600)
            reporter = mockup_jobs.claim(project_id, "pipeline")
            if reporter is None:
                return
        error: Optional[str] = None
        try:
            db = SessionLocal()
            try:
                project = db.get(Project, project_id)
                if project is None or self._cancel_requested(db, project):
                    return

                def still_wanted(db: Session, project: Project) -> bool:
                    # The front end may have been rewritten while this was queued or
                    # drawing. A picture of the version that was replaced is worse than
                    # no picture, and the Ship review captions it as current.
                    current = self.latest_row(db, project, Phase.FRONTEND_ENGINEER.value)
                    if current is None or current.id != source_row_id:
                        log.info("Dropped a mockup of a replaced front end (%s).", project_id)
                        return False
                    return not self._has_mockup(db, project_id)

                # Asked before spending the calls as well as after: a site is minutes
                # of model time, and a stale draw would spend them on nothing.
                if not still_wanted(db, project):
                    return
                mockup.build_and_save(db, project, reporter, still_wanted=still_wanted)
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 - the build is the deliverable, not the picture
            log.warning("Mockup generation failed for %s: %s", project_id, e)
            error = f"Drawing the mockup failed: {e}"
        finally:
            mockup_jobs.finish(project_id, error)

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
        with _checkpoint_lock(project_id):
            snapshot = graph.get_state(_config(project_id))
        started = bool(snapshot.created_at) or bool(snapshot.values)
        next_node = snapshot.next[0] if snapshot.next else None
        return started, next_node

    def _reconcile_current_phase(
        self, db: Session, project: Project
    ) -> Optional[PhaseResult]:
        """Return the latest row for the current phase, healing a dead or failed one.

        Background tasks die with their process, so a row left at `running` means an
        interrupted run. A `failed` row means an attempt that raised — a redo whose
        provider dropped, say. Both are rows with no usable output sitting where the
        loop expects a deliverable, and both have the same two outcomes: the graph
        checkpointed that phase's output before things went wrong (salvage it) or it
        did not (drop the row so the phase runs again).

        Failing to heal a `failed` one is how a run could report itself **completed**
        with a phase's deliverable erased: the graph is at END, so `continue_run`
        reads "nothing left to do" and finalises over the top of an empty row.
        """
        if not project.current_phase:
            return None
        row = self.latest_row(db, project, project.current_phase)
        if row is None or row.status not in (
            PhaseStatus.RUNNING.value,
            PhaseStatus.FAILED.value,
        ):
            return row

        with _checkpoint_lock(project.id):
            values = dict(graph.get_state(_config(project.id)).values)
        salvaged = values.get("last_result")
        if salvaged and salvaged.get("phase") == row.phase:
            log.info("Recovered checkpointed output for %s/%s", project.id, row.phase)
            return self._complete_row(db, project, row, salvaged)

        self._delete_rows(db, project, [row])
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

    def _settle_cancelled(
        self, db: Session, project: Project, rewinding: bool = False
    ) -> None:
        project.status = PipelineStatus.CANCELLED.value
        project.cancel_requested = False
        if rewinding:
            # The phases after the corrected one were dropped before the stop landed,
            # so the build view now shows them as never-run. Say why, or they simply
            # disappear and the reviewer is left to wonder what ate them.
            project.last_error = (
                "Stopped while rebuilding. The phases after the one you sent back were "
                "cleared because they described the version it replaced — resume and "
                "they run again from the corrected work."
            )
        elif not project.last_error:
            project.last_error = "Stopped by you. Resume picks up from the last approved phase."
        db.commit()
        log.info("Run cancelled: %s (at %s)", project.id, project.current_phase)

    def _pause(self, db: Session, project: Project, error: ComputerDisconnected) -> None:
        """Wait for the user's computer instead of failing: nothing the build did was
        wrong, and it continues from the last finished phase when that computer is
        back — by itself, when the connector reconnects."""
        try:
            db.refresh(project)
        except Exception:  # noqa: BLE001 - deleted mid-run: nothing to pause
            return
        if project.status == PipelineStatus.CANCELLED.value:
            return  # Stopped meanwhile; that decision stands.
        phase = project.current_phase
        try:
            title = get_agent(phase).title if phase else "The build"
        except Exception:  # noqa: BLE001
            title = phase or "The build"
        name = error.device_name or "Your computer"
        if error.paused_there:
            message = (
                f"{name} is paused. {title} paused. Run `aiteam-connect resume` on it, "
                "then press Resume."
            )
        else:
            message = (
                f"{name} disconnected. {title} paused. Start the connector to continue — "
                "the build picks up from the last finished phase by itself."
            )
        project.status = PipelineStatus.PAUSED.value
        project.paused_device_id = error.device_id
        project.last_error = message
        project.cancel_requested = False
        db.commit()
        log.info("Run paused for device %s: %s (at %s)", error.device_id, project.id, phase)
        # The computer may have come back while this was being written — after the
        # reconnect looked for paused builds and found none. Look again now.
        from app.connector.hub import hub

        link = hub.live(error.device_id)
        if link is not None and link.approved and not error.paused_there:
            hub.notify_ready(project.owner_id, error.device_id)

    def _fail(self, db: Session, project: Project, message: str) -> None:
        from app.core.scrub import scrub

        project.status = PipelineStatus.FAILED.value
        # Shown on the page and kept in the database: never a key a provider quoted.
        project.last_error = scrub(message)
        db.commit()
        log.warning("Run failed: %s — %s", project.id, message)

    # ── persistence helpers ───────────────────────────────────────────────────
    def _finalize(self, db: Session, project: Project) -> None:
        """Mark the project complete and write a long-term memory summary."""
        try:
            with _checkpoint_lock(project.id):
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
        """One usage event per model call — including a schema repair round.

        A repaired phase is two calls. Recording it as one would leave the token
        total right and the call count and average latency wrong, which is the kind
        of quiet inaccuracy this dashboard exists to not have.
        """
        calls = lr.get("calls") or [
            {
                "provider": lr.get("provider_used"),
                "model": lr.get("model_used"),
                "usage": lr.get("usage") or {},
                "latency_ms": lr.get("latency_ms", 0),
                "fallback_used": lr.get("fallback_used", False),
                "is_local": lr.get("is_local"),
            }
        ]
        for call in calls:
            usage = call.get("usage") or {}
            resp = LLMResponse(
                text="",
                provider=call.get("provider") or "unknown",
                model=call.get("model") or "unknown",
                usage=Usage(**usage) if usage else Usage(),
                latency_ms=call.get("latency_ms", 0),
                fallback_used=call.get("fallback_used", False),
                is_local=call.get("is_local"),
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
                    is_local=record.get("_is_local"),
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
            memory_store.remember(project.id, project.idea, summary, owner_id=project.owner_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to write long-term memory: %s", e)


runner = PipelineRunner()
