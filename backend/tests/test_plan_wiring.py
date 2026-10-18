"""A plan says which file uses which, and a Node backend's module format is fixed with
the stack (#103).

The issue's stuck backend planned 41 files across six attempts and listed `imports` for
one of them; `main.js` was written first, blind, five times out of six, and the first two
attempts mixed `import` and `require` in one backend that then couldn't start.
"""
from __future__ import annotations

import json

from app.agents.backend_engineer import BackendEngineerAgent
from app.agents.base import AgentContext
from app.agents.code_phase import _Planned, _Run, _layer, _ordered
from app.build.scaffold import build as scaffold_build
from app.orchestration.charter import Charter, freeze, module_format_for, violations

JS = Charter.from_dict({"language": {"token": "javascript", "label": "JavaScript"},
                        "backend_framework": {"token": "express", "label": "Express"}, "module_format": "commonjs"})


# ── the plan ─────────────────────────────────────────────────────────────────
def test_a_plan_of_three_files_or_more_with_no_imports_is_asked_again_naming_the_entry_file():
    agent = BackendEngineerAgent()
    plan = {"files": [{"path": "main.js"}, {"path": "app/models/todo.js"}, {"path": "app/routes/todos.js"}]}
    [problem] = agent.plan_problems(plan, AgentContext(idea="x"))
    assert "no file lists its `imports`" in problem and "`main.js`" in problem
    plan["files"][0]["imports"] = ["app/routes/todos.js"]
    assert agent.plan_problems(plan, AgentContext(idea="x")) == []
    assert agent.plan_problems({"files": [{"path": "main.js"}, {"path": "util.js"}]}, AgentContext(idea="x")) == []


def test_a_plan_that_still_says_nothing_is_written_by_layer_with_the_entry_file_last():
    paths = ["backend/main.js", "backend/app/routes/todos.js", "backend/app/controllers/todoController.js",
             "backend/app/services/todo_service.js", "backend/app/repositories/todo_repository.js",
             "backend/app/models/todo.js"]
    ordered = [p.path for p in _ordered([_Planned(path=p) for p in paths])]
    assert ordered == list(reversed(paths))
    assert _layer("backend/server.js") == _layer("backend/src/index.js")
    # `app.js` builds the app the entry file starts: written just before it.
    assert _layer("backend/routes/todos.js") < _layer("backend/app.js") < _layer("backend/server.js")
    # Where the plan does say, it is followed instead.
    planned = [_Planned(path="backend/main.js"), _Planned(path="backend/app/models/todo.js", imports=["backend/main.js"])]
    assert [p.path for p in _ordered(planned)] == ["backend/main.js", "backend/app/models/todo.js"]


# ── the module format ────────────────────────────────────────────────────────
def test_the_format_is_fixed_with_the_stack():
    assert (module_format_for("javascript"), module_format_for("typescript"), module_format_for("python")) == (
        "commonjs", "esm", None,
    )
    design = {"tech_stack": {"backend": ["Node.js", "Express"], "database": ["PostgreSQL"], "frontend": ["React"]}}
    charter = freeze(design)
    assert charter.module_format == "commonjs"
    again = Charter.from_dict(json.loads(json.dumps(charter.as_dict())))
    assert again.module_format == "commonjs"
    assert "Backend module format: CommonJS" in charter.prompt_block()
    # A charter from before formats were fixed has none: the scaffold reads the code.
    assert Charter.from_dict({"language": {"token": "javascript", "label": "JavaScript"}}).module_format is None


