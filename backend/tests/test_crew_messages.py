"""Talk to the crew while it builds (#102): questions answered, notes kept.

A status question is answered from the build's own record — the phase working now and
its file, the fix loop's round and trend, what a stop waits for — with no model call.
A question that record can't answer is answered by a model once the step in hand is
done. A note waits for the agents it is for; on a stuck build it is Keep trying with
the person's words.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.conftest import stub
from app.core.constants import GateKind, PhaseStatus, PipelineStatus
from app.db.base import Base, SessionLocal, engine
from app.db.models import CrewMessage, PhaseResult, Project, UsageEvent
from app.orchestration import activity, autofix, crew_chat
from app.router import inflight
from app.schemas.llm import LLMResponse, Usage


@pytest.fixture(autouse=True)
def _clean_boards():
    activity._board.clear()
    activity._given.clear()
    yield
    activity._board.clear()
    activity._given.clear()


@pytest.fixture
def calls(monkeypatch, client):
    """Every model call, answered with a fixed sentence. The thread runs inline."""
    seen: list[dict] = []

    def complete(messages, **kwargs):
        seen.append({"messages": messages, **kwargs})
        return LLMResponse(text="It will: the layout is responsive.", provider="mock", model="mock-model",
                           usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    monkeypatch.setattr(crew_chat, "spawn", lambda fn, *args: fn(*args))
    return seen


def _build(client, **row) -> str:
    pid = client.post("/api/projects", json={"idea": "A todo app with smooth animations", "approval_mode": "checkpoints"}).json()["id"]
    with SessionLocal() as db:
        project = db.get(Project, pid)
        for key, value in row.items():
            setattr(project, key, value)
        db.commit()
    return pid


def _row(pid: str, phase: str, status: str, **extra) -> None:
    with SessionLocal() as db:
        db.add(PhaseResult(project_id=pid, phase=phase, agent=phase, status=status,
                           created_at=datetime.now(timezone.utc), **extra))
        db.commit()


def _running_backend(client) -> str:
    """Planning done and approved, Backend writing its fourth file of nine."""
    pid = _build(client, status=PipelineStatus.RUNNING.value, current_phase="backend_engineer",
                 heartbeat_at=datetime.now(timezone.utc))
    _row(pid, "product_manager", PhaseStatus.APPROVED.value)
    _row(pid, "system_design", PhaseStatus.APPROVED.value)
    _row(pid, "backend_engineer", PhaseStatus.RUNNING.value, model_pick={"model": "local:qwen2.5:7b"}, is_local=True)
    files = ["server.js", "db.js", "models/todo.js", "routes/todos.js"] + [f"lib/{i}.js" for i in range(5)]
    with inflight.building(pid):
        activity.begin("backend_engineer")
        activity.plan(files, 1, "An Express API over SQLite")
        activity.stage("writing", total=len(files))
        for path in files[:3]:
            activity.file(path, "ok")
        activity.file("routes/todos.js", "writing")
    return pid


def _say(client, pid: str, text: str) -> dict:
    r = client.post(f"/api/projects/{pid}/messages", json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()["message"]


# ── question or note ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,kind", [
    ("what are you doing?", "question"),
    ("Why is it stuck", "question"),
    ("status", "question"),
    ("How's it going", "question"),
    ("will it work on phones?", "question"),
    ("make it purple", "note"),
    ("skip voice input", "note"),
    ("Whatever you do, keep it simple", "note"),
    ("frontend: use a purple theme", "note"),
    # Asking for something to be done is a note, question mark or not…
    ("Can you keep all the routes in one file", "note"),
    ("could you make it purple?", "note"),
    # …unless what it asks for is to be told something, or is about the crew itself.
    ("can you tell me what's left", "question"),
    ("Could you explain why it failed", "question"),
    ("Will you be done soon", "question"),
    ("Can you see the error", "question"),
    # Other ways a question opens.
    ("Which model is running", "question"),
    ("Has the backend started", "question"),
    ("Does it work on phones", "question"),
    ("Don't add login", "note"),
])
def test_a_message_is_a_question_by_its_words_alone(text, kind):
    assert crew_chat.classify(text) == kind


def test_the_page_reads_a_message_by_the_same_words_as_the_server():
    """The box's button and the stuck card's draft follow `readsAsQuestion`, a copy of
    `classify`: its word lists must be the server's."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "frontend/components/build/CrewTalk.tsx").read_text()

    def words(name: str) -> set[str]:
        found = re.search(rf"const {name} = (?:new Set\()?\[(.*?)\]", source, re.S)
        assert found, f"{name} is not in CrewTalk.tsx any more"
        return set(re.findall(r'"([^"]+)"', found.group(1)))

    assert words("ASKS") == set(crew_chat._ASKS)
    assert words("NOT_A_REQUEST") == set(crew_chat._NOT_A_REQUEST)


