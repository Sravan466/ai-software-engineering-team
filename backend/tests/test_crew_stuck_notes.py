"""A stuck build takes your note (#102, PR 4).

On a build that stopped to ask for help, a message is the next fix round's instructions:
the person's words go into the round's prompt and onto its row in the card. A note that
decides something about the app ("skip the health check") also stands for the rest of
the build, and the round may drop what it leaves out instead of fixing it again.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import _conforming, asked_files, fenced, stub, through_question_gates
from app.db.base import SessionLocal
from app.db.models import CrewMessage, Project
from app.orchestration import autofix, crew_chat
from app.schemas.llm import LLMResponse, Usage

BROKEN = "def health(:\n    return\n"
GOOD = {
    "frontend/pages/index.jsx": "export default function Home() {\n  return <div>Hi</div>;\n}\n",
}


def _resp(text: str) -> LLMResponse:
    return LLMResponse(text=text, provider="mock", model="mock-model",
                       usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20), latency_ms=1)


class Script:
    """A backend that never compiles: the build gets stuck. Every prompt recorded."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, messages, **kwargs) -> LLMResponse:
        role = kwargs.get("role") or ""
        schema = getattr(kwargs.get("options"), "json_schema", None)
        wanted = [] if schema else asked_files(messages)
        self.calls.append({"role": role, "prompt": messages[-1].content, "writes": wanted})
        if wanted:
            return _resp(fenced({p: GOOD.get(p, BROKEN) for p in wanted}))
        if schema is None:
            return _resp(json.dumps({"summary": "ok"}))
        body = _conforming(schema)
        props = schema.get("properties") or {}
        if role == "backend_engineer" and "files" in props:
            body["files"] = [{"path": "backend/main.py", "purpose": "the API and its health check"}]
        if role == "frontend_engineer" and "files" in props:
            body["files"] = [{"path": p, "purpose": "the home page"} for p in GOOD]
        return _resp(json.dumps(body))


@pytest.fixture
def script(monkeypatch, stub_router):
    s = Script()
    stub(monkeypatch, "complete", s)
    return s


