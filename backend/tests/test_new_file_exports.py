"""A file a fix round writes new is told what it must export (#119).

Build 7eb1cd24's controller did `require('./animations')` for three names, and the round
that wrote `animations.js` was told only that it was "needed". qwen2.5:7b invented other
names three times out of three; given the import line and the names, three out of three.
"""
from __future__ import annotations

from app.agents.base import AgentContext
from app.agents.backend_engineer import BackendEngineerAgent
from app.agents.code_phase import _Run
from app.build import wiring
from app.build.check import check_phase
from app.schemas.llm import LLMResponse, Usage
from tests.conftest import _fake_complete, asked_files, fenced, stub

CONTROLLER = (
    "const { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation } = require('./animations');\n"
    "exports.create = (req, res) => { createTodoAnimation(); res.json({}); };\n"
    "exports.update = (req, res) => { updateTodoAnimation(); res.json({}); };\n"
    "exports.remove = (req, res) => { deleteTodoAnimation(); res.json({}); };\n"
)
MAIN = "const express = require('express');\nconst todos = require('./controllers/todosController');\nconst app = express();\napp.post('/api/todos', todos.create);\napp.listen(3000);\n"
RIGHT = (
    "function createTodoAnimation() {}\nfunction updateTodoAnimation() {}\nfunction deleteTodoAnimation() {}\n"
    "module.exports = { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation };\n"
)
WRONG = "function fadeIn() {}\nfunction bounce() {}\nmodule.exports = { fadeIn, bounce };\n"
NEW = "backend/controllers/animations.js"


def _files() -> list[dict]:
    return [
        {"path": "backend/main.js", "language": "javascript", "purpose": "entry", "code": MAIN},
        {"path": "backend/controllers/todosController.js", "language": "javascript", "purpose": "todos", "code": CONTROLLER},
    ]


