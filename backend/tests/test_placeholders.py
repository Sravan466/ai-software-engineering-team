"""#101: code that parses and isn't finished is a problem the fix loop sees.

Stubs, comments standing in for code, TODOs, lorem ipsum, stand-in values, empty
files, refusals, and tests that test nothing — each named with its file and line,
routed to the phase that wrote it like a missing import. And the shapes that look
like stubs and aren't (an abstract method, a Protocol, an `__init__.py`, a divider
that renders `<hr />`) are left alone.
"""
from __future__ import annotations

import json

import pytest

from app.build import placeholders as ph
from app.build.check import check_phase, file_problems
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Project
from app.evals.scorecard import score
from app.orchestration import autofix
from app.orchestration.approval import build_note
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_question_gates
from tests.test_changes import _code, _finished, _zip, script  # noqa: F401 - the fixture is used by name


def _found(path: str, code: str, cap=ph._PER_FILE) -> list[tuple[str, int, str]]:
    return [(p.kind, p.line, p.message) for p in ph.file_placeholders(path, code, cap)]


def _kinds(path: str, code: str) -> list[str]:
    return [p.kind for p in ph.file_placeholders(path, code)]


# ── Python ───────────────────────────────────────────────────────────────────
def test_a_python_function_with_no_body_is_a_placeholder():
    code = (
        "def create_user(name):\n    pass\n\n"
        "def get_user(uid):\n    \"\"\"Return a user.\"\"\"\n    ...\n\n"
        "def charge(amount):\n    raise NotImplementedError('later')\n"
    )
    found = _found("backend/app/services.py", code)
    assert [(k, line) for k, line, _ in found] == [("placeholder", 1), ("placeholder", 4), ("placeholder", 8)]
    assert "`create_user` has no body yet (only `pass`)" in found[0][2]
    assert "only `...`" in found[1][2] and "only `raise NotImplementedError`" in found[2][2]


def test_shapes_that_are_meant_to_have_no_body_are_left_alone():
    code = (
        "from abc import ABC, abstractmethod\nfrom typing import Protocol, overload\n\n"
        "class Repo(ABC):\n    @abstractmethod\n    def save(self): ...\n\n"
        "class Store(Protocol):\n    def get(self, key): ...\n\n"
        "class NotFound(Exception):\n    pass\n\n"
        "class Base:\n    def hook(self):\n        pass\n\n"
        "class Child(Base):\n    def hook(self):\n        return 1\n\n"
        "class Thing:\n    def __exit__(self, *a):\n        pass\n\n"
        "@overload\ndef f(x: int) -> int: ...\n"
        "def f(x):\n    return x\n"
    )
    assert _found("backend/app/models.py", code) == []
    assert _found("backend/app/__init__.py", "") == []
    assert _found("backend/app/__init__.py", "\"\"\"Package.\"\"\"\n") == []


def test_comments_that_stand_in_for_code_or_leave_work_undone():
    code = (
        "def a():\n    # ... existing code ...\n    return 1\n\n"
        "def b():\n    # rest of the function stays the same\n    return 2\n\n"
        "def c():\n    # TODO: send the reminder email\n    return 3\n\n"
        "def d():\n    # implement this later\n    return 4\n"
    )
    lines = [line for _, line, _ in _found("backend/app/x.py", code)]
    assert lines == [2, 6, 10]  # three a file; the fourth is the same news
    ph_all = ph.placeholder_problems({"backend/app/x.py": code}, ["backend/app/x.py"])
    assert all(p.kind == "placeholder" for p in ph_all)


def test_comments_that_only_look_lazy_are_not_flagged():
    code = (
        "def todos():\n    # The TODO list, newest first\n    # Fetch the user first,\n"
        "    # ...then their todos\n    # Loading... shows while it waits\n    return []\n"
    )
    assert _found("backend/app/todos.py", code) == []


def test_lorem_ipsum_and_stand_in_values_are_placeholders():
    code = (
        "TAGLINE = 'Lorem ipsum dolor sit amet'\n"
        "KEY = os.getenv('OPENAI_API_KEY', 'your-api-key')\n"
        "STRIPE = os.getenv('STRIPE_KEY', \"sk_test_xxx\")\n"
        "# e.g. KEY = 'your-api-key' in .env\n"
    )
    found = _found("backend/app/config.py", code)
    assert [line for _, line, _ in found] == [1, 2, 3]
    assert "lorem ipsum" in found[0][2] and "stand-in value ('your-api-key')" in found[1][2]
    # An input's format hint and a test's expected mask are values, not placeholders.
    assert _found("frontend/src/Phone.jsx", 'export function Phone() {\n  return <input placeholder="xxx-xxx-xxxx" />;\n}\n') == []
    assert _found("frontend/src/__tests__/mask.test.js", "test('m', () => { expect(mask('1')).toBe('xxx'); });\n") == []


