"""A round after a put-back is told what the last one tried and what the checker said (#119).

When a round left more problems than it fixed, the loop put the earlier attempt back,
and the checker's answer ("no exported member `createTodoAnimation`") went with the
rejected attempt. Try again started from the copy, was told only "the previous attempt
did not fix these", and wrote the same wrong names again. Now the round record keeps
what the attempt showed, and the next round is shown it.
"""
from __future__ import annotations

import json

from app.orchestration import autofix
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_question_gates

CONTROLLER = (
    "const { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation } = require('./animations');\n"
    "exports.create = (req, res) => { createTodoAnimation(); res.json({}); };\n"
    "exports.update = (req, res) => { updateTodoAnimation(); res.json({}); };\n"
    "exports.remove = (req, res) => { deleteTodoAnimation(); res.json({}); };\n"
)
MAIN = "const express = require('express');\nconst todos = require('./controllers/todos');\nconst app = express();\napp.post('/api/todos', todos.create);\napp.listen(3000);\n"
WRONG = "function smoothAddTask() {}\nfunction smoothEditTask() {}\nmodule.exports = { smoothAddTask, smoothEditTask };\n"
RIGHT = (
    "function createTodoAnimation() {}\nfunction updateTodoAnimation() {}\nfunction deleteTodoAnimation() {}\n"
    "module.exports = { createTodoAnimation, updateTodoAnimation, deleteTodoAnimation };\n"
)
NEW = "backend/controllers/animations.js"


# ── the record ───────────────────────────────────────────────────────────────
def _track(result: str = "r1") -> dict:
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    autofix.start_round(t, "guided", ["backend_engineer"], [{"key": "a", "title": "x"}], attempt="r0")
    autofix.close_round(t, ["b", "c"], attempt=result, net=True)
    t["rounds"][-1]["learned"] = {
        "added": [NEW], "changed": [],
        "exports": {NEW: {"asked": ["createTodoAnimation"], "exported": ["smoothAddTask"]}},
        "found": [{"where": "backend/controllers/todos.js", "title": "Module './animations' has no exported member 'createTodoAnimation'."}],
    }
    # The loop put the attempt before any round back, as the copy c0.
    t["kept"] = {"attempt": "c0", "round": 0}
    return t


def test_the_last_try_says_what_was_written_against_what_was_needed():
    text = autofix.last_try(_track(), attempt="c0")  # c0: the copy put back
    assert text.startswith("The build went back to the attempt before any round (1 problem, against 2 after).")
    assert f"Round 1 added `{NEW}`, exporting `smoothAddTask`. Its importers need `createTodoAnimation`." in text
    # The checker's words first: a clipped section keeps them.
    assert text.index("has no exported member 'createTodoAnimation'") < text.index("Round 1 added")
    # Only for the attempt the loop put back: not the one the round left, not any other.
    assert autofix.last_try(_track(), attempt="r1") == "" and autofix.last_try(_track(), attempt="other") == ""
    # Once the episode settles, its history is no one's last try.
    settled = _track()
    autofix.settle(settled)
    assert autofix.last_try(settled, attempt="c0") == ""
    note = autofix.code_note([{"key": "a", "title": "x", "kind": "build"}], 2, last=text)
    assert "smoothAddTask" in note and "did not fix these" not in note and "change the files named above" in note


def test_what_a_round_learned_is_read_off_its_attempt():
    row = type("Row", (), {"handoff": {"fix": {"added": [NEW], "changed": ["backend/main.js"], "new_exports": {
        NEW: {"asked": ["createTodoAnimation"], "exported": ["smoothAddTask"], "missing": ["createTodoAnimation"]}}}}})()
    seen = autofix.learned(row, ["a"], [{"key": "a", "title": "old"}, {"key": "b", "where": "x.js", "title": "new one"}])
    assert seen["added"] == [NEW] and seen["changed"] == ["backend/main.js"]
    assert seen["exports"][NEW] == {"asked": ["createTodoAnimation"], "exported": ["smoothAddTask"]}
    assert seen["found"] == [{"where": "x.js", "title": "new one"}]  # only what it wasn't sent


