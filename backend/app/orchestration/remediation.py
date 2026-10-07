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
import uuid
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


#: Where a finding came from (#77). A scanner's is the platform's evidence; Warden's
#: is its opinion — shown as a review note, never the fix loop's, never blocking.
SOURCE_TOOL = "tool"
SOURCE_MODEL = "model"


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
    #: tool | model. A row from before #77 is the model's.
    source: str = SOURCE_MODEL
    #: For a scanner's finding: which tool, which rule, and exactly where.
    tool: Optional[str] = None
    rule_id: Optional[str] = None
    cwe: Optional[str] = None
    path: Optional[str] = None
    line: Optional[int] = None
    url: Optional[str] = None
    fingerprint: Optional[str] = None
    #: Other rules that reported the same problem at the same place: `tool:rule`.
    also: tuple[str, ...] = ()

    @property
    def severe(self) -> bool:
        return self.severity in STOPPING_SEVERITIES

    @property
    def serious(self) -> bool:
        """The crew fixes this one itself. See `is_serious` and `finding_is_serious`."""
        return finding_is_serious(self.source, self.tool, self.severity, self.category, self.title)


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


def finding_is_serious(
    source: Optional[str], tool: Optional[str], severity: str, category: str = "", title: str = ""
) -> bool:
    """`is_serious`, for a finding whose source is known (#77).

    Only a scanner's finding can be serious. Warden's own are review notes: one model's
    opinion, re-judged by the same model, is not evidence enough to send the crew back
    round after round — a person reads them and decides. Nor is a dependency audit's:
    the platform owns the manifests (`build/packages.py`), so no agent can bump a
    version, and a round spent asking one to would fix nothing.
    """
    from app.build.scan import DEPENDENCY_TOOLS

    if (source or SOURCE_MODEL) != SOURCE_TOOL or (tool or "") in DEPENDENCY_TOOLS:
        return False
    return is_serious(severity, category, title)


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
        location = _text(read_key(row, "path", "location", "file", "component", "where"))
        severity = _text(read_key(row, "severity", "risk", "level", "impact")).lower()
        if not (title or category):
            continue
        recommendation = _text(
            read_key(row, "recommendation", "remediation", "fix", "mitigation")
        )
        path, line = _path_and_line(location, read_key(row, "line", "line_number", "lineNumber"))
        if line and location and not re.search(r":\d+", location):
            location = f"{location}:{line}"
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
                path=seen.path or path,
                line=seen.line or line,
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
            path=path,
            line=line,
        )
    return list(out.values())


def _path_and_line(location: str, line: object = None) -> tuple[Optional[str], Optional[int]]:
    """A model's `path` (or old `location`) and `line`, as a file and a line number.

    Models write `authController.js`, `src/controllers/authController.js:42` and
    `authController.js (line 42)`; the file is everything before the first `:` or
    space, and the line is the `line` field or the number after it.
    """
    text = (location or "").strip().replace("\\", "/")
    found = re.match(r"\s*`?([^\s:`,()]+)`?(?::(\d+))?", text)
    # The `./` prefix only: `lstrip` would take the dot off `.env` too.
    path = _unprefixed(found.group(1)) if found else None
    number: Optional[int] = None
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        number = line
    elif isinstance(line, str) and re.search(r"\d+", line):
        number = int(re.search(r"\d+", line).group())
    elif found and found.group(2):
        number = int(found.group(2))
    elif re.search(r"\bline\s+(\d+)", text, re.IGNORECASE):
        number = int(re.search(r"\bline\s+(\d+)", text, re.IGNORECASE).group(1))
    return (path or None), number


