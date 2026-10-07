"""Keep talking after a build (#79): change requests, versions, restore.

A scripted model builds a small app — two backend files and a page — and then answers
the change calls: the planner names who edits, an engineer's edit plan names the files,
and its write calls send back only those files.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from tests.conftest import _conforming, asked_files, fenced, stub, through_question_gates
from tests.test_deploy import _connect_github, fakes  # noqa: F401 - the fixture is used by name
from app.schemas.llm import LLMResponse, Usage

BACKEND = {
    "backend/main.py": "def main():\n    return 'ok'\n",
    "backend/app/util.py": "def add(a, b):\n    return a + b\n",
}
FRONTEND = {"frontend/pages/index.jsx": "export default function Home() {\n  return <div>Hi</div>;\n}\n"}
HEALTH = "def health():\n    return {'status': 'ok'}\n"
BROKEN = "def health(:\n    return\n"


def _resp(text: str) -> LLMResponse:
    return LLMResponse(text=text, provider="mock", model="mock-model",
                       usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20), latency_ms=1)


class Script:
    """The model, scripted by role. `plan` is what the change planner answers; `edits`
    is each engineer's edit plan; `writes` the code a write call returns per path."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.plan = {"summary": "Add a health endpoint", "phases": ["backend_engineer"],
                     "files_likely": ["backend/main.py"], "needs_design": False, "needs_db_change": False}
        self.edits = {
            "backend_engineer": {"summary": "Add /health", "files": [{"path": "backend/routes/health.py", "purpose": "the health route"}]},
            "frontend_engineer": {"summary": "Nothing", "files": []},
        }
        self.writes = {**BACKEND, **FRONTEND, "backend/routes/health.py": HEALTH}
        self.changing = False

    def __call__(self, messages, **kwargs) -> LLMResponse:
        role = kwargs.get("role") or ""
        schema = getattr(kwargs.get("options"), "json_schema", None)
        self.calls.append({"role": role, "schema": schema, "messages": messages, "changing": self.changing})
        if role == "planner":
            return _resp(json.dumps(self.plan))
        if schema is None:
            wanted = asked_files(messages)
            if wanted:
                return _resp(fenced({p: self.writes.get(p, "x = 1\n") for p in wanted}))
            return _resp(json.dumps(_conforming({"type": "object", "properties": {"summary": {"type": "string"}}})))
        props = schema.get("properties") or {}
        if role in ("backend_engineer", "frontend_engineer") and "deleted" in props and "framework" not in props:
            return _resp(json.dumps(self.edits.get(role, {"summary": "", "files": []})))
        body = _conforming(schema)
        if role == "backend_engineer" and "files" in props:
            body["files"] = [{"path": p, "purpose": "backend file"} for p in BACKEND]
        if role == "frontend_engineer" and "files" in props:
            body["files"] = [{"path": p, "purpose": "the home page"} for p in FRONTEND]
        return _resp(json.dumps(body))

    def roles_since(self, mark: int) -> set[str]:
        return {c["role"] for c in self.calls[mark:]}


@pytest.fixture
def script(monkeypatch, stub_router):
    s = Script()
    stub(monkeypatch, "complete", s)
    return s


def _finished(client, **body) -> str:
    payload = {"idea": "A tiny calculator app", "routing_mode": "local_only", "approval_mode": "unattended", **body}
    pid = client.post("/api/projects", json=payload).json()["id"]
    assert client.post(f"/api/projects/{pid}/run").status_code == 200
    project = through_question_gates(client, pid)
    assert project["status"] == "completed", project.get("last_error")
    return pid


def _zip(client, pid: str, version=None) -> dict[str, str]:
    url = f"/api/projects/{pid}/download" + (f"?version={version}" if version else "")
    r = client.get(url)
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return {n: z.read(n).decode() for n in z.namelist()}


def _code(files: dict[str, str]) -> dict[str, str]:
    return {p: c for p, c in files.items() if p.startswith(("backend/", "frontend/"))}


