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
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.constants import PHASE_LABELS, PHASE_ORDER, GateKind, PhaseStatus, PipelineStatus
from app.core.logging import get_logger
from app.db.models import CrewMessage, Project

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

_ASKS = frozenset({"what", "why", "when", "how", "where", "who", "is", "are", "can", "will", "status"})


def classify(text: str) -> str:
    """`question` or `note`, from the words alone."""
    said = text.strip()
    if said.endswith("?"):
        return QUESTION
    first = re.match(r"[a-z]+", said.lower())
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
        return "working" if project.status in ("running", "cancelled") else "not_started"
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
        verb = {"writing": "writing", "fixing": "fixing", "changing": "changing"}[stage]
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
        top = max(set(kinds), key=kinds.count) if kinds else "other"
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
#: Words that make a question one about the build's progress, answered from its record.
_STATUS_WORDS = frozenset({
    "doing", "status", "progress", "happening", "working", "going", "now", "step", "phase", "file", "files",
    "done", "finished", "finish", "left", "round", "rounds", "slow", "update", "next", "after", "long", "longer",
    "eta", "soon", "waiting", "wait", "stopped", "paused", "stuck", "wrong", "problem", "problems", "error",
    "errors", "fail", "failed", "failing", "broken", "fix", "fixing", "model", "models", "llm", "started", "start",
})
_STUCK_WORDS = frozenset({
    "stuck", "wrong", "problem", "problems", "error", "errors", "fail", "failed", "failing", "broken", "fix",
    "fixing",
})
_STATUS_PHRASES = re.compile(r"\bwhat'?s up\b|\bup to\b|\bwhere (are|is) (you|we|it|things)\b|\bhow('?s| is| are) (it|things)\b")


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def is_status(text: str) -> bool:
    """Whether the build's own record answers this question."""
    return bool(_words(text) & _STATUS_WORDS) or bool(_STATUS_PHRASES.search(text.lower()))


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
    if project.status == PipelineStatus.RUNNING.value and project.current_phase in _ORDER and phase in ahead:
        between = _ORDER.index(phase) - _ORDER.index(project.current_phase) - 1
        return f"{who} hasn't started yet. " + (
            f"{title(project.current_phase)} is working now, and {who} is next."
            if between <= 0
            else f"{title(project.current_phase)} is working now, then {between} more before it."
        )
    return f"{who} hasn't started yet."


def status_answer(project: Project, text: str) -> tuple[str, Optional[str]]:
    """The answer to a status question, and the agent who gives it. Never calls a model."""
    rows = _rows(project)
    words = _words(text)
    status = project.status
    asks_stuck = bool(words & _STUCK_WORDS)
    asks_model = bool(words & {"model", "models", "llm"})

    if status == PipelineStatus.AWAITING_APPROVAL.value and project.gate_kind == GateKind.NEEDS_HELP.value:
        why = why_stuck(project)
        return (why or "The crew stopped and asked for help." + (f" {project.gate_note}" if project.gate_note else "")), project.current_phase

    phase = named_phase(text)
    if phase and not asks_model and not asks_stuck:
        return _phase_answer(project, phase, rows), phase

    if status == PipelineStatus.RUNNING.value and project.stalled:
        return (
            "Nothing is driving this build: the server probably restarted mid-phase. Resume "
            "re-runs that phase from the last checkpoint and keeps everything approved.",
            None,
        )
    if status == PipelineStatus.RUNNING.value:
        if asks_model:
            return (_model_line(project, rows) or "The model hasn't answered yet, so I can't name it."), project.current_phase
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
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _reply_out(m: CrewMessage) -> dict:
    return {"id": m.id, "agent": m.agent, "text": m.text, "status": m.status, "created_at": _iso(m.created_at)}


def out(m: CrewMessage, replies: Iterable[CrewMessage] = ()) -> dict:
    """A person's message as the page reads it, with the crew's replies to it."""
    return {
        "id": m.id,
        "text": m.text,
        "kind": m.kind,
        "status": m.status,
        "agent": m.agent,
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
    return [out(m, by.get(m.id, [])) for m in asked]


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
    base = m.created_at
    if base is not None:
        base = base if base.tzinfo else base.replace(tzinfo=timezone.utc)
        if at <= base:
            at = base + timedelta(microseconds=1)
    return at


# ── a note, for now ───────────────────────────────────────────────────────────
def note_reply(project: Project, text: str) -> tuple[str, Optional[str], list[str]]:
    """What happens to a note, said plainly; who it's for. A note waits, `queued`."""
    rows = _rows(project)
    ahead = not_started(project, rows)
    named = addressed_to(text)
    if named:
        state = phase_state(project, named, rows)
        if state == "not_started":
            return f"Got it. {title(named)} hasn't started, so it gets this note when it starts.", named, [named]
        if state == "working" and named in CODE_PHASES:
            return f"Got it. {title(named)} is writing its files now; it reads this note before the next one.", named, [named]
        return (
            f"{title(named)} has already {'started' if state == 'working' else 'finished'}. This note "
            "waits with the build, and the crew checks whether it changes that work after this step.",
            named,
            [named],
        )
    if not ahead:
        return "Every phase has started, so this note waits with the build until the crew can act on it.", None, []
    first = ahead[0]
    if project.status in (PipelineStatus.CANCELLED.value, PipelineStatus.FAILED.value, PipelineStatus.PAUSED.value) or (
        project.status == PipelineStatus.RUNNING.value and project.stalled
    ):
        return f"Got it. The build is stopped, so {title(first)} gets this note when it resumes.", first, ahead
    rest = ahead[1:]
    if not rest:
        said = f"{title(first)} hasn't started, so it gets this note when it starts."
    elif len(rest) == 1:
        said = f"{title(first)} and {title(rest[0])} haven't started, so they get this note when they start."
    else:
        said = f"{title(first)} and the {len(rest)} after it haven't started, so they get this note when they start."
    return f"Got it. {said}", first, ahead


# ── questions only a model can answer ─────────────────────────────────────────
WAIT_FOR_MODEL = "The crew answers this after the step in hand."
ASK_NOW = "Asking the crew."

#: How a question that needs a model is sent off. A thread of its own, so the request
#: returns at once; tests run it inline.
spawn: Callable[..., None] = lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True, name="crew-answer").start()

