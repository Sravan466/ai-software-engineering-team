"""When the pipeline stops for a person, and why.

Eight identical gates made review a rubber stamp: the same card and the same two
buttons after every handoff, whether the agent renamed a field or wrote the whole
backend. Gating is worth its interruption only where being wrong is expensive, so
the default policy stops twice —

    Describe → [Plan review] → build (unattended) → [Ship review] → Deliver
                                    ↳ interrupts on a severe finding or a cost overrun

— and lets the run through everywhere else. `every_phase` keeps the old rhythm for
anyone who wants it; `unattended` never stops.

This module owns that decision and nothing else. It reads a finished phase and
answers one question, so the runner stays a loop and the policy stays legible.

Two of those gates read specific keys off an agent's output — Ledger's projected cost,
Warden's findings — and a gate that cannot find its key used to return `None`, which
means "carry on". Reading nothing looked exactly like reading "nothing to worry about",
so a renamed field took the gate down without a word. Three things stop that here:
every read is by *normalised* key, so `overallRiskAssessment` and `overall_risk_assessment`
are the same name; a missing total falls back to the line items rather than to zero; and
an output that failed its schema outright **stops the run** instead of sailing past.
A gate that silently stops gating is worse than no gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.core.constants import (
    ApprovalMode,
    GateKind,
    PLAN_GATE_PHASE,
    Phase,
    SHIP_GATE_PHASE,
    SchemaStatus,
)

#: Severities Warden is allowed to stop an unattended build over. Anything below this
#: is worth reading at the Ship review, not worth interrupting a run for.
STOPPING_SEVERITIES = frozenset({"critical", "high"})

#: Phases whose output a gate actually reads. When one of these fails validation the
#: check it feeds cannot run, and the run stops for a person instead of pretending it did.
GATED_PHASES = (Phase.SECURITY_ENGINEER.value, Phase.COST_ESTIMATION.value)


# ── reading a key that may have been renamed ─────────────────────────────────
def _norm(name: object) -> str:
    """`overallRiskAssessment`, `overall_risk_assessment`, `Overall Risk Assessment`
    all collapse to one string, so case and punctuation drift stop mattering."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def read_key(source: object, *names: str) -> object:
    """The first of `names` that actually holds something, matched by normalised key.

    Agents rename keys — that is the drift this whole module now assumes. Matching
    on the shape of the name rather than its exact spelling costs nothing and turns
    a class of silent gate failures into a non-event.

    "Holds something" rather than "is present" is the important half. A model that
    writes `"findings": null` next to a populated `"security_findings"` has reported
    findings; stopping at the null because the key existed would lose them, which is
    the whole failure this module is being hardened against.
    """
    if not isinstance(source, dict):
        return None
    flat = {_norm(k): v for k, v in source.items()}
    for name in names:
        value = flat.get(_norm(name))
        if value is not None:
            return value
    return None


def _as_rows(value: object) -> list[dict]:
    """A list of objects, whatever the agent wrapped it in (or forgot to)."""
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


@dataclass(frozen=True)
class Gate:
    """A stop: which review to render, and the one line that says why it happened."""

    kind: str
    note: Optional[str] = None


# ── reading what an agent reported ───────────────────────────────────────────
def severe_findings(output: object) -> list[dict]:
    """Security findings severe enough to interrupt, newest schema or not.

    Models are inconsistent about case, about what they call the list, and about
    whether a single finding is wrapped in one at all — so this reads leniently on
    every axis. What matters is that something says critical or high.
    """
    findings = _as_rows(
        read_key(output, "findings", "security_findings", "vulnerabilities", "issues")
    )
    return [
        f
        for f in findings
        if str(read_key(f, "severity", "risk", "level", "impact") or "").strip().lower()
        in STOPPING_SEVERITIES
    ]


def _number(value: object) -> Optional[float]:
    try:
        if isinstance(value, bool) or value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_rows(rows: object, *fields: str) -> Optional[float]:
    """Total a line-item list, taking each row's first present field.

    The fields are alternatives for the same number, not separate costs: a model that
    gives a range writes `high_usd`, one that gives a point estimate may only write
    `low_usd`, and adding both would double-count the line. The choice is made *per
    row*, because one list routinely mixes the two — picking a winning field for the
    whole list silently drops every row that used the other one.
    """
    rows = _as_rows(rows)
    if not rows:
        return None
    total = 0.0
    counted = False
    for row in rows:
        present = [v for v in (_number(read_key(row, f)) for f in fields) if v is not None]
        if not present:
            continue
        # A placeholder zero in the preferred field is not this line's cost. Same rule
        # as the reported total above it: the first real number wins, and a zero only
        # stands when every alternative on the row is also zero.
        total += next((v for v in present if v), 0.0)
        counted = True
    return total if counted else None