# ── the change itself ─────────────────────────────────────────────────────────
def test_a_finished_build_has_a_first_version(client, script):
    pid = _finished(client)
    listed = client.get(f"/api/projects/{pid}/versions").json()
    assert [v["number"] for v in listed["versions"]] == [1]
    assert listed["versions"][0]["label"] == "First build" and listed["current"] == 1
    assert client.get(f"/api/projects/{pid}").json()["current_version"]["number"] == 1


def test_a_change_edits_only_what_it_needs_and_keeps_every_other_file(client, script):
    pid = _finished(client)
    before = _zip(client, pid)
    mark = len(script.calls)
    r = client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    assert r.status_code == 202, r.text

    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project.get("last_error")
    assert project["current_version"]["number"] == 2
    # Only the backend edits; QA writes tests for it, Warden rescans, Ledger re-estimates.
    roles = script.roles_since(mark)
    assert {"planner", "backend_engineer", "qa_engineer", "security_engineer", "cost_estimation"} <= roles
    assert not roles & {"product_manager", "system_design", "frontend_engineer", "devops_engineer"}, roles

    after = _zip(client, pid)
    assert after["backend/routes/health.py"] == HEALTH
    for path, code in _code(before).items():
        assert after.get(path) == code, f"{path} changed"
    # v1 is still there, as it was.
    assert _code(_zip(client, pid, version=1)) == _code(before)

    changes = client.get(f"/api/projects/{pid}/changes").json()["changes"]
    assert changes[0]["status"] == "done" and changes[0]["version"] == 2
    assert changes[0]["plan"]["phases"] == ["backend_engineer"]


def test_the_edit_prompt_shows_the_likely_files_whole_and_the_rest_by_path(client, script):
    pid = _finished(client)
    mark = len(script.calls)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    edit_plan = next(
        c for c in script.calls[mark:]
        if c["role"] == "backend_engineer" and c["schema"] and "deleted" in (c["schema"].get("properties") or {})
    )
    prompt = edit_plan["messages"][-1].content
    # Its own previous work is not stripped: the likely file whole, the rest by path.
    assert BACKEND["backend/main.py"] in prompt
    assert "`backend/app/util.py`" in prompt
    assert "add a /health endpoint" in prompt
    assert BACKEND["backend/app/util.py"] not in prompt


def test_a_change_that_wont_compile_parks_the_change_and_the_old_version_still_ships(client, script):
    pid = _finished(client)
    before = _code(_zip(client, pid))
    script.writes["backend/routes/health.py"] = BROKEN
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})

    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "needs_help"
    assert project["change"]["status"] == "needs_help"
    assert project["current_version"]["number"] == 1
    # What ships is the version before the change, not the change half-made.
    assert _code(_zip(client, pid)) == before
    assert client.get(f"/api/projects/{pid}/artifacts").json()["version"] == 1
    live = client.get(f"/api/projects/{pid}/artifacts?live=true").json()
    assert any(f["path"] == "backend/routes/health.py" for f in live["files"])

    # Discarding it puts v1 back: the build is finished again, at v1.
    r = client.post(f"/api/projects/{pid}/changes/{project['change']['id']}/discard", json={"reason": "not now"})
    assert r.status_code == 202, r.text
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and project["change"] is None
    assert project["current_version"]["number"] == 1
    assert _code(_zip(client, pid)) == before
    live = client.get(f"/api/projects/{pid}/artifacts?live=true").json()
    assert not any(f["path"] == "backend/routes/health.py" for f in live["files"])
    assert client.get(f"/api/projects/{pid}/changes").json()["changes"][0]["status"] == "discarded"


def test_the_change_review_shows_a_diff_and_discard_restores_the_previous_attempts(client, script):
    pid = _finished(client)
    client.patch(f"/api/projects/{pid}", json={"approval_mode": "checkpoints"})
    rows_before = {p["phase"]: p["output"] for p in client.get(f"/api/projects/{pid}").json()["phases"] if p["status"] == "approved"}
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})

    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "ship"
    change = project["change"]
    assert change["status"] == "awaiting_approval"
    diff = client.get(f"/api/projects/{pid}/changes/{change['id']}/diff").json()
    assert diff["base"] == 1
    added = [f for f in diff["files"] if f["status"] == "added"]
    assert [f["path"] for f in added] == ["backend/routes/health.py"]
    assert added[0]["lines"][0] == "+def health():"
    assert not any(f["path"] == "backend/main.py" for f in diff["files"])

    client.post(f"/api/projects/{pid}/changes/{change['id']}/discard")
    project = client.get(f"/api/projects/{pid}").json()
    now = {p["phase"]: p["output"] for p in project["phases"] if p["status"] == "approved"}
    assert now == rows_before

    # And the next change starts from v1, not from the discarded one.
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    assert client.get(f"/api/projects/{pid}").json()["gate_kind"] == "ship"
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and project["current_version"]["number"] == 2