def test_each_backend_file_in_the_other_format_is_one_stack_problem():
    output = {"files": [
        {"path": "backend/app/models/todo.js", "code": "import mongoose from 'mongoose';\nimport x from './x';\nexport default mongoose;\n"},
        {"path": "backend/app/routes/todos.js", "code": "import express from 'express';\nexport default express.Router();\n"},
        {"path": "backend/main.js", "code": "const express = require('express');\nmodule.exports = express();\n"},
        {"path": "backend/tests/todo.test.js", "code": "import x from '../main';\n"},
        {"path": "backend/scripts/run.mjs", "code": "import x from './a.mjs';\n"},
    ]}
    found = [v for v in violations(JS, "backend_engineer", output) if "CommonJS" in v]
    assert [v.split("`")[1] for v in found] == ["backend/app/models/todo.js", "backend/app/routes/todos.js"]
    assert all("one file" not in v and "uses `import`/`export`" in v for v in found)


def test_the_scaffold_follows_the_format_the_stack_fixed():
    tree = {"backend/main.js": "import express from 'express';\nexport default express();\n"}
    manifest = next(f for f in scaffold_build(tree, JS).files if f.path == "backend/package.json")
    assert json.loads(manifest.content)["type"] == "commonjs"
    esm = Charter.from_dict({**JS.as_dict(), "module_format": "esm"})
    tree = {"backend/main.js": "const e = require('express');\nmodule.exports = e;\n"}
    manifest = next(f for f in scaffold_build(tree, esm).files if f.path == "backend/package.json")
    assert json.loads(manifest.content)["type"] == "module"


def test_every_backend_write_is_told_the_format():
    run = _Run(BackendEngineerAgent(), AgentContext(idea="x", charter=JS))
    assert any("Write CommonJS" in line for line in run._instructions())
    plain = _Run(BackendEngineerAgent(), AgentContext(idea="x"))
    assert not any("CommonJS" in line for line in plain._instructions())


# ── review of #112 ───────────────────────────────────────────────────────────
def test_a_file_that_mixes_the_formats_is_a_stack_problem():
    output = {"files": [{"path": "backend/app/routes/todos.js",
                         "code": "import express from 'express';\nconst r = express.Router();\nmodule.exports = r;\n"}]}
    [found] = violations(JS, "backend_engineer", output)
    assert "uses both" in found and "`backend/app/routes/todos.js`" in found


def test_layers_read_names_as_written_and_app_is_a_package_not_a_layer():
    assert _layer("backend/src/userRoutes.js") == 4 and _layer("backend/src/todoController.js") == 3
    fastapi = ["backend/app/main.py", "backend/app/crud.py", "backend/app/database.py", "backend/app/models.py"]
    ordered = [p.path for p in _ordered([_Planned(path=p) for p in fastapi])]
    assert ordered[0] == "backend/app/models.py" and ordered[-1] == "backend/app/main.py"


def test_one_stray_import_doesn_t_put_the_entry_file_first():
    planned = [_Planned(path="backend/main.js"), _Planned(path="backend/app/routes/todos.js"),
               _Planned(path="backend/app/models/todo.js", imports=["backend/app/routes/todos.js"])]
    assert [p.path for p in _ordered(planned)][-1] == "backend/main.js"


def test_typescript_isn_t_held_to_commonjs_and_config_files_are():
    output = {"files": [
        {"path": "backend/src/types.ts", "code": "export interface Todo { id: string }\n"},
        {"path": "backend/jest.config.js", "code": "export default {};\n"},
    ]}
    # Node loads every `.js` file by the package's `type`, a tool's config included.
    assert [v.split("`")[1] for v in violations(JS, "backend_engineer", output)] == ["backend/jest.config.js"]


def test_an_es_module_that_loads_commonjs_on_purpose_isn_t_mixed():
    from app.orchestration.charter import module_format_of

    assert module_format_of("import { createRequire } from 'module';\nconst require = createRequire(import.meta.url);\nconst a = require('./a.json');\nexport default a;\n") == "esm"
    assert module_format_of("import x = require('x');\nexport const y = x;\n") == "esm"
    assert module_format_of("// was: const x = require('x')\nimport x from 'x';\nexport default x;\n") == "esm"


