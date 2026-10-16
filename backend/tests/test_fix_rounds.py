"""Fix rounds edit; they never rewrite (#103).

A build fix round used to re-run the whole code phase: a new plan, new file names, new
mismatches, every round. Now it is handed the attempt it fixes and edits the files the
problems are in, plus the file each problem names, and keeps every other file byte for
byte.
"""
from __future__ import annotations

import json

from app.agents.base import AgentContext
from app.agents.backend_engineer import BackendEngineerAgent
from app.agents.code_phase import _Run
from app.build import fixscope
from app.build.check import check_phase
from app.schemas.llm import LLMResponse, Usage
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_database_gate

SERVER = (
    "import express from 'express';\n"
    "import { listTodos } from './services/todoService.js';\n"
    "const app = express();\n"
    "app.get('/api/todos', (req, res) => res.json(listTodos()));\n"
    "app.listen(3000);\n"
)
SERVICE = "export function getTodos() {\n  return [];\n}\n"
FIXED_SERVICE = "export function getTodos() {\n  return [];\n}\n\nexport function listTodos() {\n  return getTodos();\n}\n"
MODEL = "export const Todo = { title: '', done: false };\n"
HEALTH = "export default function health(req, res) {\n  res.json({ ok: true });\n}\n"


def _files(**over: str) -> list[dict]:
    code = {
        "backend/server.js": SERVER,
        "backend/services/todoService.js": SERVICE,
        "backend/models/todo.js": MODEL,
        "backend/routes/health.js": HEALTH,
        **over,
    }
    return [{"path": p, "language": "javascript", "purpose": f"the {p.rsplit('/', 1)[-1]} module", "code": c}
            for p, c in code.items()]


def _problems(files: list[dict]) -> list[dict]:
    check = check_phase({}, "backend_engineer", {"summary": "s", "files": files})
    return [p.as_dict() for p in check.problems]