# ── through the loop ─────────────────────────────────────────────────────────
class Crew:
    """The Backend Engineer writes the controller without its module; its first fix
    round writes the module with the wrong names (twice, so the repair can't save it);
    a later round writes it right only when shown the names the checker asked for."""

    def __init__(self) -> None:
        self.asks: list[str] = []

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith("You are the Backend Engineer"):
            return resp
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            if "files" in payload:
                payload["files"] = [{"path": "main.js", "purpose": "entry", "imports": ["./controllers/todos"]},
                                    {"path": "controllers/todos.js", "purpose": "todos"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        text = messages[-1].content
        wanted = asked_files(messages)
        code = {"backend/main.js": MAIN, "backend/controllers/todos.js": CONTROLLER}
        if NEW in wanted:
            self.asks.append(text)
            # Fix 2 switched off (#119's check): only the last try can say the names.
            code[NEW] = RIGHT if "# What the last try showed" in text and "createTodoAnimation" in text else WRONG
        return resp.model_copy(update={"text": fenced({p: code.get(p, CONTROLLER) for p in wanted})})


def test_a_try_again_after_a_put_back_is_shown_the_last_try_and_gets_it_right(client, monkeypatch):
    from app.build import wiring

    # #119's fix 2 off: the new file is told no names, as the issue's check asks.
    monkeypatch.setattr(wiring, "wanted_by", lambda target, files: [])
    crew = Crew()
    stub(monkeypatch, "complete", crew)
    pid = client.post("/api/projects", json={"idea": "A todo API with animations", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "needs_help"
    t = project["auto_fix"]["tracks"]["build:backend_engineer"]
    assert t["stopped"]["kept_best"]["round"] == 0  # round 1 broke more: put back
    assert t["rounds"][0]["learned"]["exports"][NEW]["exported"] == ["smoothAddTask", "smoothEditTask"]
    # No importer's names were read (fix 2 off): none are claimed as needed.
    assert t["rounds"][0]["learned"]["exports"][NEW]["asked"] == []
    assert "# What the last try showed" not in crew.asks[0]

    assert client.post(f"/api/projects/{pid}/auto-fix/retry", json={"rounds": 1}).status_code == 200
    project = through_question_gates(client, pid)
    retry = crew.asks[-1]
    assert "# What the last try showed" in retry and "exporting `smoothAddTask`, `smoothEditTask`" in retry
    assert "Its importers need `smoothAddTask`" not in retry
    assert "createTodoAnimation" in retry
    t = project["auto_fix"]["tracks"]["build:backend_engineer"]
    assert t["rounds"][-1]["after"] == 0  # this time it compiled


def test_from_round_2_the_import_lines_of_files_not_shown_whole_are():
    from types import SimpleNamespace

    from app.agents.code_phase import _Run, _Written

    run = SimpleNamespace(
        place_of={"backend/controllers/todos.js": "backend/controllers/todos.js", "backend/main.js": "backend/main.js"},
        written={"backend/controllers/todos.js": _Written("backend/controllers/todos.js", CONTROLLER + "\n// … 900 more lines\n", "javascript", ""),
                 "backend/main.js": _Written("backend/main.js", MAIN, "javascript", "")},
        ctx=SimpleNamespace(fixing=({"path": "backend/controllers/todos.js", "message": "x"}, {"path": "backend/main.js", "message": "y"})),
    )
    text = _Run._named_imports(run, shown={"backend/main.js"})
    assert "`backend/controllers/todos.js`:" in text and "require('./animations')" in text
    assert "exports.create" not in text and "backend/main.js" not in text  # import lines only; shown files aren't
    # A statement over several lines reads whole.
    from app.agents.code_phase import _import_statements

    assert _import_statements("import {\n  a,\n  b,\n} from './x';\nconst {\n  c,\n} = require('./y');\n") == [
        "import { a, b, } from './x'", "const { c, } = require('./y')"]
