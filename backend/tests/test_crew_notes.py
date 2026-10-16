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


def test_beside_a_reviewers_note_the_persons_words_come_first():
    from app.agents.base import feedback_frame, feedback_text

    ctx = AgentContext(idea="x", feedback="Fix the import in main.py", asked="- no login")
    assert feedback_frame(ctx).startswith("# Reviewer feedback")
    assert feedback_text(ctx).startswith("The person asked for this during the build:\n- no login\n\n")
    alone = AgentContext(idea="x", asked="- no login")
    assert feedback_frame(alone) == "# What the person asked for — keep to it\n"
    assert feedback_text(alone) == "- no login"


def test_taking_a_note_again_hands_it_once(client):
    pid = _project(client)
    mid = _note(pid, meant_for=["backend_engineer"])
    for _ in range(3):
        assert crew_chat.take_notes(pid, "backend_engineer") == f"- {NOTE}"
    assert crew_chat.take_notes(pid, "frontend_engineer") == ""
    with SessionLocal() as db:
        m = db.get(CrewMessage, mid)
        assert m.applied_to == ["backend_engineer"] and m.status == "applied"
        replies = db.query(CrewMessage).filter_by(reply_to=mid).count()
    assert replies == 1, "a phase run again replied again"
