"""Code phases plan their files, then write them in batches sized to the model (#81).

A scripted model stands in for the runtime throughout: it answers the plan call with a
plan it was given, and each write call with exactly the files the call's `# Write now`
list asks for, recording every prompt so the tests can see what was asked, in what
order, and that it fit.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

import pytest

from app.agents import get_agent
from app.agents import code_phase
from app.agents.base import AgentContext
from app.build import check as build_check
from app.core.config import settings
from app.core.reading import code_blocks
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project, UsageEvent
from app.orchestration import activity, claim
from app.router import inflight
from app.router.base import RequestCancelled
from app.router.model_profile import ModelProfile, files_per_call
from app.schemas.agent_outputs import BackendEngineerOutput, FrontendEngineerOutput
from app.schemas.project import ProjectOut
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_database_gate


def _profile(window: int, output: int, model: str = "m") -> ModelProfile:
    return ModelProfile(
        provider="ollama", model=model, context_limit=window, context_window=window, max_output_tokens=output
    )


#: About a 6K-character prompt budget: (4300 - 2048) * 0.9 tokens * 3 characters.
SMALL = _profile(4300, 2048)


@pytest.fixture
def big_budgets(monkeypatch):
    """No bill guard on the prompt, so a large window's budget is the window's."""
    monkeypatch.setattr(settings, "max_prompt_tokens", 0)
    return _profile(64000, 16384)


class Scripted:
    """A model for one code phase: a fixed plan, then each asked-for file's code."""

    def __init__(
        self,
        role: str,
        plan_files: list[dict],
        code: Optional[dict[str, str]] = None,
        reply: Optional[Callable[["Scripted", list[str], int], Optional[str]]] = None,
        finish: Optional[Callable[["Scripted", int], Optional[str]]] = None,
        summary: Optional[str] = None,
    ) -> None:
        self.role = role
        self.plan_files = plan_files
        self.summary = summary
        self.code = code or {}
        self.reply = reply
        self.finish = finish
        self.writes: list[list[str]] = []
        self.prompts: list[str] = []
        self.sizes: list[int] = []
        self.plans = 0

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith(f"You are the {self.role}"):
            return resp
        self.sizes.append(sum(len(m.content) for m in messages))
        self.prompts.append(messages[-1].content)
        if getattr(kwargs.get("options"), "json_schema", None):
            self.plans += 1
            payload = json.loads(resp.text)
            payload["files"] = self.plan_files
            if self.summary is not None:
                payload["summary"] = self.summary
            return resp.model_copy(update={"text": json.dumps(payload)})
        wanted = asked_files(messages)
        self.writes.append(wanted)
        n = len(self.writes)
        text = self.reply(self, wanted, n) if self.reply else None
        if text is None:
            text = fenced({p: self.code.get(p, f"# {p}\nVALUE = 1\n") for p in wanted})
        finish = self.finish(self, n) if self.finish else None
        return resp.model_copy(update={"text": text, "finish_reason": finish})


def _files(n: int, ext: str = "py") -> list[dict]:
    return [{"path": f"app/mod{i}.{ext}", "purpose": f"module {i}"} for i in range(1, n + 1)]


def _run(monkeypatch, phase: str, model: Scripted, profile: ModelProfile, ctx: Optional[AgentContext] = None):
    stub(monkeypatch, "complete", model)
    stub(monkeypatch, "profile_for", lambda *a, **k: profile)
    stub(monkeypatch, "readiness", lambda *a, **k: None)
    return get_agent(phase).run(ctx or AgentContext(idea="A tool library", prior_outputs={}))