@pytest.mark.parametrize("text,status", [
    ("what are you doing?", True),
    ("Why did it stop?", True),
    ("Who's next?", True),
    ("What are you waiting for?", True),
    ("how long will it take?", True),
    ("has the frontend started?", True),
    ("which model are you on?", True),
    # About the app, not the build: a model answers, with the build's record beside it.
    ("What file has the login form?", False),
    ("How long do login sessions last?", False),
    ("Can you add dark mode now?", False),
    ("will it work on phones?", False),
])
def test_only_a_question_about_the_build_is_answered_from_its_record(text, status):
    assert crew_chat.is_status(text) is status


# ── questions answered at once ───────────────────────────────────────────────
def test_what_are_you_doing_is_answered_from_live_progress_with_no_model_call(calls, client):
    pid = _running_backend(client)
    m = _say(client, pid, "what are you doing?")
    assert m["kind"] == "question" and m["status"] == "answered"
    [answer] = m["replies"]
    assert answer["agent"] == "backend_engineer"
    assert "Backend Engineer is writing `routes/todos.js`, file 4 of 9." in answer["text"]
    assert "Next up: Frontend Engineer." in answer["text"]
    assert calls == [], "a status answer called a model"


def test_the_fix_round_and_its_trend_are_part_of_the_answer(calls, client):
    pid = _running_backend(client)
    data = autofix.load(type("P", (), {"auto_fix": None})())
    t = autofix.track(data, "build:backend_engineer")
    for sent, left in ((3, 6), (6, 4)):
        autofix.start_round(t, "with_code", ["backend_engineer"], [{"key": f"k{i}"} for i in range(sent)])
        autofix.close_round(t, [f"n{i}" for i in range(left)], net=True)
    autofix.start_round(t, "with_code", ["backend_engineer"], [{"key": f"n{i}"} for i in range(4)])
    with SessionLocal() as db:
        db.get(Project, pid).auto_fix = data
        db.commit()
    text = _say(client, pid, "status")["replies"][0]["text"]
    assert "Fix round 3 of 3. Problems went 3 → 6 → 4." in text


def test_who_is_next_and_how_long_get_answers_of_their_own(calls, client):
    pid = _running_backend(client)
    assert _say(client, pid, "Who's next?")["replies"][0]["text"] == "Next up: Frontend Engineer."
    text = _say(client, pid, "how long will it take?")["replies"][0]["text"]
    assert text.startswith("Backend Engineer has just started. It's phase 3 of 8, and 5 more phases after it.")
    assert "no finish time" in text
    assert calls == []


def test_which_model_is_running(calls, client):
    pid = _running_backend(client)
    text = _say(client, pid, "which model are you on?")["replies"][0]["text"]
    assert text == "It's running on local:qwen2.5:7b on this computer."
    assert calls == []


def test_a_question_about_one_agent_says_where_it_is(calls, client):
    pid = _running_backend(client)
    assert _say(client, pid, "has the frontend started?")["replies"][0]["text"].startswith(
        "Frontend Engineer hasn't started yet. Backend Engineer is working now, and Frontend Engineer is next."
    )
    assert _say(client, pid, "is the architecture done?")["replies"][0]["text"] == "System Design is finished."