def _fix(monkeypatch, files: list[dict], replies: list[str], problems=None):
    """One fix round on `files`, the model answering `replies` in turn."""
    asked: list[dict] = []

    def complete(messages, **kwargs):
        schema = getattr(kwargs.get("options"), "json_schema", None)
        if "A todo API" not in messages[-1].content:
            # A mockup thread an earlier test left running: the stub is class-wide.
            return _fake_complete(messages, **kwargs)
        asked.append({"text": messages[-1].content, "schema": bool(schema)})
        if schema:
            return _fake_complete(messages, **kwargs)
        return LLMResponse(text=replies[min(len(asked), len(replies)) - 1], provider="mock", model="mock-model",
                           usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    base = {"summary": "A todo API", "endpoints_implemented": ["GET /api/todos"], "files": files}
    ctx = AgentContext(idea="A todo API", base_output=base,
                       fixing=tuple(problems if problems is not None else _problems(files)))
    result = _Run(BackendEngineerAgent(), ctx).fix()
    return result, asked


# ── which files a round edits ────────────────────────────────────────────────
def test_an_import_of_a_name_the_module_lacks_edits_the_importer_and_the_module_together():
    own = {f["path"]: f["code"] for f in _files()}
    problems = _problems(_files())
    assert any("listTodos" in p["message"] for p in problems)
    found = fixscope.scope(problems, own, own)
    assert set(found.touch) == {"backend/server.js", "backend/services/todoService.js"}
    assert found.pairs == [("backend/server.js", "backend/services/todoService.js")]
    assert not found.new and not found.unlocated


def test_an_import_one_folder_off_edits_the_importer_and_writes_nothing_new():
    server = SERVER.replace("./services/todoService.js", "./todoService.js")
    own = {f["path"]: f["code"] for f in _files(**{"backend/server.js": server})}
    problems = _problems(_files(**{"backend/server.js": server}))
    found = fixscope.scope(problems, own, own)
    assert list(found.touch) == ["backend/server.js"] and not found.new
    [message] = [p.message for p in found.touch["backend/server.js"]]
    assert "This build has `backend/services/todoService.js`" in message


def test_an_import_of_a_file_nobody_wrote_writes_it():
    server = SERVER.replace("./services/todoService.js", "./routes/todos.js")
    own = {f["path"]: f["code"] for f in _files(**{"backend/server.js": server})}
    found = fixscope.scope(_problems(_files(**{"backend/server.js": server})), own, own)
    assert list(found.new) == ["backend/routes/todos.js"]
    # Writing the module answers the import: the importer isn't asked about it.
    assert "backend/server.js" not in found.touch


def test_a_package_that_wont_install_edits_the_files_that_import_it():
    own = {"backend/server.js": "import left from 'left-padz';\n", "backend/x.js": "export const x = 1;\n"}
    problems = [{"path": "backend/package.json", "kind": "package", "step": "install",
                 "message": "`left-padz` is not a package on npm. Import a real package, or write the code yourself."}]
    found = fixscope.scope(problems, own, own)
    assert list(found.touch) == ["backend/server.js"]


def test_a_stack_note_edits_the_file_it_names_and_nothing_names_is_left_to_ask():
    own = {"backend/db.js": "const pg = require('pg');\n", "backend/x.js": "export const x = 1;\n"}
    problems = [
        {"path": None, "kind": "stack", "message": "`backend/db.js` — uses PostgreSQL where the stack charter says MongoDB"},
        {"path": "backend/package.json", "kind": "build", "step": "build", "message": "npm run build failed. Its last lines:\nsegfault"},
    ]
    found = fixscope.scope(problems, own, own)
    assert list(found.touch) == ["backend/db.js"]
    assert [p["message"] for p in found.unlocated] == [problems[1]["message"]]


# ── a round edits, and keeps everything else ─────────────────────────────────
def test_a_fix_round_edits_only_what_the_problems_name_and_keeps_the_rest_byte_for_byte(monkeypatch, stub_router):
    files = _files()
    reply = fenced({"backend/services/todoService.js": FIXED_SERVICE, "backend/server.js": SERVER})
    result, asked = _fix(monkeypatch, files, [reply])

    # One call, no plan: the importer and the module it disagrees with, shown whole.
    assert [a["schema"] for a in asked] == [False]
    assert "# Write now — fix 2 files" in asked[0]["text"]
    assert "# Your last version of backend/server.js" in asked[0]["text"]
    assert "# Your last version of backend/services/todoService.js" in asked[0]["text"]
    assert "listTodos" in asked[0]["text"]

    out = result.output
    assert [f["path"] for f in out["files"]] == [f["path"] for f in files]
    assert out["endpoints_implemented"] == ["GET /api/todos"] and out["summary"] == "A todo API"
    code = {f["path"]: f["code"] for f in out["files"]}
    assert code["backend/services/todoService.js"] == FIXED_SERVICE
    for f in files[2:]:
        assert code[f["path"]] == f["code"]
    assert result.build_status == "ok"
    record = result.handoff["fix"]
    assert record["changed"] == ["backend/services/todoService.js"] and record["kept"] == 3
    assert result.handoff["generation"]["mode"] == "fix"


def test_an_edit_that_leaves_its_file_worse_is_put_back(monkeypatch, stub_router):
    files = _files()
    worse = SERVER.replace("import express from 'express';\n", "import express from 'express';\nimport { a } from './nowhere.js';\nimport { b } from './gone.js';\n")
    reply = fenced({"backend/services/todoService.js": SERVICE, "backend/server.js": worse})
    result, _asked = _fix(monkeypatch, files, [reply])
    code = {f["path"]: f["code"] for f in result.output["files"]}
    assert code["backend/server.js"] == SERVER
    assert result.handoff["fix"]["reverted"] == ["backend/server.js"]


def test_a_file_too_long_to_show_whole_is_left_as_it_is_and_no_call_is_made(monkeypatch, stub_router):
    huge = SERVER + "".join(f"// line {i} of a very long file\n" for i in range(4000))
    files = _files(**{"backend/server.js": huge, "backend/services/todoService.js": huge.replace("listTodos", "x")})
    problems = [{"path": "backend/server.js", "line": 2, "kind": "reference",
                 "message": "uses `listTodos`, which is never imported or declared in this file."}]
    result, asked = _fix(monkeypatch, files, ["unused"], problems=problems)
    assert asked == []
    assert result.handoff["fix"]["too_long"] == ["backend/server.js"]
    assert [f["code"] for f in result.output["files"]] == [f["code"] for f in files]
    assert result.response.model == "mock-model"


def test_a_missing_file_is_asked_for_new_with_who_needs_it(monkeypatch, stub_router):
    server = SERVER.replace("./services/todoService.js", "./routes/todos.js")
    files = _files(**{"backend/server.js": server})

    def reply_for(text: str) -> str:
        wanted = asked_files([type("M", (), {"content": text})()])
        return fenced({p: ("export function listTodos() {\n  return [];\n}\n" if p.endswith("todos.js") else server)
                       for p in wanted})

    asked: list[str] = []

    def complete(messages, **kwargs):
        if "A todo API" not in messages[-1].content:
            return _fake_complete(messages, **kwargs)
        asked.append(messages[-1].content)
        return LLMResponse(text=reply_for(messages[-1].content), provider="mock", model="mock-model",
                           usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    ctx = AgentContext(idea="A todo API", base_output={"files": files}, fixing=tuple(_problems(files)))
    result = _Run(BackendEngineerAgent(), ctx).fix()
    new = [a for a in asked if "# Write now — add 1 file" in a]
    assert new and "- `backend/routes/todos.js` (new file)" in new[0] and "needed by: `backend/server.js`" in new[0]
    assert result.handoff["fix"]["added"] == ["backend/routes/todos.js"]
    assert result.build_status == "ok"


# ── through the loop ─────────────────────────────────────────────────────────
def test_the_loop_fixes_in_place_and_never_plans_again(client, monkeypatch):
    calls = {"plan": 0, "write": [], "fix": []}
    good_main = "from fastapi import FastAPI\nfrom app.services import total\napp = FastAPI()\n"

    def fake(messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith("You are the Backend Engineer"):
            return resp
        if getattr(kwargs.get("options"), "json_schema", None):
            calls["plan"] += 1
            payload = json.loads(resp.text)
            payload["files"] = [{"path": "main.py", "purpose": "app"}, {"path": "app/services.py", "purpose": "totals"},
                                {"path": "app/models.py", "purpose": "models"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        text = messages[-1].content
        wanted = asked_files(messages)
        (calls["fix"] if "— fix " in text else calls["write"]).append(wanted)
        fixing = "# Your last version of" in text and len(calls["fix"]) > 1
        code = {
            "backend/main.py": good_main if fixing else "from fastapi import FastAPI\napp = FastAPI(\n",
            "backend/app/services.py": "def total(xs):\n    return sum(xs)\n",
            "backend/app/models.py": "class Item:\n    price = 0\n",
        }
        return resp.model_copy(update={"text": fenced({p: code[p] for p in wanted})})

    stub(monkeypatch, "complete", fake)
    pid = client.post("/api/projects", json={"idea": "A totals API", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = through_database_gate(client, pid)
    assert project["status"] == "completed", project["gate_note"]
    assert calls["plan"] == 1
    # Every fix call after the first write asked for main.py alone.
    assert all(w == ["backend/main.py"] for w in calls["fix"])
    backend = next(p for p in project["phases"] if p["phase"] == "backend_engineer" and p["status"] != "rejected")
    assert backend["handoff"]["generation"]["mode"] == "fix"
    assert backend["handoff"]["fix"]["changed"] == ["backend/main.py"]
    code = {f["path"]: f["code"] for f in backend["output"]["files"]}
    assert code["backend/app/services.py"] == "def total(xs):\n    return sum(xs)\n"
    assert [f["path"] for f in backend["output"]["files"]] == ["backend/main.py", "backend/app/services.py",
                                                                "backend/app/models.py"]


def test_an_edit_that_breaks_a_file_it_didnt_touch_is_put_back(monkeypatch, stub_router):
    uses = "import { getTodos } from '../services/todoService.js';\n\nexport default function health(req, res) {\n  res.json({ n: getTodos().length });\n}\n"
    files = _files(**{"backend/routes/health.js": uses})
    # The module gains listTodos and loses getTodos, which health.js imports.
    moved = "export function listTodos() {\n  return [];\n}\n"
    result, _asked = _fix(monkeypatch, files, [fenced({"backend/services/todoService.js": moved, "backend/server.js": SERVER})])
    code = {f["path"]: f["code"] for f in result.output["files"]}
    assert code["backend/services/todoService.js"] == SERVICE
    assert result.handoff["fix"]["reverted"] == ["backend/services/todoService.js"]


# ── review of #109 ───────────────────────────────────────────────────────────
def test_a_same_named_file_counts_only_when_it_ends_in_the_import_s_own_path():
    own = {
        "backend/src/services/api.js": "import { get } from './client/api';\nexport const x = get;\n",
        "backend/src/routes/api.js": "export default function router() {}\n",
    }
    found = fixscope.scope(_problems([{"path": p, "code": c} for p, c in own.items()]), own, own)
    # routes/api.js is not the `./client/api` the service meant: the module is written.
    assert list(found.new) == ["backend/src/services/client/api.js"]


def test_a_path_mention_is_a_whole_path():
    own = {"backend/index.ts": "export const a = 1;\n", "backend/x.ts": "export const b = 2;\n"}
    problems = [{"path": "frontend/package.json", "kind": "build", "step": "build",
                 "message": "npm run build failed. Its last lines:\nat src/index.tsx:3"}]
    assert not fixscope.scope(problems, own, own).touch


def _parse_case():
    broken = "import { listTodos } from './services/todoService.js'\nconst x = (;\n"
    files = _files(**{"backend/server.js": broken})
    # It parses now, and the checker can see the import of a name the module lacks.
    parses = "import { listTodos } from './services/todoService.js';\nimport { nope } from './models/todo.js';\nexport const x = listTodos;\n"
    return files, parses


def test_a_parse_fix_is_never_called_worse(monkeypatch, stub_router):
    files, parses = _parse_case()
    problems = [p for p in _problems(files) if p["path"] == "backend/server.js"]
    assert [p["kind"] for p in problems] == ["syntax"]
    result, _asked = _fix(monkeypatch, files, [fenced({"backend/server.js": parses})], problems=problems)
    code = {f["path"]: f["code"] for f in result.output["files"]}
    assert code["backend/server.js"] == parses and not result.handoff["fix"]["reverted"]


def test_an_importer_and_its_module_are_put_back_together(monkeypatch, stub_router):
    files = _files()
    # The call renames the module's export and follows it in the importer — and the
    # module also gains a broken import, so it is worse and goes back. The importer,
    # which now imports the new name, goes back with it.
    module = "import { gone } from './nowhere.js';\nexport function allTodos() {\n  return gone;\n}\n"
    server = SERVER.replace("listTodos", "allTodos")
    result, _asked = _fix(monkeypatch, files, [fenced({"backend/services/todoService.js": module, "backend/server.js": server})])
    code = {f["path"]: f["code"] for f in result.output["files"]}
    assert code["backend/services/todoService.js"] == SERVICE and code["backend/server.js"] == SERVER
    assert sorted(result.handoff["fix"]["reverted"]) == ["backend/server.js", "backend/services/todoService.js"]


def test_an_uncapped_check_counts_every_problem_in_a_file():
    from app.build.check import check_tree

    code = "".join(f"import {{ n{i} }} from './missing{i}.js';\n" for i in range(8))
    tree = {"backend/a.js": code}
    assert len(check_tree(tree, ["backend/a.js"]).problems) == 5
    assert len(check_tree(tree, ["backend/a.js"], cap=False).problems) == 8


def test_a_round_that_sent_the_model_nothing_is_not_a_verdict():
    from app.orchestration import autofix

    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    autofix.start_round(t, "guided", ["backend_engineer"], [{"key": "a"}])
    autofix.close_round(t, ["a"])
    t["rounds"][-1]["nothing_sent"] = True
    assert autofix.next_step(t) == "fix"


# ── second review of #109 ────────────────────────────────────────────────────
def test_an_edit_that_stops_a_file_parsing_is_put_back_though_it_hides_its_other_problems(monkeypatch, stub_router):
    files = _files()
    unbalanced = SERVER.replace("app.listen(3000);", "app.listen(3000;")
    result, _asked = _fix(monkeypatch, files, [fenced({"backend/services/todoService.js": SERVICE, "backend/server.js": unbalanced})])
    code = {f["path"]: f["code"] for f in result.output["files"]}
    assert code["backend/server.js"] == SERVER and "backend/server.js" in result.handoff["fix"]["reverted"]


def test_a_planned_file_never_written_is_written_whatever_else_shares_its_name():
    own = {"backend/models/todos.js": "export const Todo = {};\n"}
    problems = [{"path": "backend/routes/todos.js", "kind": "missing",
                 "message": "was planned but never written. Write it, or drop it and every import of it."}]
    assert list(fixscope.scope(problems, own, own).new) == ["backend/routes/todos.js"]


def test_a_dotted_module_name_gets_a_real_extension():
    own = {"frontend/src/login.ts": "import { login } from './auth.service';\nexport const go = login;\n"}
    problems = [{"path": "frontend/src/login.ts", "kind": "import",
                 "message": "imports `./auth.service`, and no file in this build is at frontend/src/auth.service. "
                            "Write that file, or import one that exists."}]
    assert list(fixscope.scope(problems, own, own).new) == ["frontend/src/auth.service.ts"]


def test_a_python_package_is_never_shadowed_by_a_new_module():
    own = {"backend/app/routes.py": "from .models.user import User\n", "backend/app/models/__init__.py": ""}
    problems = [{"path": "backend/app/routes.py", "kind": "import",
                 "message": "imports `.models.user`, and no module is at backend/app/models.py."}]
    found = fixscope.scope(problems, own, own)
    # The module inside the package is what's missing, not a `models.py` beside it.
    assert list(found.new) == ["backend/app/models/user.py"]


def test_a_missing_python_subpackage_is_written_as_its_module():
    own = {"backend/app/main.py": "from .services.todo import create\n"}
    problems = [{"path": "backend/app/main.py", "kind": "import",
                 "message": "imports `.services.todo`, and no module is at backend/app/services.py."}]
    assert list(fixscope.scope(problems, own, own).new) == ["backend/app/services/todo.py"]


def test_a_missing_stylesheet_is_written_as_one_and_a_missing_image_is_the_importer_s():
    own = {"frontend/src/main.tsx": "import './index.css';\nimport logo from './logo.png';\n"}
    problems = [
        {"path": "frontend/src/main.tsx", "kind": "import",
         "message": "imports `./index.css`, and no file in this build is at frontend/src/index.css. Write that file, or import one that exists."},
        {"path": "frontend/src/main.tsx", "kind": "import",
         "message": "imports `./logo.png`, and no file in this build is at frontend/src/logo.png. Write that file, or import one that exists."},
    ]
    found = fixscope.scope(problems, own, own)
    assert list(found.new) == ["frontend/src/index.css"] and list(found.touch) == ["frontend/src/main.tsx"]


def test_the_trend_keeps_an_episode_that_converged():
    from types import SimpleNamespace

    from app.evals.scorecard import fix_rounds
    from app.orchestration import autofix

    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    t["allowed"] = 5
    for sent, left in ((3, 6), (6, 4), (4, 0)):
        autofix.start_round(t, "guided", ["backend_engineer"], [{"key": f"{sent}-{i}"} for i in range(sent)])
        autofix.close_round(t, [f"{left}-{i}" for i in range(left)])
    autofix.settle(t)
    assert fix_rounds(SimpleNamespace(auto_fix=data))["fix_trend"] == {"backend_engineer": [3, 6, 4, 0]}


# ── third review of #109 ─────────────────────────────────────────────────────
def test_a_file_sent_back_that_was_not_asked_for_is_not_landed(monkeypatch, stub_router):
    from app.agents.code_phase import _Run as Run

    files = [
        {"path": "backend/app/main.py", "language": "python", "code": "from .db import x\nprint(x)\n"},
        {"path": "backend/app/models.py", "language": "python", "code": "class User:\n    pass\n"},
        {"path": "backend/app/routes.py", "language": "python", "code": "from .models import User\nfrom .helpers import h\n"},
    ]
    problems = [p for p in _problems(files) if p["path"] == "backend/app/main.py"]
    # The reply fixes main.py and also rewrites models.py, which nobody asked for.
    reply = fenced({"backend/app/main.py": "print(1)\n", "backend/app/models.py": "class User(\n"})

    def complete(messages, **kwargs):
        if "A todo API" not in messages[-1].content:
            return _fake_complete(messages, **kwargs)
        return LLMResponse(text=reply, provider="mock", model="mock-model",
                           usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1)

    stub(monkeypatch, "complete", complete)
    run = Run(BackendEngineerAgent(), AgentContext(idea="A todo API", base_output={"files": files}, fixing=tuple(problems)))
    # Only main.py was asked for; models.py came back too, unasked, and is not landed.
    result = run.fix()
    code = {f["path"]: f["code"] for f in result.output["files"]}
    assert code["backend/app/models.py"] == files[1]["code"]


def test_nothing_is_called_worse_when_the_attempt_before_could_not_be_checked(monkeypatch, stub_router):
    from app.agents.base import BaseAgent
    from app.build.check import BuildCheck

    real = BaseAgent._build_check

    def flaky(self, ctx, output, previous=None, cap=True):
        if not cap and output is ctx.base_output:
            return BuildCheck(status="unchecked", reason="crashed")
        return real(self, ctx, output, previous, cap)

    monkeypatch.setattr(BaseAgent, "_build_check", flaky)
    files = _files()
    # Fixes the missing export and adds an unrelated problem to the module: with no
    # "before" to compare, the edit is kept rather than every edit undone.
    module = FIXED_SERVICE + "import { gone } from './nowhere.js';\n"
    result, _asked = _fix(monkeypatch, files, [fenced({"backend/services/todoService.js": module, "backend/server.js": SERVER})])
    assert not result.handoff["fix"]["reverted"]


def test_a_stack_note_on_a_renamed_root_finds_its_file(monkeypatch, stub_router):
    files = [{"path": "server/index.js", "language": "javascript", "code": "const pg = require('pg');\nmodule.exports = pg;\n"},
             {"path": "server/util.js", "language": "javascript", "code": "module.exports = { a: 1 };\n"}]
    problems = [{"path": None, "kind": "stack",
                 "message": "`server/index.js` — uses PostgreSQL where the stack charter says the database is MongoDB."}]
    reply = fenced({"backend/index.js": "const m = require('mongodb');\nmodule.exports = m;\n"})
    result, asked = _fix(monkeypatch, files, [reply], problems=problems)
    assert [a["schema"] for a in asked] == [False]  # placed, not left to the planner
    assert "# Your last version of" in asked[0]["text"]
