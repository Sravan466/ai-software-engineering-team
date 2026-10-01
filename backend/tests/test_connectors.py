"""App connectors: the Connectors tab, and builds that use the relevant ones (#59).

The registry's format rules and checks, the account store, detection, the routes,
the build's question after the database one, linking and overrides, and the promise
the feature rests on: a key never reaches a model, a log line, `/artifacts`, the
default download, or the browser unless it is publishable.
"""
from __future__ import annotations

import io
import json
import logging
import os
import stat
import zipfile

import httpx
import pytest

from app.build import integrations
from app.build.check import secret_leaks
from app.core import connectors_store, project_secrets, userdata
from app.orchestration.charter import Charter, freeze
from tests.conftest import TEST_USER_ID, _fake_complete, stub, through_database_gate

LOCAL = {"host": "localhost"}
#: The key every leak check greps for.
SENTINEL = "sk_test_Sentinel7Qx9Pw4Lm2"
PK = "pk_test_Publishable5Rt8Yu1"


def _respond(handler):
    integrations.transport = httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """A fresh account store, and providers that answer only when a test says how."""
    path = userdata.path(TEST_USER_ID, "connectors.local.json")
    if path.exists():
        path.unlink()
    unreachable = integrations.transport
    yield
    integrations.transport = unreachable
    if path.exists():
        path.unlink()


