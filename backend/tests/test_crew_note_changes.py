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
        self.edited: set[str] = set()

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
        if editing:
            self.edited.add(role)
        if wanted:
            # After its edit plan, a phase's writes carry the change: login is gone.
            changed = role in self.edited
            return _resp(fenced({p: CODE.get(p, "x = 1\n").replace("'login'", "None") if changed else CODE.get(p, "x = 1\n") for p in wanted}))
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
        m = CrewMessage(project_id=pid, author="person", text=text, kind="note", status="queued", meant_for=[],
                        plan={"summary": "Take login out", "phases": ["backend_engineer"],
                              "files_likely": ["backend/app/routes.py"], "needs_design": False,
                              "needs_db_change": False, "touched": ["backend_engineer"], "state": "scheduled"})
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
    assert m["change"] == "applied" and "backend_engineer" in m["applied_to"]
    # Kept only for its change: settled with it.
    assert m["status"] == "applied"
    texts = [r["text"] for r in m["replies"]]
    assert "Changing it now: Backend Engineer starts on its finished work." in texts
    # And what it did, once the Backend had edited.
    assert texts[-1] == "Backend Engineer changed 1 file: `backend/app/routes.py`."
    with SessionLocal() as db:
        assert db.get(Project, pid).status == PipelineStatus.COMPLETED.value
    assert not graph.get_state(_config(pid)).values.get("change"), "the note's change outlived the build"


def test_a_change_taken_back_before_it_starts_changes_nothing(script, client):
    pid = _project(client)

    def during_frontend(role, wanted):
        if role == "frontend_engineer" and wanted and script.before:
            mid = _schedule(pid)
            r = client.post(f"/api/projects/{pid}/messages/{mid}/cancel", json={"change": True})
            assert r.status_code == 200, r.text
            script.before = None

    script.before = during_frontend
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    assert not any(c["edit_plan"] for c in script.calls), "a cancelled change still ran"
    [m] = _thread(client, pid)
    assert m["change"] == "cancelled" and m["replies"][-1]["text"] == "Cancelled the change. Nothing finished was changed."
    assert m["status"] == "cancelled", "a note kept only for its change stayed waiting"
    assert client.post(f"/api/projects/{pid}/messages/{m['id']}/cancel", json={"change": True}).status_code == 409


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
    assert m["change"] == "scheduled" and m["touched"] == ["backend_engineer"]
    # Delivered to the phases still to come all the same: the change is apart from that.
    assert m["status"] == "queued" and m["meant_for"]
    assert m["replies"][-1]["text"] == "Backend Engineer has already done that part, so it changes it after this step."
    [plan_call] = [c for c in script.calls if c["role"] == "planner"]
    assert NOTE in plan_call["prompt"] and "backend/app/routes.py" in plan_call["prompt"]


def test_a_note_about_work_still_to_come_changes_nothing_finished(script, client):
    script.plan = {"summary": "Purple", "phases": ["frontend_engineer"], "files_likely": [], "needs_design": False}
    pid = _running_frontend(client)
    client.post(f"/api/projects/{pid}/messages", json={"text": "make it purple"})
    [m] = _thread(client, pid)
    assert m["status"] == "queued" and m["change"] == "none" and m["touched"] == []
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


# ── the change apart from the delivery ───────────────────────────────────────
def test_the_check_holds_after_a_later_phase_has_taken_the_note(script, client):
    pid = _running_frontend(client)
    with SessionLocal() as db:
        m = CrewMessage(project_id=pid, author="person", text=NOTE, kind="note", status="applied",
                        meant_for=["frontend_engineer"], applied_to=["frontend_engineer"], plan={"state": "checking"})
        db.add(m)
        db.commit()
        mid = m.id
    crew_chat.scope_note(pid, mid)
    with SessionLocal() as db:
        assert crew_chat.change_state(db.get(CrewMessage, mid)) == "scheduled", "taken by Frontend, its change was dropped"


def test_a_change_whose_first_edit_never_lands_waits_again(monkeypatch, script, client):
    from app.router.base import ProviderError

    pid = _project(client)
    real = script.__call__

    def failing(messages, **kwargs):
        props = (getattr(kwargs.get("options"), "json_schema", None) or {}).get("properties") or {}
        if kwargs.get("role") == "backend_engineer" and "deleted" in props:
            raise ProviderError("Ollama failed: connection refused")
        return real(messages, **kwargs)

    stub(monkeypatch, "complete", failing)
    sent: list[str] = []

    def during_frontend(role, wanted):
        if role == "frontend_engineer" and wanted and not sent:
            sent.append(_schedule(pid))

    script.before = during_frontend
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["status"] == "failed"
    [m] = _thread(client, pid)
    assert m["change"] == "scheduled", "a change that never started was lost"
    assert m["replies"][-1]["text"] == "The build stopped before the change was made. It's made when the build goes on."


