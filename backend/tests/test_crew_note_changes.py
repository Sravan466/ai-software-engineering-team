"""Notes that change finished work (#102, PR 3).

"Don't add login" sent while Frontend writes is read against the work already done: the
change planner names the Backend, so the Backend's files are edited after the step in hand
— #79's edit mode, not a rewrite — and the build carries on to the end. The person can
take the change back until it starts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tests.conftest import _conforming, asked_files, fenced, stub, through_question_gates
from app.core.constants import PhaseStatus, PipelineStatus
from app.db.base import SessionLocal
from app.db.models import CrewMessage, PhaseResult, Project
from app.orchestration import crew_chat
from app.orchestration.graph import graph
from app.orchestration.runner import _config
from app.schemas.llm import LLMResponse, Usage

CODE = {
    "backend/main.py": "def main():\n    return 'ok'\n",
    "backend/app/routes.py": "def login():\n    return 'login'\n",
    "frontend/pages/index.jsx": "import List from '../components/List';\n\nexport default function Home() {\n  return <List />;\n}\n",
    "frontend/components/List.jsx": "export default function List() {\n  return <ul />;\n}\n",
}
FILES = {
    "backend_engineer": ["backend/main.py", "backend/app/routes.py"],
    "frontend_engineer": ["frontend/components/List.jsx", "frontend/pages/index.jsx"],
}
NOTE = "don't add login"


def _resp(text: str) -> LLMResponse:
    return LLMResponse(text=text, provider="mock", model="mock-model",
                       usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20), latency_ms=1)


class Script:
    """Two files a code phase; the planner names the Backend; the Backend's edit plan
    rewrites routes.py only. Every call recorded, in order."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.plan = {"summary": "Take login out", "phases": ["backend_engineer"],
                     "files_likely": ["backend/app/routes.py"], "needs_design": False, "needs_db_change": False}
        self.before = None

    def __call__(self, messages, **kwargs) -> LLMResponse:
        role = kwargs.get("role") or ""
        schema = getattr(kwargs.get("options"), "json_schema", None)
        wanted = [] if schema else asked_files(messages)
        props = (schema or {}).get("properties") or {}
        editing = role in FILES and "deleted" in props and "framework" not in props
        self.calls.append({"role": role, "writes": wanted, "edit_plan": editing, "prompt": messages[-1].content})
        if self.before:
            self.before(role, wanted)
        if role == "planner":
            return _resp(json.dumps(self.plan))
        if wanted:
            return _resp(fenced({p: CODE.get(p, "x = 1\n").replace("'login'", "None") if editing else CODE.get(p, "x = 1\n") for p in wanted}))
        if editing:
            return _resp(json.dumps({"summary": "No login", "files": [{"path": "backend/app/routes.py", "purpose": "routes without login"}]}))
        if schema is None:
            return _resp(json.dumps({"summary": "ok"}))
        body = _conforming(schema)
        if role in FILES and "files" in props:
            body["files"] = [{"path": p, "purpose": "part of the app"} for p in FILES[role]]
        return _resp(json.dumps(body))


@pytest.fixture
def script(monkeypatch, stub_router):
    s = Script()
    stub(monkeypatch, "complete", s)
    monkeypatch.setattr(crew_chat, "spawn", lambda fn, *args: fn(*args))
    return s


def _project(client, **body) -> str:
    payload = {"idea": "A tiny habit tracker", "routing_mode": "local_only", "approval_mode": "unattended", **body}
    return client.post("/api/projects", json=payload).json()["id"]


def _thread(client, pid: str) -> list[dict]:
    return client.get(f"/api/projects/{pid}/messages").json()["messages"]


def _schedule(pid: str, text: str = NOTE) -> str:
    """A note the planner already read: it changes the Backend's finished work."""
    with SessionLocal() as db:
        m = CrewMessage(project_id=pid, author="person", text=text, kind="note", status="scheduled", meant_for=[],
                        plan={"summary": "Take login out", "phases": ["backend_engineer"],
                              "files_likely": ["backend/app/routes.py"], "needs_design": False,
                              "needs_db_change": False, "touched": ["backend_engineer"]})
        db.add(m)
        db.commit()
        return m.id