#: Questions being answered by this process, so a restart's leftovers can be told apart.
_asking: set[str] = set()
_asking_lock = threading.Lock()

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
    said, _ = status_answer(project, "status")
    lines.append(f"# Where the build is\n{said}")
    done = [p for p in _ORDER if phase_state(project, p, rows) in ("done", "waiting")]
    if done:
        lines.append("# Finished\n" + ", ".join(title(p) for p in done))
    if project.decisions:
        lines.append("# Settled\n" + "\n".join(f"- {d}" for d in list(project.decisions)[-10:]))
    from app.orchestration.runner import runner

    index = runner._app_index(project, list(rows.values()))  # noqa: SLF001 - the one app index
    if index:
        lines.append(f"# What it is made of\n{index}")
    return "\n\n".join(lines)


def ask_model(project_id: str, message_id: str) -> None:
    """Answer one question with a model, once the generation slot is free. Never raises."""
    from app.analytics import tracker
    from app.core import identity
    from app.core.constants import RoutingMode
    from app.db.base import SessionLocal
    from app.router import inflight
    from app.router.router import router
    from app.schemas.llm import ChatMessage, GenerationOptions

    with _asking_lock:
        _asking.add(message_id)
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        asked = db.get(CrewMessage, message_id)
        if project is None or asked is None:
            return
        with identity.acting_as(project.owner_id), inflight.building(project.id, project.name), inflight.agent("Crew"):
            mode = RoutingMode(project.routing_mode or "local_only")
            profile = router.profile_for(mode, project.preferred_model, complexity="low", role="planner")
            context = _context(db, project)
            room = max(profile.prompt_char_budget - len(_SYSTEM) - len(asked.text) - 200, 400)
            if len(context) > room:
                context = context[:room].rsplit("\n", 1)[0] + "\n…"
            messages = [
                ChatMessage(role="system", content=_SYSTEM),
                ChatMessage(role="user", content=f"{context}\n\n# Their question\n{asked.text}"),
            ]
            # A few sentences; the window decides when it is smaller still.
            options = GenerationOptions(max_tokens=400)
            response = router.complete(
                messages, mode=mode, preferred_model=project.preferred_model, options=options,
                complexity="low", role="planner",
            )
        tracker.record(db, response=response, project_id=project.id, phase="crew_answer", commit=False)
        db.refresh(asked)
        if asked.kind != QUESTION or asked.status != QUEUED:
            db.commit()
            return  # read again as a note meanwhile: the answer is no longer wanted
        answer = " ".join((response.text or "").split()).strip() or "The crew had nothing to say to that."
        reply(db, asked, answer[:TEXT_MAX])
        asked.status = ANSWERED
        db.commit()
    except Exception as e:  # noqa: BLE001 - a question must never take anything down
        log.warning("Crew answer failed for %s: %s", project_id, e)
        db.rollback()
        try:
            asked = db.get(CrewMessage, message_id)
            if asked is not None and asked.status == QUEUED and asked.kind == QUESTION:
                reply(db, asked, f"The crew couldn't answer this: {_reason(e)} Ask again in a moment.")
                asked.status = DECLINED
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        with _asking_lock:
            _asking.discard(message_id)
        db.close()


def _reason(e: Exception) -> str:
    from app.router.base import RequestCancelled

    if isinstance(e, RequestCancelled):
        return "the build was stopped while it waited."
    text = " ".join(str(e).split())[:200]
    return f"{text.rstrip('.')}." if text else "the model didn't answer."


def settle_orphans(db: Session) -> int:
    """Questions a previous run of this server was answering when it stopped: say so."""
    with _asking_lock:
        live = set(_asking)
    found = db.execute(
        select(CrewMessage).where(
            CrewMessage.author == PERSON, CrewMessage.kind == QUESTION, CrewMessage.status == QUEUED
        )
    ).scalars().all()
    n = 0
    for m in found:
        if m.id in live:
            continue
        reply(db, m, "The server restarted before the crew could answer. Ask again.")
        m.status = DECLINED
        n += 1
    if n:
        db.commit()
    return n


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
    "addressed_to",
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
