"""#75: the generated code is installed, built and started before anyone approves it,
and a failed Vercel build goes back to the crew.

The real sandbox needs Docker, so these tests run the runner against a fake engine that
answers each step with output captured from real `next build`, `npm install`, Python
and Node runs. The few tests that start real containers are opt-in:
`BUILD_RUN_DOCKER_TESTS=1 pytest tests/test_build_runner.py -k docker`.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

import httpx
import pytest

from app.build import buildlog, runner as build_runner, sandbox
from app.build.sandbox import Limits, Step, StepResult
from app.core import deploy_store, vercel
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project
from tests.conftest import TEST_USER_ID, _fake_complete, asked_files, fenced, through_question_gates

# ── what real tools printed (captured from the sandbox) ─────────────────────
NEXT_TYPE_ERROR = """
> demo-frontend@0.1.0 build
> next build

  ▲ Next.js 14.2.35

   Creating an optimized production build ...
 ✓ Compiled successfully
   Skipping linting
   Checking validity of types ...
Failed to compile.

./app/page.tsx:2:9
Type error: Type 'string' is not assignable to type 'number'.

  1 | export default function Home() {
> 2 |   const count: number = 'three';
    |         ^
  3 |   return <main>{count}</main>;
Next.js build worker exited with code: 1 and signal: null
"""

NEXT_MODULE_NOT_FOUND = """
   Creating an optimized production build ...
Failed to compile.

./app/page.tsx
Module not found: Can't resolve './components/Thing'

https://nextjs.org/docs/messages/module-not-found


> Build failed because of webpack errors
"""

NEXT_SWC = """
Failed to compile.

./app/page.tsx
Error:
  x Unexpected token `main`. Expected jsx identifier
   ,-[/work/app/page.tsx:1:1]
 1 | export default function Home() {
 2 |   return <main><div></main>;
   :           ^^^^
 3 | }
   `----

Caused by:
    Syntax Error

Import trace for requested module:
./app/page.tsx


> Build failed because of webpack errors
"""

NEXT_PRERENDER = """
   Generating static pages (3/4)

Error occurred prerendering page "/". Read more: https://nextjs.org/docs/messages/prerender-error

ReferenceError: window is not defined
    at s (/work/.next/server/app/page.js:1:1940)
    at em (/work/node_modules/next/dist/compiled/next-server/app-page.runtime.prod.js:12:134808)
"""

PY_IMPORT = """Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "/usr/local/lib/python3.12/importlib/__init__.py", line 90, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
  File "<frozen importlib._bootstrap>", line 1387, in _gcd_import
  File "/work/main.py", line 1, in <module>
    from fastapi import FastAPI, NotAThing
ImportError: cannot import name 'NotAThing' from 'fastapi' (/work/.deps/fastapi/__init__.py)
"""

PY_DB_DOWN = """Traceback (most recent call last):
  File "/work/main.py", line 7, in <module>
    c.execute(text('select 1'))
  File "/work/.deps/psycopg2/__init__.py", line 122, in connect
sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server at "localhost" (::1), port 5432 failed: Connection refused
"""

NODE_CRASH = """/work/server.js:4
console.log(cfg.port);
                ^

TypeError: Cannot read properties of undefined (reading 'port')
    at Object.<anonymous> (/work/server.js:4:17)
    at Module._compile (node:internal/modules/cjs/loader:1521:14)

Node.js v20.20.2
"""

NPM_ETARGET = """npm error code ETARGET
npm error notarget No matching version found for left-pad@^99.0.0.
npm error notarget In most cases you or one of your dependencies are requesting
"""

NPM_E404 = """npm error code E404
npm error 404 Not Found - GET https://registry.npmjs.org/left-pad-does-not-exist-xyz-75 - Not found
npm error 404
npm error 404  'left-pad-does-not-exist-xyz-75@^1.0.0' is not in this registry.
"""

NPM_ERESOLVE = """npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR! Found: react@18.3.1
npm ERR! Could not resolve dependency:
npm ERR! peer react@"^17.0.0" from react-old-widget@1.2.0
"""

PIP_NONE = """ERROR: Could not find a version that satisfies the requirement fastapi==99.0 (from versions: 0.1.0, 0.115.6)
ERROR: No matching distribution found for fastapi==99.0
"""

HOSTILE_SCRIPT = """npm error code 1
npm error path /work
npm error command failed
npm error command sh -c node -e "require('https').get('https://example.com')"
npm error EAI_AGAIN
"""


# ── reading the output ───────────────────────────────────────────────────────
def test_a_next_type_error_names_the_file_line_and_error():
    [p] = buildlog.js_build_problems(NEXT_TYPE_ERROR, ["app/page.tsx"])
    assert (p.path, p.line, p.kind) == ("app/page.tsx", 2, "type")
    assert p.message == "Type error: Type 'string' is not assignable to type 'number'."


def test_next_import_syntax_and_prerender_failures_are_each_read():
    [missing] = buildlog.js_build_problems(NEXT_MODULE_NOT_FOUND, ["app/page.tsx"])
    assert missing.kind == "import" and "Can't resolve './components/Thing'" in missing.message
    [syntax] = buildlog.js_build_problems(NEXT_SWC, ["app/page.tsx"])
    # The line SWC's frame points at, not the frame's first line.
    assert (syntax.kind, syntax.line) == ("syntax", 2) and "Unexpected token `main`" in syntax.message
    [crash] = buildlog.js_build_problems(NEXT_PRERENDER, ["app/page.tsx", "app/layout.tsx"])
    assert crash.path == "app/page.tsx" and crash.kind == "runtime"
    assert "window is not defined" in crash.message


def test_tsc_esbuild_and_rollup_lines_are_read():
    out = buildlog.js_build_problems(
        "src/api.ts(12,5): error TS2322: Type 'x' is not assignable to type 'y'.\n"
        "/work/src/App.jsx:3:7: ERROR: Expected \";\" but found \"b\"\n"
        '[vite]: Rollup failed to resolve import "axios" from "/work/src/main.jsx".\n',
        ["src/api.ts", "src/App.jsx", "src/main.jsx"],
    )
    assert [(p.path, p.line, p.kind) for p in out] == [
        ("src/api.ts", 12, "type"), ("src/App.jsx", 3, "syntax"), ("src/main.jsx", None, "import"),
    ]


def test_a_python_import_error_lands_on_the_projects_own_frame():
    [p] = buildlog.python_problems(PY_IMPORT)
    assert (p.path, p.line, p.kind) == ("main.py", 1, "runtime")
    assert p.message.startswith("ImportError: cannot import name 'NotAThing'")


def test_pytest_collection_errors_are_read():
    out = buildlog.python_problems(
        "_____ ERROR collecting tests/test_api.py _____\n"
        "tests/test_api.py:3: in <module>\n    from main import nothing\n"
        "E   ImportError: cannot import name 'nothing' from 'main' (/work/main.py)\n"
    )
    assert [(p.path, p.line) for p in out] == [("tests/test_api.py", 3)]


def test_a_node_crash_lands_on_its_line():
    [p] = buildlog.node_problems(NODE_CRASH)
    assert (p.path, p.line) == ("server.js", 4) and p.message.startswith("TypeError: Cannot read properties")


def test_npm_and_pip_failures_name_the_package_not_a_wall_of_text():
    [v] = buildlog.npm_problems(NPM_ETARGET)
    assert v.kind == "package" and "`left-pad`" in v.message and "npm error" not in v.message
    [n] = buildlog.npm_problems(NPM_E404)
    assert "`left-pad-does-not-exist-xyz-75` is not a package on npm" in n.message
    [r] = buildlog.npm_problems(NPM_ERESOLVE)
    assert "`react-old-widget`" in r.message and 'react@"^17.0.0"' in r.message
    [pip] = buildlog.pip_problems(PIP_NONE)
    assert pip.path == "requirements.txt" and "`fastapi`" in pip.message


def test_output_nothing_recognises_is_kept_as_its_last_thirty_lines():
    text = "\n".join(f"line {i}" for i in range(100))
    p = buildlog.tail_problem(text, "package.json", "`next build`")
    assert p.path == "package.json" and "line 99" in p.message and "line 69" not in p.message


def test_problems_keep_the_compile_gates_caps():
    many = [buildlog.Problem(f"f{i % 3}.ts", f"error {i}", "type", i) for i in range(60)]
    capped = buildlog.capped(many)
    assert len(capped) == 15 and all(sum(1 for q in capped if q.path == p.path) <= 5 for p in capped)
    assert len(buildlog.capped([buildlog.Problem(f"f{i}.ts", "e", "type") for i in range(40)])) == 24


# ── what to run ──────────────────────────────────────────────────────────────
def _next_files(page: str = "export default function Home() { return <main />; }\n") -> dict[str, str]:
    from app.build import scaffold

    files = {"frontend/app/page.tsx": page}
    for f in scaffold.build(dict(files), None, None, "Demo").files:
        files[f.path] = f.content
    return files


def test_each_stack_gets_the_commands_its_scaffold_writes():
    plan, _ = build_runner.plan_for(build_runner.side_files(_next_files(), "frontend"), "frontend")
    assert plan.stack == "nextjs" and plan.image == sandbox.NODE_IMAGE
    assert [s.label for s in plan.steps] == ["npm install", "install scripts (offline)", "next build"]
    # Only the download has the network, and it runs no package scripts.
    assert [s.network for s in plan.steps] == [True, False, False]
    assert "--ignore-scripts" in plan.steps[0].command

    from app.build import scaffold

    py = {"backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"}
    for f in scaffold.build(dict(py), None, None, "Demo").files:
        py[f.path] = f.content
    plan, _ = build_runner.plan_for(build_runner.side_files(py, "backend"), "backend")
    assert plan.stack == "python" and [s.label for s in plan.steps] == ["pip install", "import main"]

    node = {"backend/server.js": "const express = require('express');\nconst app = express();\napp.listen(3001);\n"}
    for f in scaffold.build(dict(node), None, None, "Demo").files:
        node[f.path] = f.content
    plan, _ = build_runner.plan_for(build_runner.side_files(node, "backend"), "backend")
    assert [s.label for s in plan.steps][-2:] == ["npm run build", "node server.js"]
    assert plan.steps[-1].env["PORT"] == build_runner.BOOT_PORT

    plan, why = build_runner.plan_for({"package.json": json.dumps({"dependencies": {"next": "14"}})}, "frontend")
    assert plan is None and "no build script" in why


# ── the sandbox's own limits ─────────────────────────────────────────────────
def test_every_step_container_is_locked_down(monkeypatch):
    monkeypatch.setattr(sandbox, "docker", lambda: "/usr/bin/docker")
    box = sandbox.Sandbox(sandbox.NODE_IMAGE, Limits(memory_mb=1024, cpus=1.5, pids=256, cache="acct-1"))
    args = box._args("c1", "npm run build", network=False, env={"A": "1"})
    joined = " ".join(args)
    for flag in ("--network none", "--memory 1024m", "--memory-swap 1024m", "--cpus 1.5", "--pids-limit 256",
                 "--read-only", "--cap-drop ALL", "no-new-privileges", f"--user {sandbox.USER}"):
        assert flag in joined, flag
    # Files reach it through its own volume — never a bind mount of anything on the host.
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert all(m.startswith("type=volume,") for m in mounts) and "-v" not in args
    assert "aiteam-cache-npm-acct-1" in joined, "each account has its own package cache"
    assert "--network bridge" in " ".join(box._args("c2", "npm install", network=True, env={}))
    with pytest.raises(sandbox.SandboxError):
        sandbox.Sandbox("ubuntu:latest", Limits())


# ── a fake engine: real output, no Docker ────────────────────────────────────
class FakeEngine:
    kind = "docker"

    def __init__(self, answer: Callable[[Step, dict[str, str]], tuple[int, str]]) -> None:
        self.answer = answer
        self.runs: list[dict[str, str]] = []

    def run(self, image, files, steps, limits, on_cancel, on_step):
        self.runs.append(dict(files))
        out, failed = [], False
        for step in steps:
            if failed:
                out.append(StepResult(step.name, step.label, None, 0.0, skipped=True))
                continue
            on_step(step)
            code, text = self.answer(step, files)
            out.append(StepResult(step.name, step.label, code, 1.0, text))
            failed = code != 0
        return out


@pytest.fixture
def real_build(monkeypatch):
    """Switch real builds on, against an engine the test scripts."""
    monkeypatch.setattr(settings, "build_run_enabled", True)

    def use(answer) -> FakeEngine:
        engine = FakeEngine(answer)
        monkeypatch.setattr(build_runner, "engine", engine)
        return engine

    yield use


def _ok(step, files):
    return 0, "added 105 packages in 3s\n" if step.label == "npm install" else "done\n"


def test_a_failed_step_becomes_problems_with_its_step(real_build):
    real_build(lambda step, files: (1, NEXT_TYPE_ERROR) if step.name == "build" else _ok(step, files))
    run = build_runner.run_build(_next_files(), "frontend")
    assert run.status == "failed" and run.runner == "docker"
    [p] = run.problems
    assert (p.path, p.line, p.step) == ("frontend/app/page.tsx", 2, "build")
    record = run.as_dict()
    assert record["summary"] == "`next build` failed · 1 error"
    assert record["steps"][-1]["tail"].endswith("Next.js build worker exited with code: 1 and signal: null")


def test_a_passing_build_says_what_it_installed_and_ran(real_build):
    real_build(_ok)
    record = build_runner.run_build(_next_files(), "frontend").as_dict()
    assert record["status"] == "ok" and record["summary"].startswith("Installed 105 packages · `next build` passed")


def test_a_hostile_install_script_fails_the_build(real_build):
    real_build(lambda step, files: (1, HOSTILE_SCRIPT) if step.label.startswith("install scripts") else _ok(step, files))
    run = build_runner.run_build(_next_files(), "frontend")
    assert run.status == "failed" and run.problems[0].kind == "package"
    assert "reach the network" in run.problems[0].message


def test_a_backend_that_only_needs_its_database_is_not_sent_back(real_build):
    from app.build import scaffold

    files = {"backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"}
    for f in scaffold.build(dict(files), None, None, "Demo").files:
        files[f.path] = f.content
    real_build(lambda step, files: (1, PY_DB_DOWN) if step.name == "boot" else _ok(step, files))
    record = build_runner.run_build(files, "backend").as_dict()
    assert record["status"] == "ok" and record["steps"][-1]["inconclusive"]
    real_build(lambda step, files: (1, PY_IMPORT) if step.name == "boot" else _ok(step, files))
    run = build_runner.run_build(files, "backend")
    assert run.status == "failed" and run.problems[0].path == "backend/main.py" and run.problems[0].step == "boot"


def test_a_build_that_runs_out_of_time_is_unchecked_not_failed(real_build):
    def slow(step, files):
        return _ok(step, files)

    engine = real_build(slow)
    engine.run = lambda image, files, steps, limits, on_cancel, on_step: [
        StepResult("install", "npm install", None, 300.0, "", timed_out=True),
        *[StepResult(s.name, s.label, None, 0.0, skipped=True) for s in steps[1:]],
    ]
    run = build_runner.run_build(_next_files(), "frontend")
    assert run.status == "unchecked" and "ran out of time" in run.reason and not run.problems


def test_no_docker_is_unchecked_and_says_so(monkeypatch, client):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", None)
    monkeypatch.setattr(sandbox, "available", lambda refresh=False: (False, "Docker isn't installed on the computer running the backend.", None))
    run = build_runner.run_build(_next_files(), "frontend")
    assert run.status == "unchecked" and "Docker isn't installed" in run.reason
    shown = client.get("/api/settings/build-runner").json()
    assert shown["kind"] == "none" and not shown["available"] and "Docker" in shown["reason"]

    from app.agents.base import AgentContext
    from app.build.check import BuildCheck

    # And the phase keeps the parser's verdict: no runner changes nothing at the Ship gate.
    agent = __import__("app.agents", fromlist=["get_agent"]).get_agent("frontend_engineer")
    build = BuildCheck()
    record = agent._run_build(AgentContext(idea="x", prior_outputs={}), {"files": [{"path": "app/page.tsx", "code": "export default function P(){return null}\n"}]}, build)
    assert build.status == "ok" and record["status"] == "unchecked"


def test_the_builder_service_is_used_when_configured(monkeypatch, client):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(settings, "build_runner_url", "http://builder:8100")
    monkeypatch.setattr(build_runner, "engine", None)
    monkeypatch.setattr(build_runner, "_builder_health", lambda url: (True, None))
    shown = client.get("/api/settings/build-runner").json()
    assert shown["kind"] == "builder" and shown["available"] and shown["detail"] == "http://builder:8100"
    chosen, _ = build_runner._engine()
    assert isinstance(chosen, build_runner.BuilderEngine)


def test_the_builder_service_refuses_what_it_should():
    from app.build import builder_service

    with pytest.raises(ValueError):
        builder_service.run({"image": "alpine:latest", "files": {}, "steps": [{"name": "build"}]})
    with pytest.raises(ValueError):
        builder_service.run({"image": sandbox.NODE_IMAGE, "files": {"a": 1}, "steps": [{"name": "build"}]})
    capped = builder_service._limits({"seconds": 99999, "memory_mb": 99999, "cpus": 64})
    assert capped.seconds <= builder_service.MAX_SECONDS and capped.memory_mb <= builder_service.MAX_MEMORY_MB


# ── the whole loop, with a scripted model ────────────────────────────────────
BROKEN_PAGE = "export default function Home() {\n  const count: number = 'three';\n  return <main>{count}</main>;\n}\n"
FIXED_PAGE = "export default function Home() {\n  const count: number = 3;\n  return <main>{count}</main>;\n}\n"


class Frontend:
    """The Frontend Engineer: plans `app/page.tsx`, writes it broken until told why."""

    def __init__(self, fixed_after: Callable[[str], bool]) -> None:
        self.fixed_after = fixed_after
        self.prompts: list[str] = []

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith("You are the Frontend Engineer"):
            return resp
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            payload["files"] = [{"path": "app/page.tsx", "purpose": "home page"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        page = FIXED_PAGE if self.fixed_after(prompt) else BROKEN_PAGE
        return resp.model_copy(update={"text": fenced({p: page for p in asked_files(messages)})})


def _type_checks(step, files):
    if step.name == "build" and "'three'" in files.get("app/page.tsx", ""):
        return 1, NEXT_TYPE_ERROR
    return _ok(step, files)


def _unattended(client, idea: str) -> str:
    r = client.post("/api/projects", json={"idea": idea, "routing_mode": "local_only", "approval_mode": "unattended"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    return pid


def _current(project: dict, phase: str) -> dict:
    return [p for p in project["phases"] if p["phase"] == phase and p["status"] != "rejected"][-1]


def test_a_type_error_next_build_rejects_is_fixed_before_the_ship_review(client, monkeypatch, real_build):
    """Acceptance: caught before Ship, fixed through the loop, and the fix builds."""
    from app.router.router import router as model_router

    engine = real_build(_type_checks)
    # Fixed only once the crew's fix loop sends the phase back with the build's error —
    # the phase's own repair round inside the run still writes it broken.
    model = Frontend(lambda prompt: "fails the build" in prompt)
    monkeypatch.setattr(model_router, "complete", model)
    pid = _unattended(client, "A counter page")
    project = client.get(f"/api/projects/{pid}").json()

    assert project["status"] == "completed", project.get("gate_note")
    front = _current(project, "frontend_engineer")
    assert front["build_status"] == "ok" and front["build_run"]["status"] == "ok"
    assert front["build_run"]["summary"].startswith("Installed 105 packages")
    track = project["auto_fix"]["tracks"]["build:frontend_engineer"]
    [round_] = track["rounds"]
    assert round_["problems"][0]["step"] == "build" and round_["fixed"]
    # The note the phase was re-run with carried next build's own words.
    note = next(p for p in model.prompts if "fails the build" in p)
    assert "Type 'string' is not assignable to type 'number'" in note and "frontend/app/page.tsx:2" in note
    # The sandbox built the fixed page.
    assert any("= 3;" in run.get("app/page.tsx", "") for run in engine.runs)


def test_a_build_that_never_builds_parks_and_never_ships(client, monkeypatch, real_build):
    from app.router.router import router as model_router

    real_build(_type_checks)
    monkeypatch.setattr(model_router, "complete", Frontend(lambda prompt: False))
    pid = _unattended(client, "A page that never builds")
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "needs_help"
    front = _current(project, "frontend_engineer")
    assert front["build_status"] == "failed" and front["build_run"]["status"] == "failed"
    assert front["build_note"][0]["step"] == "build"


# ── Vercel's failure, back to the crew ───────────────────────────────────────
GOOD_VERCEL = "vercel_tok_GOOD_0123456789abcd"
VERCEL_LOG = [
    "Running \"npm run build\"",
    "> next build",
    "Failed to compile.",
    "./app/page.tsx:2:9",
    "Type error: Type 'string' is not assignable to type 'number'.",
    f"leaked {GOOD_VERCEL}",
    "Error: Command \"npm run build\" exited with 1",
]


class Vercel:
    def __init__(self) -> None:
        self.state = "ERROR"
        self.log = list(VERCEL_LOG)
        self.event_params: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {GOOD_VERCEL}":
            return httpx.Response(403, json={"error": {"message": "no"}})
        path = request.url.path
        if path == "/v2/user":
            return httpx.Response(200, json={"user": {"username": "ada"}})
        if path == "/v13/deployments/dpl_9":
            return httpx.Response(200, json={"id": "dpl_9", "readyState": self.state, "errorCode": "BUILD_FAILED",
                                             "errorStep": "build", "errorMessage": "Command \"npm run build\" exited with 1"})
        if path == "/v3/deployments/dpl_9/events":
            self.event_params.append(dict(request.url.params))
            return httpx.Response(200, json=[{"type": "stdout", "text": line} for line in self.log])
        return httpx.Response(404, json={})


@pytest.fixture
def vercel_fake(monkeypatch, client):
    fake = Vercel()
    monkeypatch.setattr(vercel, "transport", httpx.MockTransport(fake))
    r = client.put("/api/deploy/vercel/token", json={"token": GOOD_VERCEL}, headers={"host": "localhost"})
    assert r.json()["applied"], r.text
    yield fake
    deploy_store.for_user(TEST_USER_ID).remove(deploy_store.VERCEL)


def _building(pid: str) -> None:
    with SessionLocal() as db:
        p = db.get(Project, pid)
        p.deploy_target, p.deploy_status, p.deploy_id = "vercel", "building", "dpl_9"
        db.commit()


def test_a_failed_vercel_deploy_reopens_the_fix_loop_with_vercels_errors(client, monkeypatch, real_build, vercel_fake):
    from app.router.router import router as model_router

    real_build(_ok)  # the sandbox passes it; Vercel is what fails
    model = Frontend(lambda prompt: "failed the build on Vercel" in prompt)
    monkeypatch.setattr(model_router, "complete", model)
    # The first build ships the page Vercel will reject.
    pid = _unattended(client, "A counter page for Vercel")
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"
    _building(pid)

    r = client.get(f"/api/projects/{pid}/deploy")
    first = r.json()
    assert first["status"] == "fixing" and GOOD_VERCEL not in r.text
    assert "./app/page.tsx:2:9" in first["log"] and vercel_fake.event_params[0]["limit"] == "-1"
    assert "BUILD_FAILED" in first["error"]
    with SessionLocal() as db:
        kept = db.get(Project, pid).deploy_log
    assert len(kept) == len(VERCEL_LOG) and all(GOOD_VERCEL not in line for line in kept)

    # TestClient ran the background fix; unattended, the build finished again.
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project.get("gate_note")
    note = next(p for p in model.prompts if "failed the build on Vercel" in p)
    assert "Type 'string' is not assignable" in note and "frontend/app/page.tsx:2" in note
    track = project["auto_fix"]["tracks"]["build:frontend_engineer"]
    vercel_round = next(r for r in track["rounds"] if r.get("source") == "vercel")
    assert vercel_round["problems"][0]["step"] == "vercel" and vercel_round["fixed"]
    # The phases after the frontend were rebuilt on the fixed code.
    assert _current(project, "qa_engineer")["created_at"] > _current(project, "frontend_engineer")["created_at"]

    state = client.get(f"/api/projects/{pid}/deploy").json()
    assert state["status"] == "fixed" and state["fix"]["state"] == "fixed" and state["fix"]["round"] == 1
    assert state["log"], "the failed build's log stays on screen until the next deploy"


def test_vercel_errors_outside_the_crews_code_are_shown_not_sent_back(client, monkeypatch, real_build, vercel_fake):
    real_build(_ok)
    pid = _unattended(client, "A page Vercel can't configure")
    _building(pid)
    vercel_fake.log = ["Error: Missing required environment variable STRIPE_PUBLISHABLE_KEY"]
    state = client.get(f"/api/projects/{pid}/deploy").json()
    assert state["status"] == "error" and state["fix"] is None
    assert "isn't in a file the crew wrote" in state["error"]
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"


def test_vercel_failing_again_and_again_stops_sending_it_back(client, monkeypatch, real_build, vercel_fake):
    from app.router.router import router as model_router

    real_build(_ok)
    monkeypatch.setattr(model_router, "complete", Frontend(lambda prompt: False))
    pid = _unattended(client, "A page Vercel keeps failing")
    monkeypatch.setattr(settings, "auto_fix_max_rounds", 1)
    _building(pid)
    assert client.get(f"/api/projects/{pid}/deploy").json()["status"] == "fixing"
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"
    _building(pid)
    state = client.get(f"/api/projects/{pid}/deploy").json()
    assert state["status"] == "error" and "still fails after the crew fixed it 1 time" in state["error"]

    vercel_fake.state = "READY"
    _building(pid)
    client.get(f"/api/projects/{pid}/deploy")
    with SessionLocal() as db:
        assert db.get(Project, pid).auto_fix["vercel"]["attempts"] == 0, "a deploy that builds resets the count"


def test_the_new_columns_are_added_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, idea TEXT)"))
        conn.execute(text("CREATE TABLE phase_results (id VARCHAR(32) PRIMARY KEY, phase TEXT)"))
        conn.execute(text("INSERT INTO phase_results (id, phase) VALUES ('r', 'frontend_engineer')"))
    applied = run_migrations(engine)
    assert "projects.deploy_log" in applied and "phase_results.build_run" in applied
    assert {"build_run"} <= {c["name"] for c in inspect(engine).get_columns("phase_results")}
    assert run_migrations(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT phase, build_run FROM phase_results")).fetchall() == [("frontend_engineer", None)]


# ── real containers (opt-in) ─────────────────────────────────────────────────
docker_only = pytest.mark.skipif(
    os.environ.get("BUILD_RUN_DOCKER_TESTS") != "1" or not sandbox.available(refresh=True)[0],
    reason="starts real containers: set BUILD_RUN_DOCKER_TESTS=1 with Docker running",
)


@docker_only
def test_docker_a_hostile_postinstall_cannot_reach_the_network():
    files = {
        "package.json": json.dumps({"name": "x", "version": "1.0.0", "scripts": {
            "postinstall": "node -e \"require('https').get('https://example.com',()=>process.exit(0)).on('error',e=>{console.error(e.code);process.exit(1)})\""}}),
    }
    box = sandbox.Sandbox(sandbox.NODE_IMAGE, Limits(seconds=120, cache="tests"))
    results = box.run(files, [build_runner._NPM_INSTALL, build_runner._NPM_SCRIPTS])
    assert results[0].ok and not results[1].ok and "EAI_AGAIN" in results[1].output


@docker_only
def test_docker_a_step_past_its_budget_is_killed():
    box = sandbox.Sandbox(sandbox.NODE_IMAGE, Limits(seconds=6, cache="tests"))
    [r] = box.run({"a.txt": "x"}, [Step("build", "sleep", "sleep 60")])
    assert r.timed_out and r.seconds < 30
    import subprocess

    left = subprocess.run(["docker", "ps", "-q", "--filter", f"name={box.volume}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", "the timed-out container was left running"