def projected_monthly_cost(output: object) -> Optional[float]:
    """Ledger's projected monthly run cost in USD, or None when it reported none.

    The agent is asked for a total, but a model that skips it still itemises — so
    fall back to adding the line items up rather than treating a missing total as
    "free" and sailing past a cap the reviewer set.
    """
    if not isinstance(output, dict):
        return None

    # A reported total above zero is the answer. A reported *zero* is usually a field
    # the model left at its placeholder while itemising real money underneath it, and
    # taking it at face value is how a $490/month build slips under a $100 cap — so a
    # zero is remembered rather than returned, and only stands if nothing else does.
    # It cannot short-circuit the other total either: `high: 0, low: 50` is a build
    # that costs 50.
    zero_reported = False
    for names in (
        ("total_monthly_high_usd", "total_monthly_cost_usd", "monthly_total_usd"),
        ("total_monthly_low_usd", "estimated_monthly_cost_usd", "total_monthly_usd"),
    ):
        value = _number(read_key(output, *names))
        if value:
            return value
        zero_reported = zero_reported or value is not None

    # Infrastructure and third-party spend are different money and both count.
    parts = [
        total
        for total in (
            _sum_rows(
                read_key(output, "monthly_infra_cost", "infrastructure_cost", "infra_costs"),
                "high_usd",
                "low_usd",
            ),
            _sum_rows(
                read_key(
                    output,
                    "api_or_third_party_cost",
                    "third_party_cost",
                    "api_costs",
                    "api_cost",
                ),
                "monthly_usd",
            ),
        )
        if total is not None
    ]
    if parts:
        return sum(parts)
    return 0.0 if zero_reported else None


# ── the decision ─────────────────────────────────────────────────────────────
def decide_gate(
    project, phase_key: str, output: object, schema_status: Optional[str] = None
) -> Optional[Gate]:
    """Should the pipeline stop after `phase_key`? Returns the gate, or None.

    `project` is the live row: the policy is re-read on every phase so switching a
    run to unattended — or adding gates back to one going sideways — takes effect
    from the next handoff rather than at the next restart.

    `schema_status` is what validation made of the agent's output. When a gate's own
    phase failed it, the check that gate performs did not really happen, and the run
    stops so a person does it instead.
    """
    mode = project.effective_approval_mode

    if mode == ApprovalMode.UNATTENDED.value:
        return None
    if mode == ApprovalMode.EVERY_PHASE.value:
        return Gate(GateKind.PHASE.value)

    # ── checkpoints: two decisions, plus what the run itself raises ──
    unchecked = unchecked_note(phase_key, schema_status)
    overrun = (
        cost_overrun_note(project, output)
        if phase_key == Phase.COST_ESTIMATION.value
        else None
    )

    if phase_key == SHIP_GATE_PHASE.value:
        # Ledger is the last phase, so an overrun cannot interrupt a build that has
        # already finished. What it changes is what this stop *is*: a review of a
        # finished product, one that costs more than you allowed, or one whose cost
        # nobody could read.
        if overrun:
            return Gate(GateKind.COST.value, overrun)
        return Gate(GateKind.SHIP.value, unchecked)

    if overrun:
        # Reachable only if Ledger stops being the last phase — then an overrun is a
        # real mid-run interrupt, and this is where it fires.
        return Gate(GateKind.COST.value, overrun)

    if phase_key == PLAN_GATE_PHASE.value:
        return Gate(GateKind.PLAN.value)

    if phase_key == Phase.SECURITY_ENGINEER.value:
        severe = severe_findings(output)
        if severe:
            return Gate(GateKind.SECURITY.value, _security_note(severe))
        if unchecked:
            # Warden's report is unreadable, so "no severe findings" is not a fact —
            # it is the absence of one. Stop rather than infer the reassuring half,
            # and say which of the two happened: a SECURITY gate would announce a
            # finding that was never made.
            return Gate(GateKind.UNCHECKED.value, unchecked)

    return None


def unchecked_note(phase_key: str, schema_status: Optional[str]) -> Optional[str]:
    """"Warden's report didn't match its shape, so the check didn't really run."""
    if phase_key not in GATED_PHASES or schema_status != SchemaStatus.INVALID.value:
        return None
    if phase_key == Phase.SECURITY_ENGINEER.value:
        return (
            "Warden's report did not match the shape the security check reads, so that "
            "check could not run. Read the findings yourself before shipping this."
        )
    return (
        "Ledger's estimate did not match the shape the cost check reads, so the cap on "
        "this build could not be checked. Read the numbers yourself."
    )


def cost_overrun_note(project, output: object) -> Optional[str]:
    """"Projected $420/mo is over your $100/mo cap." — or None when it isn't."""
    cap = _number(getattr(project, "cost_cap_usd", None))
    if cap is None or cap <= 0:
        return None
    projected = projected_monthly_cost(output)
    if projected is None or projected <= cap:
        return None
    return (
        f"Ledger projects ${projected:,.0f}/month to run this, over the "
        f"${cap:,.0f}/month cap you set for this build."
    )


def _security_note(severe: list[dict]) -> str:
    """"Warden raised 2 findings at high severity or above — SQL injection, XSS." """
    named = (str(f.get("category") or f.get("title") or "").strip() for f in severe)
    # Deduplicate before taking three, or four findings across three categories can
    # report two of them and drop the one the reviewer most needed to see.
    kinds = list(dict.fromkeys(k for k in named if k))[:3]
    count = len(severe)
    subject = f"{count} finding{'' if count == 1 else 's'} at high severity or above"
    return f"Warden raised {subject} — {', '.join(kinds)}." if kinds else f"Warden raised {subject}."