def test_why_is_it_stuck_names_the_reason_and_the_trend(calls, client):
    data = {"tracks": {"build:backend_engineer": {
        "allowed": 3, "resumed_after": 0, "episode_start": 0, "accepted": None,
        "rounds": [
            {"n": 1, "strategy": "with_code", "phases": ["backend_engineer"], "episode": 0,
             "problems": [{"key": f"a{i}"} for i in range(3)], "fixed": [], "remaining": [], "after": 6, "broke": 6},
            {"n": 2, "strategy": "with_code", "phases": ["backend_engineer"], "episode": 0,
             "problems": [{"key": f"b{i}"} for i in range(6)], "fixed": [], "remaining": [], "after": 4, "broke": 0},
        ],
        "stopped": {"reason": autofix.STOP_NO_PROGRESS, "left": 4, "keys": [], "problems": [
            {"key": "x", "title": "Module './db' has no exported member 'Todo'", "kind": "build", "phase": "backend_engineer"},
        ]},
    }}}
    pid = _build(client, status=PipelineStatus.AWAITING_APPROVAL.value, gate_kind=GateKind.NEEDS_HELP.value,
                 current_phase="backend_engineer", auto_fix=data)
    text = _say(client, pid, "why is it stuck?")["replies"][0]["text"]
    assert "The files in the backend don't connect to each other yet: 4 problems left after 2 fix rounds." in text
    assert "Problems went 3 → 6 → 4." in text
    assert "didn't leave fewer problems" in text
    assert calls == []


def test_a_stop_says_what_it_waits_for(calls, client):
    pid = _build(client, status=PipelineStatus.AWAITING_APPROVAL.value, gate_kind=GateKind.PLAN.value,
                 current_phase="system_design")
    assert "The plan is ready" in _say(client, pid, "what are you waiting for?")["replies"][0]["text"]


# ── questions only a model can answer ────────────────────────────────────────
def test_a_question_the_record_cant_answer_goes_to_a_model_after_the_step(calls, client):
    pid = _running_backend(client)
    m = _say(client, pid, "will it work on phones?")
    # The request answers before the model does: what it returns is the wait.
    assert m["status"] == "queued" and m["replies"][0]["text"] == crew_chat.WAIT_FOR_MODEL
    thread = client.get(f"/api/projects/{pid}/messages").json()["messages"]
    replies = thread[-1]["replies"]
    assert [r["text"] for r in replies] == [crew_chat.WAIT_FOR_MODEL, "It will: the layout is responsive."]
    assert thread[-1]["status"] == "answered"
    [call] = calls
    assert call["role"] == "planner" and call["complexity"] == "low"
    prompt = call["messages"][-1].content
    assert "will it work on phones?" in prompt and "A todo app with smooth animations" in prompt
    with SessionLocal() as db:
        assert db.query(UsageEvent).filter_by(project_id=pid, phase="crew_answer").count() == 1


def test_a_model_that_fails_says_so_and_the_question_is_declined(monkeypatch, calls, client):
    def broken(messages, **kwargs):
        raise RuntimeError("Ollama failed: Connection refused")

    stub(monkeypatch, "complete", broken)
    pid = _build(client, status=PipelineStatus.CANCELLED.value)
    _say(client, pid, "will it work on phones?")
    last = client.get(f"/api/projects/{pid}/messages").json()["messages"][-1]
    assert last["status"] == "declined"
    assert last["replies"][0]["text"] == crew_chat.ASK_NOW
    assert "Connection refused" in last["replies"][-1]["text"]


def test_three_questions_can_wait_on_a_model_and_a_fourth_is_turned_away(monkeypatch, client):
    monkeypatch.setattr(crew_chat, "spawn", lambda fn, *args: None)  # the model never gets to them
    pid = _running_backend(client)
    said = [_say(client, pid, f"will it work on phone {i}?") for i in range(4)]
    assert [m["status"] for m in said] == ["queued", "queued", "queued", "declined"]
    assert "still on your last questions" in said[-1]["replies"][0]["text"]


def test_questions_left_by_a_restart_are_settled(client):
    pid = _build(client, status=PipelineStatus.RUNNING.value)
    with SessionLocal() as db:
        db.add(CrewMessage(project_id=pid, author="person", text="will it scale?", kind="question", status="queued"))
        db.commit()
        assert crew_chat.settle_orphans(db) == 1
    last = client.get(f"/api/projects/{pid}/messages").json()["messages"][-1]
    assert last["status"] == "declined" and "restarted" in last["replies"][0]["text"]