def _stuck(client) -> str:
    pid = client.post("/api/projects", json={"idea": "A tiny API with a health check", "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["gate_kind"] == "needs_help", (project["status"], project.get("gate_kind"), project.get("last_error"))
    return pid


@pytest.mark.parametrize("text,decided", [
    ("skip voice input", True),
    ("no login", True),
    ("Don't add login", True),
    ("please remove the export button", True),
    ("Forget about dark mode", True),
    ("Let's skip voice input", True),
    ("No, skip login", True),
    ("please, skip login", True),
    ("Just drop dark mode", True),
    ("keep all the routes in one file", False),
    ("use bcrypt for passwords", False),
    ("No, try again", False),
    # Fix hints that merely start like a decision.
    ("dropdown should close on click", False),
    ("remove the unused import in main.py", False),
    ("cut the file into two modules", False),
    ("without changing the API, fix the import", False),
    ("No worries, keep trying", False),
    ("forget my last note, use bcrypt", False),
    ("skipping the cache is fine", False),
    # Waving off a message, or a reply, isn't a decision either.
    ("forget about that", False),
    ("forget about it", False),
    ("no luck", False),
    ("no thanks", False),
    ("No idea", False),
    ("no change", False),
    ("remove the duplicate route", False),
    ("drop the extra brace", False),
    ("remove the login page", True),
])
def test_a_decision_is_told_from_a_hint_by_its_words(text, decided):
    assert crew_chat.is_decision(text) is decided


def test_a_note_on_a_stuck_build_is_the_next_rounds_instructions_and_on_its_row(script, client):
    pid = _stuck(client)
    mark = len(script.calls)
    m = client.post(f"/api/projects/{pid}/messages", json={"text": "put the health route in main.py"}).json()["message"]
    assert m["status"] == "applied" and m["meant_for"] == []
    assert m["replies"][0]["text"] == "Trying again with your note, up to 2 more rounds."
    fix_prompts = [c["prompt"] for c in script.calls[mark:] if c["role"] == "backend_engineer" and c["writes"]]
    assert fix_prompts and "# What the person asks you to try\nput the health route in main.py" in fix_prompts[0]
    assert autofix.DECIDED not in fix_prompts[0], "a hint was told it may drop features"
    track = client.get(f"/api/projects/{pid}").json()["auto_fix"]["tracks"]["build:backend_engineer"]
    noted = [r for r in track["rounds"] if r.get("note")]
    assert noted[0]["note"] == "put the health route in main.py" and not noted[0].get("decided")


def test_a_decision_on_a_stuck_build_may_drop_what_it_leaves_out_and_stands(script, client):
    pid = _stuck(client)
    mark = len(script.calls)
    m = client.post(f"/api/projects/{pid}/messages", json={"text": "skip the health check"}).json()["message"]
    assert m["replies"][0]["text"] == (
        "Trying again with your note, up to 2 more rounds. I'll keep it as a decision for the rest of the build, "
        "and the round may drop what it leaves out instead of fixing it. "
        "I'll also check whether it changes finished work after this round."
    )
    with SessionLocal() as db:
        assert db.get(CrewMessage, m["id"]).plan.get("decided")
    # It stands for the phases still to come.
    assert m["meant_for"] and "backend_engineer" not in m["meant_for"]
    fix_prompts = [c["prompt"] for c in script.calls[mark:] if c["role"] == "backend_engineer" and c["writes"]]
    assert f"skip the health check\n{autofix.DECIDED}" in fix_prompts[0]
    # The row keeps the person's own words, and that they were a decision.
    track = client.get(f"/api/projects/{pid}").json()["auto_fix"]["tracks"]["build:backend_engineer"]
    noted = [r for r in track["rounds"] if r.get("note")]
    assert noted[0]["note"] == "skip the health check" and noted[0]["decided"] is True


def test_the_cloud_button_with_a_draft_goes_through_the_conversation(monkeypatch, script, client):
    from app.orchestration.runner import runner

    pid = _stuck(client)
    seen: list = []
    monkeypatch.setattr("app.api.routes.messages.start_keep_trying",
                        lambda db, project, **kw: (seen.append(kw), (2, ["build:backend_engineer"], "tok"))[1])
    monkeypatch.setattr("app.api.routes.messages._drive", lambda *a: None)
    r = client.post(f"/api/projects/{pid}/messages", json={"text": "skip voice input", "keep_trying": True, "model": "anthropic:claude-x"})
    assert r.status_code == 201, r.text
    assert seen == [{"note": "skip voice input", "model": "anthropic:claude-x", "decided": True}]
    # The person's say beats the words.
    seen.clear()
    with SessionLocal() as db:
        project = db.get(Project, pid)
        project.status, project.gate_kind = "awaiting_approval", "needs_help"
        db.commit()
    client.post(f"/api/projects/{pid}/messages", json={"text": "skip voice input", "keep_trying": True, "decided": False})
    assert seen[0]["decided"] is False
    assert r.json()["message"]["status"] == "applied"
    assert runner is not None


def test_a_decision_with_no_phase_left_still_becomes_a_settled_decision(client):
    pid = client.post("/api/projects", json={"idea": "A tiny app"}).json()["id"]
    with SessionLocal() as db:
        db.add(CrewMessage(project_id=pid, author="person", text="no login", kind="note", status="applied",
                           meant_for=[], applied_to=None, plan={"decided": True}))
        db.add(CrewMessage(project_id=pid, author="person", text="import it from ./db", kind="note", status="applied",
                           meant_for=[], applied_to=["backend_engineer"]))
        db.commit()
        assert crew_chat.asked_during(db, pid) == ["Asked during the build: no login"]


def test_the_person_words_and_the_decision_travel_apart():
    t = {"note": "skip voice input", "decided": True}
    t = {"note": "skip voice input", "decision": "skip voice input"}
    said, ask, decided = autofix.take_person(t)
    assert said == "skip voice input" and ask == f"skip voice input\n{autofix.DECIDED}" and decided
    assert "note" not in t and t["decision"] == "skip voice input", "the decision stands for the episode"
    record: dict = {}
    autofix.mark_round(record, said, decided)
    assert record == {"note": "skip voice input", "decided": True}
    # The next round of the episode is told the decision too, in no new words of the person's.
    said, ask, decided = autofix.take_person(t)
    assert said is None and decided and ask == f"The person decided: skip voice input\n{autofix.DECIDED}"
    # A new episode starts without it.
    t["rounds"] = []
    autofix.settle(t)
    assert "decision" not in t and autofix.take_person(t) == (None, None, False)


def test_the_box_asks_the_server_how_a_draft_reads(client):
    pid = client.post("/api/projects", json={"idea": "A tiny app"}).json()["id"]
    read = lambda text: client.post(f"/api/projects/{pid}/messages/read", json={"text": text}).json()
    assert read("skip voice input") == {"kind": "note", "decided": True}
    assert read("keep all the routes in one file") == {"kind": "note", "decided": False}
    assert read("why is it stuck?") == {"kind": "question", "decided": False}
    with SessionLocal() as db:
        assert db.query(CrewMessage).filter_by(project_id=pid).count() == 0, "reading a draft kept it"
