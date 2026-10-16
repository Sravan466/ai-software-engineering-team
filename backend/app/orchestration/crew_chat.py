"""Talk to the crew while it builds (#102): questions answered, notes kept for later.

One box on the build page takes a message at any time. This module decides what it was
and what happens to it, without a model wherever it can:

  **A question** ("what are you doing?", "why is it stuck?") is answered at once from
  what the build already knows — the phase working now and the file it is on
  (`activity`), the fix loop's rounds and trend (`auto_fix`), what a stop waits for,
  which model is running. Local generation is one slot per computer, so a status answer
  that called a model would wait behind the file being written. A question those facts
  can't answer ("will it work on phones?") is answered by a model once the step in hand
  is done: the call queues for the generation slot like any other.

  **A note** ("make it purple") waits, `queued`, for the agents it is for.

Whether a message is a question is decided by its words, not a model: it ends with `?`,
or starts with what / why / when / how / where / who / is / are / can / will / status.
The reply says how it was read, and the person can say otherwise.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.constants import PHASE_LABELS, PHASE_ORDER, GateKind, PhaseStatus, PipelineStatus
from app.core.logging import get_logger
from app.db.models import CrewMessage, Project, _aware

log = get_logger(__name__)

PERSON, CREW = "person", "crew"
QUESTION, NOTE = "question", "note"
ANSWERED = "answered"
QUEUED = "queued"
APPLIED = "applied"
SCHEDULED = "scheduled"
CANCELLED = "cancelled"
DECLINED = "declined"

#: The longest message the box takes.
TEXT_MAX = 2000
#: The person's messages the build page's poll carries, with their replies.
RECENT = 12

#: The words a question opens with. The issue's list, and the other ways a question
#: opens: "Could you explain…", "Does it work on phones", "Which model…", "Has it started".
_ASKS = frozenset({
    "what", "why", "when", "how", "where", "who", "is", "are", "can", "will", "status",
    "could", "would", "does", "did", "has", "which", "should",
})
#: Asking for something to be done is a note, question mark or not: "Can you keep all the
#: routes in one file". Not when what it asks is to be told or shown something, or about
#: the crew itself ("will you be done soon", "can you see the error").
_NOT_A_REQUEST = (
    "tell", "explain", "say", "show", "describe", "confirm", "check", "let", "see", "be", "know",
    "still", "already", "ever", "really", "not", "remember", "hear", "read", "understand",
)
_REQUEST = re.compile(rf"^(can|could|will|would)\s+(you|we|the crew)\s+(?!(?:{'|'.join(_NOT_A_REQUEST)})\b)[a-z]")


def plain(text: str) -> str:
    """Typographic apostrophes and quotes as straight ones: "What’s next?" is "What's next?"."""
    return text.translate(_STRAIGHT)


_STRAIGHT = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'})


def classify(text: str) -> str:
    """`question` or `note`, from the words alone."""
    said = plain(text).strip()
    low = said.lower()
    if _REQUEST.match(low):
        return NOTE
    if said.endswith("?"):
        return QUESTION
    first = re.match(r"[a-z]+", low)
    return QUESTION if first and first.group(0) in _ASKS else NOTE


# ── who is who ────────────────────────────────────────────────────────────────
_ORDER = [p.value for p in PHASE_ORDER]
#: The phases that write their files one call at a time, and can read a note between two.
CODE_PHASES = ("backend_engineer", "frontend_engineer")


def title(phase: Optional[str]) -> str:
    """The agent's title, as the page's personas name its role: "Backend Engineer"."""
    if not phase:
        return "The crew"
    try:
        from app.agents import get_agent

        return get_agent(phase).title
    except KeyError:
        return PHASE_LABELS.get(phase, phase)


#: Words a person uses for each phase: its title, its codename on the page, what it makes.
_NAMES: dict[str, tuple[str, ...]] = {
    "product_manager": ("product manager", "scope", "requirements", "pm"),
    "system_design": ("system design", "atlas", "architecture", "architect", "design"),
    "backend_engineer": ("backend engineer", "forge", "backend", "server", "api"),
    "frontend_engineer": ("frontend engineer", "prism", "frontend", "website", "ui"),
    "qa_engineer": ("qa engineer", "sieve", "qa", "tests", "test"),
    "security_engineer": ("security engineer", "warden", "security"),
    "devops_engineer": ("devops engineer", "relay", "devops", "deployment", "deploy"),
    "cost_estimation": ("cost estimation", "ledger", "cost", "costs"),
}


def named_phase(text: str) -> Optional[str]:
    """The phase a message is about, when it names exactly one."""
    low = text.lower()
    found = [
        phase for phase, words in _NAMES.items()
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)
    ]
    return found[0] if len(found) == 1 else None


def addressed_to(text: str) -> Optional[str]:
    """The phase a note opens by naming — "frontend: make it purple" — if it does."""
    head = re.match(r"\s*([a-z][a-z ]{1,24}?)\s*[:,—–-]\s", text.lower())
    if not head:
        return None
    for phase, words in _NAMES.items():
        if head.group(1).strip() in words:
            return phase
    return None


# ── the facts ─────────────────────────────────────────────────────────────────
def _rows(project: Project) -> dict[str, object]:
    """The attempt that counts for each phase, by phase."""
    from app.core import artifacts

    return {row.phase: row for row in artifacts._current(artifacts._rows(project))}  # noqa: SLF001


def phase_state(project: Project, phase: str, rows: Optional[dict] = None) -> str:
    """`done` | `working` | `waiting` (finished, waiting on a review) | `not_started`."""
    row = (rows if rows is not None else _rows(project)).get(phase)
    if row is None:
        return "not_started"
    status = getattr(row, "status", "")
    if status == PhaseStatus.RUNNING.value:
        # A stalled build's row still says running; nothing is working on it.
        live = project.status in ("running", "cancelled") and not project.stalled
        return "working" if live else "not_started"
    if status == PhaseStatus.PENDING_APPROVAL.value:
        return "waiting"
    return "done"


def not_started(project: Project, rows: Optional[dict] = None) -> list[str]:
    """The phases with nothing yet, in the order they run."""
    rows = rows if rows is not None else _rows(project)
    return [p for p in _ORDER if phase_state(project, p, rows) == "not_started"]


def _model_of(row) -> Optional[str]:
    if row is None:
        return None
    pick = getattr(row, "model_pick", None)
    if isinstance(pick, dict) and pick.get("model"):
        return str(pick["model"])
    return getattr(row, "model_used", None) or None


def _where_model(row) -> str:
    local = getattr(row, "is_local", None)
    return " on this computer" if local is True else " in the cloud" if local is False else ""


def _doing(project: Project, rows: dict) -> Optional[str]:
    """What the phase running now is doing, in a sentence."""
    phase = project.current_phase
    if not phase:
        return None
    who = title(phase)
    found = project.activity or {}
    stage = found.get("stage") or ""
    files = found.get("files") or []
    done, total = int(found.get("done") or 0), int(found.get("total") or 0)
    if stage in ("writing", "fixing", "changing") and files:
        verb = stage
        live = [f["path"] for f in files if f.get("state") in ("writing", "fixing")]
        if len(live) == 1:
            return f"{who} is {verb} `{live[0]}`, file {min(done + 1, total)} of {total}."
        if live:
            return f"{who} is {verb} {len(live)} files at once; {done} of {total} are done."
        return f"{who} is {verb} its files; {done} of {total} are done."
    if stage == "planning":
        return f"{who} is planning which files to write."
    if stage and found.get("label"):
        label = str(found["label"])
        return f"{who} is {label[0].lower()}{label[1:]}."
    return f"{who} is working on the {PHASE_LABELS.get(phase, phase).lower()}."


def _next(project: Project, rows: dict) -> Optional[str]:
    """Who is next, or the review that comes next."""
    phase = project.current_phase
    if phase not in _ORDER:
        return None
    from app.core.constants import ApprovalMode, PLAN_PHASES

    mode = project.effective_approval_mode
    i = _ORDER.index(phase)
    if i + 1 >= len(_ORDER):
        return "After that, the finished build comes to you to review." if mode != ApprovalMode.UNATTENDED.value else None
    plan_end = [p.value for p in PLAN_PHASES][-1]
    if phase == plan_end and mode != ApprovalMode.UNATTENDED.value:
        return "After that, the plan comes to you to review."
    if mode == ApprovalMode.EVERY_PHASE.value:
        return f"After that, you review it, then {title(_ORDER[i + 1])} starts."
    return f"Next up: {title(_ORDER[i + 1])}."


def _fixing(project: Project) -> Optional[str]:
    """The fix round in flight and its trend, if the crew is fixing something."""
    from app.orchestration import autofix

    data = autofix.view(project.auto_fix)
    if not isinstance(data, dict):
        return None
    for name, t in (data.get("tracks") or {}).items():
        if not isinstance(t, dict):
            continue
        last = autofix.open_round(t)
        if last is None:
            continue
        start = int(t.get("episode_start") or 0)
        line = f"Fix round {int(last.get('n') or 1) - start} of {int(t.get('allowed') or 0) - start}."
        points = t.get("trend") or []
        if len(points) > 1:
            line += f" Problems went {autofix.said(points)}."
        return line
    return None


def _model_line(project: Project, rows: dict) -> Optional[str]:
    row = rows.get(project.current_phase or "")
    model = _model_of(row)
    return f"It's running on {model}{_where_model(row)}." if model else None


# ── why it's stuck ────────────────────────────────────────────────────────────
_WIRING = re.compile(
    r"no exported member|no default export|has no exported|imports `?\.|no file in this build"
    r"|never imported or declared|declares .* locally",
    re.I,
)
_SIDE = {"backend_engineer": "the backend", "frontend_engineer": "the website", "qa_engineer": "the tests"}


def _kind_of(problem: dict) -> str:
    title_ = str(problem.get("title") or "")
    step = problem.get("step")
    if problem.get("kind") == "stack":
        return "stack"
    if problem.get("problem") == "placeholder":
        return "unfinished"
    if step in ("install",) or re.search(r"not a package the platform can install|is not a package on npm", title_, re.I):
        return "install"
    if step in ("build", "boot"):
        return "start"
    if re.search(r"does not parse", title_, re.I):
        return "parse"
    if _WIRING.search(title_):
        return "wiring"
    return "other"


def _stuck_track(name: str, t: dict) -> str:
    from app.orchestration import autofix

    if name == autofix.SECURITY:
        what = "A serious security problem is still open"
    elif name == autofix.TESTS:
        what = "Some of the app's tests still fail"
    else:
        phase = name[len(autofix.BUILD_PREFIX):]
        side = _SIDE.get(phase, f"{title(phase)}'s work")
        left = (t.get("stopped") or {}).get("problems") or []
        kinds = [_kind_of(p) for p in left if isinstance(p, dict)]
        # The commonest kind; a tie goes to the one seen first, as the stuck card's headline does.
        top = Counter(kinds).most_common(1)[0][0] if kinds else "other"
        what = {
            "wiring": f"The files in {side} don't connect to each other yet",
            "parse": f"Some of {side} can't run as written",
            "unfinished": f"Part of {side} isn't finished",
            "install": f"{side[0].upper()}{side[1:]} uses a package that can't be installed",
            "stack": f"Part of {side} uses a different setup from the plan",
            "start": f"{side[0].upper()}{side[1:]} doesn't start yet",
        }.get(top, f"{side[0].upper()}{side[1:]} still has problems")
    stopped = t.get("stopped") or {}
    left_n = int(stopped.get("left") or 0)
    rounds = len(autofix.episode(t))
    out = f"{what}: {left_n} problem{'' if left_n == 1 else 's'} left after {rounds} fix round{'' if rounds == 1 else 's'}."
    points = t.get("trend") or []
    if len(points) > 1:
        out += f" Problems went {autofix.said(points)}."
    reason = stopped.get("reason")
    if reason == autofix.STOP_NO_PROGRESS:
        out += " The last round didn't leave fewer problems, so the crew stopped instead of going round again."
    elif reason == autofix.STOP_LIMIT:
        out += " It used every round it was given."
    elif reason == autofix.STOP_UNCHECKED:
        out += " The re-check couldn't run, so nobody can say whether a round helped."
    kept = stopped.get("kept_best")
    if isinstance(kept, dict):
        out += f" It went back to its best attempt, with {kept.get('problems')} problems."
    return out


def why_stuck(project: Project) -> Optional[str]:
    from app.orchestration import autofix

    data = autofix.view(project.auto_fix)
    if not isinstance(data, dict):
        return None
    tracks = data.get("tracks") or {}
    stuck = [(n, t) for n, t in tracks.items() if isinstance(t, dict) and t.get("stopped") and not t.get("accepted")]
    if not stuck:
        return None
    lines = [_stuck_track(n, t) for n, t in stuck]
    lines.append("Tell the crew what to try and it goes again with your note, or press Try again.")
    return " ".join(lines)


# ── the answer ────────────────────────────────────────────────────────────────
#: What makes a question one about the build itself, answered from its record. Phrases,
#: not single words: "What file has the login form?" is about the app, and a question
#: about the app goes to a model, which is given the same record besides.
_STATUS = re.compile(
    "|".join(
        (
            r"\b(status|progress|eta|stuck)\b",
            r"\b(doing|happening|going on|working on|up to|writing)\b",
            r"\bwhat'?s up\b",
            r"\bhow('?s| is| are) (it|things|the build|everything)( going)?\b",
            r"\bwhere (are|is) (you|we|it|things|the build)\b",
            r"\bhow (long (will|until|till|before|left|is (it|this|the build)|does (it|this|the build))|much longer)\b",
            r"\bwhen (will|does|do|is) (it|you|this|the build|the crew)\b",
            r"\b(who|what)('?s| is| comes) next\b|\bwho('?s| is) (working|on it)\b",
            r"\bwaiting (for|on)\b",
            r"\b(which|what) (ai |local |cloud )?models?\b",
            r"\bwhy (did|has|is|does) (it|the build|you|the crew|everything) (stop|stopped|fail|failed|failing|pause|paused|slow|stuck)\b",
            r"\bwhat (went|is|'s) wrong\b|\bwhat'?s wrong\b",
            r"\b(is|are) (it|you|the build|the crew) (done|finished|ready|running|working|stuck|stopped)\b",
            r"\bfix (round|rounds)\b|\bwhich round\b",
        )
    )
)
#: Two status questions with an answer of their own, not the whole picture.
_ASKS_NEXT = re.compile(r"\b(who|what)('?s| is| comes) next\b")
_ASKS_TIME = re.compile(r"\bhow (long|much longer)\b|\bwhen (will|does|do|is) (it|you|this|the build|the crew)\b|\beta\b")
#: The same, about one agent: "has the frontend started?", "is the backend done?".
_PHASE_STATE = re.compile(r"\b(start|started|starting|finish|finished|done|ready|running|working|begun)\b")
_STUCK_WORDS = frozenset({
    "stuck", "wrong", "problem", "problems", "error", "errors", "fail", "failed", "failing", "broken", "fix",
    "fixing",
})


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def is_status(text: str) -> bool:
    """Whether the build's own record answers this question."""
    low = plain(text).lower()
    return bool(_STATUS.search(low)) or (named_phase(text) is not None and bool(_PHASE_STATE.search(low)))