# ── how many files per call: from the budgets, never the name ───────────────
def test_files_per_call_is_read_from_the_budgets_and_never_the_name(big_budgets):
    for name in ("qwen2.5:7b", "claude-opus", "m"):
        small = _profile(4300, 2048, model=name)
        assert files_per_call(small, 9) == 1
        big = _profile(64000, 16384, model=name)
        assert files_per_call(big, 40) == 16384 // settings.code_avg_file_tokens
        # The whole plan, when it fits.
        assert files_per_call(big, 5) == 5
    # A big window with a small reply ceiling is still one at a time.
    assert files_per_call(_profile(64000, 4096), 9) == 1
    # The person's choice wins, either way.
    assert files_per_call(SMALL, 9, override=3) == 3
    assert files_per_call(SMALL, 9, override="all") == 9
    assert files_per_call(big_budgets, 9, override=2) == 2
    # And a running average of real files moves it.
    assert files_per_call(big_budgets, 40, avg_file_tokens=4096) == 4


def test_a_small_window_plans_once_and_writes_one_file_per_call(stub_router, monkeypatch):
    model = Scripted("Backend Engineer", _files(3))
    result = _run(monkeypatch, "backend_engineer", model, SMALL)

    assert model.plans == 1
    assert model.writes == [["backend/app/mod1.py"], ["backend/app/mod2.py"], ["backend/app/mod3.py"]]
    # The deliverable has the shape it always had, and validates as it always did.
    BackendEngineerOutput.model_validate(result.output)
    assert [f["path"] for f in result.output["files"]] == [
        "backend/app/mod1.py", "backend/app/mod2.py", "backend/app/mod3.py",
    ]
    assert {f["language"] for f in result.output["files"]} == {"python"}
    assert result.output["files"][0]["purpose"] == "module 1"
    assert result.schema_status == "valid" and result.build_status == "ok"
    gen = result.handoff["generation"]
    assert gen["mode"] == "one" and gen["files_planned"] == gen["files_written"] == 3
    assert gen["calls"] == 4 and len(result.calls) == 4
    # Every prompt — plan and writes — inside the window's budget.
    assert max(model.sizes) <= SMALL.prompt_char_budget


def test_a_large_window_writes_several_files_per_call_and_a_small_plan_in_one(
    stub_router, monkeypatch, big_budgets
):
    model = Scripted("Backend Engineer", _files(20))
    result = _run(monkeypatch, "backend_engineer", model, big_budgets)
    assert len(model.writes[0]) > 1
    assert sum(len(w) for w in model.writes) == 20 and len(model.writes) < 20
    assert result.handoff["generation"]["mode"] == "batch"

    model = Scripted("Backend Engineer", _files(5))
    result = _run(monkeypatch, "backend_engineer", model, big_budgets)
    assert model.writes == [[f"backend/app/mod{i}.py" for i in range(1, 6)]]
    assert result.handoff["generation"]["files_written"] == 5


def test_the_files_per_call_a_person_chose_for_the_role_is_used(client, monkeypatch):
    r = client.put("/api/settings/roles/backend_engineer/files-per-call", json={"value": 2})
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["roles"] if x["role"] == "backend_engineer")
    assert row["files_per_call"] == 2 and row["files_per_call_auto"] == 1
    assert "files_per_call" not in next(x for x in r.json()["roles"] if x["role"] == "qa_engineer")
    try:
        model = Scripted("Backend Engineer", _files(3))
        _run(monkeypatch, "backend_engineer", model, SMALL)
        assert [len(w) for w in model.writes] == [2, 1]
    finally:
        client.put("/api/settings/roles/backend_engineer/files-per-call", json={"value": None})
    for bad in (0, -1, 51, "lots", True):
        r = client.put("/api/settings/roles/backend_engineer/files-per-call", json={"value": bad})
        assert r.status_code in (400, 422), (bad, r.status_code)
    assert client.put("/api/settings/roles/qa_engineer/files-per-call", json={"value": 2}).status_code == 400
    r = client.put("/api/settings/roles/frontend_engineer/files-per-call", json={"value": "all"})
    assert next(x for x in r.json()["roles"] if x["role"] == "frontend_engineer")["files_per_call"] == "all"
    client.put("/api/settings/roles/frontend_engineer/files-per-call", json={"value": None})


