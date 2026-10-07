"""#76: QA's tests are run for real, and what the page shows is what they measured.

The sandbox needs Docker, so most of these run the test step against a fake engine
that answers with the condensed report the in-sandbox reader prints — the readers
themselves are run here too, on reports captured from real Jest and pytest runs. The
tests that start real containers are opt-in:
`BUILD_RUN_DOCKER_TESTS=1 pytest tests/test_test_runs.py -k docker`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Optional

import pytest

from app.build import runner as build_runner, sandbox, scaffold, testrun
from app.build.check import check_phase
from app.build.sandbox import StepResult
from app.core.config import settings
from app.core.constants import GateKind, SHIP_GATE_PHASE
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project
from app.orchestration import approval, autofix
from app.schemas.agent_outputs import QAEngineerOutput
from tests.conftest import _fake_complete, asked_files, fenced, through_question_gates


def _tree(files: dict[str, str]) -> dict[str, str]:
    out = dict(files)
    for f in scaffold.build(dict(files), None, None, "Demo").files:
        out[f.path] = f.content
    return out


def _pkg(files: dict[str, str], side: str) -> dict:
    return json.loads(files[f"{side}/package.json"])


# ── the scaffold sets the runner up ──────────────────────────────────────────
VITE_APP = {
    "frontend/src/main.jsx": "import React from 'react';\nimport App from './App';\n",
    "frontend/src/App.jsx": "export default function App() { return <h1>Hello</h1>; }\n",
    "frontend/index.html": "<div id=\"root\"></div>\n",
}


def test_a_vite_app_with_tests_gets_vitest_run_and_a_config():
    files = _tree({**VITE_APP, "frontend/src/__tests__/App.test.jsx": "import App from '../App';\ntest('x', () => {});\n"})
    pkg = _pkg(files, "frontend")
    assert pkg["scripts"]["test"] == "vitest run"
    assert {"vitest", "@vitest/coverage-v8", "jsdom", "@testing-library/jest-dom"} <= set(pkg["devDependencies"])
    config = files["frontend/vitest.config.js"]
    assert "environment: 'jsdom'" in config and "globals: true" in config and "vitest.setup.js" in config
    assert "src/**" in config  # coverage of the app's own code, tested or not
    assert "jest-dom/vitest" in files["frontend/vitest.setup.js"]
    assert scaffold.platform_owned("frontend/vitest.config.js")


def test_a_vite_app_without_tests_gets_no_test_runner():
    files = _tree(VITE_APP)
    assert "test" not in _pkg(files, "frontend")["scripts"]
    assert "frontend/vitest.config.js" not in files and "frontend/jest.config.js" not in files


def test_next_jest_measures_coverage_over_the_apps_own_files():
    files = _tree({
        "frontend/app/page.jsx": "export default function Home() { return <main />; }\n",
        "frontend/__tests__/page.test.jsx": "import Home from '../app/page';\ntest('x', () => {});\n",
    })
    config = files["frontend/jest.config.js"]
    assert "collectCoverageFrom" in config and "jest.setup.js" in config
    assert _pkg(files, "frontend")["scripts"]["test"] == "jest"


def test_a_typescript_next_suite_has_jests_types_so_next_build_passes():
    files = _tree({
        "frontend/app/page.tsx": "export default function Home() { return <main />; }\n",
        "frontend/__tests__/page.test.tsx": "import Home from '../app/page';\ntest('x', () => { expect(Home).toBeTruthy(); });\n",
    })
    assert "@types/jest" in _pkg(files, "frontend")["devDependencies"]


@pytest.mark.parametrize(
    "test, runner",
    [
        ("import { describe, it, expect } from 'vitest';\ndescribe('a', () => { it('b', () => {}); });\n", "vitest"),
        ("const { expect } = require('chai');\ndescribe('a', () => { it('b', () => {}); });\n", "mocha"),
        ("describe('a', () => { it('b', () => { expect(1).toBe(1); }); });\n", "jest"),
        ("const { expect } = require('chai');\nconst f = jest.fn();\ntest('b', () => {});\n", "jest"),
    ],
)
def test_a_node_backend_gets_the_runner_its_tests_import(test, runner):
    files = _tree({
        "backend/server.js": "const express = require('express');\nmodule.exports = express();\n",
        "backend/tests/app.test.js": test,
    })
    pkg = _pkg(files, "backend")
    assert runner in pkg["scripts"]["test"]
    assert runner in pkg["devDependencies"]
    if runner == "mocha":
        # The test files themselves — a folder would load the app's own modules too —
        # and --exit, so an open server can't hang the run.
        assert pkg["scripts"]["test"] == "mocha --exit 'tests/app.test.js'"
    if runner == "jest":
        assert "jest" not in pkg  # one config: a file QA's own copy can't sit beside
        assert "collectCoverageFrom" in files["backend/jest.config.cjs"]


def test_qas_own_jest_config_is_replaced_not_kept_beside_the_platforms():
    from app.build.scaffold import platform_owned, superseded

    assert platform_owned("backend/jest.config.js")
    assert superseded("backend/jest.config.js", {"backend/jest.config.cjs"})


def test_a_vite_suite_written_for_jest_finds_jest_fn():
    files = _tree({**VITE_APP, "frontend/src/__tests__/App.test.jsx": "test('x', () => { jest.fn(); });\n"})
    setup = files["frontend/vitest.setup.js"]
    assert "globalThis.jest = vi" in setup and "jest-dom/vitest" in setup


def test_an_es_module_backend_runs_jest_behind_nodes_flag():
    files = _tree({
        "backend/server.js": "import express from 'express';\nexport default express();\n",
        "backend/tests/app.test.js": "import app from '../server.js';\ntest('x', () => {});\n",
    })
    assert _pkg(files, "backend")["scripts"]["test"] == "NODE_OPTIONS=--experimental-vm-modules jest"


def test_a_python_backend_with_tests_gets_pytest_ini_and_the_runners_plugins():
    files = _tree({
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "backend/tests/test_api.py": "from main import app\n\nasync def test_ok():\n    assert app\n",
    })
    ini = files["backend/pytest.ini"]
    assert "testpaths = tests" in ini and "asyncio_mode = auto" in ini and "pythonpath = ." in ini
    reqs = files["backend/requirements.txt"]
    for name in ("pytest", "pytest-cov", "pytest-json-report", "pytest-asyncio"):
        assert f"\n{name}" in reqs
    # Not a package the agents are told they may import.
    from app.build import packages as pkg

    assert "pytest-cov" not in pkg.pip_vocabulary(include_tests=True)
    assert "jsdom" not in pkg.npm_vocabulary(include_server=True, include_tests=True).split(", ")


def test_sync_tests_outside_tests_dir_get_no_testpaths_or_asyncio():
    files = _tree({
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "backend/test_main.py": "def test_ok():\n    assert True\n",
    })
    ini = files["backend/pytest.ini"]
    assert "testpaths" not in ini and "asyncio_mode" not in ini
    assert "pytest-asyncio" not in files["backend/requirements.txt"]


# ── no invented numbers ──────────────────────────────────────────────────────
def test_the_models_coverage_guess_is_accepted_and_never_kept():
    payload = {"summary": "s", "test_strategy": "t", "edge_cases": [], "risks": [], "test_files": []}
    for name in ("coverage_estimate", "estimated_coverage", "estimatedCoverage"):
        out = QAEngineerOutput.model_validate({**payload, name: "85%"}).model_dump()
        assert name not in out and "coverage_estimate" not in out


def test_a_model_cannot_report_its_own_tests_green():
    payload = {"summary": "s", "test_strategy": "t", "edge_cases": [], "risks": [], "test_files": [],
               "test_run": {"status": "ok", "passed": 99}}
    assert "test_run" not in QAEngineerOutput.model_validate(payload).model_dump()


def test_qa_is_not_asked_to_estimate_coverage_and_is_told_its_tests_run():
    from app.agents import get_agent

    qa = get_agent("qa_engineer")
    assert "coverage" not in qa.output_spec
    assert "can't run counts against you" in qa.task_instruction()
    from app.build import contract

    assert "compiled and your tests are run" in contract.prompt_block("qa_engineer")
    assert "your tests are run" not in contract.prompt_block("backend_engineer")


# ── reading what a runner reported ───────────────────────────────────────────
def _report(**kw) -> str:
    base = {"framework": "jest", "ran": True, "passed": 0, "failed": 0, "errored": 0, "skipped": 0,
            "failures": [], "suites": [], "coverage": None}
    return f"PASS __tests__/a.test.js\n{testrun.MARK}{json.dumps({**base, **kw})}\n"


def _results(test_output: str, code: int = 1, framework: str = "jest") -> list[StepResult]:
    install = [StepResult("install", "npm install", 0, 3.0, "added 12 packages in 3s\n")]
    if framework == "pytest":
        install = [StepResult("install", "pip install", 0, 3.0, "Successfully installed pytest\n")]
    return install + [StepResult("test", f"{framework} (tests)", code, 2.0, test_output)]


def _plan(framework: str = "jest") -> testrun.TestPlan:
    image = sandbox.PYTHON_IMAGE if framework == "pytest" else sandbox.NODE_IMAGE
    return testrun.TestPlan(framework, image, [], "requirements.txt" if framework == "pytest" else "package.json")


def test_one_pass_one_fail_and_measured_coverage_are_read():
    out = _report(passed=1, failed=1, coverage={"lines_pct": 61.5, "branches_pct": 40},
                  failures=[{"path": "/work/__tests__/sum.test.js", "name": "sum adds two numbers",
                             "message": "\x1b[31mError: expect(received).toBe(expected)\n\nExpected: 3\nReceived: -1\n"
                                        "    at Object.<anonymous> (/work/__tests__/sum.test.js:4:21)", "line": None}])
    run = testrun.judge(_plan(), _results(out), "frontend", ["frontend/__tests__/sum.test.js"])
    assert (run.status, run.passed, run.failed) == ("failed", 1, 1)
    assert run.coverage.lines_pct == 61.5 and run.coverage.tool == "jest"
    [f] = run.failures
    assert f.path == "frontend/__tests__/sum.test.js" and f.line == 4 and f.kind == "assertion"
    assert "Expected: 3" in f.message and "\x1b" not in f.message and " at " not in f.message
    assert run.summary() == "1 passed · 1 failed · 62% lines (jest, 5 s)"


def test_a_suite_that_never_ran_is_failed_with_a_problem_for_qa_not_unchecked():
    out = _report(passed=0, errored=1, suites=[{
        "path": "tests/test_api.py",
        "message": "ImportError while importing test module '/work/tests/test_api.py'.\n"
                   "tests/test_api.py:1: in <module>\n    from app.missing import thing\n"
                   "E   ModuleNotFoundError: No module named 'app.missing'",
    }])
    run = testrun.judge(_plan("pytest"), _results(out, 2, "pytest"), "backend", ["backend/tests/test_api.py"])
    assert run.status == "failed"
    [p] = run.problems
    assert p.path == "backend/tests/test_api.py" and p.step == "test" and p.kind == "test"
    assert "No module named 'app.missing'" in p.message


def test_a_runner_that_crashed_before_any_test_is_failed():
    run = testrun.judge(_plan(), _results("Error: Cannot find module 'next/jest'\n"), "frontend", ["frontend/__tests__/a.test.js"])
    assert run.status == "failed" and run.problems and "crashed before any test ran" in run.problems[0].message
    assert run.summary().startswith("Couldn't run:")


def test_no_tests_found_is_a_failure():
    run = testrun.judge(_plan(), _results(_report()), "frontend", ["frontend/__tests__/a.test.js"])
    assert run.status == "failed" and "found no tests" in run.problems[0].message


def test_a_test_that_reached_for_the_network_is_the_tests_to_fix():
    out = _report(passed=1, failed=1, failures=[{"path": "/work/tests/api.test.js", "name": "calls the API",
                                                 "message": "Error: connect ECONNREFUSED 127.0.0.1:5432"}])
    run = testrun.judge(_plan(), _results(out), "backend", ["backend/tests/api.test.js"])
    assert run.failures[0].kind == "environment"


def test_an_unreachable_registry_is_not_run_not_failed():
    results = [StepResult("install", "pip install", 1, 3.0, "ERROR: Could not find a version (Temporary failure in name resolution)")]
    run = testrun.judge(_plan("pytest"), results, "backend", ["backend/tests/test_a.py"])
    assert run.status == "not_run" and "registry" in run.reason and run.summary().startswith("Not run:")


def test_mocha_says_why_coverage_was_not_measured():
    out = _report(framework="mocha", passed=2)
    run = testrun.judge(_plan("mocha"), _results(out, 0), "backend", ["backend/tests/a.test.js"])
    assert run.status == "ok" and run.coverage is None and "Mocha" in run.reason
    assert "coverage not measured" in run.summary()


def test_runs_combine_into_one_record():
    ok = testrun.TestRun(status="ok", side="backend", framework="pytest", passed=3)
    bad = testrun.TestRun(status="failed", side="frontend", framework="jest", passed=2, failed=1)
    record = testrun.combine([ok, bad])
    assert record["status"] == "failed" and record["passed"] == 5 and record["failed"] == 1
    assert record["summary"] == "5 passed · 1 failed across 2 suites"
    assert testrun.combine([])["status"] == "not_run"


# ── the readers that run inside the box ──────────────────────────────────────
JEST_RESULTS = {
    "numPassedTests": 1, "numFailedTests": 1, "numPendingTests": 0, "numTodoTests": 0,
    "testResults": [
        {"name": "/work/__tests__/sum.test.js", "status": "failed", "message": "", "assertionResults": [
            {"fullName": "sum handles zero", "status": "passed", "failureMessages": []},
            {"fullName": "sum adds two numbers", "status": "failed", "location": {"line": 4, "column": 5},
             "failureMessages": ["Error: expect(received).toBe(expected)\n\nExpected: 3\nReceived: -1"]},
        ]},
        {"name": "/work/__tests__/broken.test.js", "status": "failed",
         "message": "Test suite failed to run\n\nCannot find module '../lib/nope'", "assertionResults": []},
    ],
}
JEST_COVERAGE = {"total": {"lines": {"total": 9, "covered": 2, "pct": 22.22},
                           "branches": {"total": 2, "covered": 0, "pct": 0}}}
PYTEST_REPORT = {
    "summary": {"passed": 1, "failed": 1, "error": 1, "total": 3, "collected": 2},
    "tests": [
        {"nodeid": "tests/test_api.py::test_health_is_ok", "lineno": 7, "outcome": "passed"},
        {"nodeid": "tests/test_api.py::test_creating_a_todo_returns_201", "lineno": 11, "outcome": "failed",
         "call": {"outcome": "failed", "crash": {"path": "/work/tests/test_api.py", "lineno": 13,
                                                 "message": "assert 201 == 200"},
                  "longrepr": "def test_creating_a_todo_returns_201():\n>       assert 1 == 2\nE       assert 201 == 200"}},
    ],
    "collectors": [{"nodeid": "tests/test_broken.py", "outcome": "failed",
                    "longrepr": "E   ModuleNotFoundError: No module named 'app'"}],
}
PYTEST_COVERAGE = {"totals": {"covered_lines": 9, "num_statements": 12, "covered_branches": 1, "num_branches": 2}}


def _read(cmd: list[str], *paths) -> dict:
    out = subprocess.run(cmd + [str(p) for p in paths], capture_output=True, text=True, timeout=60)
    line = next(l for l in out.stdout.splitlines() if l.startswith(testrun.MARK))
    return json.loads(line[len(testrun.MARK):])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_node_reader_condenses_a_jest_report(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps(JEST_RESULTS))
    (tmp_path / "c.json").write_text(json.dumps(JEST_COVERAGE))
    (tmp_path / "read.cjs").write_text(testrun._NODE_READER)
    got = _read(["node", str(tmp_path / "read.cjs"), "jest"], tmp_path / "r.json", tmp_path / "c.json")
    assert (got["passed"], got["failed"], got["errored"]) == (1, 1, 1)
    assert got["failures"][0]["line"] == 4 and got["suites"][0]["path"].endswith("broken.test.js")
    assert got["coverage"] == {"lines_pct": 22.22, "branches_pct": 0}


def test_the_python_reader_condenses_a_pytest_report(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps(PYTEST_REPORT))
    (tmp_path / "c.json").write_text(json.dumps(PYTEST_COVERAGE))
    (tmp_path / "read.py").write_text(testrun._PY_READER)
    got = _read([sys.executable, str(tmp_path / "read.py")], tmp_path / "r.json", tmp_path / "c.json")
    assert (got["passed"], got["failed"], got["errored"]) == (1, 1, 1)
    assert got["failures"][0]["line"] == 13 and got["suites"][0]["path"] == "tests/test_broken.py"
    assert got["coverage"] == {"lines_pct": 75.0, "branches_pct": 50.0}


def test_a_reader_with_nothing_to_read_says_nothing_ran(tmp_path):
    (tmp_path / "read.py").write_text(testrun._PY_READER)
    got = _read([sys.executable, str(tmp_path / "read.py")], tmp_path / "missing.json", tmp_path / "nope.json")
    assert got["ran"] is False


# ── a pytest file importing a module the backend never wrote ─────────────────
def test_a_test_importing_a_module_nobody_wrote_is_qas_problem_not_unchecked():
    prior = {"backend_engineer": {"files": [{"path": "backend/main.py", "code": "from fastapi import FastAPI\napp = FastAPI()\n"}]}}
    qa = {"test_files": [{"path": "backend/tests/test_api.py", "framework": "pytest", "targets": "",
                          "code": "from app.missing import thing\n\ndef test_x():\n    assert thing\n"}]}
    check = check_phase(prior, "qa_engineer", qa, None)
    assert check.status == "failed"
    assert any(p.path == "backend/tests/test_api.py" and p.kind in ("import", "package") for p in check.problems)


# ── no runner here ───────────────────────────────────────────────────────────
def test_no_runner_is_not_run_with_the_reason(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", None)
    monkeypatch.setattr(settings, "build_runner_url", "")
    monkeypatch.setattr(sandbox, "available", lambda refresh=False: (False, "Docker isn't installed on the computer running the backend.", None))
    files = _tree({"backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
                   "backend/tests/test_a.py": "def test_a():\n    assert True\n"})
    run = build_runner.run_tests(files, "backend")
    assert run.status == "not_run" and "Docker isn't installed" in run.reason
    assert run.summary().startswith("Not run:") and "%" not in run.summary()


def test_a_side_with_no_runner_set_up_fails_without_blaming_qa(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", SimpleNamespace(kind="docker", run=lambda *a, **k: []))
    files = {"frontend/package.json": json.dumps({"name": "x", "scripts": {"build": "nuxt build"}}),
             "frontend/__tests__/a.test.js": "test('x', () => {});\n"}
    run = build_runner.run_tests(files, "frontend")
    # Never a pass (constraint 4) — and nothing QA could change, so nothing sent to QA.
    assert run.status == "failed" and "no test runner" in run.reason and run.problems == []


def test_a_test_that_throws_from_its_own_body_is_qas_to_fix():
    out = _report(failed=2, failures=[
        {"path": "/work/__tests__/a.test.js", "name": "renders",
         "message": "ReferenceError: render is not defined\n    at Object.<anonymous> (/work/__tests__/a.test.js:5:3)"},
        {"path": "/work/__tests__/a.test.js", "name": "adds",
         "message": "ReferenceError: total is not defined\n    at sum (/work/lib/sum.js:2:10)\n"
                    "    at Object.<anonymous> (/work/__tests__/a.test.js:9:3)"},
    ])
    run = testrun.judge(_plan(), _results(out), "frontend", ["frontend/__tests__/a.test.js"])
    # The first never imported `render`; the second hit a bug in the code it tests.
    assert [f.kind for f in run.failures] == ["error", "assertion"]
    routed = autofix.route_tests({"rounds": []}, autofix.test_failures(testrun.combine([run])))
    assert sorted(p["phase"] for p in routed) == ["frontend_engineer", "qa_engineer"]


def test_a_suite_in_the_wrong_language_fails_when_a_runner_is_here(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", SimpleNamespace(kind="docker", run=lambda *a, **k: []))
    files = _tree({"backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    files["backend/tests/app.test.js"] = "test('x', () => {});\n"
    run = build_runner.run_tests(files, "backend")
    assert run.status == "failed" and run.problems and "JavaScript" in run.problems[0].message


# ── the loop: failures go to the owner, then QA ──────────────────────────────
BROKEN_SUM = "export function sum(a, b) {\n  return a - b;\n}\n"
FIXED_SUM = "export function sum(a, b) {\n  return a + b;\n}\n"
SUM_TEST = (
    "import { sum } from '../lib/sum';\n\n"
    "describe('sum', () => {\n"
    "  it('adds two numbers', () => {\n    expect(sum(1, 2)).toBe(3);\n  });\n"
    "  it('handles zero', () => {\n    expect(sum(0, 0)).toBe(0);\n  });\n"
    "});\n"
)


class Crew:
    """The Frontend Engineer writes `lib/sum.js` (broken until a test note says why);
    QA writes one suite with a passing and a failing test."""

    def __init__(self, fixes: bool) -> None:
        self.fixes = fixes
        self.frontend_prompts: list[str] = []
        self.qa_calls = 0
        self.qa_prompts: list[str] = []

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        system = messages[0].content
        if system.startswith("You are the QA Engineer"):
            self.qa_calls += 1
            self.qa_prompts.append(messages[-1].content)
            payload = json.loads(resp.text)
            payload["test_files"] = [{"path": "frontend/__tests__/sum.test.js", "framework": "jest",
                                      "targets": "frontend/lib/sum.js", "code": SUM_TEST}]
            payload["coverage_estimate"] = "95%"
            return resp.model_copy(update={"text": json.dumps(payload)})
        if not system.startswith("You are the Frontend Engineer"):
            return resp
        prompt = messages[-1].content
        self.frontend_prompts.append(prompt)
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            payload["files"] = [{"path": "app/page.jsx", "purpose": "home"}, {"path": "lib/sum.js", "purpose": "math"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        fixed = self.fixes and autofix.TEST_NOTE_PREFIX in prompt
        code = {"app/page.jsx": "export default function Home() { return <main />; }\n",
                "lib/sum.js": FIXED_SUM if fixed else BROKEN_SUM}
        written = {p: next((c for name, c in code.items() if p.endswith(name)), "// x\n") for p in asked_files(messages)}
        return resp.model_copy(update={"text": fenced(written)})


class Engine:
    """Installs and builds anything; runs the suite as Jest would, from `lib/sum.js`."""

    kind = "docker"

    def __init__(self) -> None:
        self.test_runs = 0

    def run(self, image, files, steps, limits, on_cancel, on_step):
        out = []
        for step in steps:
            on_step(step)
            if step.name != "test":
                out.append(StepResult(step.name, step.label, 0, 1.0, "added 40 packages in 1s\n"))
                continue
            self.test_runs += 1
            broken = "a - b" in files.get("lib/sum.js", "")
            report = {"framework": "jest", "ran": True, "passed": 1 if broken else 2, "failed": 1 if broken else 0,
                      "errored": 0, "skipped": 0, "suites": [], "coverage": {"lines_pct": 61.5, "branches_pct": 50.0},
                      "failures": [{"path": "/work/__tests__/sum.test.js", "name": "sum adds two numbers",
                                    "message": "Error: expect(received).toBe(expected)\n\nExpected: 3\nReceived: -1",
                                    "line": 5}] if broken else []}
            out.append(StepResult("test", step.label, 1 if broken else 0, 1.0, f"{testrun.MARK}{json.dumps(report)}\n"))
        return out


@pytest.fixture
def sandboxed(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    engine = Engine()
    monkeypatch.setattr(build_runner, "engine", engine)
    return engine


def _run(client, monkeypatch, crew: Crew, idea: str, mode: str = "unattended") -> dict:
    from app.router.router import router as model_router

    monkeypatch.setattr(model_router, "complete", crew)
    r = client.post("/api/projects", json={"idea": idea, "routing_mode": "local_only", "approval_mode": mode})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    return client.get(f"/api/projects/{pid}").json()


def _current(project: dict, phase: str) -> dict:
    return [p for p in project["phases"] if p["phase"] == phase and p["status"] != "rejected"][-1]


def test_a_failing_test_goes_to_the_owner_and_the_same_suite_judges_the_fix(client, monkeypatch, sandboxed):
    """Acceptance: 1 passed + 1 failed, routed to the Frontend Engineer, kept, re-run green."""
    crew = Crew(fixes=True)
    project = _run(client, monkeypatch, crew, "A calculator")
    assert project["status"] == "completed", project.get("gate_note")

    track = project["auto_fix"]["tracks"]["tests"]
    [round_] = track["rounds"]
    assert round_["phases"] == ["frontend_engineer"] and round_["handover"] is True
    [problem] = round_["problems"]
    assert problem["kind"] == "test" and problem["where"] == "frontend/__tests__/sum.test.js:5"
    assert round_["fixed"] == [problem["key"]]
    # The engineer was told what failed and shown the test, and told the tests stay.
    note = next(p for p in crew.frontend_prompts if autofix.TEST_NOTE_PREFIX in p)
    assert "Expected: 3" in note and "expect(sum(1, 2)).toBe(3)" in note and "the tests stay as they are" in note
    # QA wrote its suite once: the fix was judged by the same tests, run again.
    assert crew.qa_calls == 1 and sandboxed.test_runs == 2
    qa = _current(project, "qa_engineer")
    assert qa["test_run"]["status"] == "ok" and qa["test_run"]["passed"] == 2 and qa["test_run"]["kept"] is True
    assert qa["test_run"]["runs"][0]["coverage"] == {"lines_pct": 61.5, "branches_pct": 50.0, "tool": "jest"}
    # The guess never reached what was stored.
    assert "coverage_estimate" not in qa["output"]
    art = client.get(f"/api/projects/{project['id']}/artifacts").json()
    assert "## Tests" in art["readme"] and "2 passed" in art["readme"]


def test_a_failure_the_owner_cannot_fix_goes_to_qa_then_asks_for_help(client, monkeypatch, sandboxed):
    crew = Crew(fixes=False)
    project = _run(client, monkeypatch, crew, "A calculator that stays broken")
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "needs_help"
    assert "1 failing test" in project["gate_note"]

    track = project["auto_fix"]["tracks"]["tests"]
    assert [r["phases"] for r in track["rounds"]] == [["frontend_engineer"], ["qa_engineer"]]
    assert track["stopped"]["reason"] == "no_progress"
    # QA was asked to decide which is wrong, and re-wrote its suite for it.
    assert crew.qa_calls == 2 and "decide which is wrong" in crew.qa_prompts[-1]
    qa = _current(project, "qa_engineer")
    run = qa["test_run"]
    assert (run["status"], run["passed"], run["failed"]) == ("failed", 1, 1)
    assert run["runs"][0]["coverage"]["lines_pct"] == 61.5
    assert run["runs"][0]["failures"][0]["name"] == "sum adds two numbers"

    # Moving past it is a decision on the record, and the build then completes.
    r = client.post(f"/api/projects/{project['id']}/auto-fix/accept",
                    json={"kind": "accepted_risk", "reason": "sum is replaced by a library next sprint"})
    assert r.status_code == 200, r.text
    done = client.get(f"/api/projects/{project['id']}").json()
    assert done["status"] == "completed", done.get("gate_note")
    accepted = done["auto_fix"]["tracks"]["tests"]["accepted"]
    assert accepted["reason"] == "sum is replaced by a library next sprint"


def test_a_re_check_that_could_not_run_is_never_counted_as_fixed(client, monkeypatch, sandboxed):
    """The fix round's re-check hit an unreachable registry: the round says so, nothing
    is "fixed", and the card says the tests weren't run."""
    crew = Crew(fixes=True)
    runs = {"n": 0}
    real_run = sandboxed.run

    def flaky(image, files, steps, limits, on_cancel, on_step):
        if any(s.name == "test" for s in steps):
            runs["n"] += 1
            if runs["n"] == 2:
                return [StepResult("install", "npm install", 1, 1.0, "npm error code EAI_AGAIN getaddrinfo EAI_AGAIN registry.npmjs.org")] + [
                    StepResult(s.name, s.label, None, 0.0, skipped=True) for s in steps[1:]]
        return real_run(image, files, steps, limits, on_cancel, on_step)

    monkeypatch.setattr(sandboxed, "run", flaky)
    project = _run(client, monkeypatch, crew, "A calculator whose re-check can't reach npm")
    first, second = project["auto_fix"]["tracks"]["tests"]["rounds"]
    # The owner's round couldn't be judged: nothing fixed, the test carried, and why.
    assert first["fixed"] == [] and "registry" in first["unjudged"]
    assert [p["key"] for p in first["carried"]] == [p["key"] for p in first["problems"]]
    # So it went on, to QA — and a run that did happen judged it.
    assert second["phases"] == ["qa_engineer"] and second["fixed"] == [first["problems"][0]["key"]]
    assert project["status"] == "completed", project.get("gate_note")
    assert _current(project, "qa_engineer")["test_run"]["status"] == "ok"