def test_an_empty_file_or_a_refusal_is_named_instead_of_its_parse_error():
    assert _found("backend/app/empty.py", "  \n") == [("placeholder", None, "is empty. Write the whole file.")]
    refusal = "I'm sorry, but I can't write that file.\n"
    [problem] = file_problems("frontend/app/page.tsx", refusal)
    assert problem.kind == "placeholder" and problem.message.startswith("starts with “I'm sorry")
    [problem] = file_problems("backend/main.py", "Here is the file you asked for:\n\nimport os\n")
    assert problem.kind == "placeholder"
    # A README may start however it likes.
    assert file_problems("README.md", "Here is how to run it.\n") == []
    # Code that doesn't parse, and is not a refusal, is the parser's to name.
    [problem] = file_problems("backend/main.py", "def main(:\n    pass\n")
    assert problem.kind == "syntax"


def test_a_module_with_no_code_is_a_placeholder():
    assert _kinds("backend/app/models.py", '"""Models."""\n# to come\n') == ["placeholder"]
    assert _kinds("frontend/src/api.js", "// API client goes here\n") == ["placeholder"]


# ── JavaScript and TypeScript ────────────────────────────────────────────────
def test_an_exported_function_or_component_with_an_empty_body_is_a_placeholder():
    code = (
        "import React from 'react';\n"
        "// Don't render when empty\n"
        "export function Card({ title }: { title: string }) {}\n"
        "export const List = (props: Props) => {\n  return <ul>{props.items.map(i => <li key={i}>{i}</li>)}</ul>;\n};\n"
        "export async function GET() { throw new Error('Not implemented'); }\n"
        "function Empty() { return null; }\n"
        "export { Empty };\n"
        "export const noop = () => {};\n"
        "const re = /[{]/;\n"
        "export function ok() { const s = \"}\"; return `${s}{`; }\n"
    )
    found = _found("frontend/src/components/Card.tsx", code)
    assert [line for _, line, _ in found] == [3, 7, 8]
    assert "`Card` has an empty body" in found[0][2]
    assert "`GET` only throws “not implemented”" in found[1][2]
    assert "`Empty` renders nothing" in found[2][2]


def test_components_that_render_something_small_are_not_stubs():
    code = (
        "export function Divider() { return <hr />; }\n"
        "export default function Page() { return <StandupForm />; }\n"
        "export const Spacer = () => <div className=\"h-4\" />;\n"
    )
    assert _found("frontend/src/ui.tsx", code) == []


def test_placeholder_text_in_jsx_is_a_placeholder_but_a_to_do_apps_copy_is_not():
    code = "export default function Page() {\n  return <div><p>Content goes here</p></div>;\n}\n"
    [(kind, line, message)] = _found("frontend/app/page.tsx", code)
    assert (kind, line) == ("placeholder", 2) and "Content goes here" in message
    todo_app = "export default function TodoApp() {\n  // TODO list state\n  return <main><h1>Todo</h1><Todos /></main>;\n}\n"
    assert _found("frontend/app/page.tsx", todo_app) == []


# ── tests that test nothing ──────────────────────────────────────────────────
def test_python_tests_that_assert_nothing_or_nothing_real_are_weak():
    code = (
        "import pytest\nfrom unittest.mock import patch\n\n"
        "def check_ok(r):\n    assert r.status_code == 200\n\n"
        "@pytest.fixture\ndef test_user():\n    return {'id': 1}\n\n"
        "def test_create(client):\n    client.post('/x')\n\n"
        "def test_list(client):\n    check_ok(client.get('/x'))\n\n"
        "def test_raises():\n    with pytest.raises(ValueError):\n        int('x')\n\n"
        "def test_true():\n    assert True\n\n"
        "@pytest.mark.skip(reason='later')\ndef test_later():\n    assert 1 == 2\n\n"
        "class TestThing:\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
    )
    found = _found("backend/tests/test_api.py", code, cap=None)
    assert [(k, line) for k, line, _ in found] == [
        ("weak_test", 11), ("weak_test", 21), ("weak_test", 25), ("weak_test", 29),
    ]
    assert "test `test_create` asserts nothing" in found[0][2]
    assert "test `test_true` only asserts something that is always true" in found[1][2]
    assert "test `test_later` is skipped" in found[2][2]


