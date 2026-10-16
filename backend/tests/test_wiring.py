"""The platform mends import wiring itself, before any model round (#103).

Each case is the compile gate's own problem, mended by `app.build.wiring` and checked
again: the problem goes, and nothing new comes.
"""
from __future__ import annotations

from app.build import wiring
from app.build.check import check_phase, code_fixes


def _check(files: dict[str, str]):
    return check_phase({}, "backend_engineer", {"files": [{"path": p, "code": c} for p, c in files.items()]})


def _mend(files: dict[str, str]) -> tuple[dict[str, str], list, int, int]:
    """(files after, fixes, problems before, problems after)."""
    before = _check(files)
    out, made = wiring.mend(before.problems, files, files.keys(), before.exports)
    fixes = code_fixes(out, [p.path for p in before.problems if p.code in (2459,)])
    out, more = wiring.apply_code_fixes(out, fixes)
    after = _check(out)
    return out, made + more, len(before.problems), len(after.problems)


def test_a_relative_import_one_folder_off_is_pointed_at_the_file():
    files = {
        "backend/app/controllers/todo.js": "import { list } from './todo_service.js';\nexport const all = list;\n",
        "backend/app/services/todo_service.js": "export function list() {\n  return [];\n}\n",
    }
    out, made, before, after = _mend(files)
    assert "from '../services/todo_service.js'" in out["backend/app/controllers/todo.js"]
    assert [f.kind for f in made] == ["path"] and (before, after) == (1, 0)


def test_a_python_relative_import_one_package_off_is_pointed_at_the_module():
    files = {
        "backend/app/routes/items.py": "from .models import Item\n\nITEMS = [Item()]\n",
        "backend/app/models.py": "class Item:\n    name = ''\n",
    }
    out, made, before, after = _mend(files)
    assert "from ..models import Item" in out["backend/app/routes/items.py"] and (before, after) == (1, 0)


def test_a_named_import_of_a_default_export_becomes_a_default_import():
    files = {
        "backend/server.ts": "import { createApp } from './app';\nexport const app = createApp();\n",
        "backend/app.ts": "export default function createApp() {\n  return {};\n}\n",
    }
    out, made, before, after = _mend(files)
    assert "import createApp from './app'" in out["backend/server.ts"] and (before, after) == (1, 0)


def test_a_default_import_of_a_module_with_no_default_becomes_a_named_one():
    files = {
        "backend/server.ts": "import router from './routes';\nexport const r = router;\n",
        "backend/routes.ts": "export const router = { get() {} };\n",
    }
    out, made, before, after = _mend(files)
    assert "import { router } from './routes'" in out["backend/server.ts"] and (before, after) == (1, 0)


def test_require_destructuring_of_a_default_export_takes_the_export_itself():
    """The issue's own case: `main.js` destructured a name out of a module whose only
    export is its default — TypeScript's 2614, "did you mean `import getTodos from`"."""
    files = {
        "backend/main.js": "const { getTodos } = require('./routes/todos');\nmodule.exports = getTodos;\n",
        "backend/routes/todos.js": "export default function getTodos() {\n  return [];\n}\n",
    }
    assert [p.code for p in _check(files).problems] == [2614]
    out, made, n_before, n_after = _mend(files)
    assert "const getTodos = require('./routes/todos')" in out["backend/main.js"] and (n_before, n_after) == (1, 0)


def test_typescript_s_own_spelling_fix_is_applied():
    files = {
        "backend/server.ts": "import { getTodo } from './api';\nexport const g = getTodo;\n",
        "backend/api.ts": "export function getTodos() {\n  return [];\n}\n",
    }
    out, made, before, after = _mend(files)
    assert "getTodos" in out["backend/server.ts"].splitlines()[0] and (before, after) == (1, 0)


def test_typescript_s_not_exported_fix_adds_the_export_and_nothing_else():
    files = {
        "backend/server.ts": "import { helper } from './util';\nexport const h = helper;\n",
        "backend/util.ts": "function helper() {\n  return 1;\n}\nexport const other = 2;\n",
    }
    out, made, before, after = _mend(files)
    assert "export function helper" in out["backend/util.ts"] and (before, after) == (1, 0)
    assert out["backend/util.ts"].count("return 1;") == 1


def test_a_name_that_exists_nowhere_is_left_for_the_model():
    files = {
        "backend/server.ts": "import { nowhere } from './api';\nexport const n = nowhere;\n",
        "backend/api.ts": "export function list() {\n  return [];\n}\n",
    }
    out, made, before, after = _mend(files)
    assert not made and out == files