# ── fenced code, not escaped JSON ────────────────────────────────────────────
TRICKY = (
    'const greet = (name) => `Hello, ${name}! "quoted" and \\\\backslash\\\\`;\n'
    "const re = /\\d+\\.\\d+/;\n"
    "export const md = '```not a fence end';\n"
    "export default greet;\n"
)


def test_quotes_backslashes_and_template_literals_round_trip_byte_for_byte(stub_router, monkeypatch):
    blocks = code_blocks(f"### frontend/lib/greet.ts\n````ts\n{TRICKY}````\n")
    assert len(blocks) == 1 and blocks[0].code == TRICKY and blocks[0].complete

    model = Scripted(
        "Frontend Engineer",
        [{"path": "lib/greet.ts", "purpose": "greeting"}],
        reply=lambda m, wanted, n: f"Here it is.\n\n### {wanted[0]}\n````typescript\n{TRICKY}````\n",
    )
    result = _run(monkeypatch, "frontend_engineer", model, SMALL)
    FrontendEngineerOutput.model_validate(result.output)
    assert result.output["files"][0]["code"] == TRICKY
    assert result.output["files"][0]["language"] == "typescript"


def test_the_reader_takes_headings_bold_names_info_strings_and_cut_blocks():
    text = (
        "### File: `backend/a.py`\n```python\nA = 1\n```\n"
        "**backend/b.py**\n```\nB = 2\n```\n"
        "```py backend/c.py\nC = 3\n```\n"
        "Some prose mentioning utils/x.py in passing.\n```\nD = 4\n```\n"
        "### backend/e.py\n```\nE = 5\n"
    )
    blocks = code_blocks(text)
    assert [(b.path, b.code, b.complete) for b in blocks] == [
        ("backend/a.py", "A = 1\n", True),
        ("backend/b.py", "B = 2\n", True),
        ("backend/c.py", "C = 3\n", True),
        ("", "D = 4\n", True),
        ("backend/e.py", "E = 5\n", False),
    ]


def test_a_model_that_answers_in_json_anyway_still_lands_its_files(stub_router, monkeypatch):
    model = Scripted(
        "Backend Engineer",
        _files(1),
        reply=lambda m, wanted, n: json.dumps({"files": [{"path": wanted[0], "code": "X = 1\n"}]}),
    )
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert result.output["files"][0]["code"] == "X = 1\n"


# ── a reply cut off at the output limit ──────────────────────────────────────
def test_a_cut_off_batch_is_halved_and_never_resent(stub_router, monkeypatch, big_budgets):
    def reply(m, wanted, n):
        if n == 1:  # two files land whole, the third is cut mid-file
            return fenced({wanted[0]: "A = 1\n", wanted[1]: "B = 2\n"}) + f"### {wanted[2]}\n```\nC = (\n"
        return None

    model = Scripted(
        "Backend Engineer", _files(4), reply=reply, finish=lambda m, n: "length" if n == 1 else None
    )
    monkeypatch.setattr(code_phase, "MODE_OVERRIDE", None)
    stub(monkeypatch, "files_per_call_choice", lambda *a, **k: 4)
    result = _run(monkeypatch, "backend_engineer", model, big_budgets)
    first, second = model.writes[0], model.writes[1]
    assert len(first) == 4 and len(second) == 2 and second != first
    assert second == ["backend/app/mod3.py", "backend/app/mod4.py"]
    assert result.truncated_replies == 1 and result.handoff["generation"]["files_written"] == 4


def test_one_file_cut_off_is_asked_for_once_as_two_modules(stub_router, monkeypatch):
    def reply(m, wanted, n):
        if n == 1:
            return f"### {wanted[0]}\n```\nBIG = [\n"
        if n == 2:
            return fenced({wanted[0]: "from app.mod1_rows import ROWS\nBIG = ROWS\n", "backend/app/mod1_rows.py": "ROWS = []\n"})
        return None

    model = Scripted("Backend Engineer", _files(1), reply=reply, finish=lambda m, n: "length" if n == 1 else None)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert "cut off" in model.prompts[2] and "two files" in model.prompts[2]
    paths = [f["path"] for f in result.output["files"]]
    assert paths == ["backend/app/mod1.py", "backend/app/mod1_rows.py"]
    plan = {p["path"]: p["origin"] for p in result.handoff["generation"]["plan"]}
    assert plan["backend/app/mod1_rows.py"] == "split"
    # A split is not an unplanned file: no note for it.
    assert not [p for p in result.build_problems if p["kind"] == "plan"]


