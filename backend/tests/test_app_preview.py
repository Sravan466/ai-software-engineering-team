"""#78: the Preview tab shows the generated app, and changing the preview changes the code.

The app is built from `artifacts.assemble()` and served from the sandbox at an origin
of its own (`<token>.localhost`), reached only through this backend. Edits made on it
become Frontend attempts — checked, built, and in the archive from then on. The
sketch drawn from the plan stays, labelled, for before the code exists and for when
the app can't run.

The app's sandbox is faked here (`app_runtime.engine`): a "build" that renders the
generated page's heading into HTML the way `next build` would, so what is served can
be traced byte for byte. The real container is an opt-in test at the bottom.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from typing import Optional

import pytest

from app.build import runner as build_runner
from app.build import sandbox
from app.build.sandbox import StepResult
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project
from app.preview import app_proxy, app_runtime, source
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_question_gates

HERO = (
    "export default function Hero() {\n"
    "  return (\n"
    '    <section className="py-20 bg-indigo-600 text-white">\n'
    '      <h1 className="text-4xl font-bold">Welcome MARKER-78</h1>\n'
    '      <a className="text-indigo-100" href="/recipes">Browse recipes</a>\n'
    "    </section>\n"
    "  );\n"
    "}\n"
)
PAGE = "import Hero from '../components/Hero';\n\nexport default function Home() {\n  return <main><Hero /></main>;\n}\n"
#: Where the heading opens in Hero.tsx, as the preview build tags it.
H1 = "frontend/components/Hero.tsx:4:7"

parser = pytest.mark.skipif(not source.available()[0], reason="needs node and the TypeScript parser")


# ── the model: a Frontend that writes Hero + page, and changes Hero when asked ──
class Crew:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.new_heading = "Fresh from the crew"

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith("You are the Frontend Engineer"):
            return resp
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            payload["files"] = [
                {"path": "components/Hero.tsx", "purpose": "the hero"},
                {"path": "app/page.tsx", "purpose": "home page", "imports": ["components/Hero.tsx"]},
            ]
            return resp.model_copy(update={"text": json.dumps(payload)})
        prompt = messages[-1].content
        if "change 1 file" in prompt:
            self.edits.append(prompt)
            changed = HERO.replace("Welcome MARKER-78", self.new_heading)
            return resp.model_copy(update={"text": fenced({"frontend/components/Hero.tsx": changed})})
        files = {p: (HERO if p.endswith("Hero.tsx") else PAGE) for p in asked_files(messages)}
        return resp.model_copy(update={"text": fenced(files)})


# ── the sandbox: a build that renders the heading, as next build would ─────────
def render(files: dict[str, str]) -> bytes:
    hero = files.get("frontend/components/Hero.tsx", "")
    m = re.search(r"<h1([^>]*)>([^<]*)</h1>", hero)
    attrs, words = (m.group(1), m.group(2)) if m else ("", "")
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><title>Recipes</title></head>"
        f"<body><main><section><h1{attrs}>{words}</h1></section></main>"
        "<script src=\"/_next/static/chunks/main.js\" async=\"\"></script></body></html>"
    ).encode("utf-8")


class FakeHandle:
    def __init__(self, plan: app_runtime.AppPlan) -> None:
        self.plan = plan
        self.html = render(plan.files)
        self.requests: list[tuple[str, str, dict]] = []
        self.up = True

    def request(self, method, path, headers, body, timeout=60.0):
        self.requests.append((method, path, dict(headers)))
        if path.startswith("/_next/"):
            return 200, {"content-type": "application/javascript", "set-cookie": "x=1"}, b"console.log(1)"
        if path.startswith("/__api"):
            return 503, {"content-type": "application/json"}, b'{"detail":"frontend only"}'
        if path == "/account":
            return 307, {"location": "http://localhost/login?next=%2Faccount"}, b""
        return 200, {
            "content-type": "text/html; charset=utf-8",
            "set-cookie": "session=stolen; Domain=localhost",
            "x-frame-options": "DENY",
            "content-security-policy": "default-src *",
        }, self.html

    def alive(self) -> bool:
        return self.up

    def stop(self) -> None:
        self.up = False

    def start_backend(self, plan, inst) -> None:
        inst.set_backend("down", "No database here.")


class FakeAppEngine:
    kind = "fake"

    def __init__(self) -> None:
        self.plans: list[app_runtime.AppPlan] = []
        self.handles: list[FakeHandle] = []
        self.fail: Optional[str] = None

    def start(self, inst, plan, on_step):
        self.plans.append(plan)
        for step in plan.steps:
            on_step(step)
        if self.fail:
            raise app_runtime.AppBuildFailed(self.fail, [{"path": "frontend/app/page.tsx", "message": "boom"}])
        handle = FakeHandle(plan)
        self.handles.append(handle)
        return handle


@pytest.fixture
def app_engine(monkeypatch):
    engine = FakeAppEngine()
    monkeypatch.setattr(app_runtime, "engine", engine)
    monkeypatch.setattr(settings, "build_run_enabled", True)
    yield engine
    app_runtime.stop_all()
    app_runtime._transient.clear()


def _ok(step, files):
    return 0, "added 105 packages in 3s\n" if step.label == "npm install" else "done\n"


class BuildEngine:
    """The real build (#75), scripted: `fails` decides which trees don't build."""

    kind = "docker"

    def __init__(self, fails=lambda files: False) -> None:
        self.fails = fails
        self.runs: list[dict[str, str]] = []

    def run(self, image, files, steps, limits, on_cancel, on_step):
        self.runs.append(dict(files))
        out, failed = [], False
        for step in steps:
            if failed:
                out.append(StepResult(step.name, step.label, None, 0.0, skipped=True))
                continue
            on_step(step)
            if step.name == "build" and self.fails(files):
                out.append(StepResult(step.name, step.label, 1, 1.0,
                                      "Failed to compile.\n./components/Hero.tsx:4:7\nType error: broken heading\n"))
                failed = True
                continue
            code, text = _ok(step, files)
            out.append(StepResult(step.name, step.label, code, 1.0, text))
        return out


@pytest.fixture
def built(monkeypatch):
    engine = BuildEngine()
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", engine)
    return engine


def _wait(fn, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def _finished_build(client, monkeypatch, crew: Optional[Crew] = None) -> str:
    stub(monkeypatch, "complete", crew or Crew())
    r = client.post("/api/projects", json={"idea": "A recipe site", "routing_mode": "local_only", "approval_mode": "unattended"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    _wait(lambda: client.get(f"/api/projects/{pid}").json()["status"] in ("completed", "awaiting_approval"), 30)
    return pid


def _running(client, pid: str) -> dict:
    client.post(f"/api/projects/{pid}/preview/app")
    return _wait(lambda: (lambda s: s if s["app"]["status"] == "running" else None)(
        client.get(f"/api/projects/{pid}/preview").json()
    ))


def _host(url: str) -> str:
    return url.split("://", 1)[1]


def _frontend_row(pid: str) -> PhaseResult:
    with SessionLocal() as db:
        rows = (
            db.query(PhaseResult)
            .filter(PhaseResult.project_id == pid, PhaseResult.phase == "frontend_engineer")
            .order_by(PhaseResult.created_at, PhaseResult.id)
            .all()
        )
        current = [r for r in rows if r.status not in ("rejected", "failed", "running")]
        db.expunge_all()
        return current[-1]


def _hero(pid: str) -> str:
    row = _frontend_row(pid)
    return next(f["code"] for f in row.output["files"] if f["path"].endswith("components/Hero.tsx"))


# ── the app is what is previewed ─────────────────────────────────────────────
@parser
def test_the_preview_is_the_built_app_served_byte_for_byte_from_its_own_origin(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    state = _running(client, pid)
    assert state["source"] == "app"
    app = state["app"]
    assert app["built_from"] == _frontend_row(pid).id and app["stack"] == "nextjs"
    assert re.match(r"^http://[0-9a-f]{32}\.localhost:8000$", app["url"])

    page = client.get("/", headers={"host": _host(app["url"])})
    handle = app_engine.handles[-1]
    assert page.status_code == 200
    # What the sandbox served, byte for byte — with only the picker's loader in <head>.
    assert page.content == app_proxy.inject(handle.html)
    assert b"MARKER-78" in page.content
    assert page.content.replace(app_proxy.stub().encode(), b"") == handle.html
    # And it was built from the archive's own files.
    plan = app_engine.plans[-1]
    from app.core import artifacts

    with SessionLocal() as db:
        assembled = artifacts.assemble(db.get(Project, pid))
    for f in assembled["files"]:
        if f["path"].startswith("frontend/") and f["path"] != "frontend/next.config.js":
            assert re.sub(r' data-src="[^"]*"', "", plan.files[f["path"]]) == f["content"], f["path"]
    assert f'data-src="{H1}"' in plan.files["frontend/components/Hero.tsx"]
    assert plan.tagged >= 3

    # Served on its own terms: no cookies, our CSP, framed only by this app.
    assert "set-cookie" not in page.headers
    assert "x-frame-options" not in page.headers
    csp = page.headers["content-security-policy"]
    assert "connect-src 'self'" in csp and "frame-ancestors" in csp and "http://localhost:3000" in csp
    assert "default-src *" not in csp
    asset = client.get("/_next/static/chunks/main.js", headers={"host": _host(app["url"])})
    assert asset.status_code == 200 and "set-cookie" not in asset.headers
    # No sketch was drawn: the app runs.
    assert state["revisions"] == []


@parser
def test_the_app_sees_its_own_host_and_its_redirects_stay_on_its_origin(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    host = _host(_running(client, pid)["app"]["url"])
    r = client.get("/account", headers={"host": host}, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/login?next=%2Faccount"
    method, path, headers = app_engine.handles[-1].requests[-1]
    assert headers["host"] == host and "cookie" not in headers


@parser
def test_a_start_the_sandbox_couldnt_finish_waits_for_a_retry(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)

    def broken(inst, plan, on_step):
        raise sandbox.SandboxError("Docker couldn't pull node:20-alpine")

    app_engine.start = broken
    client.post(f"/api/projects/{pid}/preview/app")
    state = _wait(lambda: (lambda s: s if s["app"]["status"] == "failed" else None)(
        client.get(f"/api/projects/{pid}/preview").json()
    ))
    assert "couldn't pull" in state["app"]["reason"]
    # Not tried again by itself, however long the tab stays open...
    row, why, at = app_runtime._transient[pid]
    app_runtime._transient[pid] = (row, why, at - 3600)
    client.post(f"/api/projects/{pid}/preview/app")
    assert client.get(f"/api/projects/{pid}/preview").json()["app"]["status"] == "failed"
    # ...only when asked.
    del app_engine.start  # the engine's own start again
    client.post(f"/api/projects/{pid}/preview/app", json={"retry": True})
    _wait(lambda: client.get(f"/api/projects/{pid}/preview").json()["app"]["status"] == "running")


@parser
def test_an_app_preview_that_fails_to_build_asks_for_the_sketch(client, monkeypatch, built, app_engine):
    from app.orchestration.runner import runner

    asked: list = []
    monkeypatch.setattr(runner, "draw_sketch_for", lambda pid, row, owner: asked.append((pid, row)))
    pid = _finished_build(client, monkeypatch)
    app_engine.fail = "The app didn't build: `next build` failed · 1 error"
    client.post(f"/api/projects/{pid}/preview/app")
    _wait(lambda: asked)
    assert asked == [(pid, _frontend_row(pid).id)]
    with SessionLocal() as db:
        assert runner._app_preview_runs(db, _frontend_row(pid).id) is False


def test_the_proxy_refuses_a_host_without_a_live_token(client, app_engine):
    for host in ("0" * 32 + ".localhost:8000", "nottoken.localhost:8000", "localhost:8000"):
        r = client.get("/api/projects", headers={"host": host})
        if host == "localhost:8000":
            assert r.status_code == 200  # the API itself, signed in
        elif host.startswith("0"):
            assert r.status_code == 404 and "stopped" in r.text
        else:
            assert r.status_code in (200, 401)  # not a preview host at all: the API answers
    # The token is the whole credential: the API's cookie never reaches a preview.
    assert app_runtime.token_from_host("z" * 32 + ".localhost") is None
    assert app_runtime.token_from_host("a" * 32 + ".localhost:8000") == "a" * 32
    assert app_runtime.token_from_host("a" * 32 + ".evil.com") is None


def test_a_served_preview_has_no_network_and_publishes_no_port(monkeypatch):
    monkeypatch.setattr(sandbox, "docker", lambda: "/usr/bin/docker")
    box = sandbox.Sandbox(sandbox.NODE_IMAGE, sandbox.Limits(), preview=True)
    args = box._args("x", "node relay", network=False, env={}, interactive=True, cache=False)
    assert args[args.index("--network") + 1] == "none"
    assert not any(a in ("-p", "--publish", "-P", "--publish-all") for a in args)
    assert "--read-only" in args and "ALL" in args and "-i" in args
    assert not any("aiteam-cache" in a for a in args)
    assert "aiteam.preview=1" in args


@parser
def test_an_idle_preview_stops_and_starts_again_on_the_next_open(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    first = _running(client, pid)["app"]["url"]
    inst = app_runtime.serving(pid)
    monkeypatch.setattr(settings, "preview_app_ttl_seconds", 60)
    inst.last_access -= 61
    assert app_runtime.reap() == [inst]
    assert app_runtime.lookup(inst.token) is None
    assert client.get("/", headers={"host": _host(first)}).status_code == 404
    assert client.get(f"/api/projects/{pid}/preview").json()["app"]["status"] == "idle"
    again = _running(client, pid)["app"]["url"]
    assert again != first
    assert client.get("/", headers={"host": _host(again)}).status_code == 200


@parser
def test_a_build_stopped_while_it_builds_is_never_put_on_screen_or_called_failed(client, monkeypatch, built, app_engine):
    import threading

    pid = _finished_build(client, monkeypatch)
    gate, started = threading.Event(), threading.Event()
    real = app_engine.start

    def slow(inst, plan, on_step):
        started.set()
        gate.wait(5)
        return real(inst, plan, on_step)

    app_engine.start = slow
    client.post(f"/api/projects/{pid}/preview/app")
    assert started.wait(5)
    app_runtime.stop(pid)  # let go while it builds
    gate.set()
    time.sleep(0.3)
    assert app_runtime.serving(pid) is None and pid not in app_runtime._transient
    assert client.get(f"/api/projects/{pid}/preview").json()["app"]["status"] == "idle"


def test_at_most_max_running_apps_and_the_least_recent_stops(monkeypatch):
    monkeypatch.setattr(settings, "preview_app_max_running", 1)
    a = app_runtime.Instance("pa", None, "ra", None)
    b = app_runtime.Instance("pb", None, "rb", None)
    for inst, when in ((a, 1.0), (b, 2.0)):
        inst.status, inst.handle, inst.last_access = app_runtime.RUNNING, FakeHandle.__new__(FakeHandle), when
        inst.handle.up = True
        app_runtime._serving[inst.project_id] = inst
    try:
        app_runtime._cap()
        assert app_runtime.serving("pa") is None and app_runtime.serving("pb") is b
    finally:
        app_runtime.stop_all()


# ── the sketch, when there is no app ─────────────────────────────────────────
def test_before_the_frontend_the_tab_offers_the_sketch_and_says_why(client, app_engine):
    pid = client.post("/api/projects", json={"idea": "A recipe site", "routing_mode": "local_only"}).json()["id"]
    state = client.get(f"/api/projects/{pid}/preview").json()
    assert state["app"]["status"] == "none"
    assert state["source"] is None and "hasn't written the frontend" in state["source_note"]


def test_with_builds_off_the_app_is_unavailable_and_says_so(client, monkeypatch):
    monkeypatch.setattr(app_runtime, "engine", None)
    monkeypatch.setattr(settings, "build_run_enabled", False)
    ok, why = app_runtime.available()
    assert not ok and "BUILD_RUN_ENABLED" in why


@parser
def test_a_preview_build_that_fails_falls_back_to_the_sketch_with_the_reason(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    app_engine.fail = "The app didn't build: `next build` failed · 1 error"
    client.post(f"/api/projects/{pid}/preview/app")
    state = _wait(lambda: (lambda s: s if s["app"]["status"] == "failed" else None)(
        client.get(f"/api/projects/{pid}/preview").json()
    ))
    assert state["source"] is None or state["source"] == "sketch"
    assert state["app"]["reason"].startswith("The app didn't build") and state["app"]["problems"]
    # Remembered: opening the tab again doesn't rebuild what is known to fail...
    builds = len(app_engine.plans)
    client.post(f"/api/projects/{pid}/preview/app")
    time.sleep(0.2)
    assert len(app_engine.plans) == builds
    # ...until asked to.
    app_engine.fail = None
    client.post(f"/api/projects/{pid}/preview/app", json={"retry": True})
    _wait(lambda: client.get(f"/api/projects/{pid}/preview").json()["app"]["status"] == "running")


def test_a_frontend_whose_real_build_failed_is_never_started(client, monkeypatch, app_engine):
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(build_runner, "engine", BuildEngine(fails=lambda files: True))
    monkeypatch.setattr(settings, "auto_fix_max_rounds", 0)
    pid = _finished_build(client, monkeypatch)
    state = client.get(f"/api/projects/{pid}/preview").json()
    assert state["app"]["status"] == "failed" and "didn't build" in state["app"]["reason"]
    client.post(f"/api/projects/{pid}/preview/app")
    time.sleep(0.2)
    assert app_engine.plans == []


# ── changing the app changes the code ────────────────────────────────────────
@parser
def test_typing_over_the_heading_changes_the_file_the_build_and_the_zip(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    app = _running(client, pid)["app"]
    before = _frontend_row(pid)
    builds = len(built.runs)
    r = client.post(
        f"/api/projects/{pid}/preview/patch",
        json={"ops": [{"oid": H1, "kind": "text", "text": "Cook something new"},
                      {"oid": H1, "kind": "classes", "add": ["text-5xl"], "remove": ["text-4xl"]}],
              "summary": "Heading: Text, Size", "target": "app", "built_from": app["built_from"]},
    )
    assert r.status_code == 200, r.text
    after = _frontend_row(pid)
    assert after.id != before.id
    hero = _hero(pid)
    assert '<h1 className="font-bold text-5xl">Cook something new</h1>' in hero
    assert "data-src" not in hero  # the tags are the preview build's alone
    # The other file is untouched, and the old attempt is kept, superseded.
    page = next(f["code"] for f in after.output["files"] if f["path"].endswith("app/page.tsx"))
    assert page == next(f["code"] for f in before.output["files"] if f["path"].endswith("app/page.tsx"))
    with SessionLocal() as db:
        assert db.get(PhaseResult, before.id).status == "rejected"
    # Compiled and built for real, again.
    assert after.build_status == "ok" and after.build_run["status"] == "ok"
    assert len(built.runs) > builds and "Cook something new" in built.runs[-1]["components/Hero.tsx"]
    # In the download.
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/download").content))
    member = next(n for n in z.namelist() if n.endswith("frontend/components/Hero.tsx"))
    assert "Cook something new" in z.read(member).decode()
    # The build is back where it was, and the app restarts with the change.
    state = client.get(f"/api/projects/{pid}/preview").json()
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"
    assert state["app"]["editing"]["status"] == "landed" and state["app"]["can_undo"]
    fresh = _running(client, pid)
    page = client.get("/", headers={"host": _host(fresh["app"]["url"])})
    assert b"Cook something new" in page.content

    # Undo brings the crew's version back, as a new attempt.
    r = client.post(f"/api/projects/{pid}/preview/undo?target=app&built_from={fresh['app']['built_from']}")
    assert r.status_code == 200, r.text
    assert "Welcome MARKER-78" in _hero(pid)
    state = client.get(f"/api/projects/{pid}/preview").json()
    assert state["app"]["can_redo"] and not state["app"]["can_undo"]
    r = client.post(f"/api/projects/{pid}/preview/redo?target=app")
    assert r.status_code == 200, r.text
    assert "Cook something new" in _hero(pid)


@parser
def test_ask_the_crew_rewrites_only_the_file_that_draws_the_element(client, monkeypatch, built, app_engine):
    crew = Crew()
    pid = _finished_build(client, monkeypatch, crew)
    app = _running(client, pid)["app"]
    where = client.get(f"/api/projects/{pid}/preview/locate", params={"oid": H1}).json()
    assert where["path"] == "frontend/components/Hero.tsx" and where["start_line"] == 4 and where["owner"] is None
    assert where["text"] == {"editable": True, "value": "Welcome MARKER-78"}
    page_before = next(f["code"] for f in _frontend_row(pid).output["files"] if f["path"].endswith("app/page.tsx"))

    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"oid": H1, "instruction": "Make the heading warmer", "target": "app", "built_from": app["built_from"]},
    )
    assert r.status_code == 200, r.text
    assert len(crew.edits) == 1
    prompt = crew.edits[0]
    assert "- `frontend/components/Hero.tsx`" in prompt and "line 4" in prompt and "Make the heading warmer" in prompt
    assert "Welcome MARKER-78" in prompt  # the file as it is, to change in place
    assert "app/page.tsx`" not in prompt.split("# Write now")[1].split("\n# ")[0]
    assert "Fresh from the crew" in _hero(pid)
    row = _frontend_row(pid)
    assert next(f["code"] for f in row.output["files"] if f["path"].endswith("app/page.tsx")) == page_before
    assert row.handoff["edit"]["kind"] == "ask"


@parser
def test_a_change_that_breaks_the_build_is_refused_and_nothing_moves(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    app = _running(client, pid)["app"]
    before = _frontend_row(pid)
    built.fails = lambda files: any("BROKEN" in c for p, c in files.items() if p.endswith("Hero.tsx"))
    r = client.post(
        f"/api/projects/{pid}/preview/patch",
        json={"ops": [{"oid": H1, "kind": "text", "text": "BROKEN"}], "target": "app", "built_from": app["built_from"]},
    )
    assert r.status_code == 200
    assert _frontend_row(pid).id == before.id and "Welcome MARKER-78" in _hero(pid)
    state = client.get(f"/api/projects/{pid}/preview").json()
    assert state["app"]["editing"]["status"] == "refused"
    assert "didn't build" in state["app"]["editing"]["reason"]
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and project["current_phase"] == "cost_estimation"


@parser
def test_computed_words_and_stale_addresses_are_refused_with_the_reason(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    app = _running(client, pid)["app"]
    # The page's <main> holds a component, not words.
    r = client.post(
        f"/api/projects/{pid}/preview/patch",
        json={"ops": [{"oid": "frontend/app/page.tsx:4:10", "kind": "text", "text": "x"}], "target": "app"},
    )
    detail = r.json()["detail"]
    assert r.status_code == 422 and ("other elements" in detail or "no words" in detail), detail
    # A platform file can't be changed from here.
    r = client.post(
        f"/api/projects/{pid}/preview/patch",
        json={"ops": [{"oid": "frontend/app/layout.tsx:9:5", "kind": "text", "text": "x"}], "target": "app"},
    )
    assert r.status_code == 422
    # Aimed at code that has moved on.
    r = client.post(
        f"/api/projects/{pid}/preview/patch",
        json={"ops": [{"oid": H1, "kind": "text", "text": "x"}], "target": "app", "built_from": "f" * 32},
    )
    assert r.status_code == 409 and "changed since" in r.json()["detail"]
    assert app["built_from"] == _frontend_row(pid).id


@parser
def test_site_style_on_the_app_is_written_into_the_tailwind_config_that_ships(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    app = _running(client, pid)["app"]
    assert app["theme"]["scope"] == "app" and app["theme"]["families"]["primary"] == "indigo"
    assert app["theme"]["densities"] == []
    r = client.patch(
        f"/api/projects/{pid}/preview/theme",
        json={"primary": "#0f766e", "radius": "lg", "font_pair": "editorial", "target": "app"},
    )
    assert r.status_code == 200, r.text
    from app.core import artifacts

    with SessionLocal() as db:
        files = {f["path"]: f["content"] for f in artifacts.assemble(db.get(Project, pid))["files"]}
    config = files["frontend/tailwind.config.js"]
    assert '"indigo": {' in config and '"600": "#0f766e"' in config and '"lg": "16px"' in config
    assert "Fraunces" in config
    # CSS-valid families: a name with a digit or space quoted, and headings given one stack.
    assert '"\\"Source Sans 3\\""' in config
    assert "fontFamily: \"Fraunces, ui-serif, Georgia, serif\"" in config
    css = next(c for p, c in files.items() if p.startswith("frontend/") and p.endswith(".css") and "@tailwind base" in c)
    assert css.startswith("@import url('https://fonts.googleapis.com/css2?family=Fraunces")
    # Density has no single place in code.
    r = client.patch(f"/api/projects/{pid}/preview/theme", json={"density": "compact", "target": "app"})
    assert r.status_code == 422 and "Density" in r.json()["detail"]


@parser
def test_a_crew_rewrite_of_the_frontend_keeps_the_chosen_site_style(client, monkeypatch, built, app_engine):
    pid = _finished_build(client, monkeypatch)
    _running(client, pid)
    client.patch(f"/api/projects/{pid}/preview/theme", json={"primary": "#0f766e", "target": "app"})
    assert _frontend_row(pid).output["app_theme"]["primary"]
    # A redo from the Ship review rewrites the frontend through the model.
    from app.orchestration.runner import runner

    with SessionLocal() as db:
        project = db.get(Project, pid)
        project.status = "awaiting_approval"
        project.current_phase = "cost_estimation"
        db.commit()
        runner.redo(db, project, "frontend_engineer", "tighten the layout")
    assert _frontend_row(pid).output.get("app_theme", {}).get("primary")
    # And the rewrite was built with it: the passing build is of the config that ships.
    assert '"600": "#0f766e"' in built.runs[-1]["tailwind.config.js"]


@parser
def test_a_model_that_fails_during_ask_the_crew_leaves_the_build_as_it_was(client, monkeypatch, built, app_engine):
    from app.router.base import ProviderError

    crew = Crew()
    pid = _finished_build(client, monkeypatch, crew)
    app = _running(client, pid)["app"]
    before = _frontend_row(pid)

    def down(messages, **kwargs):
        if "change 1 file" in messages[-1].content:
            raise ProviderError("the local runtime refused the connection")
        return crew(messages, **kwargs)

    stub(monkeypatch, "complete", down)
    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"oid": H1, "instruction": "Make it warmer", "target": "app", "built_from": app["built_from"]},
    )
    assert r.status_code == 200, r.text
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed" and not project.get("last_error")
    assert _frontend_row(pid).id == before.id and _frontend_row(pid).status == before.status
    state = client.get(f"/api/projects/{pid}/preview").json()
    assert state["app"]["editing"]["status"] == "refused" and "didn't answer" in state["app"]["editing"]["reason"]


@parser
def test_a_crash_inside_a_preview_change_puts_the_frontend_back(client, monkeypatch, built, app_engine):
    from app.agents.base import BaseAgent

    pid = _finished_build(client, monkeypatch)
    app = _running(client, pid)["app"]
    before = _frontend_row(pid)

    def boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(BaseAgent, "_run_build", boom)
    r = client.post(
        f"/api/projects/{pid}/preview/patch",
        json={"ops": [{"oid": H1, "kind": "text", "text": "Anything"}], "target": "app", "built_from": app["built_from"]},
    )
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"
    with SessionLocal() as db:
        rows = db.query(PhaseResult).filter(PhaseResult.project_id == pid, PhaseResult.phase == "frontend_engineer").all()
        assert not [r for r in rows if r.status == "running"]
    assert _frontend_row(pid).id == before.id and "Welcome MARKER-78" in _hero(pid)
    assert "disk full" in client.get(f"/api/projects/{pid}/preview").json()["app"]["editing"]["reason"]


def test_the_sweeps_read_volume_labels_the_way_docker_can(monkeypatch):
    calls: list = []

    class Done:
        stdout, returncode = "", 0

    def run(args, **kwargs):
        calls.append(args)
        return Done()

    monkeypatch.setattr(sandbox, "docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    sandbox.sweep()
    sandbox.sweep_previews()
    formats = [a[a.index("--format") + 1] for a in calls if "--format" in a]
    assert formats and all("index .Labels" not in f and ".Label " in f for f in formats)


def test_previews_left_under_our_own_pid_by_an_earlier_process_are_swept(monkeypatch):
    calls: list = []
    me = str(os.getpid())

    class Done:
        def __init__(self, stdout=""):
            self.stdout, self.returncode = stdout, 0

    def run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["container", "ls"]:
            return Done(f"left {me} oldinstance\nmine {me} {sandbox.INSTANCE}\n")
        return Done()

    monkeypatch.setattr(sandbox, "docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    sandbox.sweep_previews()
    removed = [a for a in calls if a[1:3] == ["container", "rm"]]
    assert removed == [["/usr/bin/docker", "container", "rm", "-f", "left"]]


def test_edits_wait_for_a_build_that_is_running(client, app_engine):
    pid = client.post("/api/projects", json={"idea": "A recipe site", "routing_mode": "local_only"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/preview/patch", json={"ops": [{"oid": H1, "kind": "text", "text": "x"}], "target": "app"})
    assert r.status_code == 409 and "hasn't written the frontend" in r.json()["detail"]


# ── the code's addresses ─────────────────────────────────────────────────────
@parser
def test_tags_and_edits_keep_to_what_the_code_states_plainly():
    files = {"frontend/components/Hero.tsx": HERO}
    tagged, n = source.tag(files)
    assert n == 3 and f'<h1 data-src="{H1}" className' in tagged["frontend/components/Hero.tsx"]
    new, refused = source.edit(files, [
        {"path": "frontend/components/Hero.tsx", "line": 4, "col": 7, "kind": "text", "text": "Hi {you} & me"},
        {"path": "frontend/components/Hero.tsx", "line": 5, "col": 7, "kind": "attr", "name": "href", "value": "/all"},
        {"path": "frontend/components/Hero.tsx", "line": 3, "col": 5, "kind": "classes", "add": [], "remove": ["shadow-lg"]},
    ])
    assert '<h1 className="text-4xl font-bold">{"Hi {you} & me"}</h1>' in new["frontend/components/Hero.tsx"]
    assert 'href="/all"' in new["frontend/components/Hero.tsx"]
    assert len(refused) == 1 and "shadow-lg" in refused[0]["reason"]
    # A JSX attribute string has no escapes: a quote in it is written as an expression.
    img = {"frontend/x.tsx": 'export default function X() {\n  return <img src="/a.png" alt="hero" />;\n}\n'}
    new, refused = source.edit(img, [{"path": "frontend/x.tsx", "line": 2, "col": 10, "kind": "attr", "name": "alt", "value": '27" screen & stand'}])
    assert not refused and 'alt={"27\\" screen & stand"}' in new["frontend/x.tsx"]
    assert source.tag(new)[1] == 1  # and it still parses


def test_while_the_crew_changes_the_frontend_the_attempt_it_replaces_still_counts():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    t = datetime(2026, 10, 7, tzinfo=timezone.utc)
    code = {"files": [{"path": "a.tsx", "code": "x"}]}
    older = SimpleNamespace(id="o", phase="frontend_engineer", status="rejected", output=code, created_at=t)
    replaced = SimpleNamespace(id="r", phase="frontend_engineer", status="rejected", output=code, created_at=t + timedelta(minutes=1))
    writing = SimpleNamespace(id="w", phase="frontend_engineer", status="running", output={}, created_at=t + timedelta(minutes=2))
    row, busy = app_runtime.current_frontend(SimpleNamespace(phases=[older, replaced, writing]))
    assert busy and row is replaced
    landed = SimpleNamespace(id="w", phase="frontend_engineer", status="pending_approval", output=code, created_at=writing.created_at)
    row, busy = app_runtime.current_frontend(SimpleNamespace(phases=[older, replaced, landed]))
    assert not busy and row is landed


def test_app_routes_skip_the_pages_that_need_a_parameter():
    routes = app_runtime.app_routes({
        "app/page.tsx": "", "app/recipes/page.tsx": "", "app/recipes/[id]/page.tsx": "",
        "app/(marketing)/about/page.tsx": "", "app/api/x/route.ts": "",
    })
    assert [r["path"] for r in routes] == ["/", "/about", "/recipes"]


def test_the_preview_columns_are_added_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, idea TEXT)"))
        conn.execute(text("CREATE TABLE preview_revisions (id VARCHAR(32) PRIMARY KEY, html TEXT)"))
        conn.execute(text("INSERT INTO preview_revisions (id, html) VALUES ('r', '<p>')"))
    applied = run_migrations(engine)
    assert "projects.preview_app" in applied and "preview_revisions.built_from" in applied
    assert run_migrations(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id, built_from FROM preview_revisions")).fetchall() == [("r", None)]
    assert "preview_app" in {c["name"] for c in inspect(engine).get_columns("projects")}


# ── a real container (opt-in) ────────────────────────────────────────────────
@pytest.mark.skipif(
    os.environ.get("PREVIEW_APP_DOCKER_TESTS") != "1" or not sandbox.available(refresh=True)[0],
    reason="builds and serves a real Next.js app: set PREVIEW_APP_DOCKER_TESTS=1 with Docker running",
)
def test_docker_a_real_next_app_is_built_and_served_through_the_relay(monkeypatch):
    from app.build.scaffold import build as scaffold_build

    tree = {"frontend/app/page.tsx": "export default function Home() {\n  return <main><h1>Hello MARKER-REAL</h1></main>;\n}\n"}
    for f in scaffold_build(tree).files:
        tree[f.path] = f.content
    monkeypatch.setattr(app_runtime, "engine", None)
    monkeypatch.setattr(settings, "build_run_enabled", True)
    inst = app_runtime.Instance("real", None, "row", None)
    plan, why = app_runtime.plan_for(tree, inst.url)
    assert plan is not None, why
    handle = app_runtime.DockerEngine().start(inst, plan, lambda step: None)
    try:
        status, headers, body = handle.request("GET", "/", {}, b"")
        assert status == 200 and b"MARKER-REAL" in body and b"data-src=" in body
    finally:
        handle.stop()