def test_a_test_that_mocks_the_module_it_tests_is_weak():
    py = "from unittest.mock import patch\n\n@patch('app.api')\ndef test_get(m):\n    assert m.called\n"
    [(kind, _, message)] = _found("backend/tests/test_api.py", py)
    assert kind == "weak_test" and "mocks `app.api`, the module it tests" in message
    # Mocking what it depends on is how a unit is tested.
    ok = "from unittest.mock import patch\n\n@patch('app.api.requests')\ndef test_get(m):\n    assert m.called\n"
    assert _found("backend/tests/test_api.py", ok) == []


def test_javascript_tests_that_assert_nothing_or_nothing_real_are_weak():
    code = (
        "import { getTodos } from '../api';\n"
        "vi.mock('../api');\n"
        "it('loads todos', async () => {\n  await getTodos();\n});\n"
        "test('works', () => { expect(true).toBe(true); });\n"
        "test('real', () => { expect(getTodos()).toEqual([]); });\n"
        "it.todo('handles errors');\n"
    )
    found = _found("frontend/src/__tests__/api.test.ts", code, cap=None)
    assert [line for _, line, _ in found] == [2, 3, 6, 8]
    assert "mocks `../api`, the module it tests" in found[0][2]
    assert "test “loads todos” asserts nothing" in found[1][2]
    assert "test “works” only asserts something that is always true" in found[2][2]
    assert "test “handles errors” is only a todo" in found[3][2]


def test_javascript_checks_through_helpers_supertest_and_partial_mocks_count():
    code = (
        "import request from 'supertest';\nimport app from '../app';\n"
        "vi.mock('../app', async () => ({ ...(await vi.importActual('../app')), log: vi.fn() }));\n"
        "jest.mock('../app', () => ({ ...jest.requireActual('../app') }));\n"
        "function expectOk(r) { expect(r.status).toBe(200); }\n"
        "test('health', () => request(app).get('/health').expect(200));\n"
        "test('list', async () => { expectOk(await request(app).get('/x')); });\n"
    )
    assert _found("backend/tests/app.test.js", code) == []


# ── edit mode: a rewrite that lost the file ──────────────────────────────────
LONG = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(10))


def test_a_rewrite_that_drops_most_of_a_file_is_caught_unless_removal_was_asked():
    lost = ph.lost_code("backend/app/util.py", LONG, "def f0(x):\n    return x\n", "make f0 return x unchanged")
    assert lost is not None and lost.kind == "placeholder" and ph.is_lost_code(lost)
    assert lost.message.startswith("dropped 18 of its 20 lines, including `f1`, `f2`, `f3` and `f4` and more.")
    assert ph.lost_code("backend/app/util.py", LONG, "def f0(x):\n    return x\n", "remove the unused helpers") is None
    # A file too small for a share of it to mean anything.
    assert ph.lost_code("backend/app/a.py", "x = 1\ny = 2\n", "x = 1\n", "") is None


# ── through the compile gate ─────────────────────────────────────────────────
def test_the_compile_gate_reports_placeholders_after_what_does_not_compile():
    output = {
        "files": [
            {"path": "main.py", "code": "from app.services import total\n\ndef main():\n    return total([1])\n"},
            {"path": "app/services.py", "code": "def total(items):\n    pass\n"},
            {"path": "app/broken.py", "code": "def x(:\n    # TODO: fix\n"},
        ]
    }
    check = check_phase({}, "backend_engineer", output, None)
    assert check.status == "failed"
    kinds = [(p.path, p.kind) for p in check.problems]
    assert ("backend/app/broken.py", "syntax") in kinds
    # The broken file's TODO waits until it parses; the stub is named.
    assert ("backend/app/services.py", "placeholder") in kinds
    assert not any(path == "backend/app/broken.py" and k == "placeholder" for path, k in kinds)
    assert kinds.index(("backend/app/broken.py", "syntax")) < kinds.index(("backend/app/services.py", "placeholder"))