def tool_key(tool: str, rule_id: str, path: str, line: Optional[int]) -> str:
    """A scanner finding's identity when first seen: `tool:rule_id:path:line bucket`.

    The bucket is four lines wide, so two reports of one rule that `scan.dedupe` kept
    apart (more than three lines between them) can never share it. Later rescans
    recognise the same finding by `same_tool_finding`, not by this key — a fix that
    moves the code down the file keeps the finding it hasn't fixed.
    """
    from app.build.scan import DEPENDENCY_TOOLS, LINE_WINDOW

    # A dependency is its package in its manifest: where it sits in the file moves as
    # packages are added above it, and says nothing about which finding it is.
    bucket = 0 if tool in DEPENDENCY_TOOLS else (line or 0) // (LINE_WINDOW + 1)
    basis = f"{tool}:{rule_id}:{path}:{bucket}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def tool_findings(scan_record: object) -> list[Finding]:
    """The scanners' findings on a Warden row (`PhaseResult.scan`), as `Finding`s."""
    from app.build import scan

    out = []
    for f in scan.findings_of(scan_record):
        advice = " ".join(x for x in (f.message if f.message.strip() != f.title.strip() else "", f.fix_hint) if x)
        out.append(
            Finding(
                key=tool_key(f.tool, f.rule_id, f.path, f.line),
                title=f.title or f.rule_id,
                severity=f.severity,
                category=f.category,
                location=f.location,
                recommendation=advice,
                owner_phase=f.owner_phase,
                source=SOURCE_TOOL,
                tool=f.tool,
                rule_id=f.rule_id,
                cwe=f.cwe,
                path=f.path,
                line=f.line,
                url=f.url,
                fingerprint=f.fingerprint,
                also=tuple(f.also),
            )
        )
    return out


def _same_file(a: Optional[str], b: Optional[str]) -> bool:
    """One file, however much of its path each side wrote: `users.js` is
    `backend/routes/users.js`, but never `admin/users.js` vs `routes/users.js`."""
    if not a or not b:
        return False
    a, b = _unprefixed(a.lower()), _unprefixed(b.lower())
    if a == b or a.endswith("/" + b) or b.endswith("/" + a):
        return True
    # The tree renames an agent's own root (`server/app/x.py` is placed at
    # `backend/app/x.py`): the same path below the first folder is the same file — but
    # only between the agent's own root and a placed one. `backend/src/index.js` and
    # `frontend/src/index.js` are two files. (The page's `samePath` has the same rule.)
    sides = ("backend", "frontend")
    root_a, root_b = a.split("/", 1)[0], b.split("/", 1)[0]
    if (root_a in sides) == (root_b in sides):
        return False
    below = lambda p: p.split("/", 1)[1] if "/" in p else ""  # noqa: E731
    return "/" in below(a) and below(a) == below(b)


def _unprefixed(path: str) -> str:
    """A path without a leading `./` or `/` — never a dotfile's own dot."""
    return re.sub(r"^(?:\./|/)+", "", path)


def _near(a: Optional[int], b: Optional[int]) -> bool:
    """Two known lines within the window. A missing line is never near anything here:
    a note with no line is matched by its words, not by sharing a file
    (`scan._near`, which deduplicates one report, treats two missing lines as one)."""
    from app.build.scan import LINE_WINDOW

    return a is not None and b is not None and abs(a - b) <= LINE_WINDOW


def same_tool_finding(row, f: Finding) -> bool:
    """Whether a tracked scanner finding is the one a rescan just reported.

    The same rule (or one of the rules reporting the same problem with it) in the same
    file, and either within three lines of where it was or on the very same code — a
    fix that added an import above it moved it, and did not fix it.
    """
    from app.build.scan import DEPENDENCY_TOOLS

    if not (rules_of(row) & {f"{f.tool}:{f.rule_id}", *f.also}) or not _same_file(row.path, f.path):
        return False
    if f.tool in DEPENDENCY_TOOLS or (row.line is None and f.line is None):
        return True
    return _near(row.line, f.line) or bool(row.fingerprint and row.fingerprint == f.fingerprint)


def rules_of(row) -> set[str]:
    """Every rule a tracked scanner finding has been reported by, lead included."""
    return {*(row.rules or []), f"{row.tool}:{row.rule_id}"}


#: Words too common to say two findings are about the same problem.
_COMMON = frozenset({
    "missing", "data", "user", "users", "input", "code", "security", "issue", "found", "with",
    "from", "when", "into", "that", "this", "detected", "detection", "audit", "possible", "lang",
    "python", "javascript", "typescript", "react", "express", "flask", "django", "should", "could",
})


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 4 and w not in _COMMON}


def repeats(note: Finding, tool: Finding) -> bool:
    """Whether a review note is a scanner's finding said again: the same place (file,
    within three lines) *and* the same problem — the note's words share one with the
    scanner's category, title or rule. An IDOR note beside an XSS finding is not one."""
    if not (_same_file(tool.path, note.path) and _near(tool.line, note.line)):
        return False
    # One place, and a scanner finding at least as severe: a note about three files
    # isn't one of them, and a critical note isn't a medium scanner finding.
    if "," in (note.location or "") or _more_severe(note.severity, tool.severity):
        return False
    about = _words(f"{tool.category} {tool.title} {(tool.rule_id or '').replace('.', ' ').replace('-', ' ')}")
    return bool(_words(f"{note.category} {note.title}") & about)