# ── checks batch, they do not multiply ───────────────────────────────────────
def test_a_planned_file_imported_before_it_is_written_is_not_a_problem(stub_router, monkeypatch):
    code = {
        "backend/main.py": "from models import Item\n\nITEMS = [Item('a')]\n",
        "backend/models.py": "class Item:\n    def __init__(self, name):\n        self.name = name\n",
    }
    model = Scripted(
        "Backend Engineer",
        [{"path": "main.py", "purpose": "entry"}, {"path": "models.py", "purpose": "models"}],
        code=code,
    )
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert model.writes == [["backend/main.py"], ["backend/models.py"]]  # no repair call between
    assert result.build_status == "ok" and result.build_problems == []


def test_a_file_that_does_not_parse_is_fixed_before_the_next_batch(stub_router, monkeypatch):
    def reply(m, wanted, n):
        if n == 1:
            return fenced({wanted[0]: "def broken(:\n"})
        return None

    model = Scripted("Backend Engineer", _files(2), reply=reply)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert model.writes == [["backend/app/mod1.py"], ["backend/app/mod1.py"], ["backend/app/mod2.py"]]
    assert "does not parse" in model.prompts[2] and "def broken(:" in model.prompts[2]
    # The repaired file keeps the extension's name for its language, whatever the fence said.
    assert result.output["files"][0]["language"] == "python"
    assert result.build_status == "ok" and result.schema_status == "valid"
    assert result.handoff["generation"]["repairs"] == 1 and result.repair_rounds == 1



def test_a_file_outside_the_plan_is_kept_and_noted_but_never_gated(stub_router, monkeypatch):
    model = Scripted(
        "Backend Engineer",
        _files(1),
        reply=lambda m, wanted, n: fenced({wanted[0]: "A = 1\n", "backend/extra.py": "B = 2\n"}),
    )
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert [f["path"] for f in result.output["files"]] == ["backend/app/mod1.py", "backend/extra.py"]
    assert result.build_status == "ok"
    assert result.build_problems == [
        {"path": "backend/extra.py", "line": None, "kind": "plan", "message": "was not in the plan; kept"}
    ]


def test_a_phase_spawns_one_javascript_check_however_many_files(stub_router, monkeypatch):
    runs = []
    real = build_check._run_js

    def counting(groups):
        runs.append(groups)
        return real(groups)

    monkeypatch.setattr(build_check, "_run_js", counting)
    code = {f"frontend/app/mod{i}.tsx": f"export const VALUE{i} = {i};\n" for i in range(1, 5)}
    model = Scripted("Frontend Engineer", _files(4, "tsx"), code=code)
    result = _run(monkeypatch, "frontend_engineer", model, SMALL)
    assert len(model.writes) == 4 and len(runs) == 1
    assert result.build_status in ("ok", "unchecked")


# ── accounted for, and interruptible ─────────────────────────────────────────
def _held(pid: str, project: Project, token: Optional[str]):
    """Run as the build's driver: its claim held, its calls named for the build."""
    from contextlib import ExitStack

    stack = ExitStack()
    db = stack.enter_context(SessionLocal())
    stack.enter_context(claim.holding(db, db.get(Project, pid), token))
    stack.enter_context(inflight.building(pid, "Tool library"))
    return stack


def _project(client) -> str:
    r = client.post("/api/projects", json={"idea": "A tool library", "routing_mode": "local_only"})
    return r.json()["id"]


