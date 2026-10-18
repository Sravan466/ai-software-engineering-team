"""When an import points at nothing, the import may be what's wrong (#119).

Build bf5eec2f's `routes/api.js` did `require('./routes/user')`. Each round wrote the
literal path — `routes/routes/user.js`, then `routes/routes/api/users/register.js`, then
`routes/routes/api/users/api/users/register.js` — and never touched `api.js`, the file
that was wrong. Now an import that repeats its own folder is pointed at the file it
meant; every other missing module is asked for beside its importer; and a new file's
own imports are answered in the round that wrote it.
"""
from __future__ import annotations

from app.agents.base import AgentContext
from app.agents.backend_engineer import BackendEngineerAgent
from app.agents.code_phase import _Run
from app.build import fixscope
from app.build.check import check_phase
from app.schemas.llm import LLMResponse, Usage
from tests.conftest import _fake_complete, asked_files, fenced, stub

API = (
    "const express = require('express');\nconst router = express.Router();\n"
    "const userRoutes = require('./routes/user');\nconst todoRoutes = require('./routes/todo');\n"
    "router.use('/api/users', userRoutes);\nrouter.use('/api/todos', todoRoutes);\nmodule.exports = router;\n"
)
MAIN = "const express = require('express');\nconst api = require('./routes/api');\nconst app = express();\napp.use(api);\napp.listen(3000);\n"
ROUTER = "const express = require('express');\nconst router = express.Router();\nrouter.get('/', (req, res) => res.json([]));\nmodule.exports = router;\n"


