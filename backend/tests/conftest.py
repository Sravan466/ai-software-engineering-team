"""Test fixtures.

Points the app at a throwaway SQLite DB and stubs the LLM router with a fake provider so
the whole pipeline (orchestration, approvals, debate, persistence) can be exercised without
any local runtime or any cloud key.
"""
from __future__ import annotations

import json
import re
import os
import tempfile
from typing import Optional

# Must be set BEFORE importing the app (settings are read at import time).
_tmp = tempfile.mkdtemp(prefix="aiteam_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
# The graph's checkpointer is a second SQLite file, opened at import. Left at its
# default it is the developer's real `data/checkpoints.sqlite`, and every pipeline
# test wrote its throwaway builds into it.
os.environ["CHECKPOINT_DB_PATH"] = f"{_tmp}/checkpoints.sqlite"
os.environ["DEFAULT_ROUTING_MODE"] = "local_only"
os.environ["ENABLE_DEBATE"] = "true"
# The compile gate never installs anything from a test. If this checkout's frontend
# has TypeScript installed, the gate reads JavaScript with it; otherwise JavaScript is
# reported unchecked and the tests that need a parser skip themselves.
os.environ["BUILD_CHECK_PROVISION"] = "false"
# Nor does it build anything in Docker (#75): the real build is off unless a test puts
# a fake engine in `app.build.runner.engine` and switches it on.
os.environ["BUILD_RUN_ENABLED"] = "false"
os.environ["BUILD_RUNNER_URL"] = ""
# Nor scan anything (#77): the security scanners are off unless a test switches them
# on and scripts what they report (`scripted_scan`).
os.environ["SECURITY_SCAN_ENABLED"] = "false"
# Model sources: none. Left alone, the suite would find whatever runtimes the machine
# running it has on loopback — and the developer's `.env` would add its own — so a
# test's answer would depend on what happened to be running.
os.environ["LOCAL_DETECT"] = "false"
os.environ["LOCAL_SOURCES"] = ""
os.environ["OLLAMA_BASE_URL"] = ""
# The vector store is cwd-relative too. With Chroma installed, the suite's memory and
# knowledge-base calls would open the developer's real `data/chroma`.
os.environ["CHROMA_PERSIST_DIR"] = f"{_tmp}/chroma"
# Each account's settings files, likewise cwd-relative.
os.environ["USER_DATA_DIR"] = f"{_tmp}/users"
# The key saved API keys are encrypted with. Left alone it would be generated into
# the developer's own ~/.config.
os.environ["SECRETS_KEY_FILE"] = f"{_tmp}/secrets.key"
os.environ.pop("SECRETS_ENCRYPTION_KEY", None)
_frontend = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isfile(os.path.join(_frontend, "node_modules", "typescript", "package.json")):
    os.environ["BUILD_TOOLCHAIN_DIR"] = os.path.abspath(_frontend)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# The two settings files — cloud keys and sources, and each role's model — are
# cwd-relative, which under pytest is the developer's real `backend/data`. Pointed
# at the throwaway directory before the router reads them at import, so no test can
# read a real choice or write over one.
from pathlib import Path  # noqa: E402

from app.core import model_roles as _model_roles, secrets_store as _secrets_store  # noqa: E402
from app.core import model_settings as _model_settings  # noqa: E402

_secrets_store._PATH = Path(_tmp) / "providers.local.json"
_model_roles._PATH = Path(_tmp) / "model_roles.local.json"
_model_settings._PATH = Path(_tmp) / "model_settings.local.json"

from app.main import app  # noqa: E402
import httpx  # noqa: E402

from app.router import keycheck as _keycheck  # noqa: E402

# No test reaches a real provider. A key check that nobody stubbed finds the provider
# "unreachable" — unverified, which leaves routing as it was before checks existed.
_keycheck.transport = httpx.MockTransport(lambda request: httpx.Response(503, json={}))
# Nor GitHub or Vercel: a push or deploy nobody stubbed finds them unreachable.
from app.core import github_publish as _github_publish, vercel as _vercel  # noqa: E402

_github_publish.transport = httpx.MockTransport(lambda request: httpx.Response(503, json={}))
_vercel.transport = httpx.MockTransport(lambda request: httpx.Response(503, json={}))
# Nor Stripe, Resend, Clerk or OpenAI: a connector check nobody stubbed is "saved, not tested".
from app.build import integrations as _integrations  # noqa: E402

_integrations.transport = httpx.MockTransport(lambda request: httpx.Response(503, json={}))
from app.core import auth as _auth, identity  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import User  # noqa: E402
from app.router.model_profile import ModelProfile  # noqa: E402
from app.router.router import ModelRouter, Readiness  # noqa: E402
from app.schemas.llm import LLMResponse, Usage  # noqa: E402

# The account every test acts as. Made once, directly, so the suite doesn't depend
# on the sign-up route — the accounts tests exercise that. It owns the install, as
# the only account on a self-hosted backend does.
TEST_EMAIL = "tester@example.com"
TEST_PASSWORD = "correct horse battery"
init_db()
with SessionLocal() as _db:
    _user = User(email=TEST_EMAIL, password_hash=_auth.hash_password(TEST_PASSWORD), is_owner=True)
    _db.add(_user)
    _db.commit()
    TEST_USER_ID = _user.id
# Work done straight from a test — calling the router, a store, the runner — is done
# for this account, the same as a request signed in as it.
identity.bind(TEST_USER_ID)

# What the debate parser reads. Agents get a payload built from their own schema
# instead — see `_conforming`.
MOCK_JSON = (
    '{"summary": "mock deliverable", '
    '"decision": "PostgreSQL", "arguments": [{"agent": "Security", "position": "Postgres", '
    '"rationale": "relational integrity"}], "rationale": "fits the relational data model"}'
)


def _conforming(schema: dict) -> object:
    """Build the smallest value that satisfies `schema`.

    The stub answers the schema it was handed, which is the same schema a real
    provider constrains decoding to. That keeps these tests exercising orchestration
    rather than accidentally exercising the repair loop — and it means a schema that
    stops matching its agent's model shows up here as a failure.
    """
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties") or {}
        required = schema.get("required") or list(props)
        return {name: _conforming(props.get(name, {})) for name in required}
    if kind == "array":
        return [_conforming(schema.get("items") or {"type": "string"})]
    if kind in ("number", "integer"):
        return 12
    if kind == "boolean":
        return True
    return "mock deliverable"


def asked_files(messages) -> list[str]:
    """The paths a code phase's write call asks for, from its `# Write now` list (#81)."""
    text = messages[-1].content if messages else ""
    head, found, rest = text.partition("# Write now")
    if not found:
        return []
    return re.findall(r"^- `([^`]+)`", rest, flags=re.MULTILINE)


def fenced(files: dict[str, str]) -> str:
    """A write call's answer: each file as `### path` and one fenced block."""
    return "".join(f"### {path}\n```\n{code}{'' if code.endswith(chr(10)) else chr(10)}```\n\n" for path, code in files.items())


def _fake_complete(messages, **kwargs) -> LLMResponse:
    options = kwargs.get("options")
    schema = getattr(options, "json_schema", None)
    wanted = [] if schema else asked_files(messages)
    if wanted:
        text = fenced({path: "mock deliverable" for path in wanted})
    else:
        text = json.dumps(_conforming(schema)) if schema else MOCK_JSON

    return LLMResponse(
        text=text,
        provider="mock",
        model="mock-model",
        usage=Usage(prompt_tokens=12, completion_tokens=34, total_tokens=46),
        latency_ms=3,
    )


#: The model these tests size their prompts against. Fixed on purpose: a probe would
#: read whatever the developer happens to have pulled, so budget-sensitive behaviour
#: would differ between a laptop with a 32k model and CI with none.
STUB_PROFILE = ModelProfile(
    provider="mock",
    model="mock-model",
    context_limit=32768,
    context_window=32768,
    max_output_tokens=4096,
    supports_schema_format=True,
    source="probe",
)


def _fake_profile(*_args, **_kwargs) -> ModelProfile:
    return STUB_PROFILE


def _fake_readiness(*_args, **_kwargs) -> Readiness:
    return Readiness(ok=True)


@pytest.fixture
def stub_router(monkeypatch):
    """Replace the LLM router with the deterministic fake (no local runtime / cloud keys).

    All three halves are stubbed, and none of them is a detail.

    `profile_for` is called before every prompt, and left live it reaches out to a
    local runtime over HTTP — so the suite would depend on whether the machine running it
    has a model pulled, and would block on a timeout per phase when nothing answers.

    `readiness` is the pre-flight check that refuses to start a run whose model was
    never downloaded. Left live it asks that same unreachable runtime and declines to
    start anything — correct on a machine with no local runtime, and exactly wrong
    for a suite whose whole point is not to need one.
    """
    stub(monkeypatch, "complete", _fake_complete)
    stub(monkeypatch, "profile_for", _fake_profile)
    stub(monkeypatch, "readiness", _fake_readiness)


def stub(monkeypatch, name: str, fn) -> None:
    """Replace one router method for every account's router, for this test.

    Patched on the class: every account has its own router, so stubbing whichever
    one a test happened to hold would leave the one a request resolves live.
    """
    monkeypatch.setattr(ModelRouter, name, staticmethod(fn))


def through_database_gate(c: TestClient, pid: str) -> dict:
    """Answer "Continue, I'll add it later" if the run is parked on its database.

    The stub architecture picks PostgreSQL, which needs credentials, so every full run
    now stops there once — in every review mode. Tests about something else answer it
    here, the same way a person would, and carry on.
    """
    project = c.get(f"/api/projects/{pid}").json()
    if project.get("gate_kind") == "database":
        r = c.post(f"/api/projects/{pid}/database/later")
        assert r.status_code == 200, r.text
        project = c.get(f"/api/projects/{pid}").json()
    return project


def through_question_gates(c: TestClient, pid: str) -> dict:
    """Answer both questions only the person can — the database, then the services —
    with "later", the way a person who wants to see the build first would."""
    project = through_database_gate(c, pid)
    if project.get("gate_kind") == "integrations":
        r = c.post(f"/api/projects/{pid}/integrations/later")
        assert r.status_code == 200, r.text
        project = c.get(f"/api/projects/{pid}").json()
    return project


def sign_in(c: TestClient, email: str = TEST_EMAIL, password: str = TEST_PASSWORD) -> TestClient:
    r = c.post("/api/auth/signin", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def client(stub_router):
    _auth.attempts.reset()
    with TestClient(app) as c:
        yield sign_in(c)


@pytest.fixture(autouse=True)
def _fresh_key_check_limit():
    """The key-check rate limit is per account, and the whole suite is one account."""
    _keycheck.limiter.reset()
    from app.api.routes import deploy as _deploy_routes

    _deploy_routes.limiter.reset()
    from app.api.routes import connectors as _connectors_routes

    _connectors_routes.limiter.reset()
    yield


# ── the security scanners, scripted (#77) ────────────────────────────────────
#: The tree a scripted scan sees: one backend file and one frontend page, owned by the
#: engineers who wrote them.
SCAN_TREE = {
    "backend/app/index.js": (
        "const express = require('express');\n"
        "const db = require('./db');\n"
        "const MONGO = 'mongodb+srv://admin:hunter2pass@cluster0.example.net/app';\n"
        "const app = express();\n"
        "app.get('/u/:id', (req, res) => db.query(\"select * from users where id=\" + req.params.id));\n"
        "module.exports = app;\n"
    ),
    "frontend/pages/groups/new.jsx": "export default function New({ html }) {\n" + "  // …\n" * 10
    + "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n}\n",
}
SCAN_OWNERS = {"backend/app/index.js": "backend_engineer", "frontend/pages/groups/new.jsx": "frontend_engineer"}


def semgrep_hit(rule: str, path: str, line: int, severity: str = "ERROR", cwe: str = "CWE-89", message: str = "") -> dict:
    """One Semgrep result, as the in-sandbox reader condenses it."""
    return {"rule": rule, "path": path, "line": line, "end": line, "severity": severity, "confidence": "HIGH",
            "cwe": f"{cwe}: something", "message": message or f"{rule} matched.", "url": None, "fix": None}


class ScriptedScanner:
    """A fake sandbox that answers the scan's steps (#77).

    Semgrep reports whatever `scans` says next — one list per scan, the last one
    repeating — and every other tool runs clean. `down_after` makes every scan after
    that many fail the way a missing Docker does. `trees` records what each scan was
    handed.
    """

    kind = "docker"

    def __init__(self, scans: list[list[dict]], npm: Optional[list[dict]] = None):
        self.scans = scans
        self.npm = npm or []
        self.trees: list[dict] = []
        self.down_after: Optional[int] = None

    def run(self, image, files, steps, limits, on_cancel, on_step):
        from app.build import scan as _scan, sandbox as _sandbox
        from app.build.sandbox import StepResult

        if self.down_after is not None and len(self.trees) >= self.down_after:
            raise _sandbox.SandboxError("Docker isn't running.")
        found: list[dict] = []
        if image == _scan.PYTHON_IMAGE:
            self.trees.append(dict(files))
            found = self.scans.pop(0) if len(self.scans) > 1 else self.scans[0]
        out = []
        for step in steps:
            on_step(step)
            if step.name not in ("scan",) and step.label != "install scanners":
                # A build's own step (the sandbox switch is on for scans): it passes.
                out.append(StepResult(step.name, step.label, 0, 0.1, ""))
                continue
            if step.label == "install scanners":
                text = "scanners already installed"
            elif step.label == "semgrep":
                text = _scan.MARK + json.dumps({"tool": "semgrep", "ran": True, "version": "1.139.0",
                                                "packs": list(_scan.REGISTRY_PACKS),
                                                "findings": found, "total": len(found)})
            elif step.label == "bandit":
                text = _scan.MARK + json.dumps({"tool": "bandit", "ran": True, "version": "1.8.6", "findings": []})
            elif step.label.startswith("pip-audit"):
                side = step.label[step.label.find("(") + 1 : -1]
                text = _scan.MARK + json.dumps({"tool": "pip-audit", "side": side, "ran": True, "findings": []})
            else:
                side = step.label[step.label.find("(") + 1 : -1]
                text = _scan.MARK + json.dumps({"tool": "npm audit", "side": side, "ran": True, "version": "10.8.2",
                                                "findings": self.npm, "total": len(self.npm)})
            out.append(StepResult(step.name, step.label, 0, 0.1, text))
        return out


def scripted_scan(monkeypatch, scans: list[list[dict]], tree: Optional[dict] = None, npm: Optional[list] = None,
                  owners: Optional[dict] = None) -> ScriptedScanner:
    """Switch the scanners on over `tree`, with Semgrep reporting `scans` in turn."""
    from app.build import runner as _runner, scan as _scan
    from app.core.config import settings as _settings

    scanner = ScriptedScanner(scans, npm)
    monkeypatch.setattr(_settings, "security_scan_enabled", True)
    # The scans run in the sandbox, so its switch is on — and so are builds, which the
    # scripted sandbox passes.
    monkeypatch.setattr(_settings, "build_run_enabled", True)
    monkeypatch.setattr(_runner, "engine", scanner)
    # Every scan here is of the same scripted tree: each must really run.
    monkeypatch.setattr(_scan, "REUSE_SECONDS", -1)
    chosen = dict(tree or SCAN_TREE)
    if npm:
        chosen.setdefault("frontend/package.json", '{\n  "dependencies": {\n    "lodash": "4.17.15"\n  }\n}\n')
    monkeypatch.setattr(_scan, "scan_tree", lambda prior, charter=None, **kw: (dict(chosen), dict(owners or SCAN_OWNERS), sorted(chosen)))
    return scanner