def test_stop_between_two_files_stops_before_the_next_call(client, monkeypatch):
    pid = _project(client)

    def reply(m, wanted, n):
        if n == 2:  # Stop lands while file 2 is being written
            with SessionLocal() as db:
                db.query(Project).filter(Project.id == pid).update({"cancel_requested": True})
                db.commit()
        return None

    model = Scripted("Backend Engineer", _files(3), reply=reply)
    with SessionLocal() as db:
        token = db.get(Project, pid).run_token
    with _held(pid, None, token):
        with pytest.raises(RequestCancelled):
            _run(monkeypatch, "backend_engineer", model, SMALL)
    assert model.writes == [["backend/app/mod1.py"], ["backend/app/mod2.py"]]
    assert activity.get(pid) is None


def test_a_second_driver_is_noticed_between_files_and_the_first_stops(client, monkeypatch):
    pid = _project(client)

    def reply(m, wanted, n):
        if n == 1:  # a resume claims the build while file 1 is being written
            with SessionLocal() as db:
                db.query(Project).filter(Project.id == pid).update({"run_token": claim.new_token()})
                db.commit()
        return None

    model = Scripted("Backend Engineer", _files(3), reply=reply)
    with SessionLocal() as db:
        token = db.get(Project, pid).run_token
    with _held(pid, None, token):
        with pytest.raises(claim.Superseded):
            _run(monkeypatch, "backend_engineer", model, SMALL)
    assert model.writes == [["backend/app/mod1.py"]]


def _build_until_backend(client, monkeypatch, model: Scripted) -> str:
    stub(monkeypatch, "complete", model)
    stub(monkeypatch, "profile_for", lambda *a, **k: SMALL)
    r = client.post(
        "/api/projects",
        json={"idea": "A tool library", "routing_mode": "local_only", "approval_mode": "unattended"},
    )
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    return pid


def test_a_stopped_build_keeps_nothing_of_the_phase_it_was_writing(client, monkeypatch):
    from app.orchestration.runner import PipelineRunner

    def reply(m, wanted, n):
        if n == 2:
            with SessionLocal() as db:
                found = db.query(Project).filter(Project.idea == "A tool library").order_by(Project.created_at.desc()).first()
                PipelineRunner().stop(db, found, "Stopped by you.")
        return None

    model = Scripted("Backend Engineer", _files(3), reply=reply)
    pid = _build_until_backend(client, monkeypatch, model)
    assert model.writes == [["backend/app/mod1.py"], ["backend/app/mod2.py"]]
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "cancelled"
    with SessionLocal() as db:
        rows = db.query(PhaseResult).filter(PhaseResult.project_id == pid, PhaseResult.phase == "backend_engineer").all()
        usage = db.query(UsageEvent).filter(UsageEvent.project_id == pid, UsageEvent.phase == "backend_engineer").count()
    assert all(r.status == "failed" and not r.output for r in rows)
    assert usage == 0


def test_every_call_is_one_usage_event_and_the_phase_total_is_their_sum(client, monkeypatch):
    model = Scripted("Backend Engineer", _files(3))
    pid = _build_until_backend(client, monkeypatch, model)
    with SessionLocal() as db:
        row = (
            db.query(PhaseResult)
            .filter(PhaseResult.project_id == pid, PhaseResult.phase == "backend_engineer")
            .one()
        )
        events = db.query(UsageEvent).filter(UsageEvent.project_id == pid, UsageEvent.phase == "backend_engineer").all()
    assert len(events) == 4 == 1 + len(model.writes)
    assert row.total_tokens == sum(e.total_tokens for e in events) == 4 * 46
    assert row.handoff["generation"]["calls"] == 4
    assert [f["path"] for f in row.output["files"]] == [f"backend/app/mod{i}.py" for i in range(1, 4)]