_GATE_WAIT = {
    GateKind.PLAN.value: "The plan is ready: the requirements and the architecture. The build waits for you to approve it or send it back.",
    GateKind.SHIP.value: "The build is finished and waits for your review before anything ships.",
    GateKind.SECURITY.value: "Security Engineer found something serious, and the build waits for your decision on it.",
    GateKind.UNCHECKED.value: "A phase's report couldn't be read, so its check never ran. The build waits for you.",
    GateKind.COST.value: "The cost estimate is over your cap, and the build waits for you to decide.",
    GateKind.STACK.value: "A phase doesn't match the stack the plan settled on, and the build waits for you.",
    GateKind.BUILD.value: "The code doesn't build yet, and the build waits for your decision.",
    GateKind.DATABASE.value: "The build waits for you to connect its database, or choose to add it later.",
    GateKind.INTEGRATIONS.value: "The build waits for you to connect the services it uses, or choose to add them later.",
}


def _how_long(project: Project) -> str:
    """No finish time is promised — it turns on the model — only what the record says."""
    phase = project.current_phase
    if phase not in _ORDER:
        return "The build has just started."
    i = _ORDER.index(phase)
    left = len(_ORDER) - i - 1
    took = int((project.elapsed_seconds or 0) // 60)
    at = f"{title(phase)} has been working for {took} minute{'' if took == 1 else 's'}" if took else f"{title(phase)} has just started"
    after = f"{left} more phase{'' if left == 1 else 's'} after it" if left else "it's the last phase"
    return f"{at}. It's phase {i + 1} of {len(_ORDER)}, and {after}. How long each takes depends on the model, so there's no finish time to give."


def _phase_answer(project: Project, phase: str, rows: dict) -> str:
    state = phase_state(project, phase, rows)
    who = title(phase)
    if state == "working":
        return _doing(project, rows) or f"{who} is working now."
    if state == "waiting":
        return f"{who} is finished and waiting for your review."
    if state == "done":
        row = rows.get(phase)
        from app.core import artifacts

        files = len(list(artifacts.iter_files(row.output if isinstance(getattr(row, "output", None), dict) else {})))
        return f"{who} is finished" + (f": {files} file{'' if files == 1 else 's'}." if files else ".")
    ahead = [p for p in _ORDER if phase_state(project, p, rows) == "not_started"]
    current = project.current_phase
    if (
        project.status == PipelineStatus.RUNNING.value
        and current in _ORDER
        and phase in ahead
        and _ORDER.index(phase) > _ORDER.index(current)
    ):
        between = _ORDER.index(phase) - _ORDER.index(current) - 1
        return f"{who} hasn't started yet. " + (
            f"{title(project.current_phase)} is working now, and {who} is next."
            if between <= 0
            else f"{title(project.current_phase)} is working now, then {between} more before it."
        )
    return f"{who} hasn't started yet."


def status_answer(project: Project, text: str, rows: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """The answer to a status question, and the agent who gives it. Never calls a model."""
    rows = rows if rows is not None else _rows(project)
    text = plain(text)
    words = _words(text)
    status = project.status
    asks_stuck = bool(words & _STUCK_WORDS)
    asks_model = bool(words & {"model", "models", "llm"})

    if status == PipelineStatus.AWAITING_APPROVAL.value and project.gate_kind == GateKind.NEEDS_HELP.value:
        why = why_stuck(project)
        return (why or "The crew stopped and asked for help." + (f" {project.gate_note}" if project.gate_note else "")), project.current_phase

    if status == PipelineStatus.RUNNING.value and project.stalled:
        return (
            "Nothing is driving this build: the server probably restarted mid-phase. Resume "
            "re-runs that phase from the last checkpoint and keeps everything approved.",
            None,
        )

    phase = named_phase(text)
    if phase and not asks_model and not asks_stuck:
        return _phase_answer(project, phase, rows), phase
    if status == PipelineStatus.RUNNING.value and project.current_phase not in _ORDER:
        return f"The build is starting. {title(_ORDER[0])} goes first.", _ORDER[0]
    if status == PipelineStatus.RUNNING.value:
        if asks_model:
            return (_model_line(project, rows) or "The model hasn't answered yet, so I can't name it."), project.current_phase
        low = text.lower()
        if _ASKS_NEXT.search(low):
            return (_next(project, rows) or f"{title(project.current_phase)} is the last phase."), project.current_phase
        if _ASKS_TIME.search(low):
            return _how_long(project), project.current_phase
        if project.change:
            change = project.change
            lead = f"The crew is making change #{change.get('number')}: {change.get('text')}."
        else:
            lead = ""
        parts = [lead, _doing(project, rows), _fixing(project), _next(project, rows)]
        if project.cancel_requested:
            parts.append("You pressed Stop, so it stops once this step finishes.")
        return " ".join(p for p in parts if p), project.current_phase
    if status == PipelineStatus.AWAITING_APPROVAL.value:
        wait = _GATE_WAIT.get(project.gate_kind or "")
        note = (project.gate_note or "").strip()
        return (wait or note or "The build is waiting for you."), project.current_phase
    if status == PipelineStatus.CANCELLED.value:
        if project.current_phase and phase_state(project, project.current_phase, rows) == "working" and not project.stalled:
            return f"You pressed Stop. {title(project.current_phase)} stops once the step in hand finishes.", project.current_phase
        return "You stopped the build. Everything made before the stop is kept; Resume starts from the last approved phase.", None
    if status == PipelineStatus.FAILED.value:
        why = (project.last_error or "").strip()
        return ("The build stopped on an error" + (f": {why}" if why else ".") + " Resume starts again from the last checkpoint."), None
    if status == PipelineStatus.PAUSED.value:
        return "The build is paused: its model is on your computer, which isn't connected. It carries on by itself when it's back.", None
    if status == PipelineStatus.COMPLETED.value:
        return "The build is finished. Say what should change and the crew edits it.", None
    return "The build hasn't started yet.", None


# ── the thread ────────────────────────────────────────────────────────────────
def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _aware(value).isoformat()


def _reply_out(m: CrewMessage) -> dict:
    return {"id": m.id, "agent": m.agent, "text": m.text, "status": m.status, "created_at": _iso(m.created_at)}


def out(m: CrewMessage, replies: Iterable[CrewMessage] = (), since: Optional[datetime] = None) -> dict:
    """A person's message as the page reads it, with the crew's replies to it."""
    return {
        "id": m.id,
        "text": m.text,
        "kind": m.kind,
        "status": m.status,
        "agent": m.agent,
        # A note a phase of this run can still be handed (#102): what the crew floor's
        # "Got your note" shows, judged here once rather than again on the page.
        "standing": standing(m, since),
        # Checked against finished work (#102 PR 3): still reading it, or which finished
        # phases it changes after this step.
        "checking": bool((m.plan or {}).get("checking")),
        "touched": list((m.plan or {}).get("touched") or []),
        "meant_for": list(m.meant_for or []),
        "applied_to": list(m.applied_to or []),
        "change_request_id": m.change_request_id,
        "created_at": _iso(m.created_at),
        "applied_at": _iso(m.applied_at),
        "replies": [_reply_out(r) for r in replies],
    }


def thread(db: Session, project_id: str, limit: Optional[int] = None) -> list[dict]:
    """The person's messages, oldest first, each with its replies — the newest `limit`."""
    q = (
        select(CrewMessage)
        .where(CrewMessage.project_id == project_id, CrewMessage.author == PERSON)
        .order_by(CrewMessage.created_at.desc())
    )
    if limit:
        q = q.limit(limit)
    asked = list(reversed(db.execute(q).scalars().all()))
    if not asked:
        return []
    replies = db.execute(
        select(CrewMessage)
        .where(CrewMessage.project_id == project_id, CrewMessage.reply_to.in_([m.id for m in asked]))
        .order_by(CrewMessage.created_at)
    ).scalars().all()
    by: dict[str, list[CrewMessage]] = {}
    for r in replies:
        by.setdefault(r.reply_to or "", []).append(r)
    since = run_began(db, project_id)
    return [out(m, by.get(m.id, []), since) for m in asked]


def recent(project: Project) -> list[dict]:
    """The newest few, for `ProjectOut`. Empty when the project isn't in a session."""
    db = object_session(project)
    if db is None:
        return []
    return thread(db, project.id, RECENT)


def reply(db: Session, asked: CrewMessage, text: str, agent: Optional[str] = None) -> CrewMessage:
    """The crew's answer to `asked`, added to the session."""
    found = CrewMessage(
        project_id=asked.project_id,
        author=CREW,
        agent=agent,
        text=text,
        kind=asked.kind,
        status=ANSWERED,
        reply_to=asked.id,
        created_at=_later_than(asked),
    )
    db.add(found)
    return found


def now() -> datetime:
    return datetime.now(timezone.utc)


def _later_than(m: CrewMessage) -> datetime:
    """Now, and never at or before `m` — a reply written in the same instant still sorts after it."""
    at = now()
    base = _aware(m.created_at)
    if base is not None:
        if at <= base:
            at = base + timedelta(microseconds=1)
    return at


# ── a note, for now ───────────────────────────────────────────────────────────
#: Said of a note that may change work already done (#102 PR 3), until the planner has read it.
CHECK_AFTER = "I'll check whether this changes finished work after this step."


def note_reply(project: Project, text: str, check: bool = False) -> tuple[str, Optional[str], list[str]]:
    """What happens to a note, said plainly; who it's for. A note waits, `queued`.

    `check`: some work is finished, and the note is read against it once the generation
    slot is free (`scope_note`) — the reply says so."""
    said, agent, meant_for = _note_reply(project, text, check)
    if check and meant_for and CHECK_AFTER not in said:
        said = f"{said} I'll also check whether it changes finished work after this step."
    return said, agent, meant_for


def _note_reply(project: Project, text: str, check: bool) -> tuple[str, Optional[str], list[str]]:
    rows = _rows(project)
    ahead = not_started(project, rows)
    stopped = project.status in (
        PipelineStatus.CANCELLED.value, PipelineStatus.FAILED.value, PipelineStatus.PAUSED.value
    ) or (project.status == PipelineStatus.RUNNING.value and project.stalled)
    current = project.current_phase
    row = rows.get(current or "")
    # Stopped mid-phase: that phase runs again when the build resumes.
    rerun = stopped and row is not None and getattr(row, "status", "") == PhaseStatus.RUNNING.value
    named = addressed_to(text)
    if named:
        state = phase_state(project, named, rows)
        if rerun and named == current:
            return f"Got it. The build is stopped, so {title(named)} gets this note when it resumes.", named, [named]
        if state == "not_started":
            return f"Got it. {title(named)} hasn't started, so it gets this note when it starts.", named, [named]
        if named == current and not stopped and _writing_files(project, rows):
            return f"Got it. {title(named)} is writing its files now; it reads this note before the next one.", named, [named]
        if check:
            return f"{title(named)} has already {'started' if state == 'working' else 'finished'}. {CHECK_AFTER}", named, [named]
        return (
            f"{title(named)} has already {'started' if state == 'working' else 'finished'}, so this note "
            "waits for the next time that work is sent back.",
            named,
            [named],
        )
    if rerun and current in _ORDER and current not in ahead:
        ahead = [current, *ahead]
    if not stopped and _writing_files(project, rows):
        # A code phase between two files reads it before the next one (#102).
        later = list(ahead)
        ahead = [current, *ahead]
        said = f"Got it. {title(current)} reads this before its next file"
        if not later:
            return f"{said}.", current, ahead
        if len(later) == 1:
            return f"{said}, and {title(later[0])} gets it when it starts.", current, ahead
        return f"{said}, and {title(later[0])} and the {len(later) - 1} after it get it when they start.", current, ahead
    if not ahead:
        if check:
            return f"Every agent has started on this build. {CHECK_AFTER}", None, []
        return (
            "Every agent has started on this build, so no one is left to take this note. Once it's "
            "finished, ask for it as a change."
        ), None, []
    first = ahead[0]
    if stopped:
        return f"Got it. The build is stopped, so {title(first)} gets this note when it resumes.", first, ahead
    rest = ahead[1:]
    if not rest:
        said = f"{title(first)} hasn't started, so it gets this note when it starts."
    elif len(rest) == 1:
        said = f"{title(first)} and {title(rest[0])} haven't started, so they get this note when they start."
    else:
        said = f"{title(first)} and the {len(rest)} after it haven't started, so they get this note when they start."
    return f"Got it. {said}", first, ahead


def _writing_files(project: Project, rows: dict) -> bool:
    """Whether the phase working now is a code phase that will read a note before another
    file: files still planned, and not a fix round — whose prompts don't carry notes."""
    current = project.current_phase
    if current not in CODE_PHASES or phase_state(project, current, rows) != "working":
        return False
    found = project.activity or {}
    # `fixing` here is one file's in-loop repair; a whole fix round is caught below.
    if found.get("ended") or found.get("stage") not in ("planning", "writing", "fixing"):
        return False
    from app.orchestration import autofix

    track = (autofix.load(project)["tracks"]).get(autofix.build_track(current))
    if track and autofix.open_round(track) is not None:
        return False
    files = found.get("files") or []
    # The file being written is already in flight: only one still planned comes after.
    return not files or any(f.get("state") == "planned" for f in files)


# ── notes reach the phases they're for ────────────────────────────────────────
_HANDED = (QUEUED, APPLIED)
#: Said over a phase's notes: two notes can disagree, and the slot's budget cuts the end.
_NEWEST_FIRST = "Newest first; where two disagree, the newer one stands."


def _note_filter(project_id):
    """Which messages are notes a phase may be handed — one definition, for the notes
    taken and for what `claim.between_calls` reads, so the two can't drift apart."""
    return (
        CrewMessage.project_id == project_id,
        CrewMessage.author == PERSON,
        CrewMessage.kind == NOTE,
        CrewMessage.status.in_(_HANDED),
    )


def _notes(db: Session, project_id: str) -> list[CrewMessage]:
    return list(db.execute(select(CrewMessage).where(*_note_filter(project_id)).order_by(CrewMessage.created_at)).scalars())


def notes_signature():
    """Two scalar subqueries — how many notes the build holds, and when the newest was
    sent — for `claim.between_calls`, which reads them in the read it already makes before
    every file. A note added, or one taken back, moves one or the other."""
    from sqlalchemy import func

    return (
        select(func.count()).select_from(CrewMessage).where(*_note_filter(Project.id)).scalar_subquery(),
        select(func.max(CrewMessage.created_at)).where(*_note_filter(Project.id)).scalar_subquery(),
    )


def signature_of(count: int, newest) -> tuple:
    """The pair `notes_signature` reads, in one comparable form — the time as UTC ISO,
    whether it came back from the database's MAX() or off a loaded note."""
    if isinstance(newest, str):
        try:
            newest = datetime.fromisoformat(newest)
        except ValueError:
            pass
    if isinstance(newest, datetime):
        newest = _aware(newest).isoformat()
    return (int(count or 0), newest)


def for_phase(m: CrewMessage, phase: str) -> bool:
    """A note is for the phases it named or that hadn't started when it was sent. One for
    no phase — sent through Keep trying, for that fix round only, or left with no phase to
    reach (`[]`, or NULL from before notes said) — is handed to none."""
    return phase in (m.meant_for or [])


def run_began(db: Session, project_id: str):
    """When the build's current run began, for its notes: a note sent before the build was
    last finished belongs to that run, and is in its settled decisions now."""
    from sqlalchemy import func

    from app.db.models import Version

    return db.execute(select(func.max(Version.created_at)).where(Version.project_id == project_id)).scalar()


def take_notes(project_id: str, phase: str) -> Optional[tuple[str, tuple]]:
    """Every note for `phase`, as its prompt carries them (one a line, oldest first), and
    the signature of the notes the build held when they were read.

    At a phase's start (the graph node, a redo) and between two of a code phase's files.
    Each note handed over is marked handed to `phase`; the first phase to take a note says
    so in the thread. Taken again on a re-run — a resume, a redo — so a note stands for the
    whole build. None when they couldn't be read: the caller keeps what it had, and a note
    that can't be read never stops a build."""
    from app.db.base import SessionLocal

    try:
        with SessionLocal() as db:
            held = _notes(db, project_id)
            since = run_began(db, project_id)
            mine = [m for m in held if for_phase(m, phase) and _this_run(m, since)]
            for m in mine:
                given = list(m.applied_to or [])
                if phase in given:
                    continue
                m.applied_to = [*given, phase]
                if m.status == QUEUED:
                    m.status = APPLIED
                    m.applied_at = now()
                    m.agent = phase
                    reply(db, m, f"Got your note. {title(phase)} is working with it now.", phase)
            db.commit()
            newest = max((m.created_at for m in held if m.created_at is not None), default=None)
            # Newest first: a window too small for every note cuts the oldest.
            lines = [f"- {' '.join(m.text.split())}" for m in reversed(mine)]
            text = "\n".join([_NEWEST_FIRST, *lines]) if len(lines) > 1 else "\n".join(lines)
            return text, signature_of(len(held), newest)
    except Exception:  # noqa: BLE001
        log.exception("Couldn't read the notes for %s on %s", phase, project_id)
        return None


#: "Asked during the build" lines kept with the stack line: every reader takes the tail.
ASKED_DECISIONS = 5


def asked_during(db: Session, project_id: str, label: str = "the build", this_run: bool = False) -> list[str]:
    """What the person asked for while a run went on, for the app's settled decisions: the
    notes a phase took, the newest few. Not a Keep-trying note, which was for one fix round.
    The first build's are set when it finishes; a kept change's (`this_run`) with it."""
    since = run_began(db, project_id) if this_run else None
    return [
        f"Asked during {label}: {' '.join(m.text.split())[:200]}"
        for m in _notes(db, project_id)
        if m.applied_to and m.meant_for and _this_run(m, since)
    ][-ASKED_DECISIONS:]


def _this_run(m: CrewMessage, since: Optional[datetime]) -> bool:
    return since is None or m.created_at is None or _aware(m.created_at) > _aware(since)


def standing(m: CrewMessage, since: Optional[datetime]) -> bool:
    """Whether a phase of this run could still be handed this note."""
    return (
        m.author == PERSON and m.kind == NOTE and m.status in _HANDED and bool(m.meant_for) and _this_run(m, since)
    )





# ── notes that change finished work (#102 PR 3) ───────────────────────────────
_FINISHED_ROW = (PhaseStatus.APPROVED.value, PhaseStatus.PENDING_APPROVAL.value)
#: Where a note can be checked against finished work and changed after the step in hand.
_CHECKABLE = (
    PipelineStatus.RUNNING.value, PipelineStatus.AWAITING_APPROVAL.value, PipelineStatus.CANCELLED.value,
    PipelineStatus.FAILED.value, PipelineStatus.PAUSED.value,
)


def finished_rows(db: Session, project: Project) -> dict:
    """The phases whose work is done, by phase: the attempt that counts, with output."""
    from app.orchestration import versions

    return {
        r.phase: r for r in versions.current_rows(db, project)
        if r.status in _FINISHED_ROW and isinstance(r.output, dict) and r.output
    }


def checks_finished_work(db: Session, project: Project, text: str) -> bool:
    """Whether a note is read against the work already done: some is, the build isn't
    finished (a change request is how that's changed), no change request is open, and the
    note isn't addressed to a phase still to come."""
    from app.orchestration import changes

    if project.status not in _CHECKABLE or not finished_rows(db, project):
        return False
    if changes.open_change(db, project) is not None:
        return False
    named = addressed_to(text)
    return not (named and phase_state(project, named) == "not_started")


def scheduled(db: Session, project_id: str) -> list[CrewMessage]:
    """Notes whose change to finished work waits for the step in hand to end."""
    return list(
        db.execute(
            select(CrewMessage)
            .where(CrewMessage.project_id == project_id, CrewMessage.author == PERSON, CrewMessage.status == SCHEDULED)
            .order_by(CrewMessage.created_at)
        ).scalars()
    )


def touched_by(raw: object, finished: set[str]) -> dict:
    """What the planner said, held to the work that's done — no guessing: a plan naming
    only phases still to come (or nothing) changes no finished work."""
    from app.orchestration import changes

    data = raw if isinstance(raw, dict) else {}
    wanted = {str(p).strip() for p in data.get("phases") or [] if isinstance(p, str)}
    editors = [p for p in changes.EDITORS if p in wanted and p in finished]
    design = (bool(data.get("needs_design")) or bool(data.get("needs_db_change"))) and "system_design" in finished
    return {
        "summary": str(data.get("summary") or "").strip()[:300],
        "phases": editors,
        "files_likely": [str(p).strip() for p in data.get("files_likely") or [] if isinstance(p, str) and str(p).strip()][:20],
        "needs_design": design,
        "needs_db_change": bool(data.get("needs_db_change")),
        "touched": [p for p in _ORDER if p in editors or (design and p == "system_design")],
    }


def _who(phases: list[str]) -> str:
    names = [title(p) for p in phases]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def scope_note(project_id: str, message_id: str) -> None:
    """Read a note against the work already done, once the generation slot is free: the
    change planner (role `planner`) names who changes what, and the finished phases it
    names change after the step in hand (`runner._note_change`). Never raises."""
    from app.agents.base import AgentContext
    from app.agents.change_planner import planner
    from app.analytics import tracker
    from app.core import identity
    from app.core.constants import RoutingMode
    from app.db.base import SessionLocal
    from app.orchestration.charter import Charter
    from app.orchestration.runner import runner
    from app.router import inflight

    try:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            asked = db.get(CrewMessage, message_id)
            if project is None or asked is None or not (asked.plan or {}).get("checking"):
                return
            done = finished_rows(db, project)
            ctx = AgentContext(
                idea=project.idea,
                routing_mode=RoutingMode(project.routing_mode or "local_only"),
                preferred_model=project.preferred_model,
                charter=Charter.from_dict(project.charter),
                change=asked.text,
                extra_context=runner._app_index(project, list(done.values())),  # noqa: SLF001 - the one app index
            )
            owner, name, finished = project.owner_id, project.name, set(done)
        with identity.acting_as(owner), inflight.building(project_id, name), inflight.agent("Change planner"):
            result = planner.run(ctx)
        with SessionLocal() as db:
            tracker.record(db, response=result.response, project_id=project_id, phase="crew_note_plan", commit=False)
            asked = db.get(CrewMessage, message_id)
            project = db.get(Project, project_id)
            if asked is None or project is None or asked.status != QUEUED or not (asked.plan or {}).get("checking"):
                db.commit()
                return  # taken back, or read as a question, meanwhile
            plan = touched_by(result.output, finished)
            asked.plan = plan
            touched = plan["touched"]
            if touched and project.status in _CHECKABLE:
                asked.status = SCHEDULED
                asked.agent = touched[0]
                they = "it changes" if len(touched) == 1 else "they change"
                reply(db, asked, f"{_who(touched)} {'has' if len(touched) == 1 else 'have'} already done that part, so {they} it after this step.", touched[0])
            elif touched:
                reply(db, asked, "The build finished first, so nothing was changed. Ask for this as a change.")
                asked.status = DECLINED
            elif asked.meant_for:
                reply(db, asked, "Checked: it doesn't change finished work.")
            else:
                reply(db, asked, "Checked: it doesn't change finished work, and no agent is left to take it. Once the build is finished, ask for it as a change.")
                asked.status = DECLINED
            db.commit()
    except Exception as e:  # noqa: BLE001 - a note must never take anything down
        log.warning("Checking a note against finished work failed for %s: %s", project_id, e)
        try:
            with SessionLocal() as db:
                asked = db.get(CrewMessage, message_id)
                if asked is not None and (asked.plan or {}).get("checking"):
                    asked.plan = None
                    if asked.status == QUEUED:
                        reply(db, asked, f"I couldn't check whether this changes finished work: {_reason(e)}")
                        if not asked.meant_for:
                            asked.status = DECLINED
                    db.commit()
        except Exception:  # noqa: BLE001
            log.exception("Couldn't record that checking a note failed for %s", project_id)


# ── questions only a model can answer ─────────────────────────────────────────
WAIT_FOR_MODEL = "The crew answers this after the step in hand."
ASK_NOW = "Asking the crew."

#: How a question that needs a model is sent off. A thread of its own, so the request
#: returns at once; tests run it inline.
spawn: Callable[..., None] = lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True, name="crew-answer").start()

_SYSTEM = (
    "You are the crew of an AI software engineering team, building the app described "
    "below for the person you're talking to. Answer their question in two or three plain "
    "sentences, from what you know here. If it isn't decided yet, say what the plan is "
    "and that it can still change. Don't promise changes and don't write code."
)


def _context(db: Session, project: Project) -> str:
    from app.orchestration.charter import Charter

    rows = _rows(project)
    lines = [f"# The app\n{(project.idea or '').strip()[:600]}"]
    charter = Charter.from_dict(project.charter)
    if charter:
        lines.append(charter.prompt_block())
    said, _ = status_answer(project, "status", rows)
    lines.append(f"# Where the build is\n{said}")
    done = [p for p in _ORDER if phase_state(project, p, rows) in ("done", "waiting")]
    if done:
        lines.append("# Finished\n" + ", ".join(title(p) for p in done))
    if project.decisions:
        from app.orchestration.changes import settled

        lines.append("# Settled\n" + "\n".join(f"- {d}" for d in settled(list(project.decisions), 10)))
    from app.orchestration.runner import runner

    index = runner._app_index(project, list(rows.values()))  # noqa: SLF001 - the one app index
    if index:
        lines.append(f"# What it is made of\n{index}")
    return "\n\n".join(lines)


def ask_model(project_id: str, message_id: str) -> None:
    """Answer one question with a model, once the generation slot is free. Never raises.

    No session is held while the call waits for the slot or runs — on a local model that
    is minutes: the record is read in one short session and the answer written in another."""
    from app.analytics import tracker
    from app.core import identity
    from app.core.constants import RoutingMode
    from app.db.base import SessionLocal
    from app.router import inflight
    from app.router.router import router
    from app.schemas.llm import ChatMessage, GenerationOptions

    try:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            asked = db.get(CrewMessage, message_id)
            if project is None or asked is None:
                return
            owner, name, question = project.owner_id, project.name, asked.text
            mode = RoutingMode(project.routing_mode or "local_only")
            preferred = project.preferred_model
            context = _context(db, project)
        with identity.acting_as(owner), inflight.building(project_id, name), inflight.agent("Crew"):
            profile = router.profile_for(mode, preferred, complexity="low", role="planner")
            room = max(profile.prompt_char_budget - len(_SYSTEM) - len(question) - 200, 400)
            if len(context) > room:
                context = context[:room].rsplit("\n", 1)[0] + "\n…"
            messages = [
                ChatMessage(role="system", content=_SYSTEM),
                ChatMessage(role="user", content=f"{context}\n\n# Their question\n{question}"),
            ]
            # A few sentences; the window decides when it is smaller still.
            response = router.complete(
                messages, mode=mode, preferred_model=preferred, options=GenerationOptions(max_tokens=400),
                complexity="low", role="planner",
            )
        with SessionLocal() as db:
            tracker.record(db, response=response, project_id=project_id, phase="crew_answer", commit=False)
            asked = db.get(CrewMessage, message_id)
            # Read again as a note meanwhile, or gone with its build: the answer isn't wanted.
            if asked is not None and asked.kind == QUESTION and asked.status == QUEUED:
                answer = " ".join((response.text or "").split()).strip() or "The crew had nothing to say to that."
                reply(db, asked, answer[:TEXT_MAX])
                asked.status = ANSWERED
            db.commit()
    except Exception as e:  # noqa: BLE001 - a question must never take anything down
        log.warning("Crew answer failed for %s: %s", project_id, e)
        try:
            with SessionLocal() as db:
                asked = db.get(CrewMessage, message_id)
                if asked is not None and asked.status == QUEUED and asked.kind == QUESTION:
                    reply(db, asked, f"The crew couldn't answer this: {_reason(e)} Ask again in a moment.")
                    asked.status = DECLINED
                    db.commit()
        except Exception:  # noqa: BLE001
            log.exception("Couldn't record that the crew's answer failed for %s", project_id)


def _reason(e: Exception) -> str:
    from app.router.base import RequestCancelled

    if isinstance(e, RequestCancelled):
        return "the build was stopped while it waited."
    text = " ".join(str(e).split())[:200]
    return f"{text.rstrip('.')}." if text else "the model didn't answer."


def settle_orphans(db: Session) -> int:
    """At startup: questions the last run of this server was answering when it stopped, and
    notes kept for no phase before notes said who they were for — waiting for nobody.

    The app is one process — a build's run is a thread in it, and so is every answer
    (`activity` and the claim make the same assumption) — so at startup none is live."""
    found = db.execute(
        select(CrewMessage).where(
            CrewMessage.author == PERSON, CrewMessage.kind == QUESTION, CrewMessage.status == QUEUED
        )
    ).scalars().all()
    for m in found:
        reply(db, m, "The server restarted before the crew could answer. Ask again.")
        m.status = DECLINED
    # In Python: a JSON column keeps None as JSON `null`, which `IS NULL` doesn't match.
    stranded = [
        m for m in db.execute(
            select(CrewMessage).where(
                CrewMessage.author == PERSON, CrewMessage.kind == NOTE, CrewMessage.status == QUEUED
            )
        ).scalars()
        if m.meant_for is None
    ]
    for m in stranded:
        reply(db, m, "No agent was left to take this note. Once the build is finished, ask for it as a change.")
        m.status = DECLINED
    if found or stranded:
        db.commit()
    return len(found) + len(stranded)


__all__ = [
    "ANSWERED",
    "APPLIED",
    "CANCELLED",
    "CREW",
    "DECLINED",
    "NOTE",
    "PERSON",
    "QUESTION",
    "QUEUED",
    "SCHEDULED",
    "CHECK_AFTER",
    "addressed_to",
    "checks_finished_work",
    "finished_rows",
    "scheduled",
    "scope_note",
    "touched_by",
    "asked_during",
    "for_phase",
    "notes_signature",
    "signature_of",
    "take_notes",
    "ask_model",
    "classify",
    "is_status",
    "named_phase",
    "not_started",
    "now",
    "note_reply",
    "out",
    "phase_state",
    "recent",
    "reply",
    "settle_orphans",
    "status_answer",
    "thread",
    "title",
    "why_stuck",
]
