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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.constants import (
    ApprovalMode,
    GateKind,
    PLAN_GATE_PHASE,
    Phase,
    SHIP_GATE_PHASE,
)

#: Severities Warden is allowed to stop an unattended build over. Anything below this
#: is worth reading at the Ship review, not worth interrupting a run for.
STOPPING_SEVERITIES = frozenset({"critical", "high"})


@dataclass(frozen=True)
class Gate:
    """A stop: which review to render, and the one line that says why it happened."""

    kind: str
    note: Optional[str] = None


# ── reading what an agent reported ───────────────────────────────────────────
def severe_findings(output: object) -> list[dict]:
    """Security findings severe enough to interrupt, newest schema or not.

    Models are inconsistent about case and about wrapping the list, so this reads
    leniently: what matters is that a `severity` says critical or high.
    """
    if not isinstance(output, dict):
        return []
    findings = output.get("findings")
    if not isinstance(findings, list):
        return []
    return [
        f
        for f in findings
        if isinstance(f, dict)
        and str(f.get("severity", "")).strip().lower() in STOPPING_SEVERITIES
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
    if not isinstance(rows, list):
        return None
    total = 0.0
    counted = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in fields:
            value = _number(row.get(field))
            if value is not None:
                total += value
                counted = True
                break
    return total if counted else None


def projected_monthly_cost(output: object) -> Optional[float]:
    """Ledger's projected monthly run cost in USD, or None when it reported none.

    The agent is asked for a total, but a model that skips it still itemises — so
    fall back to adding the line items up rather than treating a missing total as
    "free" and sailing past a cap the reviewer set.
    """
    if not isinstance(output, dict):
        return None

    reported: Optional[float] = None
    for key in ("total_monthly_high_usd", "total_monthly_low_usd"):
        reported = _number(output.get(key))
        if reported is not None:
            break

    # A reported total above zero is the answer. A reported *zero* is usually a field
    # the model left at its placeholder while itemising real money underneath it, and
    # taking it at face value is how a $490/month build slips under a $100 cap.
    if reported:
        return reported

    # Infrastructure and third-party spend are different money and both count.
    parts = [
        total
        for total in (
            _sum_rows(output.get("monthly_infra_cost"), "high_usd", "low_usd"),
            _sum_rows(output.get("api_or_third_party_cost"), "monthly_usd"),
        )
        if total is not None
    ]
    if parts:
        return sum(parts)
    return reported


# ── the decision ─────────────────────────────────────────────────────────────
def decide_gate(project, phase_key: str, output: object) -> Optional[Gate]:
    """Should the pipeline stop after `phase_key`? Returns the gate, or None.

    `project` is the live row: the policy is re-read on every phase so switching a
    run to unattended — or adding gates back to one going sideways — takes effect
    from the next handoff rather than at the next restart.
    """
    mode = project.effective_approval_mode

    if mode == ApprovalMode.UNATTENDED.value:
        return None
    if mode == ApprovalMode.EVERY_PHASE.value:
        return Gate(GateKind.PHASE.value)

    # ── checkpoints: two decisions, plus what the run itself raises ──
    overrun = (
        cost_overrun_note(project, output)
        if phase_key == Phase.COST_ESTIMATION.value
        else None
    )

    if phase_key == SHIP_GATE_PHASE.value:
        # Ledger is the last phase, so an overrun cannot interrupt a build that has
        # already finished. What it changes is what this stop *is*: a review of a
        # finished product, or a review of one that costs more than you allowed.
        return Gate(GateKind.COST.value if overrun else GateKind.SHIP.value, overrun)

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

    return None


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
