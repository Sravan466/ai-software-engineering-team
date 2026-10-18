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
    # Read off the whole module: asked for, not held to — `module.exports = pool` has it.
    assert members.loose == ["connect"] and members.named == [] and members.default is None
    both = wiring.wanted("a.mjs", "import Anim, { fade as f } from './anim.js';\n", "./anim.js")
    assert (both.default, both.named, both.commonjs) == ("Anim", ["fade"], False)
    # The plan says `default`: `Anim` is the importer's own name for it, not the module's.
    assert both.labels() == ["default", "fade"]
    star = wiring.wanted("a.ts", "import * as anim from './anim';\nanim.fadeIn(); anim.fadeOut();\n", "./anim")
    assert star.named == ["fadeIn", "fadeOut"]
    [py] = wiring.wanted_by("app/services/todo.py", {"app/routes.py": "from .services.todo import create_todo, list_todos\n"})
    assert py.named == ["create_todo", "list_todos"]
    # Every statement of the module, merged; a commented-out one isn't a line.
    two = wiring.wanted("a.js", "// import { old } from './a'\nimport A from './a';\nimport { b } from './a';\n", "./a")
    assert (two.default, two.named) == ("A", ["b"]) and "old" not in two.line
    # A whole `require` destructured later takes those names.
    config = wiring.wanted("a.js", "const config = require('./config');\nconst { port, host } = config;\n", "./config")
    assert config.loose == ["port", "host"] and config.default is None
    # A comment that says `import` above the real line doesn't hide it.
    said = wiring.wanted("a.js", "// we import the helpers below\nimport { fadeIn, slide } from './animations';\n", "./animations")
    assert said is not None and said.named == ["fadeIn", "slide"]
    # A type part and a destructuring default don't lose the names beside them.
    assert wiring.wanted("a.ts", "import { type Opts, fadeIn } from './anim';\n", "./anim").named == ["fadeIn"]
    assert wiring.wanted("a.js", "const { a, b = 1, c: d } = require('./x');\n", "./x").named == ["a", "b", "c"]
    # The files that import a module, by the compile gate's own resolution.
    files = {"backend/c/t.js": CONTROLLER, "backend/x.js": "import a from './c/animations.js';\n", "backend/y.js": "import 'express';\n"}
    assert wiring.importers_of("backend/c/animations.js", files) == [("backend/c/t.js", "./animations"), ("backend/x.js", "./c/animations.js")]


def test_missing_names_are_what_a_module_doesn_t_export():
    from app.build import exports

    wants = [wiring.wanted("c.js", CONTROLLER, "./animations")]
    assert wiring.unexported(wants, exports.read(NEW, RIGHT), RIGHT) == []
    assert wiring.unexported(wants, exports.read(NEW, WRONG), WRONG) == ["createTodoAnimation", "updateTodoAnimation", "deleteTodoAnimation"]
    # A CommonJS module's own value is asked for, never called missing: no reader here
    # sees `module.exports = function …` or `= express.Router()`.
    whole = [wiring.wanted("a.js", "const r = require('./r');\napp.use(r);\n", "./r")]
    for code in ("module.exports = express.Router();\n", "module.exports = function (app) {};\n", "module.exports = { a: 1 };\n"):
        assert wiring.unexported(whole, exports.read("r.js", code), code) == [], code
    # What's read off a whole module isn't held against a module that sets its value.
    db = [wiring.wanted("a.js", "const db = require('./db');\ndb.query('x');\n", "./db")]
    assert wiring.unexported(db, exports.read("db.js", "const pool = new Pool();\nmodule.exports = pool;\n"), "const pool = new Pool();\nmodule.exports = pool;\n") == []
    # Exports built by a call, a require, a spread or Object.assign are unknown, not missing.
    for code in ("module.exports = require('./impl');\n", "module.exports = makeAnimations();\n",
                 "module.exports = { ...require('./a') };\n", "Object.assign(module.exports, { a: 1 });\n"):
        assert wiring.unexported(wants, exports.read(NEW, code), code) == [], code
    # An ES default nobody exports is.
    es = [wiring.wanted("a.mjs", "import Anim from './anim.js';\n", "./anim.js")]
    assert wiring.unexported(es, exports.read("anim.js", "export const x = 1;\n"), "export const x = 1;\n") == ["default"]
    # Keys after an inline function body are exports too.
    obj = "module.exports = {\n  createTodoAnimation: () => { return 1 },\n  updateTodoAnimation() { return 2 },\n  deleteTodoAnimation,\n};\n"
    assert wiring.unexported(wants, exports.read(NEW, obj), obj) == []
    # A re-export the reader can't follow may hold any name: nothing is called missing.
    assert wiring.unexported(wants, exports.read(NEW, "export * from './more';\n"), "export * from './more';\n") == []


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
    # Listed with the names it was asked for, not the ones it wrote instead.
    assert "Exports: createTodoAnimation, updateTodoAnimation, deleteTodoAnimation" in asked[1]
    assert "Exports: fadeIn" not in asked[1]
    assert result.build_status == "ok"
    assert result.handoff["fix"]["new_exports"][NEW]["missing"] == []