def _round(monkeypatch, answers: list[str]):
    """One fix round on the controller missing `./animations`; the new file is written
    from `answers` in turn. Returns (result, every prompt asking for the new file)."""
    asked: list[str] = []

    def complete(messages, **kwargs):
        if "A todo API" not in messages[-1].content or getattr(kwargs.get("options"), "json_schema", None):
            return _fake_complete(messages, **kwargs)
        text = messages[-1].content
        wanted = asked_files([type("M", (), {"content": text})()])
        if NEW in wanted:
            asked.append(text)
        code = answers[min(len(asked), len(answers)) - 1] if asked else RIGHT
        return LLMResponse(text=fenced({p: code if p == NEW else CONTROLLER for p in wanted}), provider="mock",
                           model="mock-model", usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    files = _files()
    problems = [p.as_dict() for p in check_phase({}, "backend_engineer", {"summary": "s", "files": files}).problems]
    assert any("./animations" in p["message"] for p in problems)
    ctx = AgentContext(idea="A todo API", base_output={"summary": "A todo API", "files": files}, fixing=tuple(problems))
    return _Run(BackendEngineerAgent(), ctx).fix(), asked


# ── reading what an importer takes ──────────────────────────────────────────
def test_each_way_of_importing_says_what_it_takes():
    w = wiring.wanted("c.js", CONTROLLER, "./animations")
    assert w.named == ["createTodoAnimation", "updateTodoAnimation", "deleteTodoAnimation"] and w.default is None and w.commonjs
    assert w.line.startswith("const { createTodoAnimation") and w.line.endswith("require('./animations');")
    # A whole `require` used as a value is the module itself; one read through is its members.
    assert wiring.wanted("a.js", "const r = require('./r');\napp.use('/x', r);\n", "./r").default == "r"
    members = wiring.wanted("a.js", "const db = require('./db');\ndb.connect();\n// db.never()\nconst s = 'db.no()';\n", "./db")
    assert members.named == ["connect"] and members.default is None
    both = wiring.wanted("a.mjs", "import Anim, { fade as f } from './anim.js';\n", "./anim.js")
    assert (both.default, both.named, both.commonjs) == ("Anim", ["fade"], False)
    assert both.labels() == ["Anim (default)", "fade"]
    star = wiring.wanted("a.ts", "import * as anim from './anim';\nanim.fadeIn(); anim.fadeOut();\n", "./anim")
    assert star.named == ["fadeIn", "fadeOut"]
    py = wiring.wanted("app/routes.py", "from .services.todo import create_todo, list_todos\n", ".services.todo")
    assert py.named == ["create_todo", "list_todos"]
    # The files that import a module, by the compile gate's own resolution.
    files = {"backend/c/t.js": CONTROLLER, "backend/x.js": "import a from './c/animations.js';\n", "backend/y.js": "import 'express';\n"}
    assert wiring.importers_of("backend/c/animations.js", files) == [("backend/c/t.js", "./animations"), ("backend/x.js", "./c/animations.js")]


def test_missing_names_are_what_a_module_doesn_t_export():
    from app.build import exports

    wants = [wiring.wanted("c.js", CONTROLLER, "./animations")]
    assert wiring.unexported(wants, exports.read(NEW, RIGHT)) == []
    assert wiring.unexported(wants, exports.read(NEW, WRONG)) == ["createTodoAnimation", "updateTodoAnimation", "deleteTodoAnimation"]
    whole = [wiring.wanted("a.js", "const r = require('./r');\napp.use(r);\n", "./r")]
    assert wiring.unexported(whole, exports.read("r.js", "exports.x = 1;\n")) == ["default"]
    assert wiring.unexported(whole, exports.read("r.js", "module.exports = router;\n")) == []
    # A re-export the reader can't follow may hold any name: nothing is called missing.
    assert wiring.unexported(wants, exports.read(NEW, "export * from './more';\n")) == []


# ── the round ───────────────────────────────────────────────────────────────
def test_a_new_file_is_shown_its_importer_s_line_and_told_the_names(monkeypatch, stub_router):
    result, asked = _round(monkeypatch, [RIGHT])
    ask = asked[0]
    assert f"- `{NEW}` (new file)" in ask
    assert "`backend/controllers/todosController.js` imports it: `const { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation } = require('./animations');`" in ask
    assert "Export exactly: `createTodoAnimation`, `updateTodoAnimation`, `deleteTodoAnimation`, each by that name" in ask
    assert "module.exports = { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation }" in ask
    assert "Write it with exactly what those files import from it." not in ask
    assert result.build_status == "ok" and len(asked) == 1
    record = result.handoff["fix"]["new_exports"][NEW]
    assert record == {"asked": ["createTodoAnimation", "updateTodoAnimation", "deleteTodoAnimation"],
                      "exported": ["createTodoAnimation", "updateTodoAnimation", "deleteTodoAnimation"], "missing": []}
    # The names are the new file's plan entry too, so the plan and the index list them.
    plan = {p["path"]: p for p in result.handoff["generation"]["plan"]}
    assert plan[NEW]["exports"] == ["createTodoAnimation", "updateTodoAnimation", "deleteTodoAnimation"]


def test_a_new_file_missing_a_name_is_asked_once_more_by_name(monkeypatch, stub_router):
    result, asked = _round(monkeypatch, [WRONG, RIGHT])
    assert len(asked) == 2
    assert "doesn't export `createTodoAnimation`, `updateTodoAnimation`, `deleteTodoAnimation`" in asked[1]
    assert result.build_status == "ok"
    assert result.handoff["fix"]["new_exports"][NEW]["missing"] == []


def test_names_still_missing_after_the_retry_are_carried_not_hidden(monkeypatch, stub_router):
    result, asked = _round(monkeypatch, [WRONG, WRONG])
    assert len(asked) == 2  # once more, then no further: the next round has it
    record = result.handoff["fix"]["new_exports"][NEW]
    assert record["exported"] == ["fadeIn", "bounce"] and "createTodoAnimation" in record["missing"]
    assert result.build_status == "failed"
    assert any("createTodoAnimation" in p["message"] for p in result.build_problems)