def test_a_next_js_backend_fixes_no_format_and_a_bad_stored_value_is_ignored():
    design = {"tech_stack": {"backend": ["Next.js", "JavaScript"], "frontend": ["Next.js"]}}
    assert freeze(design).module_format is None
    assert Charter.from_dict({**JS.as_dict(), "module_format": ["commonjs"]}).module_format is None


def test_a_language_implied_by_the_frontend_fixes_no_backend_format():
    design = {"tech_stack": {"backend": ["Fastify"], "frontend": ["Next.js"]}}
    assert freeze(design).module_format is None


def test_the_ask_again_names_a_source_file_as_the_entry():
    agent = BackendEngineerAgent()
    plan = {"files": [{"path": "index.html"}, {"path": "src/index.css"}, {"path": "src/main.tsx"}, {"path": "src/App.tsx"},
                      {"path": "src/api.ts"}]}
    [problem] = agent.plan_problems(plan, AgentContext(idea="x"))
    assert "`src/main.tsx`" in problem and "index.html" not in problem


def test_imports_of_packages_wire_nothing():
    agent = BackendEngineerAgent()
    plan = {"files": [{"path": "main.js", "imports": ["express"]}, {"path": "app/models/todo.js", "imports": ["mongoose"]},
                      {"path": "app/routes/todos.js", "imports": ["express"]}]}
    assert agent.plan_problems(plan, AgentContext(idea="x"))


def test_a_node_framework_with_no_language_named_still_fixes_a_format():
    assert freeze({"tech_stack": {"backend": ["Express"]}}).module_format == "commonjs"


# ── the plan's imports, as a plan writes them (#119) ────────────────────────
def _normalised(files: list[dict]) -> dict[str, _Planned]:
    from types import SimpleNamespace

    fake = SimpleNamespace(agent=SimpleNamespace(key="backend_engineer"), ctx=SimpleNamespace(charter=None), left_to_platform=[])
    return {p.path: p for p in _Run._normalise(fake, {"files": files})}


def test_every_shape_the_gate_accepts_keeps_its_import_through_normalise():
    # The issue's table: the gate accepted `app/models/todo` and `_normalise` then dropped
    # it, and the way code writes an import (`../models/todo`, `./models/todo`) neither.
    agent = BackendEngineerAgent()
    paths = ["app/models/todo.js", "app/controllers/todosController.js", "app/routes/todos.js", "main.js"]
    for shape in ("app/models/todo.js", "./app/models/todo.js", "app/models/todo", "../models/todo", "./models/todo"):
        files = [{"path": p, "purpose": "x"} for p in paths]
        files[1]["imports"] = [shape]
        assert agent.plan_problems({"files": files}, AgentContext(idea="x")) == [], shape
        assert _normalised(files)["backend/app/controllers/todosController.js"].imports == ["backend/app/models/todo.js"], shape


def test_a_7b_plan_written_the_javascript_way_keeps_all_of_its_wiring():
    # qwen2.5:7b's own plan for the issue's idea: 0 of 11 files kept an import before.
    plan = {
        "index.js": ["./config", "./routes/auth", "./routes/todos", "./routes/streak"], "config.js": [],
        "models/user.js": ["bcryptjs", "jsonwebtoken", "config"], "models/todo.js": ["mongoose", "config"],
        "routes/auth.js": ["express", "./controllers/authController", "config"],
        "routes/todos.js": ["express", "./controllers/todoController"],
        "routes/streak.js": ["express", "./controllers/streakController"],
        "controllers/authController.js": ["jsonwebtoken", "./models/user"],
        "controllers/todoController.js": ["./models/todo"], "controllers/streakController.js": ["./models/streak"],
        "models/streak.js": ["mongoose"],
    }
    files = [{"path": p, "purpose": "x", "imports": i} for p, i in plan.items()]
    assert BackendEngineerAgent().plan_problems({"files": files}, AgentContext(idea="x")) == []
    out = _normalised(files)
    b = "backend/"
    assert out[b + "index.js"].imports == [b + "config.js", b + "routes/auth.js", b + "routes/todos.js", b + "routes/streak.js"]
    assert out[b + "routes/auth.js"].imports == [b + "controllers/authController.js", b + "config.js"]
    assert out[b + "controllers/todoController.js"].imports == [b + "models/todo.js"]
    assert out[b + "models/user.js"].imports == [b + "config.js"]
    # Packages are left alone, and dropped without a word: they were never plan files.
    assert not any(p.dropped for p in out.values())
    # Written in the order the wiring now gives: each file after what it imports.
    order = list(out)
    assert order.index(b + "models/todo.js") < order.index(b + "controllers/todoController.js") < order.index(
        b + "routes/todos.js") < order.index(b + "index.js")


