"""Notes reach the work that hasn't started (#102, PR 2).

A note sent while the build runs waits for the phases it is for. Each takes it when it
starts — a resume too, which starts the phase again — and a code phase takes a new one
between two files, so the files still to write carry it. It is carried in the prompt's
feedback slot and its budget, never a section of its own, and once the build is finished
it is one of the app's settled decisions.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import _conforming, asked_files, fenced, stub, through_question_gates
from app.agents import AGENTS
from app.agents.base import AgentContext
from app.core.constants import PipelineStatus
from app.db.base import SessionLocal
from app.db.models import CrewMessage, Project
from app.orchestration import crew_chat
from app.schemas.llm import LLMResponse, Usage
from tests.test_schema_conformance import _profile

#: What test_changes builds, which compiles: two backend files, a page and a part it uses.
CODE = {
    "backend/main.py": "def main():\n    return 'ok'\n",
    "backend/app/routes.py": "def add(a, b):\n    return a + b\n",
    "frontend/pages/index.jsx": "import List from '../components/List';\n\nexport default function Home() {\n  return <List />;\n}\n",
    "frontend/components/List.jsx": "export default function List() {\n  return <ul />;\n}\n",
}
FILES = {
    "backend_engineer": ["backend/main.py", "backend/app/routes.py"],
    "frontend_engineer": ["frontend/components/List.jsx", "frontend/pages/index.jsx"],
}
NOTE = "use a purple theme everywhere"


def _resp(text: str) -> LLMResponse:
    return LLMResponse(text=text, provider="mock", model="mock-model",
                       usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20), latency_ms=1)


class Script:
    """The model: two files for each code phase, one a call; every prompt recorded."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        #: Run before answering a call: `(role, messages) -> None`.
        self.before = None

    def __call__(self, messages, **kwargs) -> LLMResponse:
        role = kwargs.get("role") or ""
        schema = getattr(kwargs.get("options"), "json_schema", None)
        wanted = [] if schema else asked_files(messages)
        self.calls.append({"role": role, "plan": schema is not None, "writes": wanted, "prompt": messages[-1].content})
        if self.before:
            self.before(role, wanted)
        if wanted:
            return _resp(fenced({p: CODE.get(p, 'x = 1\n') for p in wanted}))
        if schema is None:
            return _resp(json.dumps({"summary": "ok"}))
        body = _conforming(schema)
        if role in FILES and "files" in (schema.get("properties") or {}):
            body["files"] = [{"path": p, "purpose": "part of the app"} for p in FILES[role]]
        return _resp(json.dumps(body))

    def prompts(self, role: str, *, plan: bool | None = None) -> list[str]:
        return [c["prompt"] for c in self.calls if c["role"] == role and (plan is None or c["plan"] == plan)]


@pytest.fixture
def script(monkeypatch, stub_router):
    s = Script()
    stub(monkeypatch, "complete", s)
    return s


def _project(client, **body) -> str:
    payload = {"idea": "A tiny habit tracker", "routing_mode": "local_only", "approval_mode": "unattended", **body}
    return client.post("/api/projects", json=payload).json()["id"]


def _note(pid: str, text: str = NOTE, meant_for=None) -> str:
    with SessionLocal() as db:
        m = CrewMessage(project_id=pid, author="person", text=text, kind="note", status="queued", meant_for=meant_for)
        db.add(m)
        db.commit()
        return m.id


def _thread(client, pid: str) -> list[dict]:
    return client.get(f"/api/projects/{pid}/messages").json()["messages"]