def test_a_side_that_never_ran_keeps_its_failures_while_the_other_side_passes():
    t = {"rounds": [{"n": 1, "problems": [
        {"key": "b", "side": "backend", "path": "backend/tests/test_a.py"},
        {"key": "f", "side": "frontend", "path": "frontend/__tests__/a.test.js"},
    ], "fixed": None}], "episode_start": 0}
    run = testrun.combine([
        testrun.TestRun.not_run("backend", "The package registry couldn't be reached, so the tests weren't run."),
        testrun.TestRun(status="ok", side="frontend", framework="jest", passed=2),
    ])
    assert run["status"] == "ok"  # what ran, passed — but the backend test was never reached
    assert autofix.judge_tests_round(t, run, "registry down") == ["f"]
    assert t["rounds"][0]["remaining"] == ["b"] and t["rounds"][0]["unjudged"] == "registry down"


def test_a_suite_that_could_not_run_at_all_holds_ship_until_waived():
    run = testrun.combine([testrun.TestRun(status="failed", side="frontend", reason="The frontend has no test runner set up.")])
    assert autofix.test_failures(run) == []  # nobody in the crew to send it to
    [held] = autofix.unwaived_tests({"tracks": {}}, run)
    assert held["test"] == "frontend suite" and "no test runner" in held["title"]
    data = {"tracks": {}}
    autofix.accept_tests(data, "accepted_risk", "we test this by hand", [held["key"]])
    assert autofix.unwaived_tests(data, run) == []