def test_a_relative_import_is_read_from_the_importer_s_folder_first():
    from app.agents.code_phase import _plan_import

    plan = {"user.js", "routes/user.js", "routes/api.js", "app/models/user.js", "app/models/todo.ts", "lib/db/index.js"}
    # JavaScript's own rule: `./user` from `routes/api.js` is `routes/user.js`.
    assert _plan_import("routes/api.js", "./user", plan) == "routes/user.js"
    assert _plan_import("api.js", "./user", plan) == "user.js"
    # A folder's index, and NodeNext's `.js` for a `.ts` source.
    assert _plan_import("main.js", "./lib/db", plan) == "lib/db/index.js"
    assert _plan_import("app/x.ts", "./models/todo.js", plan) == "app/models/todo.ts"
    # `@/`, `~/` and `/` are the plan's root.
    assert _plan_import("routes/api.js", "@/user", plan) == "user.js"
    # The last resort is the one plan file ending in the import's path — never one of two.
    assert _plan_import("main.js", "./models/todo", plan) == "app/models/todo.ts"
    assert _plan_import("lib/x.js", "../user", {"routes/user.js", "app/models/user.js", "lib/x.js"}) is None
    # A file never imports itself — nor, failing that, the root file of its name.
    assert _plan_import("routes/api.js", "./api", plan | {"api.js"}) is None
    # A bare name is a package unless a plan file is at it as written.
    assert _plan_import("main.js", "express", plan) is None
    assert _plan_import("main.js", "user", plan) == "user.js"
    # A leading `backend/` is the plan's root, placed: written as the placed path, kept.
    assert _plan_import("main.js", "backend/app/models/user", plan) == "app/models/user.js"


def test_a_package_is_never_wired_to_a_plan_file_of_its_name():
    from app.agents.code_phase import _meant_planned, _plan_import

    plan = {"src/firebase/auth.js", "src/hooks/useAuth.js", "chart.jsx", "app/email/utils.py", "app/main.py", "a/y.py", "a/x.js"}
    assert _plan_import("src/hooks/useAuth.js", "firebase/auth", plan) is None
    assert _plan_import("app/main.py", "email.utils", plan) is None
    assert _plan_import("main.ts", "chart.js", plan) is None
    assert not _meant_planned("chart.js", {"src", "app"}, python=False)
    assert not _meant_planned("next/router", {"src", "app"}, python=False)
    assert _meant_planned("app.models.user", {"src", "app"}, python=True)
    # A JavaScript import is never a Python file, nor `./x.py` a JavaScript one.
    assert _plan_import("a/x.js", "./y.js", plan) is None
    assert _plan_import("a/z.py", "./x.py", plan) is None


def test_the_gate_and_normalise_agree_on_placed_paths_and_platform_files():
    agent = BackendEngineerAgent()
    files = [{"path": "app/models/todo.js"}, {"path": "app/controllers/t.js", "imports": ["backend/app/models/todo.js"]},
             {"path": "main.js", "imports": ["backend/app/controllers/t"]}]
    assert agent.plan_problems({"files": files}, AgentContext(idea="x")) == []
    out = _normalised(files)
    assert out["backend/main.js"].imports == ["backend/app/controllers/t.js"]
    # A file the platform writes is no wiring the gate counts, and its import is recorded.
    files = [{"path": "main.js", "imports": ["./jest.config.js"]}, {"path": "jest.config.js"}, {"path": "app/x.js"},
             {"path": "app/y.js"}]
    assert agent.plan_problems({"files": files}, AgentContext(idea="x"))
    assert _normalised(files)["backend/main.js"].dropped == ["./jest.config.js"]