def test_the_check_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "check_placeholders", False)
    output = {"files": [{"path": "app/services.py", "code": "def total(items):\n    pass\n"}]}
    assert check_phase({}, "backend_engineer", output, None).status == "ok"
    assert file_problems("backend/app/services.py", "def total(items):\n    pass\n") == []


def test_the_ship_review_says_unfinished_not_doesnt_compile():
    note = build_note([{"path": "backend/app/services.py", "line": 1, "kind": "placeholder", "message": "`total` has no body yet"}])
    assert note.startswith("1 file isn't finished, after the crew's fix rounds.")
    mixed = build_note([
        {"path": "a.py", "kind": "placeholder", "message": "x"}, {"path": "b.py", "kind": "syntax", "message": "y"},
    ])
    assert mixed.startswith("2 files don't compile")


def test_a_fix_note_says_what_is_wrong_with_unfinished_code():
    problem = autofix._build_problem(
        {"path": "backend/app/services.py", "line": 1, "kind": "placeholder", "message": "`total` has no body yet"},
        "backend_engineer",
    )
    assert problem["problem"] == "placeholder"
    weak = autofix._build_problem({"path": "backend/tests/test_a.py", "kind": "weak_test", "message": "asserts nothing"}, "qa_engineer")
    note = autofix.code_note([problem, weak], 1)
    assert "- (is unfinished) in `backend/app/services.py:1`" in note
    assert "- (tests nothing) in `backend/tests/test_a.py`" in note
    # Keys are the file and the error, as for every compile problem.
    assert problem["key"] == autofix._build_problem(
        {"path": "backend/app/services.py", "line": 9, "kind": "placeholder", "message": "`total` has no body yet"},
        "backend_engineer",
    )["key"]


# ── the loop: a stub and an assert-free test, sent back and rewritten ────────
STUB = "def total(items):\n    pass\n"
REAL = "def total(items):\n    return sum(items)\n"
MAIN = "from app.services import total\n\n\ndef main():\n    return total([1, 2])\n"
WEAK_TEST = "from app.services import total\n\n\ndef test_total():\n    total([1, 2])\n"
REAL_TEST = "from app.services import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n"