class FullStack(Crew):
    """A failing *backend* test: FORGE fixes it once told, PRISM is never asked."""

    BROKEN = "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/total')\ndef total():\n    return {'total': 1 - 2}\n"
    FIXED = BROKEN.replace("1 - 2", "1 + 2")

    def __init__(self) -> None:
        super().__init__(fixes=True)
        self.frontend_calls = 0
        self.frontend_at_fix: Optional[int] = None

    def __call__(self, messages, **kwargs):
        system = messages[0].content
        resp = _fake_complete(messages, **kwargs)
        if system.startswith("You are the Frontend Engineer"):
            self.frontend_calls += 1
        if system.startswith("You are the QA Engineer"):
            self.qa_calls += 1
            payload = json.loads(resp.text)
            payload["test_files"] = [{"path": "backend/tests/test_total.py", "framework": "pytest", "targets": "backend/main.py",
                                      "code": "from main import app\n\n\ndef test_total_is_three():\n    assert app\n"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        if not system.startswith("You are the Backend Engineer"):
            return super().__call__(messages, **kwargs) if system.startswith("You are the Frontend Engineer") else resp
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            payload["files"] = [{"path": "main.py", "purpose": "app"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        told = autofix.TEST_NOTE_PREFIX in messages[-1].content
        if told and self.frontend_at_fix is None:
            self.frontend_at_fix = self.frontend_calls
        code = self.FIXED if told else self.BROKEN
        return resp.model_copy(update={"text": fenced({p: code for p in asked_files(messages)})})


class PyEngine(Engine):
    def __init__(self) -> None:
        super().__init__()
        self.frontend_builds = 0

    def run(self, image, files, steps, limits, on_cancel, on_step):
        if any(s.name == "build" for s in steps) and "lib/sum.js" in files:
            self.frontend_builds += 1
        if not any(s.name == "test" for s in steps) or "main.py" not in files:
            return super().run(image, files, steps, limits, on_cancel, on_step)
        self.test_runs += 1
        broken = "1 - 2" in files["main.py"]
        report = {"framework": "pytest", "ran": True, "passed": 0 if broken else 1, "failed": 1 if broken else 0,
                  "errored": 0, "skipped": 0, "suites": [], "coverage": {"lines_pct": 80.0, "branches_pct": None},
                  "failures": [{"path": "tests/test_total.py", "name": "test_total_is_three",
                                "message": "E       assert -1 == 3", "line": 5}] if broken else []}
        return [StepResult("install", "pip install", 0, 1.0, "Successfully installed fastapi\n"),
                StepResult("test", "pytest (tests)", 1 if broken else 0, 1.0, f"{testrun.MARK}{json.dumps(report)}\n")]


def test_a_backend_fix_keeps_the_frontend_it_did_not_touch(client, monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    engine = PyEngine()
    monkeypatch.setattr(build_runner, "engine", engine)
    crew = FullStack()
    project = _run(client, monkeypatch, crew, "A totals API")
    assert project["status"] == "completed", project.get("gate_note")
    [round_] = project["auto_fix"]["tracks"]["tests"]["rounds"]
    assert round_["phases"] == ["backend_engineer"] and round_["fixed"]
    # FORGE was re-run; PRISM's frontend and SIEVE's suite were kept and re-checked.
    front = _current(project, "frontend_engineer")
    assert front["handoff"]["kept"] is True
    assert crew.qa_calls == 1 and engine.test_runs == 2
    # Not one Frontend Engineer call after FORGE was told about the failing test, and
    # its unchanged files weren't built a second time.
    assert crew.frontend_at_fix and crew.frontend_calls == crew.frontend_at_fix
    assert engine.frontend_builds == 1


def test_a_kept_suite_that_no_longer_loads_carries_its_failures_unfixed():
    """The fix renamed a module, so the kept test file no longer loads: its failures
    weren't judged, so they aren't fixed — whatever QA writes next."""
    t = {"rounds": [{"n": 1, "problems": [{"key": "k", "side": "backend", "path": "backend/tests/test_a.py"}],
                     "fixed": None}], "episode_start": 0}
    run = testrun.combine([testrun.TestRun(
        status="failed", side="backend", framework="pytest", passed=1, errored=1,
        problems=[testrun.Problem("backend/tests/test_a.py", "The test file couldn't run: No module named 'app.old'", "test", 1, "test")])])
    assert autofix.judge_tests_round(t, run) == []
    assert t["rounds"][0]["remaining"] == ["k"] and t["rounds"][0]["carried"]


# ── the Ship gate ────────────────────────────────────────────────────────────
def test_a_red_suite_is_a_build_gate_and_a_green_one_is_not():
    project = SimpleNamespace(effective_approval_mode="checkpoints", cost_cap_usd=None)
    failing = [{"key": "k", "title": "sum adds two numbers\nExpected: 3\nReceived: -1"}]
    gate = approval.decide_gate(project, SHIP_GATE_PHASE.value, {}, failing_tests=failing)
    assert gate.kind == GateKind.BUILD.value and "1 of QA's tests fails" in gate.note
    assert "sum adds two numbers" in gate.note
    assert approval.decide_gate(project, SHIP_GATE_PHASE.value, {}, failing_tests=[]).kind == GateKind.SHIP.value
    # Unattended runs stop for it too: it isn't a judgement call.
    project.effective_approval_mode = "unattended"
    assert approval.decide_gate(project, SHIP_GATE_PHASE.value, {}, failing_tests=failing).kind == GateKind.BUILD.value


def test_shipping_over_failing_tests_needs_a_waiver_with_a_reason(client):
    r = client.post("/api/projects", json={"idea": "A red suite at Ship", "routing_mode": "local_only"})
    pid = r.json()["id"]
    failing_run = testrun.combine([testrun.TestRun(
        status="failed", side="frontend", framework="jest", passed=1, failed=1,
        failures=[testrun.TestFailure("frontend/__tests__/sum.test.js", "sum adds two numbers", "Expected: 3", 5)])])
    with SessionLocal() as db:
        project = db.get(Project, pid)
        db.add(PhaseResult(project_id=pid, phase="qa_engineer", agent="QA Engineer", status="pending_approval",
                           output={}, content_md="", test_run=failing_run))
        project.status = "awaiting_approval"
        project.gate_kind = GateKind.BUILD.value
        project.current_phase = SHIP_GATE_PHASE.value
        db.commit()

    r = client.post(f"/api/projects/{pid}/approve")
    assert r.status_code == 409 and "QA's test" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/tests/waive", json={"kind": "accepted_risk", "reason": "x"})
    assert r.status_code == 422  # a reason needs at least three characters
    r = client.post(f"/api/projects/{pid}/tests/waive",
                    json={"kind": "accepted_risk", "reason": "the rounding test is wrong; fixed upstream"})
    assert r.status_code == 200 and r.json()["waived"] == 1
    project = client.get(f"/api/projects/{pid}").json()
    accepted = project["auto_fix"]["tracks"]["tests"]["accepted"]
    assert accepted["kind"] == "accepted_risk" and accepted["reason"].startswith("the rounding test")
    from app.orchestration.runner import runner

    with SessionLocal() as db:
        assert not runner.failing_tests(db.get(Project, pid))


# ── evals ────────────────────────────────────────────────────────────────────
def test_the_scorecard_reports_what_was_measured_and_nothing_else():
    from app.evals.scorecard import tests

    qa = SimpleNamespace(phase="qa_engineer", test_run={"runs": [
        {"status": "failed", "passed": 3, "failed": 1, "errored": 0, "coverage": {"lines_pct": 61.5}},
        {"status": "ok", "passed": 4, "failed": 0, "errored": 0, "coverage": {"lines_pct": 80.0}},
    ]})
    assert tests([qa]) == {"tests_passed_pct": 0.875, "coverage_lines_pct": 61.5}
    nothing = SimpleNamespace(phase="qa_engineer", test_run={"status": "not_run", "runs": []})
    assert tests([nothing]) == {"tests_passed_pct": None, "coverage_lines_pct": None}


def test_the_test_run_column_is_added_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, idea TEXT)"))
        conn.execute(text("CREATE TABLE phase_results (id VARCHAR(32) PRIMARY KEY, phase TEXT)"))
        conn.execute(text("INSERT INTO phase_results (id, phase) VALUES ('r', 'qa_engineer')"))
    assert "phase_results.test_run" in run_migrations(engine)
    assert "test_run" in {c["name"] for c in inspect(engine).get_columns("phase_results")}
    assert run_migrations(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT phase, test_run FROM phase_results")).fetchall() == [("qa_engineer", None)]


# ── real containers (opt-in) ─────────────────────────────────────────────────
docker_only = pytest.mark.skipif(
    os.environ.get("BUILD_RUN_DOCKER_TESTS") != "1" or not sandbox.available(refresh=True)[0],
    reason="starts real containers: set BUILD_RUN_DOCKER_TESTS=1 with Docker running",
)


@docker_only
def test_docker_a_real_pytest_run_counts_measures_and_names_what_wont_import(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", None)
    files = _tree({
        "backend/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.post('/todos', status_code=201)\n"
                           "def create():\n    return {'id': 1}\n",
        "backend/tests/test_api.py": "from fastapi.testclient import TestClient\n\nfrom main import app\n\n"
                                     "client = TestClient(app)\n\n\ndef test_it_exists():\n    assert app\n\n\n"
                                     "def test_creating_a_todo_returns_201():\n    assert client.post('/todos').status_code == 200\n",
        "backend/tests/test_broken.py": "from app.missing import thing\n\n\ndef test_never_runs():\n    assert thing\n",
    })
    run = build_runner.run_tests(files, "backend")
    assert (run.status, run.passed, run.failed, run.errored) == ("failed", 1, 1, 1), run.as_dict()
    assert run.coverage is not None and run.coverage.lines_pct and run.coverage.tool == "pytest-cov"
    assert run.failures[0].name == "test_creating_a_todo_returns_201"
    assert any("No module named 'app'" in p.message for p in run.problems)


@docker_only
def test_docker_a_real_jest_run_on_a_next_frontend(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", None)
    files = _tree({
        "frontend/app/page.jsx": "export default function Home() { return <main>Hi</main>; }\n",
        "frontend/lib/sum.js": BROKEN_SUM,
        "frontend/__tests__/sum.test.js": SUM_TEST,
    })
    run = build_runner.run_tests(files, "frontend")
    assert (run.status, run.passed, run.failed) == ("failed", 1, 1), run.as_dict()
    assert run.coverage is not None and run.coverage.lines_pct is not None
    assert run.failures[0].path == "frontend/__tests__/sum.test.js" and run.failures[0].line == 5


@docker_only
def test_docker_a_real_vitest_run_on_a_vite_frontend(monkeypatch):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", None)
    files = _tree({
        **VITE_APP,
        "frontend/src/__tests__/App.test.jsx": "import { render, screen } from '@testing-library/react';\n"
                                               "import App from '../App';\n\ntest('shows the greeting', () => {\n"
                                               "  render(<App />);\n  expect(screen.getByText('Hello')).toBeInTheDocument();\n});\n",
    })
    run = build_runner.run_tests(files, "frontend")
    assert (run.status, run.passed, run.failed) == ("ok", 1, 0), run.as_dict()
    assert run.coverage is not None and run.coverage.tool == "vitest (v8)"