def same_model_finding(row, f: Finding) -> bool:
    """Whether a tracked review note is the one the model just reported.

    Its own words (the key), or — reworded — the same file and line: a model that calls
    "Missing ownership check" "IDOR on GET /orders/:id" the next time is still pointing
    at the same handler, and the first must not be called fixed for it.
    """
    if row.finding_key == f.key:
        return True
    return _same_file(row.path, f.path) and _near(row.line, f.line)


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
        if f.source == SOURCE_TOOL:
            # The scanner's own words, with the rule and the exact line (#77): the
            # rescan that judges this fix runs the same rule on the same file.
            cwe = f", {f.cwe}" if f.cwe else ""
            title = f.title.rstrip(".")
            lines.append(
                f"- [{f.severity}] {f.tool} `{f.rule_id}` at `{f.location}` ({f.category or 'security'}{cwe}): "
                f"{title}.{fix}"
            )
        else:
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
    if any(f.source == SOURCE_TOOL for f in items):
        retry += (
            "\n\nThe scanner runs again on the files you return: a finding is fixed when "
            "its rule no longer matches there, not when the code looks different."
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
def sync_dispositions(
    db, project, output: object, readable: bool = True, scan: object = None
) -> list:
    """Record this audit's findings against the ones already being tracked.

    Called after every security phase, including the re-audit that follows a fix, so
    it has to answer these questions at once and get all of them right:

      * a **scanner's** finding a rescan no longer reports — that rule, in that file,
        near that line or on that code — and which was sent back to be fixed, is
        **fixed**. Only a rescan by the tool that found it can say so: one whose tool
        didn't run this time (no sandbox, no network) is left exactly as it was;
      * a finding that vanished without having been sent back is **gone**, not fixed.
        It stops blocking, because refusing to ship over something the current report
        does not mention is asking the reviewer to waive a finding that is not there
        — but it is not called a remediation, because nobody performed one;
      * a finding the reviewer **waived** stays waived when it reappears, or waiving
        would last until the next audit and mean nothing;
      * Warden's own findings are **review notes** (#77): matched by their words, or —
        reworded — by the same file and line, and closed by the model's re-review as
        before. One that repeats a scanner's finding at the same place is the
        scanner's, and isn't tracked twice;
      * anything else is open.

    `readable` is what stops the model half becoming a hole. A report that failed its
    own schema yields no findings at all, and treating that as "everything went away"
    would close a note because nobody could parse the report that raised it. When the
    report is unreadable, that half only adds — it never resolves. The scanners' half
    doesn't depend on it: their reports were read by the platform.
    """
    from app.build import scan as scanner
    from app.db.models import SecurityDisposition

    rows = (
        db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
    )
    tools_rows = [r for r in rows if r.source == SOURCE_TOOL]
    model_rows = [r for r in rows if r.source != SOURCE_TOOL]
    keys = {r.finding_key for r in rows}

    # ── the scanners ──
    result = scanner.ScanResult.from_dict(scan)
    current = tool_findings(scan) if result is not None else []
    # Every row has its id from the moment it exists (the column default only lands at
    # flush), so one made a moment ago is never matched to a second report as well.
    matched: set[str] = set()
    for f in current:
        row = _closest(
            [r for r in tools_rows if r.id not in matched and same_tool_finding(r, f)], f.line
        )
        if row is None:
            key = f.key
            n = 1
            while key in keys:
                # A different finding was first seen in this bucket and has since moved
                # on: same identity, distinct key.
                key = hashlib.sha256(f"{f.key}:{n}".encode("utf-8")).hexdigest()[:32]
                n += 1
            keys.add(key)
            row = SecurityDisposition(
                id=uuid.uuid4().hex, project_id=project.id, finding_key=key, status=FindingStatus.OPEN.value
            )
            _fill(row, f)
            db.add(row)
            tools_rows.append(row)
            matched.add(row.id)
            continue
        matched.add(row.id)
        # Still reported. A waiver is the reviewer's standing decision and survives;
        # anything else goes back to open, including a fix that did not take.
        if row.status != FindingStatus.WAIVED.value:
            row.status = FindingStatus.OPEN.value
        _fill(row, f, keep_title=True)
    for row in tools_rows:
        if row.id in matched or row.status in FindingStatus.settled():
            continue
        if result is None or not result.covers(row.tool, row.path, row.rule_id):
            # Not rescanned where it is: its tool didn't run this time, didn't read that
            # file, ran on the other side of the tree, or was cut short. Silence.
            continue
        _resolve(row, project, f"{row.tool} no longer reports it at {row.location or row.path}")

    # ── Warden's review notes ──
    # A repeat of what a scanner already reported there isn't tracked twice: the
    # scanner's finding is the one tracked, with its rule and its rescan. A repeat still
    # claims a record it already has, so that record isn't closed as "no longer
    # mentioned" while the scanner is reporting the very same problem.
    notes = read_findings(output, project)
    repeat_keys = {f.key for f in notes if any(repeats(f, t) for t in current)}
    # Its own words first, for every note; only then a reworded one by file and line,
    # among the rows nobody claimed — so a nearby note never takes another's row (and
    # its waiver) because it happened to be read first.
    claimed: dict[str, object] = {}
    for f in notes:
        row = next((r for r in model_rows if r.id not in matched and r.finding_key == f.key), None)
        if row is not None:
            matched.add(row.id)
            claimed[f.key] = row
    for f in notes:
        if f.key in claimed:
            continue
        # A waiver is a decision about one note. It never passes to a different, more
        # severe note that merely sits beside it: that one is new, and asked about.
        nearby = [
            r for r in model_rows
            if r.id not in matched and same_model_finding(r, f)
            and not (r.status == FindingStatus.WAIVED.value and _more_severe(f.severity, r.severity))
        ]
        row = min(nearby, key=lambda r: abs((r.line or 0) - (f.line or 0))) if nearby else None
        if row is not None:
            matched.add(row.id)
            claimed[f.key] = row
    for f in notes:
        row = claimed.get(f.key)
        if row is not None and f.key in repeat_keys:
            # The scanner now reports this very problem, with its rule and its rescan:
            # the note is superseded — not reopened (it would be asked about twice),
            # and not "fixed" (nothing was).
            if row.status != FindingStatus.WAIVED.value:
                row.status = FindingStatus.GONE.value
            twin = next(t for t in current if repeats(f, t))
            row.rule_id = f"{twin.tool}:{twin.rule_id}"
            continue
        if row is None:
            if f.key in keys or f.key in repeat_keys:
                continue
            keys.add(f.key)
            row = SecurityDisposition(
                id=uuid.uuid4().hex,
                project_id=project.id,
                finding_key=f.key,
                title=f.title,
                severity=f.severity,
                category=f.category,
                location=f.location,
                recommendation=f.recommendation,
                owner_phase=f.owner_phase,
                status=FindingStatus.OPEN.value,
                source=SOURCE_MODEL,
                path=f.path,
                line=f.line,
            )
            db.add(row)
            model_rows.append(row)
            matched.add(row.id)
            continue
        if row.status != FindingStatus.WAIVED.value:
            row.status = FindingStatus.OPEN.value
        row.severity = f.severity or row.severity
        row.location = f.location or row.location
        row.recommendation = f.recommendation or row.recommendation
        row.owner_phase = f.owner_phase or row.owner_phase
        row.path = f.path or row.path
        row.line = f.line or row.line
        row.source = SOURCE_MODEL
    if readable:
        for row in model_rows:
            if row.id in matched or row.status in FindingStatus.settled():
                continue
            _resolve(row, project, "the re-review no longer mentions it")

    db.commit()
    return list(
        db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
    )


def _more_severe(a: str, b: str) -> bool:
    """Whether severity `a` outranks `b` (an unrated one outranks nothing)."""
    rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    return rank.get((a or "").lower(), 99) < rank.get((b or "").lower(), 99)


def _closest(rows: list, line: Optional[int]):
    """Of several tracked findings a report could be, the nearest one."""
    if not rows:
        return None
    return min(rows, key=lambda r: abs((r.line or 0) - (line or 0)))


def _fill(row, f: Finding, keep_title: bool = False) -> None:
    """A tracked scanner finding, brought up to what the latest scan says."""
    if not keep_title or not row.title:
        row.title = f.title
        row.category = f.category
    row.severity = f.severity or row.severity
    row.location = f.location
    row.recommendation = f.recommendation or row.recommendation
    row.owner_phase = f.owner_phase or row.owner_phase
    row.source = SOURCE_TOOL
    row.tool = f.tool
    row.rule_id = f.rule_id
    row.rule_url = f.url or row.rule_url
    row.cwe = f.cwe or row.cwe
    row.path = f.path
    row.line = f.line
    row.fingerprint = f.fingerprint or row.fingerprint
    row.rules = sorted(set(row.rules or []) | {f"{f.tool}:{f.rule_id}", *f.also})


def _resolve(row, project, why: str) -> None:
    """Gone from the report. Only a finding that was actually sent back can be called
    *fixed* — one that simply stopped being mentioned is a rebuild that may have
    removed it, and this cannot tell. Both stop blocking; only one claims a fix."""
    if row.status == FindingStatus.FIX_REQUESTED.value:
        row.status = FindingStatus.FIXED.value
        log.info("Security finding fixed — %s: %s (%s)", why, row.title, project.id)
    else:
        row.status = FindingStatus.GONE.value
        log.info("Security finding no longer reported — %s: %s (%s)", why, row.title, project.id)


def row_source(row) -> str:
    """A row from before #77 has no source: it was the model's."""
    return row.source or SOURCE_MODEL


def row_is_serious(row) -> bool:
    return finding_is_serious(row_source(row), row.tool, row.severity, row.category or "", row.title or "")


def row_blocks(row) -> bool:
    """Whether this finding holds the build until it is fixed or waived.

    A scanner's critical or high finding, or a serious one. Never a review note: what
    one model thinks of the code is read and decided on, but it doesn't hold a build
    back on its own (#77).
    """
    return row_source(row) == SOURCE_TOOL and (row.severity in STOPPING_SEVERITIES or row_is_serious(row))


def as_finding(row) -> Finding:
    """A tracked row, as the `Finding` a fix note is written from."""
    return Finding(
        key=row.finding_key,
        title=row.title,
        severity=row.severity,
        category=row.category,
        location=row.location,
        recommendation=row.recommendation,
        owner_phase=row.owner_phase,
        source=row_source(row),
        tool=row.tool,
        rule_id=row.rule_id,
        cwe=row.cwe,
        path=row.path,
        line=row.line,
        url=row.rule_url,
        fingerprint=row.fingerprint,
    )


def open_notes(db, project) -> list:
    """Warden's critical and high review notes nobody has settled — what the Security
    stop asks a person to read. From the dispositions, so a waived note stays waived
    however many re-audits repeat it, and a note folded into a scanner's finding isn't
    asked about twice."""
    from app.db.models import SecurityDisposition

    read = notes_read(project)
    return [
        row
        for row in db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
        if row_source(row) == SOURCE_MODEL
        and row.severity in STOPPING_SEVERITIES
        and row.status not in FindingStatus.settled()
        and _read_mark(row) not in read
    ]


def _read_mark(row) -> str:
    """What a person read: this note, in this file, at this severity. The same title
    somewhere else, or rated higher, is something they haven't read."""
    return f"{row.finding_key}|{row.path or ''}|{(row.severity or '').lower()}"


def is_read(project, row) -> bool:
    return _read_mark(row) in notes_read(project)


def notes_read(project) -> set[str]:
    """The review notes a person has already read and approved past at a Security stop."""
    data = project.auto_fix if isinstance(project.auto_fix, dict) else {}
    return set(data.get("notes_read") or [])


def mark_notes_read(db, project) -> None:
    """Approving a Security stop is reading its notes: a later re-audit that repeats
    them doesn't stop the build to ask again. (A new note still does.)"""
    from app.orchestration import autofix

    data = autofix.load(project)
    data["notes_read"] = sorted(set(data.get("notes_read") or []) | {_read_mark(r) for r in open_notes(db, project)})
    autofix.save(project, data)


def unresolved(db, project, serious: Optional[bool] = None) -> list:
    """Findings that hold the build and have been neither fixed nor waived.

    `serious=True` is the crew's to-do list, `serious=False` the reviewer's question,
    and `None` both — what has to be settled before a build ships. Only scanners'
    findings: see `row_blocks`.
    """
    from app.db.models import SecurityDisposition

    return [
        row
        for row in db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
        if row_blocks(row)
        and row.status not in FindingStatus.settled()
        and (serious is None or row_is_serious(row) == serious)
    ]


__all__ = [
    "CODE_PHASES",
    "SOURCE_MODEL",
    "SOURCE_TOOL",
    "Finding",
    "as_finding",
    "finding_is_serious",
    "row_blocks",
    "same_model_finding",
    "same_tool_finding",
    "tool_findings",
    "tool_key",
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