def test_the_build_says_which_file_is_being_written_while_it_is(client, monkeypatch):
    seen: list[tuple] = []

    def reply(m, wanted, n):
        with SessionLocal() as db:
            found = db.query(Project).filter(Project.idea == "A tool library").order_by(Project.created_at.desc()).first()
            out = ProjectOut.model_validate(found).activity
        seen.append((out, inflight.current_agent()))
        return None

    model = Scripted("Backend Engineer", _files(3), reply=reply, summary="Three   modules:\n the loans,\tthe tools, the people.")
    pid = _build_until_backend(client, monkeypatch, model)
    out, label = seen[1]
    assert out["phase"] == "backend_engineer" and out["stage"] == "writing"
    assert out["detail"] == "backend/app/mod2.py" and out["total"] == 3 and out["done"] == 1
    assert [f["state"] for f in out["files"]] == ["ok", "writing", "planned"]
    assert label == "Backend Engineer — writing backend/app/mod2.py (2 of 3)"
    # The feed (#86): planning is a finished step, file after file is still one step,
    # and the plan's summary is there to show under it.
    assert out["trail"] == [{"stage": "planning", "detail": "", "done": 0, "total": 3}]
    assert seen[2][0]["trail"] == out["trail"]
    assert out["note"] == "Three modules: the loans, the tools, the people."
    # And nothing once the phase is over.
    assert client.get(f"/api/projects/{pid}").json()["activity"] is None


def test_each_command_is_a_step_and_writing_and_fixing_are_one():
    with inflight.building("p-trail"):
        activity.begin("frontend_engineer")
        activity.stage("planning")
        activity.plan(["a.tsx", "b.tsx"], 1, note="x " * 400)
        activity.stage("writing", detail="a.tsx", total=2)
        activity.stage("fixing", detail="a.tsx", total=2)
        activity.stage("writing", detail="b.tsx", total=2)
        activity.file("a.tsx", "ok")
        activity.file("b.tsx", "ok")
        activity.stage("checking", total=2)
        activity.stage("building", detail="npm install")
        activity.stage("building", detail="npm install")  # the same command again is not a new step
        activity.stage("building", detail="next build")
        out = activity.get("p-trail")
        activity.end()
    assert [(t["stage"], t["detail"]) for t in out["trail"]] == [
        ("planning", ""), ("writing", ""), ("checking", ""), ("building", "npm install"),
    ]
    assert out["trail"][1]["done"] == 2 and out["trail"][1]["total"] == 2
    assert (out["stage"], out["detail"]) == ("building", "next build")
    assert len(out["note"]) <= activity.NOTE_MAX and out["note"].endswith("…")
    assert activity.get("p-trail") is None


def test_a_phase_that_never_plans_does_not_report_a_planning_step():
    with inflight.building("p-qa"):
        activity.begin("qa_engineer")
        activity.stage("testing", detail="npm install")
        activity.stage("testing", detail="jest")
        out = activity.get("p-qa")
        activity.end()
    assert [(t["stage"], t["detail"]) for t in out["trail"]] == [("testing", "npm install")]
    assert out["note"] == ""


def test_the_trail_keeps_only_the_latest_steps():
    with inflight.building("p-long"):
        activity.begin("backend_engineer")
        for i in range(activity.TRAIL_MAX + 5):
            activity.stage("building", detail=f"step {i}")
        out = activity.get("p-long")
        activity.end()
    assert len(out["trail"]) == activity.TRAIL_MAX
    assert out["trail"][-1]["detail"] == f"step {activity.TRAIL_MAX + 3}"


# ── the old path, when there is no plan to write from ────────────────────────
def test_a_plan_with_no_files_falls_back_to_one_reply_and_says_so(stub_router, monkeypatch):
    model = Scripted("Backend Engineer", [])
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    gen = result.handoff["generation"]
    assert gen["mode"] == "whole" and "no files" in gen["reason"]
    # The plan's calls (and its repair round) are counted with the reply's.
    assert model.plans == 3 and not model.writes
    assert gen["calls"] == len(result.calls) == 3
    BackendEngineerOutput.model_validate(result.output)