# ── notes ────────────────────────────────────────────────────────────────────
def test_a_note_waits_for_the_agents_that_havent_started(calls, client):
    pid = _running_backend(client)
    m = _say(client, pid, "make it purple")
    assert m["kind"] == "note" and m["status"] == "queued"
    assert m["replies"][0]["text"] == (
        "Got it. Frontend Engineer and the 4 after it haven't started, so they get this note when they start."
    )
    assert m["replies"][0]["agent"] == "frontend_engineer"
    assert m["meant_for"] == ["frontend_engineer", "qa_engineer", "security_engineer", "devops_engineer", "cost_estimation"]
    assert calls == []


def test_a_note_on_a_build_stopped_mid_phase_goes_to_that_phase_first(calls, client):
    pid = _running_backend(client)
    with SessionLocal() as db:
        db.get(Project, pid).status = PipelineStatus.CANCELLED.value
        db.commit()
    m = _say(client, pid, "use a simpler layout")
    assert m["replies"][0]["text"] == "Got it. The build is stopped, so Backend Engineer gets this note when it resumes."
    assert m["meant_for"][:2] == ["backend_engineer", "frontend_engineer"]


def test_a_note_for_one_agent_says_so(calls, client):
    pid = _running_backend(client)
    m = _say(client, pid, "frontend: use a purple theme")
    assert m["replies"][0]["text"] == "Got it. Frontend Engineer hasn't started, so it gets this note when it starts."
    assert m["agent"] == "frontend_engineer", "who it waits for is kept on the message"


def test_a_note_on_a_stuck_build_is_keep_trying_with_it(calls, client):
    data = {"tracks": {"build:backend_engineer": {
        "allowed": 3, "resumed_after": 0, "episode_start": 0, "accepted": None,
        "rounds": [{"n": i, "strategy": "with_code", "phases": ["backend_engineer"], "episode": 0,
                    "problems": [{"key": "a"}], "fixed": [], "remaining": ["a"]} for i in (1, 2, 3)],
        "stopped": {"reason": autofix.STOP_LIMIT, "left": 1, "keys": ["a"]},
    }}}
    pid = _build(client, status=PipelineStatus.AWAITING_APPROVAL.value, gate_kind=GateKind.NEEDS_HELP.value,
                 current_phase="backend_engineer", auto_fix=data)
    import app.api.routes.messages as routes

    driven: list[str] = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(routes, "_drive", lambda project_id, token: driven.append(project_id))
    try:
        m = _say(client, pid, "keep all the routes in one file")
    finally:
        monkey.undo()
    assert m["status"] == "applied" and m["applied_to"] == ["backend_engineer"]
    assert m["replies"][0]["text"] == "Trying again with your note, up to 2 more rounds."
    assert driven == [pid]
    with SessionLocal() as db:
        project = db.get(Project, pid)
        assert project.status == PipelineStatus.RUNNING.value
        assert project.auto_fix["tracks"]["build:backend_engineer"]["note"] == "keep all the routes in one file"


def test_a_note_with_nothing_left_to_fix_waits_like_any_other(calls, client):
    # Stuck on security, with every serious finding waived: the card says "Continue the
    # build", and no fix round would read a note.
    data = {"tracks": {"security": {
        "allowed": 3, "resumed_after": 0, "episode_start": 0, "accepted": None,
        "rounds": [{"n": 1, "strategy": "with_code", "phases": ["backend_engineer"], "episode": 0,
                    "problems": [{"key": "a"}], "fixed": [], "remaining": ["a"]}],
        "stopped": {"reason": autofix.STOP_NO_PROGRESS, "left": 1, "keys": ["a"]},
    }}}
    pid = _build(client, status=PipelineStatus.AWAITING_APPROVAL.value, gate_kind=GateKind.NEEDS_HELP.value,
                 current_phase="cost_estimation", auto_fix=data)
    m = _say(client, pid, "use postgres")
    assert m["status"] == "queued"
    with SessionLocal() as db:
        project = db.get(Project, pid)
        assert project.status == PipelineStatus.AWAITING_APPROVAL.value, "a note claimed a round nobody runs"
        assert "note" not in project.auto_fix["tracks"]["security"]


