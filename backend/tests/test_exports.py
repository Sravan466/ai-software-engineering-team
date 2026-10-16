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
    assert ex.read("a.js", "exports.default = Thing;\n").default == ""


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
    check = check_tree(files, list(files), exports=True)
    assert check.exports["frontend/components/index.ts"].render() == "named: Button, TodoList · types: Item"
    assert check.exports["frontend/components/Button.tsx"].render() == "default: Button"
    assert check.exports["backend/main.py"].render() == "names: app"
    # The default-vs-named mistake is counted, and the module named as the page imports
    # it — never by the checker's in-memory path, which a model would copy.
    assert check.export_misses == 1
    [problem] = check.problems
    assert "has no default export" in problem.message and "/__build__" not in problem.message
    assert '"../components/TodoList"' in problem.message
    # Asked for only by the code phases: the scorecard's check doesn't pay for it.
    assert check_tree(files, list(files)).exports == {}


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
    # The code's names, and the planned one it left out, said rather than dropped.
    assert "Exports: names: total (planned, not exported: calculate_total)" in written


def test_a_repair_after_the_tree_check_sees_the_checkers_exports(monkeypatch):
    """`export *` the light reader can't follow, the checker can: the repair of a page
    importing a name the index doesn't have is told what the index really has."""
    from app.core.config import settings

    # The platform would mend this one itself since #103 (`TodoListView` is a near miss
    # of `TodoList`): this is about what a repair is shown when one runs.
    monkeypatch.setattr(settings, "platform_wiring_fixes", False)
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


# ── review of #105 ───────────────────────────────────────────────────────────
def test_the_names_a_regex_could_take_for_a_name_are_not_names():
    assert ex.read("a.tsx", "export default class extends React.Component {}\n").default == ""
    assert ex.read("a.ts", "export const enum Color { Red }\n").named == ["Color"]
    assert ex.read("a.ts", "export function *gen() {}\nexport function* gen2() {}\n").named == ["gen", "gen2"]


def test_a_package_offers_what_it_imports_and_names_inside_top_level_blocks_count():
    assert ex.read("app/models/__init__.py", "from app.models.user import User\n").named == ["User"]
    # Elsewhere an absolute import is the module's own business, not its exports.
    assert ex.read("app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n").named == ["app"]
    code = "try:\n    import orjson as json_lib\nexcept ImportError:\n    json_lib = None\nif True:\n    def helper():\n        return 1\n"
    assert ex.read("app/util.py", code).named == ["json_lib", "helper"]


def test_the_checker_names_dunder_exports_as_written():
    check = check_tree({"frontend/lib/x.ts": "export const __foo = 1;\n"}, ["frontend/lib/x.ts"], exports=True)
    assert check.exports["frontend/lib/x.ts"].named == ["__foo"]


def test_a_file_cut_off_before_it_ended_says_nothing_about_its_exports():
    from app.agents.code_phase import _Written

    cut = _Written("frontend/a.ts", "export function A() {}\nexport function B(", "typescript", "", complete=False)
    assert cut.real_exports() is None
    whole = _Written("frontend/a.ts", "export function A() {}\n", "typescript", "")
    assert whole.real_exports().named == ["A"]


def test_an_empty_read_falls_back_to_the_plan_in_the_written_list(monkeypatch):
    plan = [
        {"path": "components/Odd.tsx", "purpose": "odd", "exports": ["Odd"]},
        {"path": "app/page.tsx", "purpose": "home", "imports": ["components/Odd.tsx"]},
    ]
    model = Scripted("Frontend Engineer", plan, code={
        # Exports nothing a reader can see: what the plan said is the best there is.
        "frontend/components/Odd.tsx": "function Odd() {\n  return <p>odd</p>;\n}\n",
        "frontend/app/page.tsx": PAGE,
    })
    _run(monkeypatch, "frontend_engineer", model, SMALL)
    written = model.prompts[-1].split("# Written so far")[1].split("\n# ")[0]
    assert "`frontend/components/Odd.tsx` — odd. Exports: Odd" in written


# ── re-review of #105 ────────────────────────────────────────────────────────
def test_a_re_export_of_nothing_is_not_an_export():
    files = {
        "frontend/components/Button.tsx": "export const Button = () => <button/>;\n",
        "frontend/components/index.ts": "export { default } from './Button';\nexport { Button } from './Button';\n",
    }
    check = check_tree(files, list(files), exports=True)
    found = check.exports["frontend/components/index.ts"]
    assert found.default is None and found.named == ["Button"]


def test_comparisons_and_script_locals_are_not_exports():
    assert ex.read("a.js", "if (exports.x === 1) {}\nexports.y = 2;\n").named == ["y"]
    main = "def create():\n    return 1\n\nif __name__ == '__main__':\n    args = parse()\n    server = create()\n"
    assert ex.read("app/main.py", main).named == ["create"]


def test_a_long_list_says_how_many_more_and_namespaces_are_read():
    many = "".join(f"export const n{i} = {i};\n" for i in range(24))
    assert ex.read("a.ts", many).render().endswith("n19 (+4 more)")
    assert ex.read("a.ts", "export namespace Api { export const x = 1; }\n").named == ["Api"]


def test_a_planned_export_the_code_left_out_is_said_not_dropped(monkeypatch):
    plan = [
        {"path": "lib/hooks.ts", "purpose": "hooks", "exports": ["useTodos", "useFilter"]},
        {"path": "app/page.tsx", "purpose": "home", "imports": ["lib/hooks.ts"]},
    ]
    model = Scripted("Frontend Engineer", plan, code={
        "frontend/lib/hooks.ts": "export function useTodos() { return []; }\nfunction useFilter() { return 'all'; }\n",
        "frontend/app/page.tsx": "import { useTodos } from '../lib/hooks';\nexport default function Page() {\n  return <p>{useTodos().length}</p>;\n}\n",
    })
    _run(monkeypatch, "frontend_engineer", model, SMALL)
    written = model.prompts[-1].split("# Written so far")[1].split("\n# ")[0]
    assert "`frontend/lib/hooks.ts` — hooks. Exports: named: useTodos (planned, not exported: useFilter)" in written