def test_restoring_v1_after_v3_makes_v4_with_v1s_files(client, script):
    pid = _finished(client)
    v1 = _code(_zip(client, pid))
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    script.edits["backend_engineer"] = {"summary": "Add /ping", "files": [{"path": "backend/routes/ping.py", "purpose": "ping"}]}
    script.writes["backend/routes/ping.py"] = "def ping():\n    return 'pong'\n"
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /ping endpoint"})
    assert client.get(f"/api/projects/{pid}").json()["current_version"]["number"] == 3

    r = client.post(f"/api/projects/{pid}/versions/1/restore")
    assert r.status_code == 202, r.text
    listed = client.get(f"/api/projects/{pid}/versions").json()
    assert listed["current"] == 4
    assert listed["versions"][0]["label"] == "Restored v1" and listed["versions"][0]["restored_from"] == 1
    assert _code(_zip(client, pid)) == v1
    # A change after the restore works on v1's files.
    script.edits["backend_engineer"] = {"summary": "Add /health", "files": [{"path": "backend/routes/health.py", "purpose": "health"}]}
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    files = _code(_zip(client, pid))
    assert "backend/routes/health.py" in files and "backend/routes/ping.py" not in files


def test_a_schema_change_runs_system_design_first(client, script):
    pid = _finished(client)
    script.plan = {**script.plan, "needs_db_change": True, "summary": "Add a comments table"}
    mark = len(script.calls)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a comments table"})
    roles = [c["role"] for c in script.calls[mark:]]
    assert "system_design" in roles and roles.index("system_design") < roles.index("backend_engineer")
    design = next(c for c in script.calls[mark:] if c["role"] == "system_design")
    assert "What you delivered before" in design["messages"][-1].content
    assert client.get(f"/api/projects/{pid}").json()["current_version"]["number"] == 2


# ── the same checks as a run ──────────────────────────────────────────────────
def test_a_change_checks_readiness_first(client, script, monkeypatch):
    from app.router.router import Readiness, router as model_router

    pid = _finished(client)
    monkeypatch.setattr(model_router, "readiness", lambda *a, **k: Readiness(ok=False, reason="The default can't write."))
    r = client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    assert r.status_code == 409 and "can't write" in r.json()["detail"]
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"
    assert client.get(f"/api/projects/{pid}/changes").json()["changes"] == []


def test_a_change_over_the_cost_cap_stops_at_the_cost_review(client, script):
    pid = _finished(client)
    client.patch(f"/api/projects/{pid}", json={"approval_mode": "checkpoints", "cost_cap_usd": 1})
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "cost" and project["change"]["status"] == "awaiting_approval"


def test_one_change_at_a_time_and_only_on_a_finished_build(client, script):
    pid = _finished(client)
    client.patch(f"/api/projects/{pid}", json={"approval_mode": "checkpoints"})
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    again = client.post(f"/api/projects/{pid}/changes", json={"text": "and dark mode"})
    assert again.status_code == 409
    fresh = client.post("/api/projects", json={"idea": "Another app", "routing_mode": "local_only"}).json()["id"]
    assert client.post(f"/api/projects/{fresh}/changes", json={"text": "x"}).status_code == 409
    blank = client.post(f"/api/projects/{pid}/changes", json={"text": "   "})
    assert blank.status_code in (400, 409, 422)


def test_a_planner_that_fails_changes_nothing(client, script, monkeypatch):
    from app.router.base import ProviderError

    pid = _finished(client)
    real = script.__call__

    def failing(messages, **kwargs):
        if kwargs.get("role") == "planner":
            raise ProviderError("the runtime went away")
        return real(messages, **kwargs)

    stub(monkeypatch, "complete", failing)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and project["current_version"]["number"] == 1
    change = client.get(f"/api/projects/{pid}/changes").json()["changes"][0]
    assert change["status"] == "failed" and "went away" in change["note"]


