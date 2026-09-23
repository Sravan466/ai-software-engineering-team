"""Test fixtures.

Points the app at a throwaway SQLite DB and stubs the LLM router with a fake provider so
the whole pipeline (orchestration, approvals, debate, persistence) can be exercised without
any local runtime or any cloud key.
"""
from __future__ import annotations

import json
import os
import tempfile

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


def _fake_complete(messages, **kwargs) -> LLMResponse:
    options = kwargs.get("options")
    schema = getattr(options, "json_schema", None)
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


def sign_in(c: TestClient, email: str = TEST_EMAIL, password: str = TEST_PASSWORD) -> TestClient:
    r = c.post("/api/auth/signin", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def client(stub_router):
    _auth.attempts.reset()
    with TestClient(app) as c:
        yield sign_in(c)