# ── at a phase's start ───────────────────────────────────────────────────────
def test_a_note_sent_before_a_phase_starts_is_in_its_plan_and_write_prompts(script, client):
    pid = _project(client)
    assert client.post(f"/api/projects/{pid}/messages", json={"text": NOTE}).status_code == 201
    assert client.post(f"/api/projects/{pid}/run").status_code == 200
    project = through_question_gates(client, pid)
    assert project["status"] == "completed", (project.get("gate_kind"), project.get("gate_note"), project.get("last_error"))

    for role in ("product_manager", "system_design", "backend_engineer", "frontend_engineer", "qa_engineer"):
        prompts = script.prompts(role)
        assert prompts and all(NOTE in p for p in prompts), f"{role} wasn't told"
    # In the feedback slot, under its own heading — a phase's first run has no attempt to review.
    [plan] = script.prompts("frontend_engineer", plan=True)
    assert "# What the person asked for — keep to it\n- " + NOTE in plan
    assert "Reviewer feedback on your previous attempt" not in plan
    assert all(NOTE in p for p in script.prompts("frontend_engineer", plan=False))

    [m] = _thread(client, pid)
    assert m["status"] == "applied"
    assert m["applied_to"][:3] == ["product_manager", "system_design", "backend_engineer"]
    # One reply when it's first taken, from whoever took it; the rest show as "Applied to".
    assert [r["text"] for r in m["replies"]][-1] == "Got your note. Product Manager is working with it now."
    assert m["replies"][-1]["agent"] == "product_manager"


def test_a_note_for_one_agent_reaches_that_agent_only(script, client):
    pid = _project(client)
    client.post(f"/api/projects/{pid}/messages", json={"text": "frontend: make the list sortable"})
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    assert all("make the list sortable" in p for p in script.prompts("frontend_engineer"))
    assert not any("make the list sortable" in p for p in script.prompts("backend_engineer"))
    assert _thread(client, pid)[0]["applied_to"] == ["frontend_engineer"]


def test_notes_asked_during_the_build_are_settled_decisions(script, client):
    pid = _project(client)
    client.post(f"/api/projects/{pid}/messages", json={"text": "skip voice input"})
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    with SessionLocal() as db:
        assert "Asked during the build: skip voice input" in (db.get(Project, pid).decisions or [])


# ── between two files ────────────────────────────────────────────────────────
def test_a_note_sent_while_backend_writes_reaches_its_next_file(monkeypatch, script, client):
    from app.agents import code_phase

    monkeypatch.setattr(code_phase._Run, "_batch_size", lambda self, n: 1)  # one file a call
    pid = _project(client)

    def send_mid_file(role, wanted):
        # During Backend's first file (the entry file is written last, #103).
        if role == "backend_engineer" and wanted:
            _note(pid, "keep every route in routes.py", ["backend_engineer", "frontend_engineer"])
            script.before = None

    script.before = send_mid_file
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    first, second = script.prompts("backend_engineer", plan=False)[:2]
    assert "keep every route in routes.py" not in first
    assert "keep every route in routes.py" in second, "the next file didn't carry the note"
    assert all("keep every route in routes.py" in p for p in script.prompts("frontend_engineer"))


# ── on resume ────────────────────────────────────────────────────────────────
def test_a_note_left_on_a_stopped_build_is_taken_when_it_resumes(script, client):
    pid = _project(client, approval_mode="checkpoints")
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["gate_kind"] == "plan", project["gate_kind"]
    assert client.post(f"/api/projects/{pid}/stop").status_code == 200
    m = client.post(f"/api/projects/{pid}/messages", json={"text": "use a simpler layout"}).json()["message"]
    assert "gets this note when it resumes" in m["replies"][0]["text"]
    assert client.post(f"/api/projects/{pid}/resume").status_code == 200
    for _ in range(4):
        project = through_question_gates(client, pid)
        if project["status"] != "awaiting_approval":
            break
        assert client.post(f"/api/projects/{pid}/approve", json={}).status_code == 200
    assert all("use a simpler layout" in p for p in script.prompts("frontend_engineer"))
    assert script.prompts("frontend_engineer"), "the build never reached the frontend"


# ── one slot, one budget ─────────────────────────────────────────────────────
@pytest.mark.parametrize("window", [8192, 4096])
def test_a_long_note_never_overruns_a_small_window(window):
    """The person's notes share the feedback slot's budget, alone or beside a reviewer's note."""
    profile = _profile(window)
    for key, agent in AGENTS.items():
        for feedback in (None, "Tighten the scope." * 20):
            ctx = AgentContext(
                idea="A team standup bot " * 50,
                prior_outputs={d: {"files": [{"code": "x" * 400_000}]} for d in agent.depends_on},
                rag_context="r" * 300_000,
                memory_context="m" * 300_000,
                extra_context="Debate decision: use Postgres.",
                feedback=feedback,
                asked="- " + "make it purple " * 400,
            )
            built = sum(len(m.content) for m in agent._build_messages(ctx, profile).messages)
            assert built <= profile.prompt_char_budget, f"{key} built {built:,} against {profile.prompt_char_budget:,}"