def _stripe_ok(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "api.stripe.com"
    return httpx.Response(200, json={"object": "balance", "livemode": False})


def _connect_stripe(client, secret=SENTINEL, pk=PK, **extra):
    _respond(_stripe_ok)
    return client.put(
        "/api/connectors/stripe",
        json={"values": {"STRIPE_SECRET_KEY": secret, "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": pk}, **extra},
        headers=LOCAL,
    )


# ── parsing: no network ──────────────────────────────────────────────────────
def test_a_key_in_the_wrong_field_is_caught_before_any_request():
    stripe = integrations.REGISTRY["stripe"]
    parsed = integrations.parse(
        stripe, {"STRIPE_SECRET_KEY": PK, "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": SENTINEL}
    )
    by = {p.name: p.message for p in parsed.problems}
    assert "publishable key" in by["STRIPE_SECRET_KEY"]
    assert "browser" in by["NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY"]


def test_quotes_and_a_name_prefix_are_trimmed():
    parsed = integrations.parse(
        integrations.REGISTRY["resend"], {"RESEND_API_KEY": "'RESEND_API_KEY=re_abc12345_xyz98765'"}
    )
    assert not parsed.problems and parsed.values["RESEND_API_KEY"] == "re_abc12345_xyz98765"


def test_stripe_keys_must_share_a_mode():
    parsed = integrations.parse(
        integrations.REGISTRY["stripe"],
        {"STRIPE_SECRET_KEY": "sk_live_abcdefghijk", "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": PK},
    )
    assert "same mode" in parsed.problems[0].message


@pytest.mark.parametrize(
    "iid, values",
    [
        ("clerk", {"CLERK_SECRET_KEY": "pk_test_abcdefghijk", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": PK}),
        ("openai", {"OPENAI_API_KEY": "not-a-key"}),
        ("resend", {"RESEND_API_KEY": "sk_test_abcdefghijk"}),
    ],
)
def test_each_connector_refuses_a_malformed_key(iid, values):
    assert integrations.parse(integrations.REGISTRY[iid], values).problems


# ── the live check: status and body, per connector ───────────────────────────
@pytest.mark.parametrize(
    "iid, values, status, body, expect",
    [
        ("stripe", {"STRIPE_SECRET_KEY": SENTINEL}, 401, {"error": {}}, "failed"),
        # A restricted key without the balance permission is still a real key.
        ("stripe", {"STRIPE_SECRET_KEY": "rk_test_abcdefghijkl"}, 403, {"error": {}}, "connected"),
        # Resend answers a bad key with 400 — and a sending-only key with 401.
        ("resend", {"RESEND_API_KEY": "re_abc12345_xyz98765"}, 400, {"name": "validation_error"}, "failed"),
        ("resend", {"RESEND_API_KEY": "re_abc12345_xyz98765"}, 401, {"name": "restricted_api_key"}, "connected"),
        ("clerk", {"CLERK_SECRET_KEY": "sk_test_abcdefghijk"}, 401, {"errors": [{"code": "clerk_key_invalid"}]}, "failed"),
        ("openai", {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrst"}, 401, {"error": {"code": "invalid_api_key"}}, "failed"),
        # The provider's own trouble is never "connected".
        ("openai", {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrst"}, 503, {}, "unchecked"),
    ],
)
def test_check_maps_each_providers_answer(iid, values, status, body, expect):
    _respond(lambda request: httpx.Response(status, json=body))
    result = integrations.check(integrations.REGISTRY[iid], values)
    assert result.result.status == expect, result.result.message


def test_openai_check_returns_the_model_list_for_the_picker():
    _respond(lambda r: httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]}))
    result = integrations.check(integrations.REGISTRY["openai"], {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrst"})
    assert result.result.status == "connected" and result.models == ["model-a", "model-b"]


def test_a_timeout_is_saved_not_tested():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    _respond(slow)
    result = integrations.check(integrations.REGISTRY["clerk"], {"CLERK_SECRET_KEY": "sk_test_abcdefghijk"})
    assert result.result.status == "unchecked"


def test_a_check_only_calls_its_own_hosts():
    seen = []
    _respond(lambda r: seen.append(r.url.host) or httpx.Response(200, json={}))
    for iid in ("stripe", "resend", "clerk", "openai"):
        found = integrations.REGISTRY[iid]
        integrations.check(found, {found.check.key_var: "x" * 20})
        assert seen[-1] in found.check.hosts


# ── detection ────────────────────────────────────────────────────────────────
CONNECTED = ["stripe", "resend", "openai"]


@pytest.mark.parametrize(
    "idea, expect",
    [
        ("A SaaS for yoga studios with monthly subscriptions and booking confirmation emails", ["stripe", "resend"]),
        ("A todo app", []),
        ("We don't take payments — a simple reading list", []),
        ("A store with checkout, using Razorpay for UPI", []),
        ("An invoicing tool with Stripe payouts", ["stripe"]),
        # Words, not payment processing (review finding 2).
        ("A subscription tracker that lists my Netflix subscriptions", []),
        ("An expense app that logs payments I make", []),
        ("Newsletter archive viewer", []),
        ("Take payments without Stripe", []),
    ],
)
def test_relevance_is_conservative(idea, expect):
    assert [m.iid for m in integrations.relevant(idea, (), CONNECTED)] == expect


def test_a_soft_phrase_picks_only_a_connected_connector():
    assert integrations.relevant("A habit tracker with sign in", (), []) == []
    assert [m.iid for m in integrations.relevant("A habit tracker with sign in", (), ["clerk"])] == ["clerk"]
    # Atlas designing its own JWT login means Clerk isn't used.
    assert integrations.relevant("A habit tracker with sign in", ["custom JWT auth with bcrypt"], ["clerk"]) == []


def test_a_negation_after_a_named_connector_counts():
    assert integrations.relevant("A blog. Sign in with Clerk is not needed", (), ["clerk"]) == []


def test_a_strong_phrase_picks_one_that_isnt_connected_so_the_build_asks():
    [match] = integrations.relevant("A store with checkout", (), [])
    assert match.iid == "stripe" and "checkout" in match.reason


def test_skip_always_wins_and_use_adds():
    idea = "A SaaS with subscriptions"
    assert integrations.relevant(idea, (), CONNECTED, skip=["stripe"]) == []
    picked = integrations.relevant("A todo app", (), CONNECTED, use=["openai"])
    assert [(m.iid, m.source) for m in picked] == [("openai", "user")]


def test_the_design_can_add_a_connector():
    [match] = integrations.relevant("A booking app", ['{"notifications": "send confirmation emails"}'], ["resend"])
    assert match.iid == "resend" and "Atlas" in match.reason


# ── the charter ──────────────────────────────────────────────────────────────
def test_the_charter_carries_the_names_and_which_side_reads_each():
    charter = freeze({"tech_stack": {"frontend": ["Next.js"], "backend": ["FastAPI"]}}, None, ("stripe",))
    assert "STRIPE_SECRET_KEY" in charter.env and "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY" in charter.env
    block = charter.prompt_block()
    assert "STRIPE_SECRET_KEY (server only)" in block
    assert "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY (client" in block
    again = Charter.from_dict(charter.as_dict())
    assert again.integrations == ("stripe",) and again.env == charter.env
    # A charter from before connectors existed has none.
    assert Charter.from_dict({"database": {"token": "postgres", "label": "PostgreSQL"}}).integrations == ()


# ── the account store ────────────────────────────────────────────────────────
def test_connecting_encrypts_hints_and_keeps_the_file_owner_only(client):
    r = _connect_stripe(client)
    assert r.json()["ok"], r.text
    assert SENTINEL not in r.text
    entry = r.json()["connector"]
    assert entry["connected"] and entry["mode"] == "test"
    assert {s["name"]: s["hint"] for s in entry["saved"]}["STRIPE_SECRET_KEY"] == "…" + SENTINEL[-4:]
    path = userdata.path(TEST_USER_ID, "connectors.local.json")
    on_disk = path.read_text()
    assert SENTINEL not in on_disk and "enc:v1:" in on_disk
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    catalog = client.get("/api/connectors").json()
    assert catalog["connected"] == 1
    assert len(catalog["categories"]) == 12


def test_an_unreadable_store_is_never_saved_over(client):
    path = userdata.path(TEST_USER_ID, "connectors.local.json")
    path.write_text("{not json")
    r = _connect_stripe(client)
    assert r.status_code == 503
    assert path.read_text() == "{not json"


def test_a_live_key_needs_confirmation(client):
    live = {"STRIPE_SECRET_KEY": "sk_live_abcdefghijk", "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_live_abcdefghijk"}
    _respond(lambda r: httpx.Response(200, json={"livemode": True}))
    r = client.put("/api/connectors/stripe", json={"values": live}, headers=LOCAL)
    assert r.status_code == 409 and r.json()["detail"]["status"] == "needs_live_confirm"
    assert not connectors_store.for_user(TEST_USER_ID).connected_ids()
    r = client.put("/api/connectors/stripe", json={"values": live, "confirm_live": True}, headers=LOCAL)
    assert r.json()["ok"] and r.json()["connector"]["mode"] == "live"


def test_writes_need_a_trusted_host_and_coming_soon_cant_connect(client):
    assert client.put("/api/connectors/stripe", json={"values": {}}, headers={"host": "evil.example"}).status_code == 403
    assert client.put("/api/connectors/shopify", json={"values": {}}, headers=LOCAL).status_code == 409


def test_a_failed_key_is_not_saved(client):
    _respond(lambda r: httpx.Response(401, json={}))
    r = client.put(
        "/api/connectors/stripe",
        json={"values": {"STRIPE_SECRET_KEY": SENTINEL, "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": PK}},
        headers=LOCAL,
    )
    assert r.json()["status"] == "failed" and r.json()["check"]["name"] == "STRIPE_SECRET_KEY"
    assert not connectors_store.for_user(TEST_USER_ID).connected_ids()


def test_preview_says_what_and_why(client):
    _connect_stripe(client)
    r = client.post("/api/connectors/preview", json={"idea": "A store with checkout and order confirmation emails"})
    rows = {c["id"]: c for c in r.json()["connectors"]}
    assert rows["stripe"]["connected"] and "checkout" in rows["stripe"]["reason"]
    assert not rows["resend"]["connected"]


# ── the build ────────────────────────────────────────────────────────────────
def _run(client, idea, mode="unattended", **extra) -> str:
    r = client.post(
        "/api/projects",
        json={"idea": idea, "routing_mode": "local_only", "approval_mode": mode, **extra},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/run").status_code == 200
    return pid


def test_a_connected_connector_is_linked_and_the_build_never_stops_for_it(client):
    _connect_stripe(client)
    pid = _run(client, "A store with checkout")
    project = through_database_gate(client, pid)
    assert project["gate_kind"] != "integrations"
    assert project["status"] == "completed", project.get("last_error")
    assert project["charter"]["integrations"] == ["stripe"]
    state = client.get(f"/api/projects/{pid}/integrations").json()
    [row] = state["connectors"]
    assert row["source"] == "account" and row["status"] == "connected"
    files = {f["path"]: f["content"] for f in client.get(f"/api/projects/{pid}/artifacts").json()["files"]}
    example = "\n".join(c for p, c in files.items() if p.endswith(".env.example"))
    assert "STRIPE_SECRET_KEY=" in example


def test_an_unconnected_connector_asks_after_the_database_in_every_mode(client):
    for mode in ("checkpoints", "unattended"):
        pid = _run(client, "A store with checkout", mode)
        project = through_database_gate(client, pid)
        assert project["gate_kind"] == "integrations", mode
        assert "Stripe" in project["gate_note"]
        # Anything but an answer is refused.
        assert client.post(f"/api/projects/{pid}/approve").status_code == 409
        r = client.post(f"/api/projects/{pid}/integrations/later")
        assert r.status_code == 200, r.text
        project = client.get(f"/api/projects/{pid}").json()
        if mode == "checkpoints":
            # The Plan review still parks after it.
            assert project["gate_kind"] == "plan"
        else:
            assert project["status"] == "completed"
        assert project["integrations_status"] == {"stripe": "later"}


def test_connecting_at_the_gate_saves_to_the_account_by_default(client):
    pid = _run(client, "A store with checkout")
    through_database_gate(client, pid)
    _respond(_stripe_ok)
    r = client.put(
        f"/api/projects/{pid}/integrations/stripe",
        json={"values": {"STRIPE_SECRET_KEY": SENTINEL, "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": PK}},
        headers=LOCAL,
    )
    assert r.json()["ok"], r.text
    assert connectors_store.for_user(TEST_USER_ID).connected_ids() == ["stripe"]
    assert client.post(f"/api/projects/{pid}/integrations/continue").status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"


def test_a_project_override_wins_and_disconnect_turns_builds_to_later(client):
    _connect_stripe(client)
    pid = _run(client, "A store with checkout")
    through_database_gate(client, pid)
    _respond(_stripe_ok)
    other = "sk_test_ProjectOnly8Kd2Ws"
    r = client.put(
        f"/api/projects/{pid}/integrations/stripe",
        json={
            "values": {"STRIPE_SECRET_KEY": other, "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": PK},
            "save_to_account": False,
        },
        headers=LOCAL,
    )
    assert r.json()["state"]["connectors"][0]["source"] == "project"
    project = client.get(f"/api/projects/{pid}").json()
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration import connectors

    with SessionLocal() as db:
        assert connectors.values_for(db.get(Project, pid))["STRIPE_SECRET_KEY"] == other
    # A build linked to the account turns to "later" when it's disconnected.
    linked = _run(client, "A shop with checkout")
    through_database_gate(client, linked)
    using = [b["id"] for b in client.get("/api/connectors/stripe").json()["used_by"]]
    assert linked in using and pid not in using  # the override build isn't listed
    r = client.delete("/api/connectors/stripe", headers=LOCAL)
    affected = [b["id"] for b in r.json()["affected"]]
    assert linked in affected and pid not in affected
    assert client.get(f"/api/projects/{linked}").json()["integrations_status"] == {"stripe": "later"}
    assert project["status"] == "completed"


def test_create_keeps_use_and_skip_and_skip_wins(client):
    _connect_stripe(client)
    pid = _run(client, "A store with checkout", connectors={"use": ["openai"], "skip": ["stripe"]})
    project = through_database_gate(client, pid)
    assert project["integrations_choice"] == {"use": ["openai"], "skip": ["stripe"]}
    assert project["charter"]["integrations"] == ["openai"]
    assert client.post("/api/projects", json={"idea": "x y z", "connectors": {"use": ["nope"]}}).status_code == 422


def test_atlas_is_told_names_only_and_skills_are_pinned(client, monkeypatch):
    prompts: list[str] = []

    def recording(messages, **kwargs):
        prompts.extend(str(getattr(m, "content", m)) for m in messages)
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", recording)
    _connect_stripe(client)
    pid = _run(client, "A store with checkout")
    through_database_gate(client, pid)
    seen = "\n".join(prompts)
    assert "The user has connected: Stripe (payments)" in seen
    assert SENTINEL not in seen and PK not in seen
    project = client.get(f"/api/projects/{pid}").json()
    used = {s for p in project["phases"] for s in (p.get("skills_used") or [])}
    assert "stripe-payments" in used


def test_no_key_reaches_a_prompt_the_charter_logs_artifacts_or_the_default_zip(client, monkeypatch, caplog):
    prompts: list[str] = []

    def recording(messages, **kwargs):
        prompts.extend(str(getattr(m, "content", m)) for m in messages)
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", recording)
    caplog.set_level(logging.DEBUG)
    _connect_stripe(client)
    pid = _run(client, "A store with checkout")
    project = through_database_gate(client, pid)
    assert project["status"] == "completed"
    assert SENTINEL not in "\n".join(prompts)
    assert SENTINEL not in json.dumps(project)
    assert SENTINEL not in client.get(f"/api/projects/{pid}/artifacts").text
    assert SENTINEL not in client.get(f"/api/projects/{pid}/integrations").text
    assert SENTINEL not in client.get("/api/connectors").text
    assert SENTINEL not in caplog.text
    default = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/download").content))
    assert not any(SENTINEL in default.read(n).decode("utf-8", "ignore") for n in default.namelist())
    # Asked for, from a trusted host: there, and only there.
    opted = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/projects/{pid}/download?include_credentials=true", headers=LOCAL).content)
    )
    assert f"STRIPE_SECRET_KEY={SENTINEL}" in opted.read("backend/.env").decode()
    # Feedback holding the saved key is refused.
    r = client.post(f"/api/projects/{pid}/reject", json={"feedback": f"use {SENTINEL}"})
    assert r.status_code in (409, 422)


def test_only_client_variables_reach_vercels_public_env(client):
    from app.api.routes.deploy import _public_connector_env
    from app.db.base import SessionLocal
    from app.db.models import Project

    _connect_stripe(client)
    pid = _run(client, "A store with checkout")
    through_database_gate(client, pid)
    frontend = {"app/page.tsx": "process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY; process.env.STRIPE_SECRET_KEY"}
    with SessionLocal() as db:
        env = _public_connector_env(db.get(Project, pid), frontend)
    assert env == {"NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": PK}


def test_changing_connectors_is_only_allowed_before_code(client):
    _connect_stripe(client)
    pid = _run(client, "A store with checkout", "checkpoints")
    project = through_database_gate(client, pid)
    assert project["gate_kind"] == "plan"
    r = client.post(f"/api/projects/{pid}/integrations", json={"add": "openai"})
    assert r.status_code == 200 and r.json()["used"] == ["stripe", "openai"]
    assert "OPENAI_API_KEY" in client.get(f"/api/projects/{pid}").json()["charter"]["env"]
    # OpenAI isn't connected, so the build waits on it rather than building keyless.
    assert client.get(f"/api/projects/{pid}").json()["gate_kind"] == "integrations"
    r = client.post(f"/api/projects/{pid}/integrations", json={"remove": "stripe"})
    assert r.json()["used"] == ["openai"]
    assert client.post(f"/api/projects/{pid}/integrations/later").status_code == 200
    # Back on the Plan review; approving it is when the code gets written.
    assert client.get(f"/api/projects/{pid}").json()["gate_kind"] == "plan"
    client.post(f"/api/projects/{pid}/approve")
    assert client.post(f"/api/projects/{pid}/integrations", json={"add": "stripe"}).status_code == 409


# ── the build check ──────────────────────────────────────────────────────────
def test_a_server_secret_in_browser_code_is_a_problem_and_server_code_is_not():
    files = {
        "frontend/app/pay/page.tsx": "'use client'\nconst k = process.env.STRIPE_SECRET_KEY",
        "frontend/app/api/checkout/route.ts": "const k = process.env.STRIPE_SECRET_KEY",
        "frontend/lib/ai.ts": "export const k = process.env.NEXT_PUBLIC_OPENAI_API_KEY",
        "frontend/lib/pk.ts": "export const k = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY",
    }
    flagged = {p.path for p in secret_leaks(files, files)}
    assert flagged == {"frontend/app/pay/page.tsx", "frontend/lib/ai.ts"}


def test_the_new_columns_migrate_onto_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, gate_kind VARCHAR(16))"))
        conn.execute(text("INSERT INTO projects VALUES ('p', 'plan')"))
    applied = run_migrations(engine)
    assert {"projects.integrations_choice", "projects.integrations_status"} <= set(applied)
    columns = {c["name"] for c in inspect(engine).get_columns("projects")}
    assert {"integrations_choice", "integrations_status"} <= columns
    assert run_migrations(engine) == []


def test_a_project_without_the_section_reads_as_empty(tmp_path):
    assert project_secrets.integrations_load(TEST_USER_ID, "0" * 32) == {}


# ── review fixes ─────────────────────────────────────────────────────────────
def test_a_redo_of_the_architecture_keeps_what_was_switched_off(client):
    _connect_stripe(client)
    pid = _run(client, "A store with checkout", "checkpoints")
    through_database_gate(client, pid)
    assert client.post(f"/api/projects/{pid}/integrations", json={"remove": "stripe"}).status_code == 200
    r = client.post(f"/api/projects/{pid}/redo", json={"phase": "system_design", "feedback": "tighter"})
    assert r.status_code == 200, r.text
    assert (client.get(f"/api/projects/{pid}").json()["charter"] or {}).get("integrations", []) == []


def test_the_badge_is_live_and_a_failed_key_is_not_connected(client):
    pid = _run(client, "A store with checkout")
    through_database_gate(client, pid)
    client.post(f"/api/projects/{pid}/integrations/later")
    assert client.get(f"/api/projects/{pid}").json()["connectors_unconnected"] == ["stripe"]
    _connect_stripe(client)  # connected in the tab: the build's mark clears at once
    assert client.get(f"/api/projects/{pid}").json()["connectors_unconnected"] == []
    _respond(lambda r: httpx.Response(401, json={}))
    assert client.post("/api/connectors/stripe/check", headers=LOCAL).json()["status"] == "failed"
    assert client.get(f"/api/projects/{pid}").json()["connectors_unconnected"] == ["stripe"]
    assert client.get(f"/api/projects/{pid}/integrations").json()["not_connected"] == ["stripe"]


def test_an_unreadable_store_is_an_error_not_an_empty_catalog(client):
    path = userdata.path(TEST_USER_ID, "connectors.local.json")
    path.write_text("{not json")
    assert client.get("/api/connectors").status_code == 503


def test_a_frontend_only_build_gets_its_server_keys_in_env_local(client, monkeypatch):
    from app.core import artifacts
    from app.db.base import SessionLocal
    from app.db.models import Project

    _connect_stripe(client)
    pid = _run(client, "A store with checkout")
    through_database_gate(client, pid)
    with SessionLocal() as db:
        project = db.get(Project, pid)
        frontend_only = {"files": [{"path": "frontend/.env.example", "content": ""}]}
        both = {"files": frontend_only["files"] + [{"path": "backend/.env.example", "content": ""}]}
        # No backend: its API routes are the server, so the secret goes to the frontend.
        assert artifacts.frontend_env(project, frontend_only)["STRIPE_SECRET_KEY"] == SENTINEL
        # With a backend: the frontend gets publishable keys only.
        assert artifacts.frontend_env(project, both) == {"NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": PK}
