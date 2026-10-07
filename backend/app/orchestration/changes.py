"""Change requests on a finished build (#79): "now add login".

A change is the pipeline, scoped. A cheap scoping call (`agents.change_planner`) reads
the request against the app's files and names who changes what; the engineers it names
then *edit* their own files — shown the current code, asked only for the files they add,
change or delete — and the rest of the crew re-checks the result: QA writes tests for
the change, Warden rescans the changed tree, Ledger re-estimates. The compile check, the
real build, the tests, the scanners and the fix loop all apply, because it runs through
the same graph as the first build.

It ends at the Ship review, shown as a review of the change (with its diff). Approve and
it becomes the next version; discard and the version it was made on is put back. A
change that gets stuck parks *the change*: the version before it still ships.

This module is the bookkeeping: which change is open, what state it is in, and what each
phase is told. The driving is `runner.change`.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session, object_session

from app.core.constants import GateKind, Phase, PipelineStatus
from app.core.logging import get_logger
from app.db.models import ChangeRequest, Project

log = get_logger(__name__)

#: The stored states. What an open change is doing is read off the build (`state`).
OPEN = "open"
DONE = "done"
FAILED = "failed"
DISCARDED = "discarded"

#: The phases a change can ask to edit their own files. System Design is reached
#: through `needs_design`; the rest re-check what the edit did (see `crew_for`).
EDITORS = (Phase.BACKEND_ENGINEER.value, Phase.FRONTEND_ENGINEER.value, Phase.DEVOPS_ENGINEER.value)
#: Re-run on every change, in edit mode: QA writes tests for the change.
TESTERS = (Phase.QA_ENGINEER.value,)
#: Re-run in full on every change: an audit and an estimate are of the whole app.
REVIEWERS = (Phase.SECURITY_ENGINEER.value, Phase.COST_ESTIMATION.value)

#: Words that point at one side when the scoping call can't say (no model, or a reply
#: naming nothing it knows). A miss falls back to every code phase the app has.
_BACKEND_WORDS = re.compile(
    r"\b(api|endpoint|route|server|backend|database|db|table|schema|model|auth|login|"
    r"signup|sign up|password|token|webhook|email|cron|health)\b",
    re.I,
)
_FRONTEND_WORDS = re.compile(
    r"\b(page|screen|button|form|ui|ux|layout|style|color|colour|theme|dark mode|font|"
    r"navbar|nav|menu|modal|table view|list|card|frontend|component|icon|image|login page|"
    r"sortable|responsive|mobile)\b",
    re.I,
)


def open_change(db: Session, project: Project) -> Optional[ChangeRequest]:
    return (
        db.query(ChangeRequest)
        .filter(ChangeRequest.project_id == project.id, ChangeRequest.status == OPEN)
        .order_by(ChangeRequest.number.desc())
        .first()
    )


def all_for(db: Session, project: Project) -> list[ChangeRequest]:
    return (
        db.query(ChangeRequest)
        .filter(ChangeRequest.project_id == project.id)
        .order_by(ChangeRequest.number.desc())
        .all()
    )


def next_number(db: Session, project: Project) -> int:
    newest = (
        db.query(ChangeRequest.number)
        .filter(ChangeRequest.project_id == project.id)
        .order_by(ChangeRequest.number.desc())
        .first()
    )
    return int(newest[0]) + 1 if newest else 1


def state(project: Project, change: ChangeRequest) -> str:
    """What a change is doing: planning | running | awaiting_approval | needs_help |
    stopped — or done | failed | discarded once it's over."""
    if change.status != OPEN:
        return change.status
    if project.status == PipelineStatus.RUNNING.value:
        return "running" if change.plan else "planning"
    if project.status == PipelineStatus.AWAITING_APPROVAL.value:
        return "needs_help" if project.gate_kind == GateKind.NEEDS_HELP.value else "awaiting_approval"
    return "stopped"


def out(project: Project, change: ChangeRequest, versions_by_id: Optional[dict] = None) -> dict:
    """One change as the API shows it."""
    from app.orchestration.versions import _iso

    produced = None
    if change.version_id:
        found = (versions_by_id or {}).get(change.version_id)
        if found is None:
            db = object_session(change)
            if db is not None:
                from app.db.models import Version

                found = db.get(Version, change.version_id)
        produced = found.number if found is not None else None
    return {
        "id": change.id,
        "number": change.number,
        "text": change.text,
        "status": state(project, change),
        "plan": change.plan,
        "note": change.note,
        "version": produced,
        "created_at": _iso(change.created_at),
        "finished_at": _iso(change.finished_at),
    }


def open_summary(project: Project) -> Optional[dict]:
    """The open change for `ProjectOut` — reads, never writes."""
    db = object_session(project)
    if db is None:
        return None
    found = open_change(db, project)
    return out(project, found) if found is not None else None


