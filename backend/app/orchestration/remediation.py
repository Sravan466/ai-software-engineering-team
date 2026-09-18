"""Routing a security finding back to whoever wrote the file it is about.

Warden's output used to be terminal. The run that prompted this shipped a documented
**high-severity "No CSRF Protection in Frontend"**, with a written remediation, and
the code went into the archive unchanged — because a finding landed in a report and
the pipeline moved on to DevOps. Nothing connected the finding to the agent who could
fix it, so nothing was ever fixed.

The connection is the file. Every finding names a location; every code phase declares
the files it wrote. Matching one to the other gives the finding an owner, and an owner
is all the existing `redo` path needs: it already re-runs one phase and rewinds
everything built on top of it, which is precisely a fix followed by a re-audit.

Where the match fails — an architectural finding, a location the model invented — the
answer is not to guess an owner. It is to say so, and let the reviewer decide, which
is the other half of the outcome this module exists to guarantee: a severe finding
leaves this pipeline fixed and re-audited, or waived on the record. Never neither.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.core.artifacts import iter_files
from app.core.constants import PHASE_ORDER, FindingStatus, Phase
from app.core.logging import get_logger
from app.core.reading import has_content
from app.orchestration.approval import STOPPING_SEVERITIES, read_key, severe_findings

log = get_logger(__name__)

#: Phases that write files a finding can be about, in pipeline order. A finding is
#: routed to the earliest owner, because fixing an earlier phase rebuilds the later
#: ones anyway — sending the same problem to Frontend and then to DevOps would run
#: the back half of the pipeline twice for one fix.
CODE_PHASES: tuple[str, ...] = (
    Phase.BACKEND_ENGINEER.value,
    Phase.FRONTEND_ENGINEER.value,
    Phase.QA_ENGINEER.value,
    Phase.DEVOPS_ENGINEER.value,
)

#: When a finding names no file this build recognises, its category is the next best
#: evidence of who owns it. Deliberately short: a guess that is merely plausible is
#: worse than handing the decision to the reviewer, because it sends a correct agent
#: back to redo correct work and calls the result a remediation.
_CATEGORY_OWNER: tuple[tuple[str, str], ...] = (
    (r"\bcsrf\b|\bxss\b|\bcors\b|clickjack|content security policy|\bcsp\b",
     Phase.FRONTEND_ENGINEER.value),
    (r"sql injection|\bauthz\b|authoriz|authentic|session|password|jwt|token|"
     r"rate.?limit|mass assignment|idor|injection",
     Phase.BACKEND_ENGINEER.value),
    (r"secret|credential|\benv\b|environment variable|docker|container|image|"
     r"tls|https|certificate|pipeline|workflow",
     Phase.DEVOPS_ENGINEER.value),
)


@dataclass(frozen=True)
class Finding:
    """One finding, with the phase that owns the file it is about."""

    key: str
    title: str
    severity: str
    category: str
    location: str
    recommendation: str
    owner_phase: Optional[str]

    @property
    def severe(self) -> bool:
        return self.severity in STOPPING_SEVERITIES


def _text(value: object) -> str:
    return str(value).strip() if has_content(value) else ""


def finding_key(category: str, title: str, location: str) -> str:
    """A stable identity for one finding, across the re-audits that follow a fix.

    Derived from what the finding *says* rather than where it sat in a list, because
    the list is regenerated every time the phase re-runs. Without this a waiver would
    last exactly until the next audit, and the reviewer would be asked the same
    question again — which is the same as not having waived it.
    """
    basis = "|".join(re.sub(r"\s+", " ", part.strip().lower()) for part in (category, title, location))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _owned_paths(project) -> list[tuple[str, str]]:
    """(path, phase) for every file this build's code phases produced, in order."""
    from app.core.artifacts import current_phases

    owned: list[tuple[str, str]] = []
    by_phase = {ph.phase: ph for ph in current_phases(project)}
    for phase_key in CODE_PHASES:
        row = by_phase.get(phase_key)
        if row is None or not isinstance(row.output, dict):
            continue
        for path, _content, _lang in iter_files(row.output):
            owned.append((path, phase_key))
    return owned


def _owner_for(location: str, owned: Iterable[tuple[str, str]]) -> Optional[str]:
    """Which phase wrote the file a finding points at.

    Matched on the basename as well as the full path: models write locations as
    `src/controllers/authController.js`, as `authController.js`, and as
    `authController.js:42`, and all three mean the same file. The longest matching
    path wins, so `user.js` does not claim `admin/user.js` when both exist.
    """
    if not location:
        return None
    hay = location.lower().replace("\\", "/")
    best: Optional[tuple[int, str]] = None
    for path, phase_key in owned:
        candidate = path.lower()
        base = candidate.rsplit("/", 1)[-1]
        if candidate in hay or (len(base) > 4 and base in hay):
            score = len(candidate)
            if best is None or score > best[0]:
                best = (score, phase_key)
    return best[1] if best else None


def _owner_by_category(category: str, title: str) -> Optional[str]:
    text = f"{category} {title}".lower()
    for pattern, phase_key in _CATEGORY_OWNER:
        if re.search(pattern, text):
            return phase_key
    return None