def test_a_change_still_waiting_when_the_build_finishes_is_missed(client):
    pid = _project(client)
    mid = _schedule(pid)
    with SessionLocal() as db:
        crew_chat.settle_unchanged(db, pid)
        m = db.get(CrewMessage, mid)
        assert crew_chat.change_state(m) == "missed" and m.status == "declined"


def test_a_notes_change_stops_being_live_once_its_edits_are_done():
    from app.orchestration.runner import runner

    held = {"note": ["x"], "edit": ["backend_engineer"], "done": []}
    assert runner._live_change(None, held) is held
    assert runner._live_change(None, {**held, "done": ["backend_engineer"]}) is None


def test_a_check_cut_off_by_a_restart_says_so(client):
    pid = _project(client)
    with SessionLocal() as db:
        m = CrewMessage(project_id=pid, author="person", text=NOTE, kind="note", status="queued", meant_for=[],
                        plan={"state": "checking"})
        db.add(m)
        db.commit()
        mid = m.id
        crew_chat.settle_orphans(db)
        m = db.get(CrewMessage, mid)
        assert crew_chat.change_state(m) == "unchecked" and m.status == "declined"


def test_taking_a_note_back_cancels_its_change_too(script, client):
    pid = _running_frontend(client)
    mid = _schedule(pid)
    r = client.post(f"/api/projects/{pid}/messages/{mid}/cancel")
    assert r.status_code == 200, r.text
    m = r.json()["message"]
    assert m["status"] == "cancelled" and m["change"] == "cancelled"


def test_the_planner_repairs_a_reply_that_isnt_its_shape(monkeypatch, stub_router):
    """A small model's first plan is often not the JSON asked for: the repair round must run."""
    from app.agents.base import AgentContext
    from app.agents.change_planner import planner

    replies = iter(["Sure! Here is the plan: change the backend.", json.dumps(Script().plan)])
    stub(monkeypatch, "complete", lambda messages, **kw: _resp(next(replies)))
    result = planner.run(AgentContext(idea="A habit tracker", change=NOTE, extra_context="backend_engineer:\n- backend/app/routes.py"))
    assert result.output["phases"] == ["backend_engineer"]


def test_an_unreadable_plan_is_not_called_no_change(monkeypatch, script, client):
    pid = _running_frontend(client)
    stub(monkeypatch, "complete", lambda messages, **kw: _resp("I think the backend, maybe."))
    client.post(f"/api/projects/{pid}/messages", json={"text": NOTE})
    [m] = _thread(client, pid)
    assert m["change"] == "unchecked"
    assert m["replies"][-1]["text"].startswith("I couldn't tell whether this changes finished work")


def test_a_notes_change_no_longer_edits_once_its_edits_are_done():
    from app.orchestration import changes

    held = {"note": ["x"], "edit": ["backend_engineer", "frontend_engineer"], "done": ["backend_engineer"]}
    assert changes.editing(held, "frontend_engineer")
    assert changes.editing(held, "backend_engineer"), "a fix round inside the change still edits"
    done = {**held, "done": ["backend_engineer", "frontend_engineer"]}
    assert not changes.editing(done, "backend_engineer") and not changes.editing(done, "frontend_engineer")
    # A change request's record is unchanged: it edits until it's reviewed.
    assert changes.editing({"id": "c1", "edit": ["backend_engineer"], "done": ["backend_engineer"]}, "backend_engineer")


def test_a_note_whose_change_is_scheduled_cant_be_read_again(script, client):
    pid = _running_frontend(client)
    mid = _schedule(pid)
    r = client.post(f"/api/projects/{pid}/messages/{mid}/reread", json={"kind": "question"})
    assert r.status_code == 409 and "already acting" in r.json()["detail"]


def test_files_changed_reads_fingerprints():
    before = crew_chat.fingerprint({"files": [{"path": "a.py", "code": "x = 1\n"}, {"path": "b.py", "code": "y = 2\n"}]})
    after = {"files": [{"path": "a.py", "code": "x = 1\n"}, {"path": "b.py", "code": "y = 3\n"}, {"path": "c.py", "code": "z = 0\n"}]}
    assert crew_chat.files_changed(before, after) == ["b.py", "c.py"]
    assert crew_chat.files_changed(before, {"files": [{"path": "a.py", "code": "x = 1\n"}, {"path": "b.py", "code": "y = 2\n"}]}) == []