# ── the plan ──────────────────────────────────────────────────────────────────
def normalise_plan(raw: object, text: str, has: set[str]) -> dict:
    """The scoping call's answer, held to what this build can do.

    `has` is the phases that produced something. A phase the app doesn't have can't
    edit its files: a change that needs one (a backend for a frontend-only app) is a
    design change. A plan naming nothing usable falls back on the request's words, and
    past those on every code phase the app has — doing too much beats doing nothing.
    """
    data = raw if isinstance(raw, dict) else {}
    wanted = [str(p).strip() for p in data.get("phases") or [] if isinstance(p, str)]
    phases = [p for p in EDITORS if p in wanted and p in has]
    likely = [str(p).strip() for p in data.get("files_likely") or [] if isinstance(p, str) and str(p).strip()][:20]
    needs_db = bool(data.get("needs_db_change"))
    needs_design = bool(data.get("needs_design")) or needs_db
    missing_side = [p for p in EDITORS[:2] if p in wanted and p not in has]
    if missing_side:
        # Asked for a side the app doesn't have: Atlas decides how it fits.
        needs_design = True
    guessed = False
    if not phases:
        for path in likely:
            head = path.lstrip("/").split("/", 1)[0].lower()
            side = {"backend": Phase.BACKEND_ENGINEER.value, "frontend": Phase.FRONTEND_ENGINEER.value}.get(head)
            if side and side in has and side not in phases:
                phases.append(side)
    if not phases:
        guessed = True
        if _BACKEND_WORDS.search(text) and Phase.BACKEND_ENGINEER.value in has:
            phases.append(Phase.BACKEND_ENGINEER.value)
        if _FRONTEND_WORDS.search(text) and Phase.FRONTEND_ENGINEER.value in has:
            phases.append(Phase.FRONTEND_ENGINEER.value)
    if not phases:
        phases = [p for p in EDITORS[:2] if p in has]
    phases = [p for p in EDITORS if p in phases]
    summary = str(data.get("summary") or "").strip()[:300]
    return {
        "summary": summary,
        "phases": phases,
        "files_likely": likely,
        "needs_design": needs_design and Phase.SYSTEM_DESIGN.value in has,
        "needs_db_change": needs_db,
        "guessed": guessed,
    }


def edit_phases(plan: dict, has: set[str]) -> list[str]:
    """Every phase that edits its own deliverable in this change, in pipeline order."""
    from app.core.constants import PHASE_ORDER

    wanted = set(plan.get("phases") or [])
    if plan.get("needs_design"):
        wanted.add(Phase.SYSTEM_DESIGN.value)
    wanted |= {p for p in TESTERS if p in has}
    return [p.value for p in PHASE_ORDER if p.value in wanted and p.value in has]


def first_phase(plan: dict, has: set[str]) -> Optional[str]:
    """Where the change starts: the earliest phase that edits."""
    edits = [p for p in edit_phases(plan, has) if p not in TESTERS]
    return edits[0] if edits else None


def note_for(change: dict, phase: str) -> str:
    """What one phase is told the change is: the request, the plan, the app's past."""
    plan = change.get("plan") or {}
    lines = [f"Change request #{change.get('number')}: {change.get('text', '').strip()}"]
    if plan.get("summary"):
        lines.append(f"Plan: {plan['summary']}")
    likely = [p for p in plan.get("files_likely") or [] if _belongs(p, phase)]
    if likely:
        lines.append("Files likely to change: " + ", ".join(likely[:12]))
    if phase == Phase.QA_ENGINEER.value:
        lines.append(
            "Write tests for this change: add test files, or change the ones it affects. "
            "Leave every other test as it is."
        )
    decisions = change.get("decisions") or []
    if decisions:
        lines.append("Already settled in this app (keep these unless the change says otherwise):")
        lines += [f"- {d}" for d in decisions[-8:]]
    return "\n".join(lines)


def editing(change: Optional[dict], phase: str) -> bool:
    """Whether `phase` edits its own deliverable in the change the checkpoint holds."""
    return bool(change) and phase in (change.get("edit") or [])


def landed(change: dict, phase: str, output: object) -> dict:
    """The change's record once `phase` landed an edit: later re-runs of it in this
    change edit this attempt, not the one the change started from."""
    base = dict(change.get("base") or {})
    if isinstance(output, dict):
        base[phase] = output
    done = list(change.get("done") or [])
    if phase not in done:
        done.append(phase)
    return {**change, "base": base, "done": done}


def _belongs(path: str, phase: str) -> bool:
    head = path.lstrip("/").split("/", 1)[0].lower()
    if phase == Phase.BACKEND_ENGINEER.value:
        return head != "frontend"
    if phase == Phase.FRONTEND_ENGINEER.value:
        return head != "backend"
    return True


# ── what the app has settled on ───────────────────────────────────────────────
def first_decisions(project: Project) -> list[str]:
    """The decisions note a build starts with: its stack, from the charter."""
    from app.orchestration.charter import Charter

    charter = Charter.from_dict(project.charter)
    line = charter.summary_line() if charter is not None else ""
    return [f"Stack — {line}"] if line else []


def remember(project: Project, line: str) -> None:
    """Add one line to what this app has settled on (bounded)."""
    notes = list(project.decisions or first_decisions(project))
    line = " ".join(line.split())[:240]
    if line and line not in notes:
        notes.append(line)
    project.decisions = notes[-30:]