def read_findings(output: object, project=None) -> list[Finding]:
    """Warden's findings, each with an owner where one can be established.

    Reading is as lenient as the security gate's, and for the same reason: this has
    to survive a model that renamed the list or wrapped a single finding in nothing.
    """
    rows = read_key(output, "findings", "security_findings", "vulnerabilities", "issues")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []

    owned = _owned_paths(project) if project is not None else []
    out: list[Finding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _text(read_key(row, "title", "name", "issue", "summary"))
        category = _text(read_key(row, "category", "type", "class"))
        location = _text(read_key(row, "location", "file", "path", "component", "where"))
        severity = _text(read_key(row, "severity", "risk", "level", "impact")).lower()
        if not (title or category):
            continue
        out.append(
            Finding(
                key=finding_key(category, title, location),
                title=title or category,
                severity=severity,
                category=category,
                location=location,
                recommendation=_text(
                    read_key(row, "recommendation", "remediation", "fix", "mitigation")
                ),
                owner_phase=_owner_for(location, owned) or _owner_by_category(category, title),
            )
        )
    return out


def severe(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severe]


def fix_instruction(items: Iterable[Finding]) -> str:
    """The note that goes back with the phase — a work order, not a complaint.

    Every severe finding this phase owns, in one message, because sending them one at
    a time would re-run the whole back half of the pipeline once per finding.
    """
    lines = []
    for f in items:
        where = f" in `{f.location}`" if f.location else ""
        fix = f" Apply this fix: {f.recommendation}" if f.recommendation else ""
        lines.append(f"- [{f.severity or 'unrated'}] {f.title}{where}.{fix}")
    return (
        "The security review found problems in the files you wrote. Fix all of them "
        "and return the complete deliverable:\n"
        + "\n".join(lines)
        + "\n\nKeep everything that was already correct, and keep the same stack — "
        "the fix is to the code, not to the technology choices."
    )


def group_by_owner(findings: Iterable[Finding]) -> dict[str, list[Finding]]:
    """Owned findings, keyed by phase, earliest phase first.

    Findings with no owner are left out on purpose: they have no agent to send back
    to, and inventing one would send a correct phase to redo correct work. They reach
    the reviewer instead, which is the honest end of that path.
    """
    order = [p.value for p in PHASE_ORDER]
    grouped: dict[str, list[Finding]] = {}
    for f in findings:
        if f.owner_phase:
            grouped.setdefault(f.owner_phase, []).append(f)
    return dict(sorted(grouped.items(), key=lambda kv: order.index(kv[0])))


# ── persistence ──────────────────────────────────────────────────────────────
def sync_dispositions(db, project, output: object) -> list:
    """Record this audit's findings against the ones already being tracked.

    Called after every security phase, including the re-audit that follows a fix, so
    it has to answer three questions at once and get all three right:

      * a finding the audit no longer reports, and which was sent back to be fixed,
        is **fixed** — that is what a re-audit not finding it means;
      * a finding the reviewer **waived** stays waived when it reappears, or waiving
        would last until the next audit and mean nothing;
      * anything else is open, and the gate will ask about it.
    """
    from app.db.models import SecurityDisposition

    findings = read_findings(output, project)
    existing = {
        row.finding_key: row
        for row in db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .all()
    }
    seen: set[str] = set()

    for f in findings:
        seen.add(f.key)
        row = existing.get(f.key)
        if row is None:
            db.add(
                SecurityDisposition(
                    project_id=project.id,
                    finding_key=f.key,
                    title=f.title,
                    severity=f.severity,
                    category=f.category,
                    location=f.location,
                    recommendation=f.recommendation,
                    owner_phase=f.owner_phase,
                    status=FindingStatus.OPEN.value,
                )
            )
            continue
        # Still reported. A waiver is the reviewer's standing decision and survives;
        # anything else goes back to open, including a fix that did not take.
        if row.status != FindingStatus.WAIVED.value:
            row.status = FindingStatus.OPEN.value
        row.severity = f.severity or row.severity
        row.location = f.location or row.location
        row.recommendation = f.recommendation or row.recommendation
        row.owner_phase = f.owner_phase or row.owner_phase

    for key, row in existing.items():
        if key in seen or row.status == FindingStatus.WAIVED.value:
            continue
        # Gone from the report. Only a finding that was actually sent back can be
        # called fixed — one that simply stopped being mentioned is a model being
        # inconsistent, and recording that as a remediation would be a lie the
        # reviewer has no way to catch.
        if row.status == FindingStatus.FIX_REQUESTED.value:
            row.status = FindingStatus.FIXED.value
            log.info("Security finding fixed and re-audited: %s (%s)", row.title, project.id)

    db.commit()
    return list(
        db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
    )


def unresolved(db, project) -> list:
    """Severe findings that have been neither fixed nor waived — the gate's question."""
    from app.db.models import SecurityDisposition

    return [
        row
        for row in db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
        if row.severity in STOPPING_SEVERITIES and row.status not in FindingStatus.settled()
    ]


__all__ = [
    "CODE_PHASES",
    "Finding",
    "finding_key",
    "fix_instruction",
    "group_by_owner",
    "read_findings",
    "severe",
    "severe_findings",
    "sync_dispositions",
    "unresolved",
]