def test_python_modules_resolve_by_package_and_relative_dots():
    from app.agents.code_phase import _plan_import

    plan = {"app/main.py", "app/models/__init__.py", "app/models/user.py", "app/routes/todos.py", "app/crud.py"}
    assert _plan_import("app/main.py", "app.models.user", plan) == "app/models/user.py"
    assert _plan_import("app/routes/todos.py", "..crud", plan) == "app/crud.py"
    assert _plan_import("app/main.py", ".models", plan) == "app/models/__init__.py"
    assert _plan_import("app/main.py", "fastapi", plan) is None


def test_an_import_meant_as_a_plan_file_that_isn_t_one_is_dropped_and_counted(monkeypatch):
    from tests.test_code_phases import SMALL, Scripted, _run

    files = [
        {"path": "app/models/todo.js", "purpose": "model"},
        {"path": "app/controllers/todos.js", "purpose": "controller",
         "imports": ["../models/todo", "./animations", "express", "models/streak", "next/router"]},
        {"path": "main.js", "purpose": "entry", "imports": ["./app/controllers/todos"]},
    ]
    model = Scripted("Backend Engineer", files)
    result = _run(monkeypatch, "backend_engineer", model, SMALL)
    record = result.handoff["generation"]
    entry = next(p for p in record["plan"] if p["path"] == "backend/app/controllers/todos.js")
    assert entry["imports"] == ["backend/app/models/todo.js"]
    # A package (`express`, `next/router`) isn't counted; a path under a plan folder is.
    assert entry["dropped_imports"] == ["./animations", "models/streak"]
    assert record["imports_dropped"] == 2
    assert "dropped_imports" not in next(p for p in record["plan"] if p["path"] == "backend/main.js")
    # Each write call names what the file imports, with that file's code (#119's ask).
    ask = next(p for p, w in zip(model.prompts[1:], model.writes) if w == ["backend/app/controllers/todos.js"])
    assert "Imports: backend/app/models/todo.js" in ask and "### backend/app/models/todo.js" in ask


def test_a_plan_whose_imports_all_resolve_nowhere_is_asked_again():
    agent = BackendEngineerAgent()
    plan = {"files": [{"path": "main.js", "imports": ["./routes/missing"]}, {"path": "app/models/todo.js"},
                      {"path": "app/routes/todos.js", "imports": ["../models/gone"]}]}
    [problem] = agent.plan_problems(plan, AgentContext(idea="x"))
    assert "no file lists its `imports`" in problem


def test_a_change_s_new_file_keeps_an_import_written_the_javascript_way(monkeypatch):
    from tests.test_code_phases import SMALL, Scripted, _run

    model = Scripted("Backend Engineer", [{"path": "app/controllers/streak.js", "purpose": "streaks", "imports": ["../models/todo"]}])
    base = {"summary": "s", "files": [{"path": "backend/app/models/todo.js", "language": "javascript",
                                        "code": "module.exports = { Todo: 1 };\n"}]}
    ctx = AgentContext(idea="A todo app", prior_outputs={}, change="Add streaks", base_output=base)
    result = _run(monkeypatch, "backend_engineer", model, SMALL, ctx)
    assert "backend/app/controllers/streak.js" in {f["path"] for f in result.output["files"]}
    ask = next(p for p, w in zip(model.prompts[1:], model.writes) if w == ["backend/app/controllers/streak.js"])
    assert "Imports: backend/app/models/todo.js" in ask and "module.exports = { Todo: 1 };" in ask