# ── after the step in hand ───────────────────────────────────────────────────
def test_a_note_that_changes_the_backend_edits_it_after_the_frontend_and_the_build_finishes(script, client):
    pid = _project(client)
    sent: list[str] = []

    def during_frontend(role, wanted):
        if role == "frontend_engineer" and wanted and not sent:
            sent.append(_schedule(pid))

    script.before = during_frontend
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["status"] == "completed", (project.get("gate_kind"), project.get("gate_note"), project.get("last_error"))

    roles = [c["role"] for c in script.calls]
    frontend_last = max(i for i, c in enumerate(script.calls) if c["role"] == "frontend_engineer" and not c["edit_plan"] and c["writes"])
    edit_at = next(i for i, c in enumerate(script.calls) if c["edit_plan"])
    assert script.calls[edit_at]["role"] == "backend_engineer"
    assert edit_at > frontend_last, "the change didn't wait for the step in hand"
    # Its files are edited, not written again: one file, the one the change is about.
    rewrites = [c for c in script.calls[edit_at:] if c["role"] == "backend_engineer" and c["writes"]]
    assert [c["writes"] for c in rewrites] == [["backend/app/routes.py"]]
    assert NOTE in script.calls[edit_at]["prompt"]
    assert "qa_engineer" in roles[edit_at:], "the build didn't carry on after the change"

    [m] = _thread(client, pid)
    assert m["status"] == "applied" and "backend_engineer" in m["applied_to"]
    assert m["replies"][-1]["text"] == "Changing it now: Backend Engineer starts on its finished work."
    with SessionLocal() as db:
        assert db.get(Project, pid).status == PipelineStatus.COMPLETED.value
    assert not graph.get_state(_config(pid)).values.get("change"), "the note's change outlived the build"


def test_a_change_taken_back_before_it_starts_changes_nothing(script, client):
    pid = _project(client)

    def during_frontend(role, wanted):
        if role == "frontend_engineer" and wanted and script.before:
            mid = _schedule(pid)
            r = client.post(f"/api/projects/{pid}/messages/{mid}/cancel")
            assert r.status_code == 200, r.text
            script.before = None

    script.before = during_frontend
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    assert not any(c["edit_plan"] for c in script.calls), "a cancelled change still ran"
    [m] = _thread(client, pid)
    assert m["status"] == "cancelled" and m["replies"][-1]["text"] == "Cancelled. Nothing was changed."
    assert client.post(f"/api/projects/{pid}/messages/{m['id']}/cancel").status_code == 409


# ── reading a note against finished work ─────────────────────────────────────
def _running_frontend(client) -> str:
    """Backend finished with its files; Frontend writing."""
    pid = _project(client)
    with SessionLocal() as db:
        project = db.get(Project, pid)
        project.status, project.current_phase = PipelineStatus.RUNNING.value, "frontend_engineer"
        project.heartbeat_at = datetime.now(timezone.utc)
        for phase in ("product_manager", "system_design", "backend_engineer"):
            out = {"files": [{"path": p, "code": CODE[p]} for p in FILES.get(phase, [])]} if phase in FILES else {"summary": "x"}
            db.add(PhaseResult(project_id=pid, phase=phase, agent=phase, status=PhaseStatus.APPROVED.value, output=out))
        db.add(PhaseResult(project_id=pid, phase="frontend_engineer", agent="frontend_engineer", status=PhaseStatus.RUNNING.value))
        db.commit()
    return pid


def test_a_note_that_touches_finished_work_is_scheduled_after_this_step(script, client):
    pid = _running_frontend(client)
    m = client.post(f"/api/projects/{pid}/messages", json={"text": NOTE}).json()["message"]
    assert m["replies"][0]["text"].endswith("I'll also check whether it changes finished work after this step.")
    [m] = _thread(client, pid)
    assert m["status"] == "scheduled" and m["touched"] == ["backend_engineer"] and not m["checking"]
    assert m["replies"][-1]["text"] == "Backend Engineer has already done that part, so it changes it after this step."
    [plan_call] = [c for c in script.calls if c["role"] == "planner"]
    assert NOTE in plan_call["prompt"] and "backend/app/routes.py" in plan_call["prompt"]


def test_a_note_about_work_still_to_come_changes_nothing_finished(script, client):
    script.plan = {"summary": "Purple", "phases": ["frontend_engineer"], "files_likely": [], "needs_design": False}
    pid = _running_frontend(client)
    client.post(f"/api/projects/{pid}/messages", json={"text": "make it purple"})
    [m] = _thread(client, pid)
    assert m["status"] == "queued" and m["touched"] == []
    assert m["replies"][-1]["text"] == "Checked: it doesn't change finished work."


def test_a_note_for_a_phase_still_to_come_is_not_checked(script, client):
    pid = _running_frontend(client)
    client.post(f"/api/projects/{pid}/messages", json={"text": "qa: test the streaks"})
    assert not [c for c in script.calls if c["role"] == "planner"]


def test_the_planner_naming_nothing_finished_is_not_guessed_into_a_change():
    finished = {"backend_engineer", "system_design"}
    assert crew_chat.touched_by({"phases": []}, finished)["touched"] == []
    assert crew_chat.touched_by({"phases": ["frontend_engineer"]}, finished)["touched"] == []
    assert crew_chat.touched_by(None, finished)["touched"] == []
    both = crew_chat.touched_by({"phases": ["backend_engineer"], "needs_design": True}, finished)
    assert both["touched"] == ["system_design", "backend_engineer"]
    # A small model often names the file and not the phase (seen on qwen2.5:7b): its word too.
    by_file = crew_chat.touched_by({"phases": [], "files_likely": ["backend/app/controllers/bookController.js"]}, finished)
    assert by_file["touched"] == ["backend_engineer"]
    assert crew_chat.touched_by({"files_likely": ["frontend/pages/index.jsx"]}, finished)["touched"] == []