def test_beside_a_reviewers_note_the_reason_it_was_sent_back_comes_first():
    from app.agents.base import feedback_frame, feedback_text

    ctx = AgentContext(idea="x", feedback="Fix the import in main.py", asked="- no login")
    assert feedback_frame(ctx).startswith("# Reviewer feedback")
    # The reason it was sent back first: a small window cuts the end, never that.
    assert feedback_text(ctx) == (
        "Fix the import in main.py\n\nThe person also asked for this during the build:\n- no login"
    )
    alone = AgentContext(idea="x", asked="- no login")
    assert feedback_frame(alone) == "# What the person asked for — keep to it\n"
    assert feedback_text(alone) == "- no login"


def test_taking_a_note_again_hands_it_once(client):
    pid = _project(client)
    mid = _note(pid, meant_for=["backend_engineer"])
    for _ in range(3):
        assert crew_chat.take_notes(pid, "backend_engineer")[0] == f"- {NOTE}"
    assert crew_chat.take_notes(pid, "frontend_engineer")[0] == ""
    with SessionLocal() as db:
        m = db.get(CrewMessage, mid)
        assert m.applied_to == ["backend_engineer"] and m.status == "applied"
        replies = db.query(CrewMessage).filter_by(reply_to=mid).count()
    assert replies == 1, "a phase run again replied again"


def test_a_keep_trying_note_is_for_its_round_not_every_phase(client):
    pid = _project(client)
    mid = _note(pid, "import Router from react-router-dom v6", meant_for=[])
    assert crew_chat.take_notes(pid, "frontend_engineer")[0] == ""
    with SessionLocal() as db:
        db.get(CrewMessage, mid).applied_to = ["frontend_engineer"]
        db.commit()
        assert crew_chat.asked_during(db, pid) == [], "a round's note became a settled decision"


def test_every_note_is_handed_over_newest_first(client):
    """No note is left waiting: all of them, newest first, so a small window cuts the oldest."""
    pid = _project(client)
    ids = [_note(pid, f"note {i}", meant_for=["backend_engineer"]) for i in range(10)]
    text, _seen = crew_chat.take_notes(pid, "backend_engineer")
    lines = text.splitlines()
    assert lines[0] == "Newest first; where two disagree, the newer one stands."
    assert lines[1:] == [f"- note {i}" for i in reversed(range(10))]
    with SessionLocal() as db:
        assert all(db.get(CrewMessage, i).applied_to == ["backend_engineer"] for i in ids)


def test_a_few_notes_are_settled_decisions_after_the_stack(client):
    pid = _project(client)
    for i in range(8):
        mid = _note(pid, f"note {i}", meant_for=["backend_engineer"])
    crew_chat.take_notes(pid, "backend_engineer")
    with SessionLocal() as db:
        kept = crew_chat.asked_during(db, pid)
    assert kept == [f"Asked during the build: note {i}" for i in range(3, 8)]


def test_a_note_added_and_one_taken_back_still_moves_the_signature(client):
    from app.orchestration import claim
    from app.router import inflight

    pid = _project(client)
    first = _note(pid, "keep it small")
    with inflight.building(pid):
        before = claim.between_calls(notes=True)
        with SessionLocal() as db:
            db.get(CrewMessage, first).status = "cancelled"
            db.commit()
        _note(pid, "make it purple")
        after = claim.between_calls(notes=True)
    assert before[0] == after[0] == 1
    assert before != after, "one note in for one out went unseen"
    assert after == crew_chat.take_notes(pid, "backend_engineer")[1], "the two reads disagree"


def test_a_failed_read_keeps_the_notes_the_phase_had(monkeypatch, client):
    from app.agents import code_phase
    from app.orchestration import claim

    pid = _project(client)
    run = code_phase._Run.__new__(code_phase._Run)
    run.agent = AGENTS["backend_engineer"]
    run.ctx = AgentContext(idea="x", asked="- no login", asked_seen=(1, "a"))
    run.notes_seen = run.ctx.asked_seen
    monkeypatch.setattr(claim, "between_calls", lambda notes=False: (2, "b"))
    monkeypatch.setattr(crew_chat, "take_notes", lambda *a: None)
    from app.router import inflight

    with inflight.building(pid):
        run._between_files()
    assert run.ctx.asked == "- no login" and run.notes_seen == (1, "a"), "a failed read wiped the notes or stopped retrying"