def _round(monkeypatch, files: list[dict], answer):
    """One fix round; `answer(wanted paths, prompt)` gives each asked file's code."""
    asked: list[str] = []

    def complete(messages, **kwargs):
        if "A todo API" not in messages[-1].content or getattr(kwargs.get("options"), "json_schema", None):
            return _fake_complete(messages, **kwargs)
        text = messages[-1].content
        asked.append(text)
        wanted = asked_files([type("M", (), {"content": text})()])
        return LLMResponse(text=fenced(answer(wanted, text)), provider="mock", model="mock-model",
                           usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    problems = [p.as_dict() for p in check_phase({}, "backend_engineer", {"summary": "s", "files": files}).problems]
    ctx = AgentContext(idea="A todo API", base_output={"summary": "A todo API", "files": files}, fixing=tuple(problems))
    return _Run(BackendEngineerAgent(), ctx).fix(), asked, problems


def _js(code: dict[str, str]) -> list[dict]:
    return [{"path": p, "language": "javascript", "purpose": p.rsplit("/", 1)[-1], "code": c} for p, c in code.items()]


# ── paths ────────────────────────────────────────────────────────────────────
def test_a_repeated_folder_sequence_is_found_and_written_once():
    assert fixscope.repeats("backend/routes/routes/user.js")
    assert fixscope.repeats("backend/routes/routes/api/users/api/users/register.js")
    assert not fixscope.repeats("frontend/src/components/ui/components/x.tsx")  # not in a row
    assert not fixscope.repeats("backend/routes/user.js")
    assert fixscope.collapse("backend/routes/routes/user") == "backend/routes/user"
    assert fixscope.collapse("backend/routes/routes/api/users/api/users/register.js") == "backend/routes/api/users/register.js"


def test_scope_points_a_repeating_import_at_the_file_it_meant():
    own = {"backend/routes/api.js": API, "backend/main.js": MAIN}
    problems = [p.as_dict() for p in check_phase({}, "backend_engineer", {"summary": "s", "files": _js(own)}).problems]
    found = fixscope.scope(problems, own, own)
    # Nothing at `routes/routes/`: the files go where the import meant, and it's repointed.
    assert set(found.new) == {"backend/routes/user.js", "backend/routes/todo.js"}
    assert sorted(r[:3] for r in found.repoint) == [
        ("backend/routes/api.js", "./routes/todo", "backend/routes/todo.js"),
        ("backend/routes/api.js", "./routes/user", "backend/routes/user.js"),
    ]
    assert "backend/routes/api.js" not in found.touch
    # Where the meant file exists, only the import changes.
    own["backend/routes/user.js"] = ROUTER
    found = fixscope.scope(problems, own, own)
    assert ("backend/routes/api.js", "./routes/user", "backend/routes/user.js") in [r[:3] for r in found.repoint]
    assert "backend/routes/user.js" not in found.new


# ── the round ────────────────────────────────────────────────────────────────
def test_bf5eec2f_writes_the_meant_files_and_repoints_the_importer(monkeypatch, stub_router):
    files = _js({"backend/main.js": MAIN, "backend/routes/api.js": API})
    result, asked, _ = _round(monkeypatch, files, lambda wanted, text: {p: ROUTER for p in wanted})
    out = {f["path"]: f["code"] for f in result.output["files"]}
    assert {"backend/routes/user.js", "backend/routes/todo.js"} <= set(out)
    assert "require('./user')" in out["backend/routes/api.js"] and "require('./todo')" in out["backend/routes/api.js"]
    assert not any(fixscope.repeats(p) for p in out)
    assert result.handoff["fix"]["repointed"] == [
        {"path": "backend/routes/api.js", "from": "./routes/user", "to": "./user"},
        {"path": "backend/routes/api.js", "from": "./routes/todo", "to": "./todo"},
    ] or sorted(r["to"] for r in result.handoff["fix"]["repointed"]) == ["./todo", "./user"]
    assert result.build_status == "ok"


CONTROLLER = (
    "const { createTodoAnimation } = require('./animations');\n"
    "exports.create = (req, res) => {\n  createTodoAnimation();\n  res.json({});\n};\n"
)
DROPPED = "exports.create = (req, res) => {\n  res.json({});\n};\n"


def test_a_missing_module_comes_with_its_importer_and_either_answer_compiles(monkeypatch, stub_router):
    files = _js({"backend/main.js": "const t = require('./controllers/todos');\nmodule.exports = t;\n",
                 "backend/controllers/todos.js": CONTROLLER})
    # The model drops the import and its call instead of writing the module.
    result, asked, _ = _round(monkeypatch, files, lambda wanted, text: {"backend/controllers/todos.js": DROPPED})
    pair = next(a for a in asked if "add 1" in a)
    assert "`backend/controllers/animations.js` (new file)" in pair and "# Your last version of backend/controllers/todos.js" in pair
    assert "change that import instead" in pair
    out = {f["path"]: f["code"] for f in result.output["files"]}
    assert "backend/controllers/animations.js" not in out and out["backend/controllers/todos.js"] == DROPPED
    assert result.build_status == "ok"


def test_a_new_file_s_own_import_of_nothing_is_asked_about_in_the_same_round(monkeypatch, stub_router):
    files = _js({"backend/main.js": "const t = require('./controllers/todos');\nmodule.exports = t;\n",
                 "backend/controllers/todos.js": CONTROLLER})
    first = "const { x } = require('./api/users/register');\nfunction createTodoAnimation() { return x; }\nmodule.exports = { createTodoAnimation };\n"
    fixed = "function createTodoAnimation() { return 1; }\nmodule.exports = { createTodoAnimation };\n"
    tries = []

    def answer(wanted, text):
        out = {}
        for p in wanted:
            if p.endswith("animations.js"):
                tries.append(text)
                out[p] = first if len(tries) == 1 else fixed
            else:
                out[p] = CONTROLLER
        return out

    result, asked, _ = _round(monkeypatch, files, answer)
    assert len(tries) == 2 and "imports `./api/users/register`, and no file in this build is at" in tries[1]
    out = {f["path"]: f["code"] for f in result.output["files"]}
    assert out["backend/controllers/animations.js"] == fixed
    assert not any("register" in p for p in out)
    assert result.build_status == "ok"


def test_a_fix_round_never_lands_a_file_it_didn_t_ask_for(monkeypatch, stub_router):
    files = _js({"backend/main.js": "const t = require('./controllers/todos');\nmodule.exports = t;\n",
                 "backend/controllers/todos.js": CONTROLLER})

    def answer(wanted, text):
        out = {p: ("function createTodoAnimation() {}\nmodule.exports = { createTodoAnimation };\n"
                   if p.endswith("animations.js") else CONTROLLER) for p in wanted}
        out["backend/routes/routes/extra.js"] = "module.exports = 1;\n"  # unasked, and a repeat
        return out

    result, _asked, _ = _round(monkeypatch, files, answer)
    assert not any(fixscope.repeats(f["path"]) for f in result.output["files"])


# ── review of #122 ───────────────────────────────────────────────────────────
def _scope(own: dict[str, str], extra=()):
    files = _js(own)
    problems = [p.as_dict() for p in check_phase({}, "backend_engineer", {"summary": "s", "files": files}).problems]
    return fixscope.scope(problems, own, set(own) | set(extra))


def test_a_project_s_own_repeated_folder_is_no_mistake():
    # Django's `mysite/mysite/`: the importer already sits in it, so the import added nothing.
    assert not fixscope.repeats("backend/mysite/mysite/views.py", beside="backend/mysite/mysite/urls.py")
    assert fixscope.repeats("backend/routes/routes/user", beside="backend/routes/api.js")


def test_a_repeating_import_points_at_the_file_one_folder_off_rather_than_writing_a_copy():
    found = _scope({"backend/routes/api.js": API, "backend/main.js": MAIN, "backend/src/routes/user.js": ROUTER})
    assert ("backend/routes/api.js", "./routes/user", "backend/src/routes/user.js") in [r[:3] for r in found.repoint]
    assert "backend/routes/user.js" not in found.new and "backend/src/routes/user.js" not in found.new


def test_a_repeating_import_of_an_image_is_the_importer_s_to_change():
    own = {"frontend/src/components/Header.jsx": "import logo from './components/logo.png';\nexport default function H() { return logo; }\n"}
    problems = [{"path": "frontend/src/components/Header.jsx", "kind": "import", "line": 1,
                 "message": "imports `./components/logo.png`, and no file in this build is at frontend/src/components/components/logo.png. Write that file, or import one that exists."}]
    found = fixscope.scope(problems, own, own)
    assert not found.repoint and not any(p.endswith((".tsx", ".jsx")) and "logo" in p for p in found.new)
    assert "frontend/src/components/Header.jsx" in found.touch


def test_one_module_two_files_import_is_asked_for_once(monkeypatch, stub_router):
    other = CONTROLLER.replace("exports.create", "exports.other")
    files = _js({"backend/main.js": "const t = require('./controllers/todos');\nconst o = require('./controllers/other');\nmodule.exports = [t, o];\n",
                 "backend/controllers/todos.js": CONTROLLER, "backend/controllers/other.js": other})
    result, asked, _ = _round(monkeypatch, files, lambda wanted, text: {
        p: ("function createTodoAnimation() {}\nmodule.exports = { createTodoAnimation };\n" if p.endswith("animations.js")
            else (other if p.endswith("other.js") else CONTROLLER)) for p in wanted})
    assert sum(1 for a in asked if "`backend/controllers/animations.js` (new file)" in a) == 1
    assert result.build_status == "ok"


def test_a_pair_with_no_room_still_writes_the_module_alone(monkeypatch, stub_router):
    from app.agents import code_phase

    files = _js({"backend/main.js": "const t = require('./controllers/todos');\nmodule.exports = t;\n",
                 "backend/controllers/todos.js": CONTROLLER})
    real = code_phase._Run._messages

    def messages(self, batch, **kw):
        if kw.get("fixing") and len(batch) > 1:
            raise code_phase._NoRoom(batch[0].path)  # the pair doesn't fit this model
        return real(self, batch, **kw)

    monkeypatch.setattr(code_phase._Run, "_messages", messages)
    result, asked, _ = _round(monkeypatch, files, lambda wanted, text: {
        p: "function createTodoAnimation() {}\nmodule.exports = { createTodoAnimation };\n" for p in wanted})
    assert "backend/controllers/animations.js" in {f["path"] for f in result.output["files"]}
    assert result.build_status == "ok"


def test_a_typescript_import_of_a_js_name_for_a_ts_file_is_no_dangling_import():
    from types import SimpleNamespace

    from app.agents.code_phase import _Run, _Written

    run = SimpleNamespace(tree={"backend/src/db.ts"}, needed_by={"backend/src/routes/user.ts": []}, written={})
    w = _Written("backend/src/routes/user.ts", "import { db } from '../db.js';\nexport const u = db;\n", "typescript", "")
    assert _Run._dangling(run, w) == []
    w = _Written("backend/src/routes/user.ts", "import { x } from '../nothing.js';\nexport const u = x;\n", "typescript", "")
    assert len(_Run._dangling(run, w)) == 1
