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

#: Phases that can own a finding: the ones that write files **and run before the
#: security review**, in pipeline order.
#:
#: That second condition is not a detail. Sending a finding back to a phase that
#: runs *after* Warden rewinds nothing Warden depends on, so the audit never re-runs
#: — the finding sits at `fix_requested` forever and the build can never be
#: approved. DevOps is the phase this excludes, and excluding it costs nothing:
#: DevOps has not written a line when the audit happens, so Warden has never seen a
#: Dockerfile it could raise a finding about. What it *has* seen is whatever config
#: the backend wrote, which is where those findings belong.
CODE_PHASES: tuple[str, ...] = tuple(
    p.value
    for p in PHASE_ORDER[: [q.value for q in PHASE_ORDER].index(Phase.SECURITY_ENGINEER.value)]
    if p.value
    in {
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
        Phase.QA_ENGINEER.value,
    }
)

#: When a finding names no file this build recognises, its category is the next best
#: evidence of who owns it. Deliberately short: a guess that is merely plausible is
#: worse than handing the decision to the reviewer, because it sends a correct agent
#: back to redo correct work and calls the result a remediation.
_CATEGORY_OWNER: tuple[tuple[str, str], ...] = (
    (r"\bcsrf\b|\bxss\b|\bcors\b|clickjack|content security policy|\bcsp\b",
     Phase.FRONTEND_ENGINEER.value),
    # Secrets and transport land on the backend rather than on DevOps: at the moment
    # the audit runs, the only configuration that exists is the backend's, and DevOps
    # cannot be sent back without stranding the finding (see `CODE_PHASES`).
    (r"sql injection|\bauthz\b|authoriz|authentic|session|password|jwt|token|"
     r"rate.?limit|mass assignment|idor|injection|secret|credential|\benv\b|"
     r"environment variable|tls|https|certificate",
     Phase.BACKEND_ENGINEER.value),
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

    @property
    def serious(self) -> bool:
        """The crew fixes this one itself. See `is_serious`."""
        return is_serious(self.severity, self.category, self.title)


# ── serious (the crew fixes it) and small (a person decides) ─────────────────
#: Severities, most severe first.
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")

#: Categories about how the product looks and reads, rather than whether it is safe.
#: Worth a person's judgement at any severity: "the contrast is low" has no single
#: right fix the way a leaked credential does.
#:
#: Matched against the **category alone**, and only when *every* word in it is one
#: of these. Titles are prose: "Admin API publicly accessible without authentication"
#: is a critical security finding that happens to contain a UI word. And a category
#: like "Insecure Design" or "Content injection" is security, so "design" and
#: "content" are not on the list; "UI / UX" and "Usability & Accessibility (a11y)" are.
_UI_UX_WORDS = frozenset(
    {
        "ui", "ux", "user", "interface", "experience", "visual", "cosmetic", "layout",
        "styling", "typography", "contrast", "spacing", "polish", "usability", "a11y",
        "accessibility", "copy", "copywriting", "wording", "and",
    }
)


def _threshold() -> int:
    from app.core.config import settings

    wanted = str(settings.auto_fix_min_severity or "high").strip().lower()
    if wanted in ("none", "off"):
        return -1  # nothing is the crew's to fix: every severe finding is asked about
    return SEVERITY_ORDER.index(wanted) if wanted in SEVERITY_ORDER else 1


def is_ui_ux(category: str, title: str = "") -> bool:
    """Whether a finding's *category* is about UI/UX. The title is not consulted."""
    words = re.findall(r"[a-z0-9]+", (category or "").lower())
    return bool(words) and all(w in _UI_UX_WORDS for w in words) and words != ["and"]


def is_serious(severity: str, category: str = "", title: str = "") -> bool:
    """Whether a finding is the crew's to fix, rather than a person's to judge.

    Serious means rated at or above `auto_fix_min_severity` (high, by default) and
    not about UI/UX. A leaked database password has one right answer, and asking a
    person whether to fix it is asking them to do the crew's job. Everything else —
    medium and low findings, and polish — is a judgement call and stays one.
    """
    rank = str(severity or "").strip().lower()
    if rank not in SEVERITY_ORDER:
        return False
    return SEVERITY_ORDER.index(rank) <= _threshold() and not is_ui_ux(category, title)


def _text(value: object) -> str:
    return str(value).strip() if has_content(value) else ""


def finding_key(category: str, title: str, owner_phase: Optional[str] = None) -> str:
    """A stable identity for one finding, across the re-audits that follow a fix.

    Derived from what the finding *says* rather than where it sat in a list, because
    the list is regenerated every time the phase re-runs. Without this a waiver would
    last exactly until the next audit, and the reviewer would be asked the same
    question again — which is the same as not having waived it.

    The **location is not** part of it, though it is still displayed: it is the most
    volatile field a model writes, coming back as `authController.js`, then
    `src/controllers/authController.js`, then `authController.js:42`, and each
    rewording would mint a new key and resurrect a waived finding.

    The **owning phase is**, and that is not a detail. "Missing Input Validation" in
    a React form and in a Flask view are two problems with two owners; keyed on words
    alone they merged into one finding assigned to whichever phase ran first, and the
    other file's instance could never be fixed — the agent handed it did not write
    that file, the rerun of the agent who did got no feedback about it, and a bounded
    remediation budget went on a fix that structurally could not land. The owner is
    derived from the file, so it survives the location being reworded; it changes
    only when the finding is genuinely about someone else's work.
    """
    parts = (category, title, owner_phase or "")
    basis = "|".join(re.sub(r"\s+", " ", part.strip().lower()) for part in parts)
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
    # Keyed, not appended, because the identity no longer includes the location and
    # one report routinely says "SQL Injection" about three files. Two rows sharing
    # a key is a dead end — the second becomes invisible to the reconciliation loop
    # and stays open forever, while `fix` and `waive` raise on the ambiguous lookup,
    # leaving a build that cannot be approved, waived or fixed. One finding, every
    # place it was seen.
    out: dict[str, Finding] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _text(read_key(row, "title", "name", "issue", "summary"))
        category = _text(read_key(row, "category", "type", "class"))
        location = _text(read_key(row, "location", "file", "path", "component", "where"))
        severity = _text(read_key(row, "severity", "risk", "level", "impact")).lower()
        if not (title or category):
            continue
        recommendation = _text(
            read_key(row, "recommendation", "remediation", "fix", "mitigation")
        )
        owner = _owner_for(location, owned) or _owner_by_category(category, title)
        key = finding_key(category, title, owner)
        seen = out.get(key)
        if seen is not None:
            out[key] = Finding(
                key=key,
                title=seen.title,
                # The worst severity anyone gave it wins: the same issue reported as
                # high in one file and medium in another is a high finding.
                severity=_worst(seen.severity, severity),
                category=seen.category or category,
                location=_join_locations(seen.location, location),
                recommendation=seen.recommendation or recommendation,
                # Same by construction — the owner is part of the key — but stated
                # rather than assumed, so this cannot drift if the key ever changes.
                owner_phase=seen.owner_phase or owner,
            )
            continue
        out[key] = Finding(
            key=key,
            title=title or category,
            severity=severity,
            category=category,
            location=location,
            recommendation=recommendation,
            owner_phase=owner,
        )
    return list(out.values())


def _worst(*severities: str) -> str:
    """The most severe of several spellings of one finding's severity."""
    order = ["critical", "high", "medium", "low"]
    ranked = [s for s in severities if s in order]
    return min(ranked, key=order.index) if ranked else next((s for s in severities if s), "")


def _join_locations(*locations: str) -> str:
    """Every place one finding was seen, deduplicated, in the order reported."""
    kept = list(dict.fromkeys(loc for loc in locations if loc))
    return ", ".join(kept)


def _earliest(*phases: Optional[str]) -> Optional[str]:
    """The owner that runs first, so a fix rebuilds the others rather than repeating."""
    order = [p.value for p in PHASE_ORDER]
    known = [p for p in phases if p in order]
    return min(known, key=order.index) if known else next((p for p in phases if p), None)


def severe(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severe]


#: How every automatic fix note starts, so a later round can tell its own notes
#: apart from a person's and clear the ones that no longer apply.
FIX_NOTE_PREFIX = "The security review found problems"

#: The approach each round takes. Repeating the same prompt to the same model is the
#: one thing self-repair research says not to do, so each round adds something.
STRATEGY_GUIDED = "guided"  # the finding, with advice checked against the skills
STRATEGY_WITH_CODE = "with_code"  # + the code it is about, and "the last try failed"
STRATEGY_STRONGER = "stronger_model"  # + the most capable model the router has


def strategy_for(round_number: int) -> str:
    if round_number <= 1:
        return STRATEGY_GUIDED
    if round_number == 2:
        return STRATEGY_WITH_CODE
    return STRATEGY_STRONGER


def _mentions(text: str, terms: Iterable[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", low) for t in terms if t)


def guidance_for(finding: Finding) -> list:
    """The skills that are the trusted fix for this finding, best known first.

    Matched on what the finding is *about* (title and category), not on the advice
    attached to it — the advice is the thing being checked.
    """
    try:
        from app.skills import registry

        library = registry.library()
        disabled = registry.disabled_names()
    except Exception as e:  # noqa: BLE001 - a missing library must not stop a fix
        log.warning("Skill library unavailable for fix guidance: %s", e)
        return []
    about = f"{finding.title} {finding.category}"
    return [
        s
        for s in library
        if s.fixes and s.usable and s.name not in disabled and _mentions(about, s.fixes)
    ]


def checked_recommendation(finding: Finding, skills: Optional[list] = None) -> Optional[str]:
    """The reviewer's advice, or None when a governing skill contradicts it.

    Warden once told an agent to fix CSRF with Helmet, which only sets headers; the
    agent did as told, the re-audit found the same hole, and the round was wasted.
    Advice that names something a governing skill rejects is dropped, and the
    skill's own procedure goes in its place.
    """
    rec = finding.recommendation
    if not rec:
        return None
    for skill in skills if skills is not None else guidance_for(finding):
        if _mentions(rec, skill.rejects):
            log.info(
                "Dropped advice for '%s' that contradicts the %s skill: %s",
                finding.title,
                skill.name,
                rec,
            )
            return None
    return rec


def snippet(project, finding: Finding, owner: Optional[str], limit: int = 1600) -> Optional[str]:
    """The code a finding is about, as it stands now — what round 2 shows the agent.

    Read from the owning phase's current deliverable. With a line number, the lines
    around it; without one, the top of the file. None when the file cannot be found,
    which is normal for an app-wide finding.
    """
    if project is None or not owner or not finding.location:
        return None
    from app.core.artifacts import current_phases

    row = next((p for p in current_phases(project) if p.phase == owner), None)
    if row is None or not isinstance(row.output, dict):
        return None
    files = list(iter_files(row.output))
    hay = finding.location.lower().replace("\\", "/")
    best = None
    for path, content, _lang in files:
        candidate = path.lower()
        base = candidate.rsplit("/", 1)[-1]
        if candidate in hay or (len(base) > 4 and base in hay):
            if best is None or len(path) > len(best[0]):
                best = (path, content or "")
    if best is None:
        return None
    path, content = best
    lines = content.splitlines()
    found = re.search(r":(\d+)", finding.location)
    if found:
        at = max(int(found.group(1)) - 1, 0)
        start = max(at - 10, 0)
        chosen = lines[start : at + 11]
        first = start + 1
    else:
        chosen, first = lines[:40], 1
    text = "\n".join(f"{first + i:>4}  {line}" for i, line in enumerate(chosen))
    if len(text) > limit:
        text = text[:limit] + "\n      …"
    return f"`{path}` as it is now:\n{text}" if text.strip() else None


def fix_instruction(
    items: Iterable[Finding],
    strategy: str = STRATEGY_GUIDED,
    project=None,
    owner: Optional[str] = None,
    round_number: int = 1,
) -> str:
    """The note that goes back with the phase — a work order, not a complaint.

    Every severe finding this phase owns, in one message, because sending them one at
    a time would re-run the whole back half of the pipeline once per finding.

    What each finding carries depends on the round. Every round gets the finding and
    the advice for it — checked against the skills library, so advice a governing
    skill contradicts is replaced by the skill's own procedure. From the second
    round the agent also sees the code as it stands and is told plainly that the
    previous attempt did not fix it.
    """
    items = list(items)
    lines = []
    procedures: dict[str, object] = {}
    for f in items:
        skills = guidance_for(f)
        for skill in skills:
            procedures.setdefault(skill.name, skill)
        rec = checked_recommendation(f, skills)
        where = f" in `{f.location}`" if f.location else ""
        app_wide = (
            " This is an app-wide concern rather than one file: fix it wherever the app "
            "handles it."
            if not f.owner_phase
            else ""
        )
        if rec:
            fix = f" Apply this fix: {rec}"
        elif skills:
            fix = f" Fix it the way the {skills[0].title.lower()} procedure below says."
        else:
            fix = ""
        lines.append(f"- [{f.severity or 'unrated'}] {f.title}{where}.{app_wide}{fix}")
        if strategy != STRATEGY_GUIDED:
            code = snippet(project, f, owner)
            if code:
                lines.append(_indent(code))

    retry = ""
    if strategy != STRATEGY_GUIDED:
        retry = (
            f"\n\nThis is fix round {round_number}. The previous attempt did not fix "
            "these — the re-check still reports every one of them. Do not repeat that "
            "attempt: change the code shown above, and make sure the fix is actually in "
            "the files you return."
        )
    if strategy == STRATEGY_STRONGER:
        retry += (
            " This is the last automatic round. Rewrite the affected code properly rather "
            "than patching around it — whatever the earlier rounds kept is what did not work."
        )
    guidance = ""
    if procedures:
        from app.skills.loader import render

        guidance = "\n\nTrusted procedure for these fixes:\n\n" + "\n\n".join(
            render(skill) for skill in procedures.values()
        )
    return (
        f"{FIX_NOTE_PREFIX} in the files you wrote. Fix all of them "
        "and return the complete deliverable:\n"
        + "\n".join(lines)
        + retry
        + guidance
        + "\n\nKeep everything that was already correct, and keep the same stack — "
        "the fix is to the code, not to the technology choices."
    )


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def route_owner(f: Finding) -> str:
    """Who fixes a finding: the phase that wrote its file, or whoever owns its
    category — and when neither says, the backend, as an app-wide concern.

    A finding with no owner used to skip the fix loop entirely, and those were
    exactly the ones that needed it: CSRF and session handling are app-wide.
    """
    return f.owner_phase or Phase.BACKEND_ENGINEER.value


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
def sync_dispositions(db, project, output: object, readable: bool = True) -> list:
    """Record this audit's findings against the ones already being tracked.

    Called after every security phase, including the re-audit that follows a fix, so
    it has to answer four questions at once and get all four right:

      * a finding the audit no longer reports, and which was sent back to be fixed,
        is **fixed** — that is what a re-audit not finding it means;
      * a finding that vanished without having been sent back is **gone**, not fixed.
        It stops blocking, because refusing to ship over something the current report
        does not mention is asking the reviewer to waive a finding that is not there
        — but it is not called a remediation, because nobody performed one;
      * a finding the reviewer **waived** stays waived when it reappears, or waiving
        would last until the next audit and mean nothing;
      * anything else is open, and the gate will ask about it.

    `readable` is what stops the second rule becoming a hole. An audit that failed
    its own schema yields no findings at all, and treating that as "everything went
    away" would clear a critical finding because nobody could parse the report that
    raised it. When the report is unreadable, this only adds — it never resolves.
    """
    from app.db.models import SecurityDisposition

    findings = read_findings(output, project)
    # Ordered, so "which row wins when two share a key" has an answer that does not
    # depend on how SQLite felt like returning them. The routes settle every row with
    # the key, so this only decides which one carries the merged detail forward.
    existing: dict[str, "SecurityDisposition"] = {}
    for row in (
        db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
    ):
        existing.setdefault(row.finding_key, row)
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
        if key in seen or row.status in FindingStatus.settled() or not readable:
            continue
        # Gone from the report. Only a finding that was actually sent back can be
        # called *fixed* — one that simply stopped being mentioned is a rebuild that
        # may have removed it or a model being inconsistent, and this cannot tell
        # which. Both stop blocking; only one claims a remediation happened.
        if row.status == FindingStatus.FIX_REQUESTED.value:
            row.status = FindingStatus.FIXED.value
            log.info("Security finding fixed and re-audited: %s (%s)", row.title, project.id)
        else:
            row.status = FindingStatus.GONE.value
            log.info("Security finding no longer reported: %s (%s)", row.title, project.id)

    db.commit()
    return list(
        db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
    )


def row_is_serious(row) -> bool:
    return is_serious(row.severity, row.category or "", row.title or "")


def unresolved(db, project, serious: Optional[bool] = None) -> list:
    """Severe findings that have been neither fixed nor waived.

    `serious=True` is the crew's to-do list, `serious=False` the reviewer's question,
    and `None` both — what has to be settled before a build ships.
    """
    from app.db.models import SecurityDisposition

    return [
        row
        for row in db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
        if (row.severity in STOPPING_SEVERITIES or row_is_serious(row))
        and row.status not in FindingStatus.settled()
        and (serious is None or row_is_serious(row) == serious)
    ]


__all__ = [
    "CODE_PHASES",
    "Finding",
    "finding_key",
    "fix_instruction",
    "group_by_owner",
    "is_serious",
    "route_owner",
    "read_findings",
    "severe",
    "severe_findings",
    "sync_dispositions",
    "unresolved",
]
