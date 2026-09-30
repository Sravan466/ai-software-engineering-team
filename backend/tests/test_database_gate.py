"""After Atlas picks the stack, the build asks for the database (#54).

The gate, its routes, the storage, the test, and the promise the whole feature rests
on: a credential value never reaches a model, a log line, `/artifacts`, the GitHub
push or a default download.
"""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import stat
import zipfile
from types import SimpleNamespace

import pytest

from app.build import dbconnect
from app.core import project_secrets, scrub
from app.orchestration.charter import Charter, freeze
from app.orchestration.runner import runner
from tests.conftest import TEST_USER_ID, _fake_complete, stub

LOCAL = {"host": "localhost"}
#: The password every leak check greps for.
SENTINEL = "Sentinel7Qx9Pw"


def _create(client, mode="checkpoints") -> str:
    r = client.post(
        "/api/projects",
        json={"idea": "A group expenses app", "routing_mode": "local_only", "approval_mode": mode},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _to_gate(client, mode="checkpoints") -> str:
    pid = _create(client, mode)
    assert client.post(f"/api/projects/{pid}/run").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    if mode == "every_phase":
        # Scope's own handoff comes first.
        assert project["gate_kind"] == "phase"
        client.post(f"/api/projects/{pid}/approve")
        project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "awaiting_approval"
    assert project["gate_kind"] == "database", project["gate_kind"]
    assert project["current_phase"] == "system_design"
    return pid


@pytest.fixture
def listener():
    """A TCP port that accepts, standing in for a database this server can reach."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    yield server.getsockname()[1]
    server.close()


@pytest.fixture(autouse=True)
def _fresh_limit():
    from app.api.routes import database

    database.limiter.reset()
    yield
    database.limiter.reset()


# ── the gate ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["checkpoints", "unattended", "every_phase"])
def test_the_run_parks_on_the_database_in_every_mode(client, mode):
    pid = _to_gate(client, mode)
    project = client.get(f"/api/projects/{pid}").json()
    assert "PostgreSQL" in project["gate_note"]
    assert project["database_status"] is None
    assert project["charter"]["env"] == ["DATABASE_URL"]
    state = client.get(f"/api/projects/{pid}/database").json()
    assert state["provider"] == "generic" and state["saved"] == []
    assert state["contract"]["variables"][0]["name"] == "DATABASE_URL"
    # Looking at another host before saving shows that host's fields.
    supa = client.get(f"/api/projects/{pid}/database?provider=supabase").json()
    assert supa["provider"] == "supabase"
    assert [v["name"] for v in supa["contract"]["variables"]][:2] == ["SUPABASE_URL", "SUPABASE_ANON_KEY"]


def test_later_resumes_and_the_plan_review_still_parks(client):
    pid = _to_gate(client)
    r = client.post(f"/api/projects/{pid}/database/later")
    assert r.status_code == 200, r.text
    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "plan"
    assert project["database_status"] == "later"
    # Approving the plan carries on to the end, with no second database stop.
    client.post(f"/api/projects/{pid}/approve")
    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "ship"
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"


def test_unattended_later_finishes_the_build(client):
    pid = _to_gate(client, "unattended")
    assert client.post(f"/api/projects/{pid}/database/later").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project["last_error"]
    zipped = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/download").content))
    assert "backend/.env" not in zipped.namelist()


def test_the_gate_is_not_approved_redone_or_rederived_around(client):
    pid = _to_gate(client)
    assert client.post(f"/api/projects/{pid}/approve").status_code == 409
    r = client.post(f"/api/projects/{pid}/redo", json={"phase": "system_design", "feedback": "use mongo"})
    assert r.status_code == 409
    # A policy change keeps the question on screen.
    client.patch(f"/api/projects/{pid}", json={"approval_mode": "unattended"})
    assert client.get(f"/api/projects/{pid}").json()["gate_kind"] == "database"
    # Continue needs something saved first.
    assert client.post(f"/api/projects/{pid}/database/continue").status_code == 409


def test_the_routes_refuse_outside_the_gate(client):
    pid = _create(client)
    assert client.post(f"/api/projects/{pid}/database/later").status_code == 400
    # No charter yet: nothing to connect.
    r = client.post(f"/api/projects/{pid}/database/check", json={"values": {}}, headers=LOCAL)
    assert r.status_code == 400


def test_writes_need_a_trusted_host(client):
    pid = _to_gate(client)
    r = client.post(f"/api/projects/{pid}/database/check", json={"values": {"DATABASE_URL": "x"}})
    assert r.status_code == 403
    assert client.delete(f"/api/projects/{pid}/database").status_code == 403


def test_sqlite_and_no_database_never_ask():
    for charter in (
        {"database": {"token": "sqlite", "label": "SQLite", "source": "system_design"}},
        {"language": {"token": "python", "label": "Python", "source": "system_design"}},
        None,
    ):
        project = SimpleNamespace(database_status=None, charter=charter)
        assert runner.database_question(project) is None
    mongo = SimpleNamespace(
        database_status=None,
        charter={"database": {"token": "mongodb", "label": "MongoDB", "source": "debate"}},
    )
    assert runner.database_question(mongo).kind == "database"
    mongo.database_status = "later"
    assert runner.database_question(mongo) is None


def test_a_changed_database_is_asked_again_and_old_values_go(tmp_path):
    pid = "f" * 32
    contract = dbconnect.contract_for("mongodb", "atlas")
    project_secrets.save(
        TEST_USER_ID, pid, contract, {"MONGODB_URI": "mongodb://u:p@h/db"},
        dbconnect.CheckResult(dbconnect.UNCHECKED, "saved"),
    )
    mongo = {"database": {"token": "mongodb", "label": "MongoDB", "source": "system_design"}}
    pg = {"database": {"token": "postgres", "label": "PostgreSQL", "source": "system_design"}}
    project = SimpleNamespace(id=pid, owner_id=TEST_USER_ID, charter=mongo, database_status="unchecked")
    runner.settle_database(project, mongo)
    assert project.database_status == "unchecked"
    project.charter = pg
    runner.settle_database(project, mongo)
    assert project.database_status is None
    assert project_secrets.load(TEST_USER_ID, pid) == {}
    project.charter = {"database": {"token": "sqlite", "label": "SQLite", "source": "system_design"}}
    runner.settle_database(project, pg)
    assert project.database_status == "none"


# ── the charter ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "stack, provider, names",
    [
        ({"database": ["Supabase (Postgres)"]}, "supabase", ["SUPABASE_URL", "SUPABASE_ANON_KEY", "DATABASE_URL"]),
        ({"database": ["PostgreSQL on Neon"]}, "neon", ["DATABASE_URL"]),
        ({"database": ["PostgreSQL"]}, "generic", ["DATABASE_URL"]),
        ({"database": ["MongoDB Atlas"]}, "atlas", ["MONGODB_URI"]),
        ({"database": ["PlanetScale"]}, "planetscale", ["DATABASE_URL"]),
        ({"database": ["Cloud Firestore"], "infra": ["Firebase"]}, "firebase", [
            "FIREBASE_API_KEY", "FIREBASE_AUTH_DOMAIN", "FIREBASE_PROJECT_ID",
            "FIREBASE_STORAGE_BUCKET", "FIREBASE_MESSAGING_SENDER_ID", "FIREBASE_APP_ID",
        ]),
        ({"database": ["DynamoDB"]}, "aws", ["AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]),
    ],
)
def test_the_charter_keeps_the_provider_and_the_names(stack, provider, names):
    charter = freeze({"tech_stack": {"backend": ["FastAPI"], **stack}})
    data = charter.as_dict()
    assert data["database_provider"] == provider
    assert data["env"] == names
    again = Charter.from_dict(data)
    assert again.database_provider == provider and list(again.env) == names
    assert ", ".join(names) in charter.prompt_block()


def test_an_old_charter_reads_as_generic():
    old = Charter.from_dict({"database": {"token": "postgres", "label": "PostgreSQL", "source": "debate"}})
    assert old.database_provider == "generic" and old.env == ("DATABASE_URL",)
    assert Charter.from_dict({"database": {"token": "sqlite", "label": "SQLite"}}).env == ()


def test_env_example_lists_the_charter_names():
    from app.build.scaffold import build as scaffold_build

    charter = freeze({"tech_stack": {"backend": ["FastAPI"], "database": ["Supabase"]}})
    sc = scaffold_build({"backend/app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"}, charter)
    example = next(f.content for f in sc.files if f.path == "backend/.env.example")
    assert "SUPABASE_URL=https://" in example
    assert "SUPABASE_ANON_KEY=change-me" in example
    assert "DATABASE_URL=" in example


# ── parsing ──────────────────────────────────────────────────────────────────
def _parse(database, provider, values):
    return dbconnect.parse(dbconnect.contract_for(database, provider), values)


def test_a_placeholder_left_in_is_caught():
    out = _parse("mongodb", "atlas", {"MONGODB_URI": "mongodb+srv://bob:<password>@c0.ab12c.mongodb.net/app"})
    assert out.problems and "<password>" in out.problems[0].message
    assert out.problems[0].step == 3
    out = _parse("postgres", "supabase", {
        "SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
        "SUPABASE_ANON_KEY": "sb_publishable_abcdefghijkl",
        "DATABASE_URL": "postgresql://postgres.x:[YOUR-PASSWORD]@aws-0.pooler.supabase.com:5432/postgres",
    })
    assert any("password" in p.message.lower() for p in out.problems)


def test_the_wrong_scheme_is_caught():
    out = _parse("postgres", "generic", {"DATABASE_URL": "mysql://u:p@h:3306/app"})
    assert "postgres" in out.problems[0].message


def test_reserved_characters_in_the_password_are_encoded():
    out = _parse("postgres", "generic", {"DATABASE_URL": ' "postgresql://app:p@ss:w/rd@db.example.com:5432/app" '})
    assert not out.problems
    assert out.values["DATABASE_URL"] == "postgresql://app:p%40ss%3Aw%2Frd@db.example.com:5432/app"
    assert out.notices and "encoded" in out.notices[0].message


def test_a_missing_database_name_and_neon_tls_are_said():
    out = _parse("postgres", "generic", {"DATABASE_URL": "postgresql://u:p@h:5432"})
    assert "database" in out.problems[0].message
    out = _parse("postgres", "neon", {"DATABASE_URL": "postgresql://u:p@ep-x.neon.tech/app"})
    assert not out.problems and any("sslmode" in n.message for n in out.notices)


def test_a_supabase_secret_key_is_refused():
    out = _parse("postgres", "supabase", {
        "SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
        "SUPABASE_ANON_KEY": "sb_secret_abcdefghijklmnop",
    })
    assert any("secret key" in p.message for p in out.problems)


def test_a_whole_firebase_config_is_split():
    blob = """const firebaseConfig = {
      apiKey: "AIzaSyA1234567890abcdefghijklmnopqrstu",
      authDomain: "demo.firebaseapp.com",
      projectId: "demo",
      storageBucket: "demo.appspot.com",
      messagingSenderId: "123456789012",
      appId: "1:123456789012:web:abc123",
    };"""
    out = _parse("firestore", "firebase", {"firebaseConfig": blob})
    assert not out.problems, out.problems
    assert out.values["FIREBASE_PROJECT_ID"] == "demo"
    assert len(out.values) == 6


# ── testing and saving ───────────────────────────────────────────────────────
def test_a_format_error_saves_nothing_and_touches_no_network(client, monkeypatch):
    pid = _to_gate(client)
    monkeypatch.setattr(dbconnect, "check_connection", lambda *a: pytest.fail("network touched"))
    r = client.post(
        f"/api/projects/{pid}/database/check",
        json={"values": {"DATABASE_URL": "postgresql://u:<password>@h/app"}},
        headers=LOCAL,
    ).json()
    assert r["ok"] is False and r["status"] == "invalid"
    assert r["problems"][0]["name"] == "DATABASE_URL"
    assert project_secrets.load(TEST_USER_ID, pid) == {}


def test_dns_and_refused_failures_each_say_their_fix(client):
    pid = _to_gate(client)
    r = client.post(
        f"/api/projects/{pid}/database/check",
        json={"values": {"DATABASE_URL": "postgresql://u:p@no-such-host.invalid:5432/app"}},
        headers=LOCAL,
    ).json()
    assert r["status"] == "failed" and r["check"]["reason"] == "dns"
    assert "no-such-host.invalid" in r["check"]["message"]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    r = client.post(
        f"/api/projects/{pid}/database/check",
        json={"values": {"DATABASE_URL": f"postgresql://u:p@127.0.0.1:{port}/app"}},
        headers=LOCAL,
    ).json()
    assert r["status"] == "failed" and r["check"]["reason"] == "refused"
    # A failed test keeps nothing.
    assert project_secrets.load(TEST_USER_ID, pid) == {}


def test_auth_and_timeout_map_to_their_own_fixes():
    atlas = dbconnect.contract_for("mongodb", "atlas")
    assert dbconnect._classify("OperationFailure: bad auth : authentication failed") == "auth"
    assert dbconnect._classify("ServerSelectionTimeoutError: timed out") == "timeout"
    assert "Network Access" in dbconnect._failure(atlas, "timeout", "MONGODB_URI", "h").message
    assert dbconnect._failure(atlas, "auth", "MONGODB_URI", "h").step == 1


def test_a_reachable_database_saves_encrypted_and_shows_only_a_hint(client, listener):
    pid = _to_gate(client)
    uri = f"postgresql://app:{SENTINEL}@127.0.0.1:{listener}/app"
    r = client.post(f"/api/projects/{pid}/database/check", json={"values": {"DATABASE_URL": uri}}, headers=LOCAL)
    body = r.json()
    assert r.status_code == 200 and body["ok"], body
    # No driver in this venv: it was reached, and said so honestly.
    assert body["status"] in ("unchecked", "connected")
    assert SENTINEL not in r.text

    path = project_secrets._path(TEST_USER_ID, pid)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    on_disk = path.read_text()
    assert SENTINEL not in on_disk and "enc:v1:" in on_disk

    state = client.get(f"/api/projects/{pid}/database").json()
    assert state["saved"] == [{"name": "DATABASE_URL", "hint": f"postgresql://…@127.0.0.1:{listener}"}]
    assert SENTINEL not in json.dumps(state)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["database_status"] in ("unchecked", "connected")

    assert client.post(f"/api/projects/{pid}/database/continue").status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["gate_kind"] == "plan"


def test_checks_are_rate_limited(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "key_checks_per_window", 2)
    monkeypatch.setattr(dbconnect, "check_connection", lambda *a: dbconnect.CheckResult(dbconnect.FAILED, "no", reason="dns"))
    pid = _to_gate(client)
    body = {"values": {"DATABASE_URL": "postgresql://u:p@h.example/app"}}
    codes = [client.post(f"/api/projects/{pid}/database/check", json=body, headers=LOCAL).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_remove_and_the_project_delete_take_the_file(client, listener):
    pid = _to_gate(client)
    uri = f"postgresql://app:{SENTINEL}@127.0.0.1:{listener}/app"
    client.post(f"/api/projects/{pid}/database/check", json={"values": {"DATABASE_URL": uri}}, headers=LOCAL)
    state = client.delete(f"/api/projects/{pid}/database", headers=LOCAL).json()
    assert state["saved"] == [] and state["status"] == "later"
    client.put(f"/api/projects/{pid}/database", json={"values": {"DATABASE_URL": uri}}, headers=LOCAL)
    assert project_secrets._path(TEST_USER_ID, pid).exists()
    client.delete(f"/api/projects/{pid}")
    assert not project_secrets._path(TEST_USER_ID, pid).exists()


# ── nothing leaks ────────────────────────────────────────────────────────────
def test_a_credential_in_feedback_is_refused_and_never_sent(client):
    pid = _to_gate(client)
    client.post(f"/api/projects/{pid}/database/later")
    for text in (
        f"use postgres://admin:{SENTINEL}@db.example.com/app please",
        "the key is sb_secret_abcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP",
    ):
        r = client.post(f"/api/projects/{pid}/redo", json={"phase": "system_design", "feedback": text})
        assert r.status_code == 422 and "credentials" in r.text
    r = client.post(f"/api/projects/{pid}/reject", json={"feedback": f"mongodb://a:{SENTINEL}@h/db"})
    assert r.status_code == 422


def test_the_scrubber_takes_the_password_out_of_a_uri():
    line = f"connect failed for postgresql://app:{SENTINEL}@db.example.com:5432/app"
    assert SENTINEL not in scrub.scrub(line)
    assert "db.example.com" in scrub.scrub(line)


def test_no_value_reaches_a_prompt_the_charter_logs_artifacts_or_the_default_zip(
    client, monkeypatch, listener, caplog
):
    prompts: list[str] = []

    def recording(messages, **kwargs):
        prompts.extend(str(getattr(m, "content", m)) for m in messages)
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", recording)
    caplog.set_level(logging.DEBUG)
    pid = _to_gate(client, "unattended")
    uri = f"postgresql://app:{SENTINEL}@127.0.0.1:{listener}/app"
    r = client.post(f"/api/projects/{pid}/database/check", json={"values": {"DATABASE_URL": uri}}, headers=LOCAL)
    assert r.json()["ok"], r.text
    assert client.post(f"/api/projects/{pid}/database/continue").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project["last_error"]

    everything_a_model_saw = "\n".join(prompts)
    assert "DATABASE_URL" in everything_a_model_saw  # the name, yes
    assert SENTINEL not in everything_a_model_saw  # the value, never
    assert SENTINEL not in json.dumps(project)
    assert SENTINEL not in client.get(f"/api/projects/{pid}/artifacts").text
    assert SENTINEL not in caplog.text

    default = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/download").content))
    assert "backend/.env" not in default.namelist()
    assert not any(SENTINEL in default.read(n).decode("utf-8", "ignore") for n in default.namelist())

    # Asked for from somewhere this backend isn't served at: refused.
    assert client.get(f"/api/projects/{pid}/download?include_credentials=true").status_code == 403
    # Asked for, it is there — and only there.
    opted = zipfile.ZipFile(
        io.BytesIO(
            client.get(f"/api/projects/{pid}/download?include_credentials=true", headers=LOCAL).content
        )
    )
    env = opted.read("backend/.env").decode()
    assert f"DATABASE_URL={uri}" in env and "Never commit" in env


def test_the_new_column_migrates_onto_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, gate_kind VARCHAR(16))"))
        conn.execute(text("INSERT INTO projects VALUES ('p', 'plan')"))
    assert "projects.database_status" in run_migrations(engine)
    assert "database_status" in {c["name"] for c in inspect(engine).get_columns("projects")}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT database_status FROM projects")).scalar() is None
    assert run_migrations(engine) == []


# ── review fixes ─────────────────────────────────────────────────────────────
def test_switching_host_at_the_gate_repoints_the_names_the_crew_reads(client, monkeypatch):
    from app.orchestration.graph import graph
    from app.orchestration.runner import _config

    monkeypatch.setattr(
        dbconnect, "check_connection",
        lambda *a: dbconnect.CheckResult(dbconnect.CONNECTED, "ok", reason="ok", host="abcdefghijklmnop.supabase.co"),
    )
    pid = _to_gate(client)
    r = client.post(
        f"/api/projects/{pid}/database/check",
        json={"provider": "supabase", "values": {
            "SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
            "SUPABASE_ANON_KEY": "sb_publishable_abcdefghijklmnop",
        }},
        headers=LOCAL,
    ).json()
    assert r["ok"], r
    names = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "DATABASE_URL"]
    assert client.get(f"/api/projects/{pid}").json()["charter"]["env"] == names
    # The checkpoint the agents read agrees with the row.
    assert graph.get_state(_config(pid)).values["charter"]["env"] == names
    assert client.post(f"/api/projects/{pid}/database/continue").status_code == 200
    # Past the gate, the host is what the code was written against.
    state = client.get(f"/api/projects/{pid}/database").json()
    assert [p["provider"] for p in state["providers"]] == ["supabase"]
    moved = client.post(
        f"/api/projects/{pid}/database/check",
        json={"provider": "neon", "values": {"DATABASE_URL": "postgresql://u:p@h.neon.tech/app"}},
        headers=LOCAL,
    )
    assert moved.status_code == 409


def test_only_secret_parts_are_scrubbed(monkeypatch):
    contract = dbconnect.contract_for("postgres", "supabase")
    parts = dbconnect.secret_parts(contract, {
        "SUPABASE_URL": "https://qwrtypsdvbnmzxcl.supabase.co",
        "SUPABASE_ANON_KEY": "sb_publishable_Zq9Kx2Lm7Np4Rt",
        "DATABASE_URL": "postgresql://postgres.ref:My%40Passw0rd@pooler.supabase.com:5432/postgres",
    })
    assert "sb_publishable_Zq9Kx2Lm7Np4Rt" in parts
    assert "My%40Passw0rd" in parts and "My@Passw0rd" in parts
    assert not any("supabase.co" in p or p.startswith("postgres") for p in parts)
    pid = "e" * 32
    project_secrets.save(TEST_USER_ID, pid, contract, {
        "SUPABASE_URL": "https://qwrtypsdvbnmzxcl.supabase.co",
        "SUPABASE_ANON_KEY": "sb_publishable_Zq9Kx2Lm7Np4Rt",
    }, dbconnect.CheckResult(dbconnect.CONNECTED, "ok"))
    try:
        # Ordinary words in a redo note are not a credential.
        assert not dbconnect.looks_like_credential("use postgres migrations over https://example.com")
        assert scrub.scrub("GET https://qwrtypsdvbnmzxcl.supabase.co/rest") == "GET https://qwrtypsdvbnmzxcl.supabase.co/rest"
        # Reading it back many times registers it once; removing forgets it.
        for _ in range(3):
            project_secrets.reveal(TEST_USER_ID, pid)
        assert scrub.holds_known("sb_publishable_Zq9Kx2Lm7Np4Rt")
    finally:
        project_secrets.remove(TEST_USER_ID, pid)
    # (The unique tail: `sb_publi…` is a prefix every publishable key shares.)
    assert not scrub.holds_known("Zq9Kx2Lm7Np4Rt")


def test_env_values_survive_python_dotenv():
    from dotenv import dotenv_values

    from app.core.artifacts import _env_value, env_file

    for value in ("mongodb://u:p%40s@h/db?a=1&b=2", "a b", "x$y", 'p"a#s', "sk/+=AbC", "it's", "b\\s"):
        line = f"K={_env_value(value)}\n"
        assert dotenv_values(stream=io.StringIO(line))["K"] == value, line
    body = env_file("# x\nDATABASE_URL=postgresql://localhost/app\nPORT=8000\n", {"DATABASE_URL": "postgres://u:p@h/db"})
    assert "DATABASE_URL=postgres://u:p@h/db" in body and "PORT=8000" in body
    # A connection string is fully encoded when saved, so it never needs quoting.
    out = _parse("postgres", "generic", {"DATABASE_URL": "postgresql://app:it's$x\\y@h:5432/app"})
    assert out.values["DATABASE_URL"] == "postgresql://app:it%27s%24x%5Cy@h:5432/app"
    assert _env_value(out.values["DATABASE_URL"]) == out.values["DATABASE_URL"]


def test_the_architects_name_is_not_a_host():
    charter = freeze({"tech_stack": {"backend": ["Express"], "database": ["MongoDB"]}},
                     {"decision": "Atlas recommends MongoDB on our own server"})
    assert charter.database_provider == "generic"
    long = "MongoDB Atlas " + "x" * 2000
    assert freeze({"tech_stack": {"backend": ["Express"], "database": ["MongoDB"]}}, {"decision": long}).database_provider == "generic"


def test_no_route_that_claims_a_build_releases_the_gate(client):
    from app.api.routes import projects as routes

    pid = _to_gate(client)
    project = SimpleNamespace(status="awaiting_approval", gate_kind="database", id=pid)
    with pytest.raises(Exception) as refused:
        routes._claim(None, project, {"awaiting_approval"})
    assert getattr(refused.value, "status_code", None) == 409


def test_a_redo_keeps_the_host_the_person_saved(client, monkeypatch):
    from app.db.base import SessionLocal
    from app.db.models import Project

    monkeypatch.setattr(
        dbconnect, "check_connection",
        lambda *a: dbconnect.CheckResult(dbconnect.CONNECTED, "ok", reason="ok", host="h"),
    )
    pid = _to_gate(client)
    client.post(
        f"/api/projects/{pid}/database/check",
        json={"provider": "neon", "values": {"DATABASE_URL": "postgresql://u:pw@ep-x.neon.tech/app?sslmode=require"}},
        headers=LOCAL,
    )
    with SessionLocal() as db:
        project = db.get(Project, pid)
        before = dict(project.charter)
        # Atlas re-freezes plain Postgres: provider generic.
        project.charter = {k: v for k, v in before.items() if k not in ("database_provider", "env")}
        runner.settle_database(project, before)
        assert Charter.from_dict(project.charter).database_provider == "neon"
        assert project.database_status == "connected"


def test_generic_hosts_are_not_named_twice():
    project = SimpleNamespace(
        database_status=None,
        charter={"database": {"token": "postgres", "label": "PostgreSQL", "source": "debate"}},
    )
    assert runner.database_question(project).note == (
        "Atlas picked PostgreSQL. Connect it now, or continue and add it later."
    )


def test_a_saved_secret_without_a_shape_is_refused_after_a_restart(client, monkeypatch):
    monkeypatch.setattr(
        dbconnect, "check_connection",
        lambda *a: dbconnect.CheckResult(dbconnect.UNCHECKED, "saved"),
    )
    pid = _to_gate(client)
    secret = "unusualPasswordNoShape"
    client.post(
        f"/api/projects/{pid}/database/check",
        json={"values": {"DATABASE_URL": f"postgresql://app:{secret}@db.example.com/app"}},
        headers=LOCAL,
    )
    client.post(f"/api/projects/{pid}/database/later")
    # As after a restart: nothing registered in this process.
    project_secrets._forget(pid)
    assert not scrub.holds_known(secret)
    r = client.post(f"/api/projects/{pid}/redo", json={"phase": "system_design", "feedback": f"the pw is {secret}"})
    assert r.status_code == 422
    assert project_secrets.register_all() >= 1 and scrub.holds_known(secret)


def test_values_are_scrubbed_while_their_test_runs(client, monkeypatch):
    seen = {}

    def echoing(contract, values):
        seen["scrubbed"] = scrub.scrub("driver said: bad password Zx81MnopQrs")
        return dbconnect.CheckResult(dbconnect.FAILED, "no", reason="auth")

    monkeypatch.setattr(dbconnect, "check_connection", echoing)
    pid = _to_gate(client)
    client.post(
        f"/api/projects/{pid}/database/check",
        json={"values": {"DATABASE_URL": "postgresql://app:Zx81MnopQrs@db.example.com/app"}},
        headers=LOCAL,
    )
    assert "Zx81MnopQrs" not in seen["scrubbed"]
    # A failed test saves nothing, and holds nothing afterwards either.
    assert not scrub.holds_known("Zx81MnopQrs")


def test_approve_at_the_gate_names_the_gate_first(client, monkeypatch):
    pid = _to_gate(client)
    r = client.post(f"/api/projects/{pid}/approve")
    assert r.status_code == 409 and "database" in r.text


# ── final review round ───────────────────────────────────────────────────────
def test_a_redo_that_names_another_host_changes_it(client, monkeypatch):
    from app.db.base import SessionLocal
    from app.db.models import Project

    monkeypatch.setattr(
        dbconnect, "check_connection",
        lambda *a: dbconnect.CheckResult(dbconnect.CONNECTED, "ok", reason="ok", host="h"),
    )
    pid = _to_gate(client)
    client.post(
        f"/api/projects/{pid}/database/check",
        json={"provider": "neon", "values": {"DATABASE_URL": "postgresql://u:pw12345678@ep-x.neon.tech/app?sslmode=require"}},
        headers=LOCAL,
    )
    with SessionLocal() as db:
        project = db.get(Project, pid)
        before = dict(project.charter)
        # A real re-freeze: Atlas's own words now name Supabase.
        old = Charter.from_dict(before)
        project.charter = Charter(old.choices, "supabase", dbconnect.env_names("postgres", "supabase"), "supabase").as_dict()
        runner.settle_database(project, before)
        assert Charter.from_dict(project.charter).database_provider == "supabase"
        assert project.database_status is None
    assert project_secrets.load(TEST_USER_ID, pid) == {}


def test_a_common_password_does_not_refuse_ordinary_notes(client, monkeypatch):
    monkeypatch.setattr(dbconnect, "check_connection", lambda *a: dbconnect.CheckResult(dbconnect.UNCHECKED, "saved"))
    pid = _to_gate(client)
    client.post(
        f"/api/projects/{pid}/database/check",
        json={"values": {"DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/app"}},
        headers=LOCAL,
    )
    client.post(f"/api/projects/{pid}/database/later")
    assert not project_secrets.holds_saved_secret(TEST_USER_ID, pid, "use postgres full-text search")
    assert "postgres" in scrub.scrub("the postgres migrations ran")
    # In a connection string it is still scrubbed where it sits.
    assert "postgres:postgres@" not in scrub.scrub("dial postgresql://postgres:postgres@localhost/app")


def test_a_failed_commit_puts_the_old_credentials_back(client, monkeypatch):
    from sqlalchemy.orm import Session

    monkeypatch.setattr(dbconnect, "check_connection", lambda *a: dbconnect.CheckResult(dbconnect.UNCHECKED, "saved"))
    pid = _to_gate(client)
    first = "postgresql://app:FirstPassw0rd@db.example.com/app"
    client.post(f"/api/projects/{pid}/database/check", json={"values": {"DATABASE_URL": first}}, headers=LOCAL)
    before = project_secrets.snapshot(TEST_USER_ID, pid)

    def broken(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Session, "commit", broken)
    with pytest.raises(Exception):
        client.post(
            f"/api/projects/{pid}/database/check",
            json={"provider": "neon", "values": {"DATABASE_URL": "postgresql://u:SecondPassw0rd@ep-x.neon.tech/app"}},
            headers=LOCAL,
        )
    monkeypatch.undo()
    assert project_secrets.snapshot(TEST_USER_ID, pid) == before
    assert project_secrets.reveal(TEST_USER_ID, pid) == {"DATABASE_URL": first}


def test_query_secrets_every_form_and_log_templates_survive():
    contract = dbconnect.contract_for("mongodb", "generic")
    parts = dbconnect.secret_parts(contract, {"MONGODB_URI": "mongodb://h/db?authToken=abc+def123xyz"})
    assert "abc+def123xyz" in parts and "abc def123xyz" in parts
    assert "abc+def123xyz" not in scrub.scrub("GET mongodb://h/db?authToken=abc+def123xyz")
    assert scrub.scrub("connecting to %s?password=%s") == "connecting to %s?password=%s"


def test_a_redo_repeating_atlas_s_own_host_keeps_the_person_s(client, monkeypatch):
    from app.db.base import SessionLocal
    from app.db.models import Project

    monkeypatch.setattr(
        dbconnect, "check_connection",
        lambda *a: dbconnect.CheckResult(dbconnect.CONNECTED, "ok", reason="ok", host="h"),
    )
    pid = _to_gate(client)
    with SessionLocal() as db:
        # Atlas's own words named Supabase.
        project = db.get(Project, pid)
        atlas = Charter.from_dict(project.charter).with_provider("supabase")
        project.charter = Charter(atlas.choices, "supabase", atlas.env, "supabase").as_dict()
        db.commit()
    client.post(
        f"/api/projects/{pid}/database/check",
        json={"provider": "neon", "values": {"DATABASE_URL": "postgresql://u:pw12345678@ep-x.neon.tech/app?sslmode=require"}},
        headers=LOCAL,
    )
    with SessionLocal() as db:
        project = db.get(Project, pid)
        before = dict(project.charter)
        assert before["database_provider"] == "neon" and before["database_provider_frozen"] == "supabase"
        # An unrelated redo: Atlas names Supabase again.
        project.charter = Charter(atlas.choices, "supabase", atlas.env, "supabase").as_dict()
        runner.settle_database(project, before)
        assert Charter.from_dict(project.charter).database_provider == "neon"
        assert project.database_status == "connected"
    assert project_secrets.load(TEST_USER_ID, pid)["provider"] == "neon"


def test_a_percent_encoded_query_password_is_still_scrubbed():
    assert "%40Hunter2x" not in scrub.scrub("dial mongodb://h/db?password=%40Hunter2x")