# ── in a real run ────────────────────────────────────────────────────────────
def test_a_build_s_wiring_is_mended_before_any_model_round(client, monkeypatch):
    import json

    from tests.conftest import _fake_complete, asked_files, fenced, stub, through_database_gate

    calls = {"fix": 0}
    code = {
        "backend/app/controllers/todo.js": "import { list } from './todo_service.js';\nexport const all = list;\n",
        "backend/app/services/todo_service.js": "export function list() {\n  return [];\n}\n",
    }

    def fake(messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith("You are the Backend Engineer"):
            return resp
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            payload["files"] = [{"path": "app/services/todo_service.js", "purpose": "service"},
                                {"path": "app/controllers/todo.js", "purpose": "controller"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        if "— fix " in messages[-1].content:
            calls["fix"] += 1
        return resp.model_copy(update={"text": fenced({p: code[p] for p in asked_files(messages) if p in code})})

    stub(monkeypatch, "complete", fake)
    pid = client.post("/api/projects", json={"idea": "A todo API in Express", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = through_database_gate(client, pid)
    backend = next(p for p in project["phases"] if p["phase"] == "backend_engineer" and p["status"] != "rejected")
    assert calls["fix"] == 0 and backend["build_status"] in ("ok", "unchecked")
    assert [w["kind"] for w in backend["handoff"]["wiring"]] == ["path"]
    files = {f["path"]: f["code"] for f in backend["output"]["files"]}
    assert "'../services/todo_service.js'" in files["backend/app/controllers/todo.js"]


def test_what_the_platform_fixed_is_counted_by_kind_on_the_round():
    from types import SimpleNamespace

    from app.orchestration import autofix

    row = SimpleNamespace(handoff={"wiring": [{"kind": "path"}, {"kind": "path"}, {"kind": "import"}]})
    assert autofix.platform_fixed(row) == {"paths": 2, "imports": 1}
    assert autofix.platform_fixed(SimpleNamespace(handoff=None)) == {}


# ── review of #111 ───────────────────────────────────────────────────────────
def test_two_files_asking_for_the_same_export_add_it_once():
    files = {
        "backend/a.ts": "import { helper } from './util';\nexport const a = helper;\n",
        "backend/b.ts": "import { helper } from './util';\nexport const b = helper;\n",
        "backend/util.ts": "function helper() {\n  return 1;\n}\nexport const other = 2;\n",
    }
    out, made, before, after = _mend(files)
    assert "export function helper" in out["backend/util.ts"] and "export export" not in out["backend/util.ts"]
    assert after == 0


def test_a_typescript_import_written_with_js_keeps_js():
    files = {
        "backend/src/controllers/todo.ts": "import { list } from './todo_service.js';\nexport const all = list;\n",
        "backend/src/services/todo_service.ts": "export function list() {\n  return [];\n}\n",
    }
    out, made, before, after = _mend(files)
    assert "from '../services/todo_service.js'" in out["backend/src/controllers/todo.ts"] and after == 0


# ── second review of #111 ────────────────────────────────────────────────────
def test_an_emoji_before_the_edit_doesn_t_move_it():
    files = {
        "backend/server.ts": "import { helper } from './util';\nexport const h = helper;\n",
        "backend/util.ts": "// 🚀🚀🚀 helpers\nfunction helper() {\n  return 1;\n}\nexport const other = 2;\n",
    }
    out, made, before, after = _mend(files)
    assert "\nexport function helper() {" in out["backend/util.ts"] and after == 0


def test_a_default_import_is_never_rebound_to_an_unrelated_export():
    files = {
        "backend/server.ts": "import db from './config';\nexport const d = db;\n",
        "backend/config.ts": "export const PORT = 3000;\n",
    }
    out, made, before, after = _mend(files)
    assert not made and out == files


def test_a_repointed_path_changes_the_import_and_nothing_else():
    files = {
        "backend/app/controllers/todo.js": (
            "import { list } from './todo_service.js';\n"
            "export const all = list;\nexport const where = './todo_service.js';\n"
        ),
        "backend/app/services/todo_service.js": "export function list() {\n  return [];\n}\n",
    }
    out, made, before, after = _mend(files)
    code = out["backend/app/controllers/todo.js"]
    assert "from '../services/todo_service.js'" in code and "where = './todo_service.js'" in code


def test_a_js_name_means_the_ts_source_only_to_typescript():
    from app.build.check import check_tree

    tree = {"backend/a.js": "const d = require('./db.js');\nmodule.exports = d;\n", "backend/db.ts": "export const x = 1;\n"}
    assert [p.kind for p in check_tree(tree, ["backend/a.js"]).problems] == ["import"]
    tree = {"backend/a.ts": "import { x } from './db.js';\nexport const y = x;\n", "backend/db.ts": "export const x = 1;\n"}
    assert check_tree(tree, ["backend/a.ts"]).problems == []
