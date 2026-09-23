"""Accounts: every route is signed in, and no account reaches another's things.

The suite's own account (`tests.conftest`) owns the install. Each test here makes
the other accounts it needs, with addresses of its own, so nothing depends on the
order tests run in.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.core import auth, identity, model_roles, model_settings, secrets_store, userdata
from app.core.config import settings
from app.db.base import Base, SessionLocal
from app.db.models import KnowledgeDoc, Project, UsageEvent, User
from app.main import app
from app.router.router import ModelRouter, routers
from tests.conftest import _fake_complete, sign_in

PASSWORD = "a long enough password"


def _account(*, owner: bool = False) -> User:
    with SessionLocal() as db:
        user = User(
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            password_hash=auth.hash_password(PASSWORD),
            is_owner=owner,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


@pytest.fixture
def two(stub_router):
    """Two signed-in browsers, as two different accounts that don't own the install."""
    auth.attempts.reset()
    a_user, b_user = _account(), _account()
    with TestClient(app) as a, TestClient(app) as b:
        sign_in(a, a_user.email, PASSWORD)
        sign_in(b, b_user.email, PASSWORD)
        yield a, a_user, b, b_user


def _project(c: TestClient, idea: str = "A shared grocery list") -> str:
    r = c.post("/api/projects", json={"idea": idea, "require_approval": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _fill(path: str, **values: str) -> str:
    return re.sub(r"{(\w+)}", lambda m: values.get(m.group(1), "x"), path)


def _routes():
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method != "HEAD":
                yield method, route.path


# ── every route is signed in ──────────────────────────────────────────────────
OPEN = {"/health", "/", "/api/auth/status", "/api/auth/signin", "/api/auth/signup", "/api/auth/signout"}


@pytest.mark.parametrize("method,path", sorted(set(_routes())))
def test_every_route_but_signing_in_and_health_needs_a_signed_in_account(method, path):
    with TestClient(app) as anonymous:
        r = anonymous.request(method, _fill(path, project_id="0" * 32, doc_id="0" * 32))
    if path in OPEN:
        assert r.status_code != 401, f"{method} {path} is meant to be open"
    else:
        assert r.status_code == 401, f"{method} {path} answered {r.status_code} to nobody"


def test_a_forged_or_signed_out_cookie_signs_in_nobody(client):
    assert client.get("/api/projects").status_code == 200
    token = client.cookies.get(auth.COOKIE)
    client.post("/api/auth/signout")
    with TestClient(app) as replay:
        replay.cookies.set(auth.COOKIE, token)
        assert replay.get("/api/projects").status_code == 401, "a signed-out session still worked"
        replay.cookies.set(auth.COOKIE, "made-up")
        assert replay.get("/api/projects").status_code == 401


# ── one account never reaches another's ───────────────────────────────────────
PROJECT_ROUTES = sorted({(m, p) for m, p in _routes() if "{project_id}" in p})


@pytest.mark.parametrize("method,path", PROJECT_ROUTES)
def test_another_accounts_project_is_not_found_on_every_route(two, method, path):
    a, _, b, _ = two
    pid = _project(a)
    r = b.request(method, _fill(path, project_id=pid, key="k"), json={})
    assert r.status_code == 404, f"{method} {path} gave {r.status_code} for someone else's project"
    # The same answer as a project that doesn't exist, so an id can't be probed.
    missing = b.request(method, _fill(path, project_id=uuid.uuid4().hex, key="k"), json={})
    assert missing.status_code == 404 and missing.json() == {
        "detail": r.json()["detail"].replace(pid, missing.json()["detail"].split("'")[1])
    }
    assert a.get(f"/api/projects/{pid}").status_code == 200, "the owner lost their project"


def test_lists_show_only_your_own(two):
    a, _, b, _ = two
    mine = _project(a, "Mine")
    theirs = _project(b, "Theirs")
    assert [p["id"] for p in a.get("/api/projects").json()] == [mine]
    assert [p["id"] for p in b.get("/api/projects").json()] == [theirs]


def test_another_account_cannot_delete_or_run_your_project(two):
    a, _, b, _ = two
    pid = _project(a)
    assert b.delete(f"/api/projects/{pid}").status_code == 404
    assert b.post(f"/api/projects/{pid}/run").status_code == 404
    with SessionLocal() as db:
        project = db.get(Project, pid)
        assert project is not None and project.status == "created"


def test_documents_belong_to_whoever_uploaded_them(two):
    a, a_user, b, _ = two
    with SessionLocal() as db:
        doc = KnowledgeDoc(owner_id=a_user.id, filename="spec.md", chunks=1)
        db.add(doc)
        db.commit()
        doc_id = doc.id
    assert [d["id"] for d in a.get("/api/rag/documents").json()] == [doc_id]
    assert b.get("/api/rag/documents").json() == []
    assert b.delete(f"/api/rag/documents/{doc_id}").status_code == 404
    with SessionLocal() as db:
        assert db.get(KnowledgeDoc, doc_id) is not None


def test_searches_only_read_your_own_documents_and_memory(monkeypatch):
    """The shared vector collection is always narrowed to the asking account."""
    from app.memory.store import MemoryStore
    from app.rag.knowledge_base import KnowledgeBase

    a_user, b_user = _account(), _account()
    with SessionLocal() as db:
        db.add(KnowledgeDoc(owner_id=a_user.id, filename="a.md", chunks=1))
        db.add(Project(owner_id=a_user.id, idea="A's build"))
        db.commit()

    asked: list = []

    class _Collection:
        def query(self, **kwargs):
            asked.append(kwargs.get("where"))
            return {"documents": [["chunk"]], "metadatas": [[{"filename": "a.md"}]]}

    kb, memory = KnowledgeBase(), MemoryStore()
    shared = _Collection()
    kb._get_collection = memory._get_collection = lambda owner_id: shared  # type: ignore[method-assign]
    assert kb.query("anything", owner_id=b_user.id) == "", "B searched A's documents"
    assert memory.recall("anything", owner_id=b_user.id) == "", "B recalled A's builds"
    assert asked == [], "nothing of B's to search, so nothing should have been asked"

    assert kb.query("anything", owner_id=a_user.id)
    assert memory.recall("anything", owner_id=a_user.id)
    with SessionLocal() as db:
        a_docs = set(db.execute(select(KnowledgeDoc.id).where(KnowledgeDoc.owner_id == a_user.id)).scalars())
        a_projects = set(db.execute(select(Project.id).where(Project.owner_id == a_user.id)).scalars())
    assert asked[0] == {"doc_id": next(iter(a_docs))} and asked[1] == {"project_id": next(iter(a_projects))}


def test_usage_is_counted_for_the_account_that_spent_it(two):
    a, a_user, b, _ = two
    pid = _project(a)
    with SessionLocal() as db:
        db.add(UsageEvent(owner_id=a_user.id, project_id=pid, provider="p", model="m", total_tokens=99))
        db.commit()
    assert a.get("/api/analytics/summary").json()["total_tokens"] == 99
    assert b.get("/api/analytics/summary").json()["total_tokens"] == 0
    assert b.get(f"/api/analytics/projects/{pid}").status_code == 404


def test_a_recorded_call_is_owned_by_the_projects_owner():
    from app.analytics import tracker
    from app.schemas.llm import LLMResponse, Usage

    user = _account()
    with SessionLocal() as db:
        project = Project(owner_id=user.id, idea="x")
        db.add(project)
        db.commit()
        # Recorded while bound to the suite's account: the project still decides.
        event = tracker.record(
            db,
            response=LLMResponse(text="", provider="p", model="m", usage=Usage(total_tokens=5)),
            project_id=project.id,
        )
        assert event.owner_id == user.id


# ── keys, sources and choices are per account ─────────────────────────────────
def test_a_cloud_key_is_one_accounts_and_never_returned_in_full(two):
    a, a_user, b, b_user = two
    r = a.put("/api/settings/providers/anthropic", json={"api_key": "sk-ant-secret-abcd"})
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "sk-ant-secret" not in a.get("/api/settings/providers").text
    assert a.get("/api/settings/providers").json()["providers"]["anthropic"]["key_hint"] == "…abcd"
    assert b.get("/api/settings/providers").json()["providers"]["anthropic"]["configured"] is False

    stored = json.loads(userdata.path(a_user.id, "providers.local.json").read_text())
    assert stored["anthropic"]["api_key"] == "sk-ant-secret-abcd"
    assert not userdata.path(b_user.id, "providers.local.json").exists()
    assert (userdata.path(a_user.id, "providers.local.json").stat().st_mode & 0o077) == 0


def test_a_model_override_set_by_one_account_never_changes_anothers_builds(two):
    a, a_user, b, b_user = two
    r = a.put("/api/settings/roles/product_manager", json={"model": "anthropic:claude-test"})
    assert r.status_code == 200, r.text
    assert b.get("/api/settings/roles").json()["roles"][0]["assigned"] is None

    from app.core.constants import RoutingMode

    a_chain = routers.for_user(a_user.id)._resolve_chain(RoutingMode.MANUAL, None, "medium", "product_manager")
    b_chain = routers.for_user(b_user.id)._resolve_chain(RoutingMode.MANUAL, None, "medium", "product_manager")
    assert ("anthropic", "claude-test") in a_chain
    assert ("anthropic", "claude-test") not in b_chain


def test_router_state_and_caches_are_per_account():
    a_user, b_user = _account(), _account()
    ra, rb = routers.for_user(a_user.id), routers.for_user(b_user.id)
    assert ra is routers.for_user(a_user.id), "an account's router is made once"
    assert ra is not rb
    assert ra.sources is not rb.sources, "one account's probes would serve another"
    assert ra._cloud["anthropic"] is not rb._cloud["anthropic"]
    ra.set_provider_key("openai", api_key="sk-a-only")
    assert ra._cloud["openai"].available() and not rb._cloud["openai"].available()
    assert settings.openai_api_key != "sk-a-only", "a key leaked into the shared settings"


def test_only_the_owner_starts_from_the_keys_in_env(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "env-key")
    owner = ModelRouter(_account(owner=True).id, owner=True)
    other = ModelRouter(_account().id, owner=False)
    assert owner._cloud["gemini"].available()
    assert not other._cloud["gemini"].available(), "the operator's key was handed to another account"


def test_a_source_added_by_one_account_is_not_anothers(two, monkeypatch):
    from app.router.runtimes import detect
    from app.router.runtimes.types import Hello

    a, a_user, b, b_user = two
    monkeypatch.setattr(detect, "reaches_server_network", lambda url: False)
    monkeypatch.setattr(detect, "identify", lambda url, key=None, **_: Hello(runtime="vllm", base_url=url))
    r = a.post(
        "/api/settings/sources",
        json={"base_url": "http://gpu.example.com:8000", "label": "A box", "confirm_remote": True},
        headers={"host": "localhost"},
    )
    assert r.status_code == 201, r.text
    sid = next(s["id"] for s in r.json()["sources"] if s["label"] == "A box")
    assert all(s["id"] != sid for s in b.get("/api/settings/local").json()["sources"])
    assert b.delete(f"/api/settings/sources/{sid}").status_code == 404
    assert b.put(f"/api/settings/sources/{sid}", json={"api_key": "k"}).status_code == 404
    assert routers.for_user(a_user.id).source(sid) is not None


def test_an_account_that_does_not_own_the_install_cannot_reach_its_network(two):
    _, _, b, _ = two
    for url in ("http://127.0.0.1:11434", "http://localhost:1234", "http://10.0.0.5:8000", "http://169.254.169.254"):
        r = b.post(
            "/api/settings/sources",
            json={"base_url": url, "confirm_remote": True},
            headers={"host": "localhost"},
        )
        assert r.status_code == 400 and "owns this install" in r.json()["detail"], url


def test_only_the_owner_changes_the_shared_skill_library(two):
    _, _, b, _ = two
    assert b.get("/api/skills").status_code == 200
    body = {"description": "d", "phases": ["qa_engineer"], "body": "Do the thing."}
    assert b.post("/api/skills", json={"name": "b-skill", **body}).status_code == 403
    assert b.delete("/api/skills/anything").status_code == 403


def test_work_with_no_account_bound_is_refused_not_guessed():
    from app.router.router import router

    seen: list = []

    def elsewhere():
        try:
            router.status()
        except identity.NoAccount as e:
            seen.append(e)

    t = threading.Thread(target=elsewhere)
    t.start()
    t.join()
    assert seen, "a thread with no account used someone's router"


def test_a_background_run_spends_its_owners_models(two, monkeypatch):
    """The run is bound to the project's owner, whoever's thread drives it."""
    from app.orchestration.runner import runner

    a, a_user, _, _ = two
    pid = _project(a)
    used: list = []

    def complete(self, messages, **kwargs):
        used.append(self.user_id)
        return _fake_complete(messages, **kwargs)

    monkeypatch.setattr(ModelRouter, "complete", complete)

    def drive():
        with SessionLocal() as db:
            runner.continue_run(db, db.get(Project, pid))

    t = threading.Thread(target=drive)
    t.start()
    t.join(60)
    assert used and set(used) == {a_user.id}, used


# ── signing in ────────────────────────────────────────────────────────────────
def test_passwords_are_stored_as_argon2id():
    user = _account()
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert auth.verify_password(PASSWORD, user.password_hash)
    assert not auth.verify_password("wrong password!", user.password_hash)


def test_the_session_cookie_is_httponly_samesite_and_secure_over_https():
    auth.attempts.reset()
    user = _account()
    with TestClient(app, base_url="https://testserver") as c:
        r = c.post("/api/auth/signin", json={"email": user.email, "password": PASSWORD})
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
    with TestClient(app) as c:
        r = c.post("/api/auth/signin", json={"email": user.email, "password": PASSWORD})
        assert "secure" not in r.headers["set-cookie"].lower(), "http://localhost couldn't sign in"


def test_a_wrong_password_and_an_unknown_email_get_the_same_answer():
    auth.attempts.reset()
    user = _account()
    with TestClient(app) as c:
        wrong = c.post("/api/auth/signin", json={"email": user.email, "password": "not it at all"})
        nobody = c.post("/api/auth/signin", json={"email": "nobody@example.com", "password": "not it at all"})
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()


def test_sign_in_attempts_are_rate_limited(monkeypatch):
    auth.attempts.reset()
    monkeypatch.setattr(settings, "signin_failures_per_email", 3)
    user = _account()
    with TestClient(app) as c:
        for _ in range(3):
            assert c.post("/api/auth/signin", json={"email": user.email, "password": "guess guess"}).status_code == 401
        blocked = c.post("/api/auth/signin", json={"email": user.email, "password": PASSWORD})
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    auth.attempts.reset()


def test_sign_ups_are_closed_unless_the_install_opens_them(monkeypatch):
    auth.attempts.reset()
    body = {"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": PASSWORD}
    with TestClient(app) as c:
        assert c.post("/api/auth/signup", json=body).status_code == 403
        monkeypatch.setattr(settings, "allow_signup", True)
        r = c.post("/api/auth/signup", json=body)
        assert r.status_code == 201 and r.json()["user"]["is_owner"] is False
        assert c.get("/api/auth/me").json()["user"]["email"] == body["email"]
        assert c.post("/api/auth/signup", json=body).status_code == 409


def test_a_request_from_a_site_this_backend_does_not_serve_is_refused(client):
    r = client.post("/api/projects", json={"idea": "x"}, headers={"origin": "http://localhost:5555"})
    assert r.status_code == 403
    ok = client.post("/api/projects", json={"idea": "A shared grocery list"}, headers={"origin": "http://localhost:3000"})
    assert ok.status_code == 201


# ── the migration ─────────────────────────────────────────────────────────────
_OLD_SCHEMA = [
    """CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, idea TEXT NOT NULL, name VARCHAR(256),
       status VARCHAR(32), current_phase VARCHAR(48), routing_mode VARCHAR(16),
       preferred_model VARCHAR(128), require_approval BOOLEAN, created_at DATETIME, updated_at DATETIME)""",
    """CREATE TABLE knowledge_docs (id VARCHAR(32) PRIMARY KEY, filename VARCHAR(512),
       content_type VARCHAR(128), chunks INTEGER, created_at DATETIME)""",
    """CREATE TABLE usage_events (id VARCHAR(32) PRIMARY KEY, project_id VARCHAR(32), phase VARCHAR(48),
       provider VARCHAR(32), model VARCHAR(128), prompt_tokens INTEGER, completion_tokens INTEGER,
       total_tokens INTEGER, cost_usd FLOAT, latency_ms INTEGER, fallback_used BOOLEAN, created_at DATETIME)""",
    "INSERT INTO projects (id, idea, status, routing_mode, require_approval) VALUES ('p1', 'Old build', 'completed', 'local_only', 1)",
    "INSERT INTO projects (id, idea, status, routing_mode, require_approval) VALUES ('p2', 'Another', 'created', 'local_only', 1)",
    "INSERT INTO knowledge_docs (id, filename, chunks) VALUES ('d1', 'notes.md', 3)",
    "INSERT INTO usage_events (id, project_id, provider, model, total_tokens) VALUES ('u1', 'p1', 'ollama', 'm', 10)",
]


def _old_install(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        for statement in _OLD_SCHEMA:
            conn.execute(text(statement))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(secrets_store, "_PATH", legacy / "providers.local.json")
    monkeypatch.setattr(model_roles, "_PATH", legacy / "model_roles.local.json")
    monkeypatch.setattr(model_settings, "_PATH", legacy / "model_settings.local.json")
    monkeypatch.setattr(userdata, "ROOT", tmp_path / "users")
    secrets_store._PATH.write_text(json.dumps({"anthropic": {"api_key": "sk-old"}}))
    model_roles._PATH.write_text(json.dumps({"qa_engineer": "ollama:qwen2.5:7b"}))
    return engine


def _upgrade(engine):
    from app.db import accounts
    from app.db.migrations import run_migrations

    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    return accounts.migrate(engine)


def test_the_migration_gives_everything_from_before_accounts_to_one_owner(tmp_path, monkeypatch):
    engine = _old_install(tmp_path, monkeypatch)
    report = _upgrade(engine)
    assert report["created_owner"] and report["adopted"] == 4
    assert sorted(report["moved"]) == ["model_roles.local.json", "providers.local.json"]

    with Session(engine) as db:
        (owner,) = db.execute(select(User)).scalars().all()
        assert owner.is_owner and not owner.claimed, "nobody should be able to sign in to it yet"
        for model in (Project, KnowledgeDoc, UsageEvent):
            assert {r.owner_id for r in db.execute(select(model)).scalars()} == {owner.id}
    moved = json.loads(userdata.path(owner.id, "providers.local.json").read_text())
    assert moved["anthropic"]["api_key"] == "sk-old", "a key saved before accounts was lost"
    assert not secrets_store._PATH.exists(), "the global key file is still readable by every account"
    assert list(secrets_store._PATH.parent.glob("providers.local.json.moved-to-account-*"))
    assert "ix_projects_owner_id" in {ix["name"] for ix in inspect(engine).get_indexes("projects")}

    # The owner's router reads the moved files: nothing has to be set up again.
    owner_router = ModelRouter(owner.id, owner=True)
    assert owner_router._cloud["anthropic"].available()
    assert owner_router._roles.get("qa_engineer") == "ollama:qwen2.5:7b"


def test_the_migration_is_safe_to_run_on_every_boot(tmp_path, monkeypatch):
    engine = _old_install(tmp_path, monkeypatch)
    _upgrade(engine)
    again = _upgrade(engine)
    assert again == {"created_owner": False, "adopted": 0, "moved": []}
    with Session(engine) as db:
        assert db.execute(select(User)).scalars().all().__len__() == 1


def test_a_row_with_no_owner_after_the_migration_is_left_unreachable(tmp_path, monkeypatch):
    engine = _old_install(tmp_path, monkeypatch)
    _upgrade(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (id, idea, status, routing_mode, require_approval, cancel_requested) VALUES ('p9', 'Stray', 'created', 'local_only', 1, 0)"))
    _upgrade(engine)
    with Session(engine) as db:
        assert db.get(Project, "p9").owner_id is None, "a stray row was handed to the owner"


def test_a_fresh_install_makes_no_account_until_someone_signs_up(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_PATH", tmp_path / "none.json")
    monkeypatch.setattr(model_roles, "_PATH", tmp_path / "none2.json")
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "none3.json")
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    assert _upgrade(engine)["created_owner"] is False
    with Session(engine) as db:
        assert db.execute(select(User)).scalars().all() == []


def test_setting_up_claims_the_owner_and_everything_it_held(tmp_path, monkeypatch):
    from app.api.routes import auth as auth_routes

    engine = _old_install(tmp_path, monkeypatch)
    _upgrade(engine)
    with Session(engine) as db:
        claimed = auth_routes._claim_or_create_owner(db, "me@example.com", "Me", auth.hash_password(PASSWORD))
        assert claimed.is_owner and claimed.claimed and claimed.email == "me@example.com"
        assert {p.owner_id for p in db.execute(select(Project)).scalars()} == {claimed.id}
        assert db.execute(select(User)).scalars().all() == [claimed], "a second owner was made"


def test_setting_up_from_another_machine_needs_the_setup_token(monkeypatch):
    """Whoever reaches a fresh install first would otherwise own it."""
    from app.api.routes import auth as auth_routes

    auth.attempts.reset()
    monkeypatch.setattr(auth_routes, "_needs_setup", lambda db: True)
    body = {"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": PASSWORD}
    with TestClient(app) as c:  # the test client's address is not loopback
        assert c.get("/api/auth/status").json()["setup_needs_token"] is True
        assert c.post("/api/auth/signup", json=body).status_code == 403
        monkeypatch.setattr(settings, "setup_token", "let-me-in")
        assert c.post("/api/auth/signup", json={**body, "setup_token": "wrong"}).status_code == 403
        r = c.post("/api/auth/signup", json={**body, "setup_token": "let-me-in"})
        assert r.status_code == 201 and r.json()["user"]["is_owner"] is True
    with SessionLocal() as db:
        made = db.execute(select(User).where(User.email == body["email"])).scalars().one()
        db.delete(made)
        db.commit()


def test_a_key_on_the_servers_runtime_does_not_make_it_yours_to_download_onto(monkeypatch):
    """Setting a key turns a detected source into an added one; it's still the server's."""
    from app.router.runtimes.sources import SourceError
    from tests.test_runtimes import FakeAdapter, _source

    router = ModelRouter(_account().id, owner=False)
    prov = _source("ollama", [])
    prov.source.base_url = "http://127.0.0.1:11434"
    prov.adapter = FakeAdapter([])
    prov.adapter.can_download = True
    router.sources._providers = {"ollama": prov}
    router.sources._loaded = True
    prov.source.origin = "added"  # what `set_key` leaves behind
    assert router.is_shared(prov)
    with pytest.raises(SourceError, match="owns this install"):
        list(router.pull("ollama", "some-model"))
    assert not ModelRouter(_account(owner=True).id, owner=True).is_shared(prov)


def test_machine_settings_on_the_servers_runtime_are_the_owners(monkeypatch):
    from app.router.runtimes.types import ModelEntry
    from tests.test_runtimes import _source

    router = ModelRouter(_account().id, owner=False)
    prov = _source("lmstudio", [ModelEntry(name="a", kind="chat")])
    router.sources._providers = {"lmstudio": prov}
    router.sources._loaded = True
    with pytest.raises(ValueError, match="server's own hardware"):
        router.set_model_generation("lmstudio:a", {"machine": {"keep_alive": "24h"}})
    # Sampling is still each account's own.
    router.set_model_generation("lmstudio:a", {"sampling": {"temperature": 0.3}})
    assert router.tuning.get(prov.settings_key("a")) == {"sampling": {"temperature": 0.3}}


# ── what the review of #45 found ──────────────────────────────────────────────
def test_the_owners_env_sources_and_the_servers_runtimes_are_not_other_accounts(monkeypatch):
    from app.router.runtimes import detect as detect_module
    from app.router.runtimes.types import Hello

    monkeypatch.setattr(
        settings, "local_sources", '[{"base_url": "https://paid.example.com", "api_key": "sk-owner-secret"}]'
    )
    monkeypatch.setattr(settings, "ollama_base_url", "")
    probed: list = []

    def fake_detect():
        probed.append(1)
        return detect_module.Detection(found=[Hello(runtime="ollama", base_url="http://127.0.0.1:11434")])

    monkeypatch.setattr(detect_module, "detect", fake_detect)
    monkeypatch.setattr(detect_module, "answers_http", lambda url, **_: False)

    other = ModelRouter(_account().id, owner=False)
    other.sources.ensure(wait=True)
    assert other.sources.ids() == set(), "another account got the owner's .env source or the server's runtime"
    assert not probed, "another account's router probed the server's loopback"

    owner = ModelRouter(_account(owner=True).id, owner=True)
    owner.sources.ensure(wait=True)
    assert any(p.source.api_key == "sk-owner-secret" for p in owner.sources.providers())

    monkeypatch.setattr(settings, "share_local_runtimes", True)
    shared = ModelRouter(_account().id, owner=False)
    shared.sources.ensure(wait=True)
    assert all(p.source.api_key != "sk-owner-secret" for p in shared.sources.providers())
    assert any(p.source.origin == "detected" for p in shared.sources.providers()), "opted in, but not shared"


def test_a_name_that_later_points_at_the_server_is_refused_on_every_request(monkeypatch):
    from app.router.base import ProviderError
    from app.router.runtimes import detect as detect_module
    from app.router.runtimes.types import Hello

    router = ModelRouter(_account().id, owner=False)
    router.sources._loaded = True
    router.sources._detected_at = 10**9
    points_home = {"now": False}
    monkeypatch.setattr(detect_module, "reaches_server_network", lambda url: points_home["now"])
    monkeypatch.setattr(detect_module, "identify", lambda url, key=None, **_: Hello(runtime="vllm", base_url=url))
    monkeypatch.setattr(router.sources, "_save", lambda: None)
    prov = router.add_source("http://rebind.example.com:8000", confirm_remote=True)
    assert prov.guard is not None
    points_home["now"] = True  # DNS flipped to 127.0.0.1 after the source was added
    with pytest.raises(ProviderError, match="server's own network"):
        prov.embed("m", ["x"])
    assert prov.state(max_age=0).reachable is False


def test_a_cookie_another_app_set_does_not_sign_anyone_out(client):
    token = client.cookies.get(auth.COOKIE)
    with TestClient(app) as c:
        r = c.get("/api/projects", headers={"cookie": f'x={{"k":1}}; a=b c; {auth.COOKIE}={token}'})
    assert r.status_code == 200, "a stray cookie from another localhost app locked the session out"


def test_pushing_without_github_is_not_mistaken_for_signing_out(client):
    pid = client.post("/api/projects", json={"idea": "A shared grocery list"}).json()["id"]
    r = client.post(f"/api/github/push/{pid}", json={})
    assert r.status_code == 409


def test_while_sign_ups_are_closed_nobody_learns_which_emails_have_accounts(monkeypatch):
    auth.attempts.reset()
    user = _account()
    with TestClient(app) as c:
        taken = c.post("/api/auth/signup", json={"email": user.email, "password": PASSWORD})
        free = c.post("/api/auth/signup", json={"email": "free@example.com", "password": PASSWORD})
    assert taken.status_code == free.status_code == 403
    assert taken.json() == free.json()


def test_a_proxied_request_is_never_from_this_machine_and_the_logged_token_sets_up(monkeypatch):
    from app.api.routes import auth as auth_routes

    auth.attempts.reset()
    monkeypatch.setattr(auth_routes, "_needs_setup", lambda db: True)
    # Reached this process from loopback, as it would behind a proxy on this host.
    monkeypatch.setattr(auth_routes, "_client", lambda request: "127.0.0.1")
    monkeypatch.setattr(settings, "setup_token", None)
    with SessionLocal() as db:
        token = auth_routes.announce_setup_token(db)
    assert token
    body = {"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": PASSWORD}
    with TestClient(app) as c:
        proxied = c.get("/api/auth/status", headers={"x-forwarded-for": "203.0.113.9"})
        assert proxied.json()["setup_needs_token"] is True, "a proxy made the whole internet look local"
        assert c.post("/api/auth/signup", json=body, headers={"x-forwarded-for": "203.0.113.9"}).status_code == 403
        r = c.post("/api/auth/signup", json={**body, "setup_token": token}, headers={"x-forwarded-for": "203.0.113.9"})
        assert r.status_code == 201
    monkeypatch.setattr(auth_routes, "_GENERATED_SETUP_TOKEN", None)
    with SessionLocal() as db:
        db.delete(db.execute(select(User).where(User.email == body["email"])).scalars().one())
        db.commit()