def test_the_eval_harness_can_ask_for_each_generation_mode(stub_router, monkeypatch, big_budgets):
    monkeypatch.setattr(code_phase, "MODE_OVERRIDE", "whole")
    result = _run(monkeypatch, "backend_engineer", Scripted("Backend Engineer", _files(5)), big_budgets)
    assert result.handoff["generation"]["mode"] == "whole"
    monkeypatch.setattr(code_phase, "MODE_OVERRIDE", "one")
    model = Scripted("Backend Engineer", _files(5))
    result = _run(monkeypatch, "backend_engineer", model, big_budgets)
    assert result.handoff["generation"]["mode"] == "one" and len(model.writes) == 5
    monkeypatch.setattr(code_phase, "MODE_OVERRIDE", "batch")
    model = Scripted("Backend Engineer", _files(5))
    _run(monkeypatch, "backend_engineer", model, big_budgets)
    assert len(model.writes) == 1


# ── every prompt fits, however much context there is ─────────────────────────
@pytest.mark.parametrize("window", [32768, 16384, 8192, 4096])
@pytest.mark.parametrize("phase", ["backend_engineer", "frontend_engineer"])
def test_no_plan_or_write_prompt_overruns_its_window(stub_router, monkeypatch, window, phase):
    profile = _profile(window, min(4096, window // 2))
    agent = get_agent(phase)
    role = agent.title
    huge = {"files": [{"path": f"x{i}.py", "code": "x" * 40_000} for i in range(10)], "summary": "s" * 9000}
    ctx = AgentContext(
        idea="A team standup bot " * 400,
        prior_outputs={d: dict(huge) for d in agent.depends_on},
        rag_context="r" * 300_000,
        memory_context="m" * 300_000,
        extra_context="Debate decision: use Postgres.",
        feedback="Fix the import in main.py. " * 400,
    )

    def reply(m, wanted, n):
        if n == 1:
            return fenced({wanted[0]: "def broken(:\n" + "y = 1\n" * 3000})  # a long echo to fit
        return None

    model = Scripted(role, [{"path": f"app/m{i}.py", "purpose": "p " * 40, "exports": ["a"] * 20} for i in range(8)], reply=reply)
    _run(monkeypatch, phase, model, profile, ctx)
    assert len(model.writes) >= 8
    assert max(model.sizes) <= profile.prompt_char_budget, (max(model.sizes), profile.prompt_char_budget)


# ── evals ───────────────────────────────────────────────────────────────────
def test_the_scorecard_reports_how_the_code_was_written(client, monkeypatch):
    from app.evals.scorecard import score
    from scripts.eval_harness import compare

    model = Scripted("Backend Engineer", _files(3))
    pid = _build_until_backend(client, monkeypatch, model)
    with SessionLocal() as db:
        card = score(db, db.get(Project, pid))
    assert card["generation_mode"]["backend_engineer"] == "one"
    assert card["calls_per_phase"]["backend_engineer"] == 4
    assert card["files_planned"] >= 3 and card["files_written"] >= 3
    assert card["compile_by_path"]["backend/app/mod1.py"] == "ok"

    lines = compare([{**card, "generation_run": "one"}, {**card, "generation_run": "whole"}])
    assert lines[0].startswith("one") and lines[1].startswith("whole") and "files" in lines[0]


def test_a_prompt_variant_can_set_a_code_phase_plan_or_write_task(tmp_path):
    from scripts.eval_harness import load_variant

    path = tmp_path / "v.json"
    path.write_text(json.dumps({"name": "v", "tasks": {"backend_engineer.plan": "Plan it.", "frontend_engineer.write": "Write it."}}))
    assert load_variant(str(path))[1] == {"backend_engineer.plan": "Plan it.", "frontend_engineer.write": "Write it."}
    path.write_text(json.dumps({"tasks": {"nobody.plan": "x"}}))
    with pytest.raises(SystemExit):
        load_variant(str(path))


# ── review fixes ────────────────────────────────────────────────────────────
def test_a_heading_that_says_more_than_the_path_still_names_the_path():
    text = (
        "### backend/app/main.py — FastAPI entry point\n```python\nA = 1\n```\n"
        "### `frontend/app/page.tsx` (updated)\n```tsx\nexport default 1;\n```\n"
        "### 2) backend/x.py: models\n```\nX = 1\n```\n"
    )
    assert [b.path for b in code_blocks(text)] == ["backend/app/main.py", "frontend/app/page.tsx", "backend/x.py"]


def test_a_nameless_block_only_lands_on_a_file_its_language_fits(stub_router, monkeypatch):
    reply = "Install first:\n```bash\npip install fastapi\n```\nThen:\n```python\nAPP = 1\n```\n"
    model = Scripted("Backend Engineer", _files(1), reply=lambda m, wanted, n: reply)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert result.output["files"][0]["code"] == "APP = 1\n"


def test_a_json_answer_fenced_as_json_is_read_for_its_files(stub_router, monkeypatch):
    def reply(m, wanted, n):
        return "```json\n" + json.dumps({"path": wanted[0], "code": "MODELS = []\n", "ok": True}) + "\n```\n"

    model = Scripted("Backend Engineer", _files(1), reply=reply)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert result.output["files"][0]["code"] == "MODELS = []\n"


def test_write_calls_carry_the_knowledge_base_memory_and_team_decision(stub_router, monkeypatch, big_budgets):
    model = Scripted("Backend Engineer", _files(1))
    ctx = AgentContext(
        idea="A tool library",
        prior_outputs={},
        rag_context="RAG: the lending API is documented here.",
        memory_context="MEMORY: last time the seed data was missing.",
        extra_context="DECISION: use PostgreSQL.",
    )
    _run(monkeypatch, "backend_engineer", model, big_budgets, ctx)
    write = model.prompts[1]
    assert "RAG: the lending API" in write and "MEMORY: last time" in write and "DECISION: use PostgreSQL" in write


def test_a_planned_file_never_written_fails_the_build(stub_router, monkeypatch):
    model = Scripted("Backend Engineer", _files(2), reply=lambda m, wanted, n: "" if "mod2" in wanted[0] else None)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert model.writes == [["backend/app/mod1.py"], ["backend/app/mod2.py"], ["backend/app/mod2.py"]]
    assert result.build_status == "failed"
    assert {"path": "backend/app/mod2.py", "kind": "missing"}.items() <= result.build_problems[-1].items()
    assert result.handoff["generation"]["unwritten"] == ["backend/app/mod2.py"]


def test_a_file_cut_off_twice_is_not_asked_for_again_whole(stub_router, monkeypatch):
    model = Scripted(
        "Backend Engineer",
        _files(1),
        reply=lambda m, wanted, n: f"### {wanted[0]}\n```\nBIG = [\n",
        finish=lambda m, n: "length",
    )
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    # The write, then the one ask to split it — no repair call for a file that can
    # only be cut off again.
    assert len(model.writes) == 2 and "two files" in model.prompts[2]
    assert result.build_status == "failed" and result.output["files"][0]["code"] == "BIG = [\n"


def test_prose_that_mentions_a_name_in_backticks_is_not_a_file():
    text = (
        "### backend/app/mod1.py\nReads config with `os.getenv`:\n```python\nX = 1\n```\n"
        "### backend/app/todos.py — uses `schemas.py` models\n```python\nY = 1\n```\n"
        "### app/(auth)/login/page.tsx\n```tsx\nexport default 1;\n```\n"
    )
    assert [b.path for b in code_blocks(text)] == [
        "backend/app/mod1.py", "backend/app/todos.py", "app/(auth)/login/page.tsx",
    ]


def test_a_file_that_arrives_after_it_was_given_up_on_counts_as_written(stub_router, monkeypatch):
    def reply(m, wanted, n):
        if wanted == ["backend/app/mod2.py"]:
            return ""  # left out twice
        if wanted == ["backend/app/mod3.py"]:  # then sent, unasked, beside the next file
            return fenced({"backend/app/mod3.py": "C = 3\n", "backend/app/mod2.py": "B = 2\n"})
        return None

    model = Scripted("Backend Engineer", _files(3), reply=reply)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    assert "backend/app/mod2.py" in [f["path"] for f in result.output["files"]]
    assert result.build_status == "ok" and result.handoff["generation"]["unwritten"] == []
