"""#101: each write call sees the exports each written file really has.

A file planned as `export default TodoList` and written as `export function TodoList`
used to be described the way the plan said, and the next file imported it as a
default — TypeScript's 2613, the most common cross-file mistake a 7B model makes.
"""
from __future__ import annotations

import json

from app.build import exports as ex
from app.build.check import check_tree
from tests.test_code_phases import SMALL, Scripted, _run


# ── the light reader ─────────────────────────────────────────────────────────
def test_javascript_exports_are_read_from_the_code_not_its_strings_or_comments():
    code = (
        "export default function App() { return <div/>; }\n"
        "export function useTodos() {}\n"
        "export type Todo = { id: string };\n"
        "export { formatDate as format, type Filter } from './util';\n"
        "export * from './all';\n"
        "// export const commented = 1\n"
        "const s = 'export const quoted = 1';\n"
    )
    found = ex.read("frontend/app/page.tsx", code)
    assert found.default == "App"
    assert found.named == ["useTodos", "format"] and found.types == ["Todo", "Filter"]
    assert found.star == ["./all"]
    assert found.render() == "default: App · named: useTodos, format · types: Todo, Filter · everything from `./all`"
    assert found.labels() == ["App (default)", "useTodos", "format", "Todo (type)", "Filter (type)"]


def test_the_default_export_by_every_spelling():
    assert ex.read("a.jsx", "const B = () => <b/>;\nexport default B;\n").default == "B"
    assert ex.read("a.jsx", "export default () => <p/>;\n").default == ""
    assert ex.read("a.ts", "export { default } from './x';\n").default == ""
    assert ex.read("a.ts", "export { default as Button } from './Button';\n").named == ["Button"]
    assert ex.read("a.ts", "export const x = 1;\n").default is None
    assert ex.read("a.js", "module.exports = router;\n").default == "router"
    assert ex.read("a.js", "module.exports = { list, create };\nexports.extra = 1;\n").named == ["list", "create", "extra"]


def test_python_exports_are_the_public_top_level_names_or_all():
    assert ex.read("app/x.py", "def a(): pass\nclass B: pass\nX = 1\n_y = 2\n").named == ["a", "B", "X"]
    assert ex.read("app/x.py", "__all__ = ['create']\ndef create(): pass\ndef other(): pass\n").named == ["create"]
    assert ex.read("app/__init__.py", "from .models import User, Item\n").named == ["User", "Item"]
    assert ex.read("app/x.py", "def broken(:\n") is None
    assert ex.read("README.md", "# hi") is None


# ── the checker's own answer ─────────────────────────────────────────────────
def test_the_tree_check_returns_each_modules_exports_with_re_exports_followed():
    files = {
        "frontend/components/TodoList.tsx": "export function TodoList() { return <ul/>; }\nexport type Item = { id: string };\n",
        "frontend/components/index.ts": "export * from './TodoList';\nexport { default as Button } from './Button';\n",
        "frontend/components/Button.tsx": "const Button = () => <button/>;\nexport default Button;\n",
        "frontend/app/page.tsx": (
            "import TodoList from '../components/TodoList';\nimport { Button } from '../components';\n"
            "export default function Page() { return <main><TodoList/><Button/></main>; }\n"
        ),
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    }
    check = check_tree(files, list(files))
    assert check.exports["frontend/components/index.ts"].render() == "named: Button, TodoList · types: Item"
    assert check.exports["frontend/components/Button.tsx"].render() == "default: Button"
    assert check.exports["backend/main.py"].render() == "names: app"
    # The default-vs-named mistake is counted, and named by the build's own path.
    assert check.export_misses == 1
    [problem] = check.problems
    assert "has no default export" in problem.message and "/__build__" not in problem.message
    assert '"frontend/components/TodoList"' in problem.message


# ── what the next write call is told ─────────────────────────────────────────
TODO_LIST = "export function TodoList() {\n  return <ul></ul>;\n}\n"
PAGE = "import { TodoList } from '../components/TodoList';\n\nexport default function Page() {\n  return <main><TodoList /></main>;\n}\n"


def test_a_file_planned_as_default_and_written_as_named_is_shown_as_named(monkeypatch):
    """Acceptance: the plan said `TodoList (default)`; the code exports it by name. The
    page's write call — and the plan it is shown — say named, from the code."""
    plan = [
        {"path": "components/TodoList.tsx", "purpose": "the list", "exports": ["TodoList (default)"]},
        {"path": "app/page.tsx", "purpose": "home", "exports": ["Page"], "imports": ["components/TodoList.tsx"]},
    ]
    model = Scripted("Frontend Engineer", plan,
                     code={"frontend/components/TodoList.tsx": TODO_LIST, "frontend/app/page.tsx": PAGE})
    result = _run(monkeypatch, "frontend_engineer", model, SMALL)
    assert model.writes == [["frontend/components/TodoList.tsx"], ["frontend/app/page.tsx"]]
    page_call = model.prompts[-1]
    written = page_call.split("# Written so far")[1].split("\n# ")[0]
    assert "`frontend/components/TodoList.tsx` — the list. Exports: named: TodoList" in written
    plan_json = json.loads(page_call.split("# The plan\n```json\n")[1].split("\n```")[0])
    todo = next(f for f in plan_json["files"] if f["path"] == "frontend/components/TodoList.tsx")
    assert todo["exports"] == ["TodoList"]
    # The phase's record of its plan agrees with the code too.
    recorded = {p["path"]: p["exports"] for p in result.handoff["generation"]["plan"]}
    assert recorded["frontend/components/TodoList.tsx"] == ["TodoList"]
    assert recorded["frontend/app/page.tsx"] == ["Page (default)"]
    assert result.build_status == "ok"


def test_a_python_module_is_shown_with_its_real_names(monkeypatch):
    plan = [
        {"path": "app/services.py", "purpose": "totals", "exports": ["calculate_total"]},
        {"path": "main.py", "purpose": "entry", "imports": ["app/services.py"]},
    ]
    model = Scripted("Backend Engineer", plan, code={
        "backend/app/services.py": "def total(items):\n    return sum(items)\n",
        "backend/main.py": "from app.services import total\n\n\ndef main():\n    return total([1])\n",
    })
    _run(monkeypatch, "backend_engineer", model, SMALL)
    written = model.prompts[-1].split("# Written so far")[1].split("\n# ")[0]
    assert "Exports: names: total" in written and "calculate_total" not in written


def test_a_repair_after_the_tree_check_sees_the_checkers_exports(monkeypatch):
    """`export *` the light reader can't follow, the checker can: the repair of a page
    importing a name the index doesn't have is told what the index really has."""
    plan = [
        {"path": "components/TodoList.tsx", "purpose": "the list"},
        {"path": "components/index.ts", "purpose": "barrel", "imports": ["components/TodoList.tsx"]},
        {"path": "app/page.tsx", "purpose": "home", "imports": ["components/index.ts"]},
    ]
    broken = "import { TodoListView } from '../components';\nexport default function Page() {\n  return <TodoListView />;\n}\n"
    model = Scripted("Frontend Engineer", plan, code={
        "frontend/components/TodoList.tsx": TODO_LIST,
        "frontend/components/index.ts": "export * from './TodoList';\n",
        "frontend/app/page.tsx": broken,
    })
    _run(monkeypatch, "frontend_engineer", model, SMALL)
    repair = next(p for p in model.prompts if "fix 1 file" in p)
    assert "`frontend/components/index.ts` — barrel. Exports: named: TodoList" in repair
    assert "everything from" not in repair.split("# Written so far")[1].split("\n# ")[0]