def test_a_push_records_the_version_it_sent(client, script, fakes):  # noqa: F811
    gh, _ = fakes
    pid = _finished(client)
    _connect_github()
    r = client.post(f"/api/github/push/{pid}", json={"name": "calc"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    r = client.post(f"/api/github/push/{pid}", json={})
    assert r.json()["version"] == 2
    full = r.json()["full_name"]
    assert "backend/routes/health.py" in gh.files(full)
    assert client.get(f"/api/projects/{pid}").json()["github_pushed_version"] == 2
    # Pushing an earlier version by number sends that version.
    r = client.post(f"/api/github/push/{pid}", json={"version": 1})
    assert r.json()["version"] == 1 and "backend/routes/health.py" not in gh.files(full)


# ── what the review found (#79) ───────────────────────────────────────────────
def test_a_diff_keeps_content_lines_that_look_like_headers():
    from app.orchestration import versions

    before = {"files": [{"path": "db.sql", "content": "-- add users table\nCREATE TABLE u (id int);\n", "phase": "backend_engineer"}]}
    after = {"files": [{"path": "db.sql", "content": "CREATE TABLE u (id int);\n++count;\n", "phase": "backend_engineer"}]}
    found = versions.diff(before, after)["files"][0]
    assert "--- add users table" in found["lines"] and "+++count;" in found["lines"]
    assert (found["added"], found["removed"]) == (1, 1)


def test_two_requests_at_once_record_one_first_version(client, script):
    import threading

    from app.db.base import SessionLocal
    from app.db.models import Project, Version
    from app.orchestration import versions

    pid = _finished(client)
    with SessionLocal() as db:
        db.query(Version).filter(Version.project_id == pid).delete()
        db.get(Project, pid).current_version_id = None
        db.commit()

    def first():
        with SessionLocal() as db:
            versions.ensure_first(db, db.get(Project, pid))

    threads = [threading.Thread(target=first) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    listed = client.get(f"/api/projects/{pid}/versions").json()
    assert [v["number"] for v in listed["versions"]] == [1]


def test_a_change_never_rewrites_a_file_it_cant_show_or_whose_reply_was_cut(client, script, monkeypatch):
    pid = _finished(client)
    before = _code(_zip(client, pid))
    script.edits["backend_engineer"] = {"summary": "Touch main", "files": [{"path": "backend/main.py", "purpose": "add a route"}]}
    real = script.__call__

    def cut(messages, **kwargs):
        resp = real(messages, **kwargs)
        if kwargs.get("role") == "backend_engineer" and "# The change to make" in messages[-1].content and not getattr(kwargs.get("options"), "json_schema", None):
            # The reply runs out of room halfway through the file.
            return LLMResponse(text="### backend/main.py\n```python\ndef main(:\n", provider="mock", model="mock-model",
                               usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1, finish_reason="length")
        return resp

    stub(monkeypatch, "complete", cut)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a route to main"})
    project = client.get(f"/api/projects/{pid}").json()
    backend = next(p for p in project["phases"] if p["phase"] == "backend_engineer" and p["status"] == "approved")
    assert backend["handoff"]["change"]["cut_off"] == ["backend/main.py"]
    # The working file stays as it was.
    assert _code(_zip(client, pid))["backend/main.py"] == before["backend/main.py"]


def test_a_failed_deploy_doesnt_relabel_whats_live(client, script):
    from app.db.base import SessionLocal
    from app.db.models import Project

    pid = _finished(client)
    with SessionLocal() as db:
        p = db.get(Project, pid)
        p.deployed_version, p.deploy_status = 1, "error"
        db.commit()
    assert not client.get(f"/api/projects/{pid}/versions").json()["versions"][0]["deployed"]
    with SessionLocal() as db:
        db.get(Project, pid).deploy_status = "ready"
        db.commit()
    assert client.get(f"/api/projects/{pid}/versions").json()["versions"][0]["deployed"]


def test_a_devops_only_change_still_gets_tests_and_a_rescan(client, script):
    pid = _finished(client)
    script.plan = {**script.plan, "phases": ["devops_engineer"], "files_likely": [], "summary": "Tweak the deploy files"}
    mark = len(script.calls)
    client.post(f"/api/projects/{pid}/changes", json={"text": "use node 20 in the deploy files"})
    roles = script.roles_since(mark)
    assert {"qa_engineer", "security_engineer", "devops_engineer", "cost_estimation"} <= roles
    assert client.get(f"/api/projects/{pid}").json()["current_version"]["number"] == 2


def test_after_a_discard_the_build_is_its_version_again(client, script):
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration import versions

    pid = _finished(client)
    client.patch(f"/api/projects/{pid}", json={"approval_mode": "checkpoints"})
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    change = client.get(f"/api/projects/{pid}").json()["change"]
    client.post(f"/api/projects/{pid}/changes/{change['id']}/discard")
    with SessionLocal() as db:
        assert versions.matches_current(db, db.get(Project, pid))
    assert client.get(f"/api/projects/{pid}/artifacts?live=true").json()["version"] == 1


def test_a_change_that_fails_before_its_first_edit_puts_the_fix_loop_back(client, script, monkeypatch):
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.router.base import ProviderError

    pid = _finished(client)
    with SessionLocal() as db:
        p = db.get(Project, pid)
        p.auto_fix = {"tracks": {"security": {"rounds": [{"n": 1}], "allowed": 1, "stopped": None, "accepted": None,
                                               "resumed_after": 0, "episode_start": 0}}}
        db.commit()
    real = script.__call__

    def failing(messages, **kwargs):
        if kwargs.get("role") == "backend_engineer":
            raise ProviderError("the runtime went away")
        return real(messages, **kwargs)

    stub(monkeypatch, "complete", failing)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and project["current_version"]["number"] == 1
    assert project["auto_fix"]["tracks"]["security"]["episode_start"] == 0


def test_the_project_list_carries_versions_and_open_changes(client, script):
    done = _finished(client)
    changing = _finished(client)
    client.patch(f"/api/projects/{changing}", json={"approval_mode": "checkpoints"})
    client.post(f"/api/projects/{changing}/changes", json={"text": "add a /health endpoint"})
    listed = {p["id"]: p for p in client.get("/api/projects").json()}
    assert listed[done]["current_version"]["number"] == 1 and listed[done]["change"] is None
    assert listed[changing]["change"]["status"] == "awaiting_approval"


def test_an_architecture_edit_keeps_every_endpoint():
    from app.agents.base import merge_edit

    base = {"api_endpoints": [{"method": "GET", "path": "/api/todos"}, {"method": "POST", "path": "/api/todos"}]}
    reply = {"api_endpoints": [{"method": "GET", "path": "/api/todos"}, {"method": "POST", "path": "/api/todos"},
                               {"method": "DELETE", "path": "/api/todos"}]}
    merged = merge_edit(base, reply)
    assert [e["method"] for e in merged["api_endpoints"]] == ["GET", "POST", "DELETE"]


def test_restoring_what_the_build_already_holds_is_refused(client, script):
    pid = _finished(client)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    assert client.post(f"/api/projects/{pid}/versions/1/restore").status_code == 202
    again = client.post(f"/api/projects/{pid}/versions/1/restore")
    assert again.status_code == 409 and "already holds" in again.json()["detail"]
    assert [v["number"] for v in client.get(f"/api/projects/{pid}/versions").json()["versions"]] == [3, 2, 1]


def test_a_change_that_cant_be_planned_changes_nothing(client, script, monkeypatch):
    from app.agents.change_planner import ChangePlannerAgent

    pid = _finished(client)

    def broken(self, ctx):
        raise ValueError("the plan came back as nonsense")

    monkeypatch.setattr(ChangePlannerAgent, "run", broken)
    client.post(f"/api/projects/{pid}/changes", json={"text": "add a /health endpoint"})
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and project["change"] is None
    change = client.get(f"/api/projects/{pid}/changes").json()["changes"][0]
    assert change["status"] == "failed" and "nonsense" in change["note"]