def test_try_again_with_a_note_is_refused_like_try_again_when_it_cant_start(calls, client):
    pid = _running_backend(client)
    r = client.post(f"/api/projects/{pid}/messages", json={"text": "keep it in one file", "keep_trying": True})
    assert r.status_code == 409, r.text
    assert client.get(f"/api/projects/{pid}/messages").json()["messages"] == [], "a refused Try again kept a note"


def test_only_the_newest_message_can_be_read_again_and_not_while_a_model_answers(monkeypatch, calls, client):
    monkeypatch.setattr(crew_chat, "spawn", lambda fn, *args: None)
    pid = _running_backend(client)
    older = _say(client, pid, "make it purple")
    waiting = _say(client, pid, "will it work on phones?")
    assert client.post(f"/api/projects/{pid}/messages/{older['id']}/reread", json={"kind": "question"}).status_code == 409
    r = client.post(f"/api/projects/{pid}/messages/{waiting['id']}/reread", json={"kind": "note"})
    assert r.status_code == 409 and "still answering" in r.json()["detail"]


def test_a_stalled_build_never_says_an_agent_is_working(calls, client):
    pid = _running_backend(client)
    with SessionLocal() as db:
        db.get(Project, pid).heartbeat_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()
    text = _say(client, pid, "is the backend done?")["replies"][0]["text"]
    assert text.startswith("Nothing is driving this build")


def test_a_note_on_a_finished_build_points_at_the_change_box(calls, client):
    pid = _build(client, status=PipelineStatus.COMPLETED.value)
    m = _say(client, pid, "add login")
    assert m["status"] == "declined" and "Say what should change" in m["replies"][0]["text"]


def test_that_was_a_question_reads_a_note_again(calls, client):
    pid = _running_backend(client)
    m = _say(client, pid, "tell me the status")
    assert m["kind"] == "note"
    r = client.post(f"/api/projects/{pid}/messages/{m['id']}/reread", json={"kind": "question"})
    assert r.status_code == 200, r.text
    again = r.json()["message"]
    assert again["kind"] == "question" and again["status"] == "answered"
    assert "Backend Engineer is writing `routes/todos.js`" in again["replies"][-1]["text"]
    assert client.post(f"/api/projects/{pid}/messages/{m['id']}/reread", json={"kind": "question"}).status_code == 409


# ── the guard and the thread ─────────────────────────────────────────────────
def test_a_message_with_a_credential_is_refused_and_kept_nowhere(calls, client):
    pid = _running_backend(client)
    r = client.post(f"/api/projects/{pid}/messages", json={"text": "use postgres://admin:hunter2secret@db.example.com:5432/app"})
    assert r.status_code == 422
    assert client.get(f"/api/projects/{pid}/messages").json()["messages"] == []


def test_a_message_is_at_most_two_thousand_characters(client):
    pid = _build(client)
    assert client.post(f"/api/projects/{pid}/messages", json={"text": "x" * 2001}).status_code == 422


def test_the_build_carries_its_newest_messages_and_the_list_does_not(calls, client):
    pid = _running_backend(client)
    for i in range(crew_chat.RECENT + 2):
        _say(client, pid, f"status {i}")
    project = client.get(f"/api/projects/{pid}").json()
    assert len(project["messages"]) == crew_chat.RECENT
    assert project["messages"][-1]["text"] == f"status {crew_chat.RECENT + 1}"
    assert all(m["replies"] for m in project["messages"])
    listed = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert "messages" not in listed


def test_messages_go_with_the_build(calls, client):
    pid = _running_backend(client)
    _say(client, pid, "status")
    with SessionLocal() as db:
        db.get(Project, pid).status = PipelineStatus.COMPLETED.value
        db.commit()
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    with SessionLocal() as db:
        assert db.query(CrewMessage).filter_by(project_id=pid).count() == 0


def test_the_table_is_added_to_a_database_from_before(tmp_path):
    from sqlalchemy import create_engine, inspect

    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    tables = [t for name, t in Base.metadata.tables.items() if name != "crew_messages"]
    Base.metadata.create_all(old, tables=tables)
    assert "crew_messages" not in inspect(old).get_table_names()
    Base.metadata.create_all(old)
    from app.db.migrations import run_migrations

    run_migrations(old)
    assert "crew_messages" in inspect(old).get_table_names()
    assert engine is not old
