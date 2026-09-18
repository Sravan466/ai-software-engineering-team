"""Shared enums and the canonical pipeline phase ordering."""
from __future__ import annotations
from typing import Optional

from enum import Enum


class RoutingMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    LOCAL_ONLY = "local_only"


class ApprovalMode(str, Enum):
    """How often the pipeline stops and asks a person.

    Gating every handoff identically makes review a rubber stamp: the same card and
    the same two buttons appear whether an agent renamed a field or wrote the whole
    backend. The default gates on *consequence* instead — two decisions where change
    is still cheap or still possible, plus an interrupt when the run itself reports
    something alarming.
    """

    #: Two decisions — the plan, then the finished build — plus conditional interrupts.
    CHECKPOINTS = "checkpoints"
    #: Stop after every phase. Kept for anyone who wants the old rhythm.
    EVERY_PHASE = "every_phase"
    #: Never stop for a judgement call. One thing still stops it: a phase that
    #: contradicts the stack the architecture froze, which is not a judgement call —
    #: the build holds two incompatible halves and every phase after it compounds them.
    UNATTENDED = "unattended"


class GateKind(str, Enum):
    """Why the pipeline is waiting, which decides what the reviewer is shown."""

    #: Scope + Atlas together: the spec, the scope and the architecture diagram.
    PLAN = "plan"
    #: One pass over the finished artifact: files, mockup, security findings, cost.
    SHIP = "ship"
    #: Warden found something severe enough to stop an otherwise unattended build.
    SECURITY = "security"
    #: A gate's own phase failed its schema, so the check that gate performs never
    #: ran. Distinct from SECURITY on purpose: "Warden found something serious" and
    #: "nobody could read Warden's report" are different facts, and telling the
    #: reviewer the first when the second happened is the failure this all fixes.
    UNCHECKED = "unchecked"
    #: Ledger's projected run cost passed the cap set for this build.
    COST = "cost"
    #: A phase wrote itself against a different stack than the architecture froze.
    #: Distinct from every other kind because it is not a judgement call: the build
    #: holds two incompatible halves, and approving it ships both in one archive.
    STACK = "stack"
    #: A single handoff, in every-phase mode.
    PHASE = "phase"


class StackStatus(str, Enum):
    """Whether a phase's output agrees with the stack charter it was built under.

    Recorded per phase because it decides whether a build is one thing or several. A
    `violated` phase used to reach the archive beside phases written against a
    different database, and nothing in the pipeline noticed.
    """

    #: Checked, and it agrees. Also what a phase with no charter to check reports.
    OK = "ok"
    #: Still contradicts the charter after being sent back with the contradiction named.
    VIOLATED = "violated"


class FindingStatus(str, Enum):
    """What was done about one security finding.

    There is deliberately no value for "seen and moved on". A critical or high
    finding leaves this pipeline either fixed and re-audited, or waived on the record
    by a person who said why — the only two outcomes the security gate accepts.
    """

    #: Reported; nothing done about it yet.
    OPEN = "open"
    #: Sent back to the agent that owns the file. The re-audit says whether it took.
    FIX_REQUESTED = "fix_requested"
    #: A later audit no longer reports it.
    FIXED = "fixed"
    #: Accepted deliberately, with a reason attached.
    WAIVED = "waived"

    @classmethod
    def settled(cls) -> frozenset[str]:
        """The states that let a build past the security gate."""
        return frozenset({cls.FIXED.value, cls.WAIVED.value})


class SchemaStatus(str, Enum):
    """What validation made of an agent's output against its declared shape.

    Recorded per phase because two gates downstream read specific keys off these
    outputs: a phase that did not match its shape is a check that did not run, and
    that has to be visible rather than inferred from a key being missing.
    """

    #: Matched first time.
    VALID = "valid"
    #: Matched after being sent back with the validation errors attached.
    REPAIRED = "repaired"
    #: Still did not match. The best attempt is kept and flagged, never silently used.
    INVALID = "invalid"


class PipelineStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Stopped by the reviewer. Resumable from the last checkpoint.
    CANCELLED = "cancelled"


class PhaseStatus(str, Enum):
    #: The agent is generating right now — no output yet. The row exists so the UI
    #: can show which agent has the work and for how long.
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: The phase was abandoned mid-flight (cancelled, or the server restarted).
    FAILED = "failed"


class Phase(str, Enum):
    """Pipeline phases in execution order. Value == agent key."""

    PRODUCT_MANAGER = "product_manager"
    SYSTEM_DESIGN = "system_design"
    BACKEND_ENGINEER = "backend_engineer"
    FRONTEND_ENGINEER = "frontend_engineer"
    QA_ENGINEER = "qa_engineer"
    SECURITY_ENGINEER = "security_engineer"
    DEVOPS_ENGINEER = "devops_engineer"
    COST_ESTIMATION = "cost_estimation"


# Ordered list driving the LangGraph edges and the UI progress tracker.
PHASE_ORDER: list[Phase] = [
    Phase.PRODUCT_MANAGER,
    Phase.SYSTEM_DESIGN,
    Phase.BACKEND_ENGINEER,
    Phase.FRONTEND_ENGINEER,
    Phase.QA_ENGINEER,
    Phase.SECURITY_ENGINEER,
    Phase.DEVOPS_ENGINEER,
    Phase.COST_ESTIMATION,
]

#: The phases the Plan review covers. They run back to back and are approved once,
#: because a scope you accept and an architecture you accept are one decision.
PLAN_PHASES: tuple[Phase, ...] = (Phase.PRODUCT_MANAGER, Phase.SYSTEM_DESIGN)

#: Where each of the two checkpoint gates falls: after the last planning phase, and
#: after the last phase of all.
PLAN_GATE_PHASE: Phase = PLAN_PHASES[-1]
SHIP_GATE_PHASE: Phase = PHASE_ORDER[-1]

PHASE_LABELS: dict[str, str] = {
    Phase.PRODUCT_MANAGER.value: "Product Requirements",
    Phase.SYSTEM_DESIGN.value: "Architecture Design",
    Phase.BACKEND_ENGINEER.value: "Backend Code",
    Phase.FRONTEND_ENGINEER.value: "Frontend Code",
    Phase.QA_ENGINEER.value: "Test Cases",
    Phase.SECURITY_ENGINEER.value: "Security Review",
    Phase.DEVOPS_ENGINEER.value: "Deployment Plan",
    Phase.COST_ESTIMATION.value: "Cost Estimation",
}


def next_phase(current: Optional[str]) -> Optional[Phase]:
    """Return the phase after `current`, or the first phase if current is None."""
    if current is None:
        return PHASE_ORDER[0]
    try:
        idx = [p.value for p in PHASE_ORDER].index(current)
    except ValueError:
        return None
    return PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else None
