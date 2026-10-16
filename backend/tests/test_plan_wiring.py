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