def test_names_still_missing_after_the_retry_are_carried_not_hidden(monkeypatch, stub_router):
    result, asked = _round(monkeypatch, [WRONG, WRONG])
    assert len(asked) == 2  # once more, then no further: the next round has it
    record = result.handoff["fix"]["new_exports"][NEW]
    assert record["exported"] == ["fadeIn", "bounce"] and "createTodoAnimation" in record["missing"]
    assert result.build_status == "failed"
    assert any("createTodoAnimation" in p["message"] for p in result.build_problems)


def test_an_import_line_that_names_nothing_still_gets_the_general_ask(monkeypatch, stub_router):
    from app.agents.code_phase import _export_ask

    assert _export_ask([wiring.Wanted("a.js", "import './anim';")]) == ""


def test_a_new_file_exporting_a_name_it_never_defines_is_asked_once_more(monkeypatch, stub_router):
    # What qwen2.5:7b wrote for `const { User } = require('../models/User')`: the name, and
    # nothing behind it. The compile gate doesn't see an undeclared name in a JS file.
    ghost = ("function createTodoAnimation() {}\nfunction updateTodoAnimation() {}\n"
             "module.exports = { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation };\n")
    result, asked = _round(monkeypatch, [ghost, RIGHT])
    assert len(asked) == 2 and "exports `deleteTodoAnimation`, but never defines it" in asked[1]
    assert result.build_status == "ok"


def test_python_importers_are_found_by_absolute_and_package_imports():
    files = {
        "backend/app/main.py": "from app.services.anim import fade, slide\n",
        "backend/app/services/__init__.py": "",
        "backend/app/services/use.py": "from . import anim\nanim.bounce()\n",
    }
    wants = {w.importer: w for w in wiring.wanted_by("backend/app/services/anim.py", files)}
    assert wants["backend/app/main.py"].named == ["fade", "slide"]
    # `from . import anim` takes the module: its attributes are asked for, never checked.
    assert wants["backend/app/services/use.py"].loose == ["bounce"] and wants["backend/app/services/use.py"].named == []


def test_a_missing_es_default_names_the_es_importer_and_the_template_lists_every_name():
    from app.agents.code_phase import _export_ask, _missing_text

    wants = [wiring.wanted("server.js", "const anim = require('./anim');\nanim();\n", "./anim"),
             wiring.wanted("ui.mjs", "import anim from './anim.js';\n", "./anim.js")]
    assert "`ui.mjs`" in _missing_text(["default"], wants)
    many = [wiring.wanted("c.js", "const { a, b, c, d, e, f, g, h } = require('./x');\n", "./x")]
    assert "module.exports = { a, b, c, d, e, f, g, h }" in _export_ask(many)


def test_a_file_is_only_judged_on_exports_the_reader_sees_whole():
    from app.build import exports

    wants = [wiring.wanted("c.js", "const { a, b } = require('./x');\n", "./x")]
    # A name, a class or a variable holding the object: seen only as a value, never judged.
    for code in ("const api = { a, b };\nmodule.exports = api;\n", "class X { static a() {} static b() {} }\nmodule.exports = X;\n"):
        assert wiring.unexported(wants, exports.read("x.js", code), code) == [], code
    # An ES default of a CommonJS module is its whole value (interop).
    es = [wiring.wanted("a.mjs", "import anim from './anim.js';\n", "./anim.js")]
    cjs = "module.exports = { a: 1 };\n"
    assert wiring.unexported(es, exports.read("anim.js", cjs), cjs) == []
    # `{ default as X }` is the default.
    named_default = [wiring.wanted("a.mjs", "import { default as X, a } from './x.js';\n", "./x.js")]
    code = "export default function X() {}\nexport const a = 1;\n"
    assert wiring.unexported(named_default, exports.read("x.js", code), code) == []
    # A re-export list is defined elsewhere, not a name this file forgot to define.
    assert exports.undefined("x.js", "export { fadeIn, slide } from './effects';\n") == []
    # A dotted Python import reads its attributes.
    [dotted] = wiring.wanted_by("backend/app/services/anim.py",
                                {"backend/app/main.py": "import app.services.anim\napp.services.anim.fade()\n"})
    assert dotted.loose == ["fade"]
