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
    assert _layer("backend/server.js") == _layer("backend/src/index.js") == _layer("backend/app.js")
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