class Stubby:
    """The Backend Engineer writes `services.py` as a stub, and QA a test with no
    assertion — until the fix loop's note names what is unfinished."""

    def __init__(self, fixes: bool, stub_backend: bool = True) -> None:
        self.fixes = fixes
        self.stub_backend = stub_backend
        self.notes: list[str] = []

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        system = messages[0].content
        prompt = "\n".join(m.content for m in messages[1:])
        schema = getattr(kwargs.get("options"), "json_schema", None)
        if system.startswith("You are the QA Engineer"):
            fixed = self.fixes and "(tests nothing)" in prompt
            if "(tests nothing)" in prompt:
                self.notes.append(prompt)
            payload = json.loads(resp.text)
            payload["test_files"] = [{"path": "backend/tests/test_services.py", "framework": "pytest",
                                      "targets": "backend/app/services.py", "code": REAL_TEST if fixed else WEAK_TEST}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        if not system.startswith("You are the Backend Engineer"):
            return resp
        if schema:
            payload = json.loads(resp.text)
            if "files" in payload:
                payload["files"] = [{"path": "main.py", "purpose": "entry"}, {"path": "app/services.py", "purpose": "totals"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        fixed = not self.stub_backend or (self.fixes and "(is unfinished)" in prompt)
        if "(is unfinished)" in prompt:
            self.notes.append(prompt)
        code = {"main.py": MAIN, "services.py": REAL if fixed else STUB}
        written = {p: next(c for name, c in code.items() if p.endswith(name)) for p in asked_files(messages)}
        return resp.model_copy(update={"text": fenced(written)})


def _build(client, monkeypatch, crew: Stubby) -> dict:
    stub(monkeypatch, "complete", crew)
    pid = client.post("/api/projects", json={"idea": "A totals service", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    return client.get(f"/api/projects/{pid}").json()


def _current(project: dict, phase: str) -> dict:
    return [p for p in project["phases"] if p["phase"] == phase and p["status"] != "rejected"][-1]


def test_a_stub_and_an_assert_free_test_are_sent_back_and_rewritten(client, monkeypatch):
    """Acceptance: two problems — a stub and a test with no assertion — each routed to
    the phase that wrote it, rewritten by the fix loop, and the build completes."""
    crew = Stubby(fixes=True)
    project = _build(client, monkeypatch, crew)
    assert project["status"] == "completed", project.get("gate_note")

    tracks = project["auto_fix"]["tracks"]
    [backend] = tracks["build:backend_engineer"]["rounds"]
    [qa] = tracks["build:qa_engineer"]["rounds"]
    [stub_problem] = backend["problems"]
    [weak_problem] = qa["problems"]
    assert stub_problem["problem"] == "placeholder" and stub_problem["where"] == "backend/app/services.py:1"
    assert "total has no body yet" in stub_problem["title"]
    assert weak_problem["problem"] == "weak_test" and "test test_total asserts nothing" in weak_problem["title"]
    assert backend["fixed"] == [stub_problem["key"]] and qa["fixed"] == [weak_problem["key"]]
    # Each was told, in its own words, what was wrong.
    assert any("(is unfinished)" in n for n in crew.notes) and any("(tests nothing)" in n for n in crew.notes)

    code = {f["path"]: f["code"] for f in _current(project, "backend_engineer")["output"]["files"]}
    assert code["backend/app/services.py"] == REAL
    tests = _current(project, "qa_engineer")["output"]["test_files"]
    assert tests[0]["code"] == REAL_TEST

    with SessionLocal() as db:
        card = score(db, db.get(Project, project["id"]))
    assert (card["placeholders"], card["weak_tests"]) == (0, 0)


@pytest.mark.parametrize("stub_backend, counted", [(True, (1, 0)), (False, (0, 1))])
def test_unfinished_code_the_crew_cannot_finish_asks_for_help_and_is_counted(client, monkeypatch, stub_backend, counted):
    project = _build(client, monkeypatch, Stubby(fixes=False, stub_backend=stub_backend))
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "needs_help"
    assert "isn't finished" in project["gate_note"] or "couldn't fix" in project["gate_note"]
    with SessionLocal() as db:
        card = score(db, db.get(Project, project["id"]))
    assert (card["placeholders"], card["weak_tests"]) == counted


# ── a change request that loses the file ─────────────────────────────────────
def test_a_change_whose_rewrite_drops_most_of_a_file_keeps_the_file(client, script):  # noqa: F811
    script.writes["backend/app/util.py"] = LONG
    pid = _finished(client)
    before = _code(_zip(client, pid))
    script.edits["backend_engineer"] = {
        "summary": "Make f0 return x", "files": [{"path": "backend/app/util.py", "purpose": "f0 returns x"}]
    }
    script.writes["backend/app/util.py"] = "def f0(x):\n    return x\n"
    mark = len(script.calls)
    r = client.post(f"/api/projects/{pid}/changes", json={"text": "make f0 return x"})
    assert r.status_code == 202, r.text

    project = client.get(f"/api/projects/{pid}").json()
    backend = next(p for p in project["phases"] if p["phase"] == "backend_engineer" and p["status"] == "approved")
    assert backend["handoff"]["change"]["lost_code"] == ["backend/app/util.py"]
    assert _code(_zip(client, pid))["backend/app/util.py"] == before["backend/app/util.py"]
    # The repair was shown the file as it was, whole — not the two lines that came back.
    repairs = [c for c in script.calls[mark:] if c["role"] == "backend_engineer" and "dropped 18" in c["messages"][-1].content]
    assert repairs and "def f9(x):" in repairs[0]["messages"][-1].content



# ── edits answer for what they wrote (review of #104) ────────────────────────
HERO = (
    "export function Hero() {\n  return <h1>Welcome</h1>;\n}\n\n"
    + "".join(f"export function part{i}() {{\n  return {i};\n}}\n\n" for i in range(6))
)


def _edit(monkeypatch, replies: list[str], code: str = HERO):
    """One "change this element" on Hero.tsx, the model answering `replies` in turn."""
    from app.agents.base import AgentContext
    from app.agents.code_phase import _Run
    from app.agents.frontend_engineer import FrontendEngineerAgent
    from app.schemas.llm import LLMResponse, Usage

    asked: list[str] = []

    def complete(messages, **kwargs):
        asked.append(messages[-1].content)
        return LLMResponse(text=replies[min(len(asked), len(replies)) - 1], provider="mock", model="mock-model",
                           usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    output = {"files": [{"path": "frontend/components/Hero.tsx", "language": "typescript", "code": code}]}
    ask = {"path": "frontend/components/Hero.tsx", "lines": [2, 2], "what": "the heading", "instruction": "Make the heading warmer"}
    run = _Run(FrontendEngineerAgent(), AgentContext(idea="A landing page"))
    return run.edit(output, ask), asked


def test_a_repair_of_an_edit_that_lost_code_still_makes_the_change(monkeypatch, stub_router):
    warm = HERO.replace("Welcome", "Welcome, friend")
    (out, _calls), asked = _edit(monkeypatch, [fenced({"frontend/components/Hero.tsx": warm.split("\n\n")[0]}),
                                              fenced({"frontend/components/Hero.tsx": warm})])
    assert len(asked) == 2
    # The repair names the change as well as the lost code, and shows the file as it was.
    assert "dropped" in asked[1] and "- change: In line 2 (the heading): Make the heading warmer" in asked[1]
    assert "export function part5()" in asked[1]
    assert out["files"][0]["code"] == warm


def test_an_edit_that_comes_back_unchanged_is_refused_not_kept(monkeypatch, stub_router):
    from app.agents.base import RevisionRefused

    with pytest.raises(RevisionRefused, match="unchanged"):
        _edit(monkeypatch, [fenced({"frontend/components/Hero.tsx": HERO})])


def test_an_edit_isnt_refused_for_a_todo_the_file_already_had(monkeypatch, stub_router):
    old = HERO.replace("export function Hero() {\n", "export function Hero() {\n  // TODO: wire search\n")
    warm = old.replace("Welcome", "Welcome, friend")
    (out, _calls), asked = _edit(monkeypatch, [fenced({"frontend/components/Hero.tsx": warm})], code=old)
    assert len(asked) == 1 and out["files"][0]["code"] == warm


def test_a_change_answers_only_for_the_files_and_placeholders_it_wrote():
    base = {"files": [
        {"path": "app/kept.py", "code": "def track():\n    pass\n"},
        {"path": "app/changed.py", "code": "def a():\n    # TODO: cache\n    return 1\n"},
        {"path": "app/worse.py", "code": "def b():\n    return 2\n"},
    ]}
    now = {"files": [
        base["files"][0],
        {"path": "app/changed.py", "code": "def a():\n    # TODO: cache\n    return 10\n"},
        {"path": "app/worse.py", "code": "def b():\n    pass\n"},
    ]}
    check = check_phase({}, "backend_engineer", now, None, previous={"output": base, "asked": "make a return 10"})
    assert [(p.path, p.kind) for p in check.problems] == [("backend/app/worse.py", "placeholder")]


def test_a_code_phase_leaves_lost_code_to_its_own_per_file_judgement():
    base = {"files": [{"path": "app/util.py", "code": LONG}]}
    now = {"files": [{"path": "app/util.py", "code": "def f0(x):\n    return x\n"}]}
    asked = "make f0 return x"
    judged = check_phase({}, "backend_engineer", now, None, previous={"output": base, "asked": asked})
    assert any(ph.is_lost_code(p) for p in judged.problems)
    left = check_phase({}, "backend_engineer", now, None, previous={"output": base, "asked": asked, "lost_code": False})
    assert left.status == "ok"
    from app.agents.backend_engineer import BackendEngineerAgent
    from app.agents.qa_engineer import QAEngineerAgent

    assert BackendEngineerAgent.lost_code_at_gate is False and QAEngineerAgent.lost_code_at_gate is True


def test_review_cases_for_the_patterns():
    # Ordinary comments and words that only look like the patterns.
    assert _found("backend/app/x.py", "def f(t):\n    # keep going if the total is unchanged\n    return t\n") == []
    assert _found("frontend/src/App.tsx", "export default function App() {\n  return <p>Add your first task here</p>;\n}\n") == []
    assert _found("backend/tests/conftest.py", "") == []
    assert ph.lost_code("backend/app/util.py", LONG, "def f0(x):\n    return x\n", "Add a dropdown to pick a category")
    assert ph.lost_code("backend/app/util.py", LONG, "def f0(x):\n    return x\n", "Add Stripe checkout")
    # The scorecard counts every one; the gate's list stops at three a file.
    many = "".join(f"def test_{i}():\n    print({i})\n\n" for i in range(8))
    assert len(ph.placeholder_problems({"backend/tests/test_a.py": many}, ["backend/tests/test_a.py"])) == 3
    assert len(ph.placeholder_problems({"backend/tests/test_a.py": many}, ["backend/tests/test_a.py"], cap=None)) == 8