def test_a_redo_carries_the_notes_too(script, client):
    pid = _project(client, approval_mode="checkpoints")
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["gate_kind"] == "plan"
    _note(pid, "no login anywhere", meant_for=["system_design", "backend_engineer"])
    mark = len(script.calls)
    r = client.post(f"/api/projects/{pid}/redo", json={"phase": "system_design", "feedback": "Use fewer services."})
    assert r.status_code == 200, r.text
    redone = [c["prompt"] for c in script.calls[mark:] if c["role"] == "system_design"]
    assert redone and all("no login anywhere" in p and "Use fewer services." in p for p in redone)


def test_a_note_for_no_phase_is_handed_to_none(client):
    """A Keep-trying note kept before notes said who they were for (NULL) was for its round."""
    pid = _project(client)
    _note(pid, "just stub the API", meant_for=None)
    assert crew_chat.take_notes(pid, "backend_engineer")[0] == ""


def test_a_note_from_before_the_build_last_finished_is_history(client):
    from datetime import datetime, timedelta, timezone

    from app.db.models import Version

    pid = _project(client)
    old = _note(pid, "make it dark-themed", meant_for=["frontend_engineer"])
    with SessionLocal() as db:
        db.get(CrewMessage, old).created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.add(Version(project_id=pid, number=1, label="First build", kind="first_build", snapshot={}))
        db.commit()
    fresh = _note(pid, "use a grid", meant_for=["frontend_engineer"])
    text, _seen = crew_chat.take_notes(pid, "frontend_engineer")
    assert text == "- use a grid", "a note from the run before was handed to this one"
    with SessionLocal() as db:
        assert db.get(CrewMessage, fresh).applied_to == ["frontend_engineer"]


def test_only_a_code_phase_between_files_pays_for_the_notes_read(client):
    from app.orchestration import claim
    from app.router import inflight

    pid = _project(client)
    with inflight.building(pid):
        assert claim.between_calls() is None
        assert claim.between_calls(notes=True) == (0, None)


def test_the_page_is_told_which_notes_still_stand(client):
    pid = _project(client)
    standing = _note(pid, "use a grid", meant_for=["frontend_engineer"])
    _note(pid, "stub the API for this round", meant_for=[])
    thread = {m["text"]: m["standing"] for m in client.get(f"/api/projects/{pid}/messages").json()["messages"]}
    assert thread == {"use a grid": True, "stub the API for this round": False}
    assert standing


def test_the_stack_line_survives_every_tail_of_the_decisions():
    from app.orchestration.changes import settled

    notes = ["Stack — FastAPI + React"] + [f"v{i}: change {i}" for i in range(2, 20)]
    assert settled(notes, 8)[0] == "Stack — FastAPI + React" and len(settled(notes, 8)) == 8
    assert settled(notes, 8)[-1] == "v19: change 19"
    assert settled(["v2: x", "v3: y"], 1) == ["v3: y"], "no stack line: just the newest"


def test_notes_a_kept_change_run_took_are_remembered_with_it(client):
    from datetime import datetime, timedelta, timezone

    from app.db.models import Version

    pid = _project(client)
    with SessionLocal() as db:
        db.add(Version(project_id=pid, number=1, label="First build", kind="first_build", snapshot={},
                       created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
        db.commit()
    _note(pid, "no login", meant_for=["backend_engineer"])
    crew_chat.take_notes(pid, "backend_engineer")
    with SessionLocal() as db:
        assert crew_chat.asked_during(db, pid, "change #2", this_run=True) == ["Asked during change #2: no login"]


def test_a_note_left_for_no_phase_before_notes_said_is_settled_at_startup(client):
    pid = _project(client)
    mid = _note(pid, "add dark mode", meant_for=None)
    with SessionLocal() as db:
        assert crew_chat.settle_orphans(db) >= 1
        m = db.get(CrewMessage, mid)
        assert m.status == "declined"
        assert db.query(CrewMessage).filter_by(reply_to=mid).count() == 1
