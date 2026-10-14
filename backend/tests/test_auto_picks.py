"""Auto picks each agent's model from this account's own record (#96)."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Optional

import pytest

from tests.conftest import TEST_USER_ID, stub, through_database_gate
from tests.test_runtimes import FakeAdapter, _NoRoles

from app.core import model_settings, secrets_store
from app.core.constants import RoutingMode
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project, User
from app.router import history, track_record
from app.router.runtimes.provider import Source, SourceProvider
from app.router.runtimes.types import ModelEntry
from app.router.track_record import Outcome
from app.schemas.llm import ChatMessage

from app.router.router import ModelRouter as _Router  # noqa: E402

#: Kept before any fixture stubs it, for the test that needs the real check.
_REAL_READINESS = _Router.readiness

GB = 1024 ** 3
BIG = ModelEntry(name="qwen2.5:7b", kind="chat", size_bytes=int(4.7 * GB))
SMALL = ModelEntry(name="qwen3:1.7b", kind="chat", size_bytes=int(1.4 * GB))
VECTORS = ModelEntry(name="nomic-embed-text", kind="embedding", size_bytes=int(0.3 * GB))


# ── the scoring, without a database ──────────────────────────────────────────
def _runs(model: str, n: int, **facts) -> list[Outcome]:
    return [Outcome(model=model, **facts) for _ in range(n)]


def test_a_record_reads_as_a_sentence():
    record = track_record.judge("ollama:a", _runs("ollama:a", 5, schema="valid", latency_ms=900))
    assert record.sample == 5 and record.score == 1.0
    assert record.reason == "valid first try in 5 of 5 runs"

    mixed = (
        _runs("ollama:a", 3, schema="valid", built=True)
        + _runs("ollama:a", 1, schema="repaired", built=False, cut_off=True)
        + [Outcome(model="ollama:a", failed=True)]
    )
    record = track_record.judge("ollama:a", mixed)
    assert record.reason == "valid first try in 3 of 5 runs, built in 3 of 4, failed in 1, cut off in 1"
    assert 0 < record.score < 1


def test_only_the_last_twenty_runs_count():
    old_failures = _runs("ollama:a", 30, failed=True)
    record = track_record.judge("ollama:a", _runs("ollama:a", 20, schema="valid") + old_failures)
    assert record.sample == 20 and record.score == 1.0


def test_an_unknown_is_left_out_rather_than_counted():
    assert Outcome(model="m").score() == 1.0, "a finished run that says nothing delivered"
    assert Outcome(model="m", schema="valid", cut_off=False, fell_back=False).score() == 1.0
    assert Outcome(model="m", schema="valid", fell_back=True).score() == 0.5


def test_the_routers_first_choice_runs_until_it_has_a_record():
    prior = [("ollama:big", "your default"), ("ollama:small", "a model that fits this computer")]
    small = track_record.judge("ollama:small", _runs("ollama:small", 5, schema="valid"))
    pick = track_record.choose(prior, {"ollama:small": small})
    assert (pick.model, pick.basis, pick.sample) == ("ollama:big", "prior", 0)
    assert pick.reason == "no record yet, so your default"

    two = track_record.judge("ollama:big", _runs("ollama:big", 2, schema="valid"))
    pick = track_record.choose(prior, {"ollama:small": small, "ollama:big": two})
    assert pick.model == "ollama:big" and pick.reason == "2 runs on record, too few to judge, so your default"


def test_a_poor_best_record_gives_an_untried_candidate_a_turn():
    prior = [("ollama:big", "the largest model that fits this computer"), ("ollama:small", "your default"),
             ("ollama:tiny", "a model that fits this computer")]
    big = track_record.judge("ollama:big", _runs("ollama:big", 5, schema="invalid"))
    pick = track_record.choose(prior, {"ollama:big": big})
    assert (pick.model, pick.basis) == ("ollama:small", "prior")
    assert pick.reason == "big did poorly here (valid first try in 0 of 5 runs), so this one gets a turn"
    # Once every candidate has a record, the best of them is kept.
    small = track_record.judge("ollama:small", _runs("ollama:small", 3, schema="repaired"))
    tiny = track_record.judge("ollama:tiny", _runs("ollama:tiny", 3, schema="invalid"))
    pick = track_record.choose(prior, {"ollama:big": big, "ollama:small": small, "ollama:tiny": tiny})
    assert (pick.model, pick.basis) == ("ollama:small", "record")
    # A good record is kept, untried candidates or not.
    good = track_record.judge("ollama:big", _runs("ollama:big", 5, schema="valid"))
    assert track_record.choose(prior, {"ollama:big": good}).model == "ollama:big"


def test_with_records_the_better_one_wins_and_speed_only_breaks_ties():
    prior = [("ollama:big", "your default"), ("ollama:small", "a model that fits this computer")]
    big = track_record.judge("ollama:big", _runs("ollama:big", 5, schema="invalid", latency_ms=500))
    small = track_record.judge("ollama:small", _runs("ollama:small", 5, schema="valid", latency_ms=9000))
    pick = track_record.choose(prior, {"ollama:big": big, "ollama:small": small})
    assert (pick.model, pick.basis, pick.reason) == ("ollama:small", "record", "valid first try in 5 of 5 runs")

    fast = track_record.judge("ollama:small", _runs("ollama:small", 5, schema="valid", latency_ms=400))
    slow = track_record.judge("ollama:big", _runs("ollama:big", 5, schema="valid", latency_ms=8000))
    assert track_record.choose(prior, {"ollama:big": slow, "ollama:small": fast}).model == "ollama:small"
    even = track_record.judge("ollama:small", _runs("ollama:small", 5, schema="valid", latency_ms=8000))
    assert track_record.choose(prior, {"ollama:big": slow, "ollama:small": even}).model == "ollama:big"


# ── what a row says about the model behind it ────────────────────────────────
def _row(**kw) -> dict:
    base = {
        "phase": "backend_engineer", "status": "approved", "provider_used": "ollama", "model_used": "big",
        "model_pick": None, "schema_status": "valid", "build_status": "ok", "build_run_status": None,
        "test_status": None, "truncated": 0, "feedback": None, "latency_ms": 1200,
    }
    return {**base, **kw}


def test_a_row_speaks_for_the_model_that_served_it():
    (one,) = history.outcomes(_row(build_run_status="failed"))
    assert (one.model, one.schema, one.built, one.cut_off, one.fell_back) == ("ollama:big", "valid", False, False, None)
    (doc,) = history.outcomes(_row(phase="product_manager", build_status="ok"))
    assert doc.built is None, "a phase with no code reports ok because it compiled nothing"
    (qa,) = history.outcomes(_row(phase="qa_engineer", test_status="failed"))
    assert qa.tested is False


def test_a_fallback_is_put_down_to_the_model_the_call_fell_back_from():
    pick = {"model": "ollama:big", "fell_back": True, "fell_back_from": ["ollama:big"]}
    served, failed = history.outcomes(_row(model_used="small", model_pick=pick))
    assert served.model == "ollama:small" and served.fell_back is False
    assert failed.model == "ollama:big" and failed.failed
    # Fell back off itself on one call and answered the rest: one mishap, not two.
    (same,) = history.outcomes(_row(model_pick={**pick, "fell_back_from": ["ollama:big:latest"]}))
    assert same.model == "ollama:big" and same.fell_back is True
    # The fix loop's pin failed and the call landed on the pick: the pin is blamed.
    escalated = {"model": "ollama:big", "fell_back": True, "fell_back_from": ["anthropic:claude-x"]}
    served, pin = history.outcomes(_row(model_pick=escalated))
    assert served.model == "ollama:big" and served.fell_back is False
    assert pin.model == "anthropic:claude-x" and pin.failed
    # A row from before calls recorded it can't say.
    (old,) = history.outcomes(_row(model_pick={"model": "ollama:big", "fell_back": True}))
    assert old.fell_back is None


def test_a_failed_phase_counts_only_when_the_model_failed():
    pick = {"model": "ollama:big"}
    failed = _row(status="failed", provider_used=None, model_used=None, model_pick=pick)
    (one,) = history.outcomes({**failed, "feedback": "The model provider failed during this phase."})
    assert one.model == "ollama:big" and one.failed
    assert history.outcomes({**failed, "feedback": "The agent produced no output."})[0].failed
    assert history.outcomes({**failed, "feedback": "Stopped before this phase finished."}) == []
    assert history.outcomes({**failed, "feedback": "Paused: the computer running this model disconnected."}) == []
    assert history.outcomes({**failed, "model_pick": None, "feedback": "The agent produced no output."}) == []


# ── read from the database, per account ──────────────────────────────────────
def _other_account() -> str:
    with SessionLocal() as db:
        user = User(email=f"other-{time.monotonic_ns()}@example.com", password_hash="x", is_owner=False)
        db.add(user)
        db.commit()
        return user.id


def _finished(owner: str, phase: str, model: str, n: int, **kw) -> list[str]:
    from datetime import datetime, timezone

    source, _, name = model.partition(":")
    ids = []
    with SessionLocal() as db:
        project = Project(idea="history", owner_id=owner, status="completed")
        db.add(project)
        db.flush()
        for _ in range(n):
            row = PhaseResult(
                project_id=project.id, phase=phase, agent=phase, status=kw.get("status", "approved"),
                provider_used=source, model_used=name, schema_status=kw.get("schema", "valid"),
                build_run=kw.get("build_run"), handoff=kw.get("handoff", {"truncated_replies": 0}),
                model_pick=kw.get("pick"), latency_ms=1000,
                started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
            )
            db.add(row)
        db.commit()
        ids.append(project.id)
    return ids


@pytest.fixture
def clean_history():
    history.forget()
    yield
    history.forget()
    with SessionLocal() as db:
        for project in db.query(Project).filter(Project.idea == "history").all():
            db.delete(project)
        db.commit()


def test_records_are_read_per_account_with_the_json_facts(clean_history):
    _finished(TEST_USER_ID, "backend_engineer", "ollama:big", 4, build_run={"status": "failed", "steps": []},
              handoff={"truncated_replies": 2, "deps": []})
    other = _other_account()
    _finished(other, "backend_engineer", "ollama:small", 5)

    mine = history.records(TEST_USER_ID)["backend_engineer"]
    assert "ollama:small" not in mine, "another account's builds never decide this one's models"
    assert mine["ollama:big"].reason == "valid first try in 4 of 4 runs, built in 0 of 4, cut off in 4"
    assert set(history.records(other)["backend_engineer"]) == {"ollama:small"}


def test_a_finished_phase_makes_that_accounts_next_read_fresh(clean_history):
    _finished(TEST_USER_ID, "devops_engineer", "ollama:big", 3)
    assert history.records(TEST_USER_ID)["devops_engineer"]["ollama:big"].sample == 3
    _finished(TEST_USER_ID, "devops_engineer", "ollama:big", 1)
    assert history.records(TEST_USER_ID)["devops_engineer"]["ollama:big"].sample == 3, "cached"
    history.changed("someone-else")
    assert history.records(TEST_USER_ID)["devops_engineer"]["ollama:big"].sample == 3, "another account's phase"
    history.changed(TEST_USER_ID)
    assert history.records(TEST_USER_ID)["devops_engineer"]["ollama:big"].sample == 4


# ── the router: one ranking for every caller ─────────────────────────────────
def _source(source_id: str, models: list[ModelEntry], *, same_machine: Optional[bool] = None) -> SourceProvider:
    return SourceProvider(
        Source(id=source_id, label=source_id.capitalize(), base_url="http://127.0.0.1:11434",
               runtime="openai-compatible", origin="detected", same_machine_override=same_machine),
        FakeAdapter(models),
    )


@pytest.fixture
def auto_router(monkeypatch):
    """The router with the sources and the record a test hands it."""
    from app.router.router import router as model_router

    blocked: set[str] = set()
    caveated: set[str] = set()
    recorded: dict[str, dict] = {}

    def compatibility(self, model, **_kw):
        level = "blocked" if model in blocked else "degraded" if model in caveated else "fits"
        return SimpleNamespace(
            level=level,
            as_dict=lambda: {"spec": f"{self.name}:{model}", "level": level, "summary": "", "reasons": [], "facts": {}},
        )

    monkeypatch.setattr(SourceProvider, "compatibility", compatibility)
    monkeypatch.setattr(history, "records", lambda user_id: recorded)

    def _install(*providers: SourceProvider, chosen: Optional[str] = None, roles=None, records=None, blocks=(),
                 caveats=()):
        monkeypatch.setattr(model_router.sources, "_providers", {p.name: p for p in providers})
        monkeypatch.setattr(model_router.sources, "_loaded", True)
        monkeypatch.setattr(model_router.sources, "_detected_at", time.monotonic() + 3600)
        monkeypatch.setattr(model_router, "_chosen_local", chosen)
        monkeypatch.setattr(model_router, "_roles", roles or _NoRoles())
        monkeypatch.setattr(model_router, "_prior_cache", {})
        monkeypatch.setattr(model_router, "tuning", model_settings)
        monkeypatch.setattr(model_router.sources, "_store", secrets_store)
        recorded.clear()
        recorded.update(history.summarise(records or []))
        blocked.clear()
        blocked.update(blocks)
        caveated.clear()
        caveated.update(caveats)
        return model_router

    return _install


def _record(role: str, model: str, n: int, **kw) -> list[dict]:
    source, _, name = model.partition(":")
    return [_row(phase=role, provider_used=source, model_used=name, **kw) for _ in range(n)]


def test_with_no_record_auto_runs_the_largest_model_that_fits(auto_router):
    model_router = auto_router(_source("ollama", [SMALL, BIG, VECTORS]), chosen="ollama:qwen3:1.7b")
    assert model_router._auto_pick("medium", "product_manager") == ("ollama", "qwen2.5:7b")
    found = model_router.explain_pick(RoutingMode.AUTO, None, "medium", "product_manager")
    assert found["reason"] == "no record yet, so the largest model that fits this computer"
    assert found["basis"] == "prior" and found["where"] == "Ollama" and found["local"]
    # Local model stays on the default the person chose.
    assert model_router._resolve_chain(RoutingMode.LOCAL_ONLY, None, "medium", "product_manager")[0] == (
        "ollama", "qwen3:1.7b")


def test_a_model_that_would_not_run_here_is_not_chosen(auto_router):
    model_router = auto_router(_source("ollama", [SMALL, BIG]), chosen="ollama:qwen3:1.7b", blocks={"qwen2.5:7b"})
    assert model_router._auto_pick("medium", "product_manager") == ("ollama", "qwen3:1.7b")


def test_each_agent_gets_the_model_its_own_record_favours(auto_router):
    records = (
        _record("product_manager", "ollama:qwen3:1.7b", 5, schema_status="valid", latency_ms=800)
        + _record("product_manager", "ollama:qwen2.5:7b", 5, schema_status="invalid")
        + _record("cost_estimation", "ollama:qwen2.5:7b", 4, schema_status="valid")
    )
    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen2.5:7b", records=records)

    assert model_router._auto_pick("medium", "product_manager") == ("ollama", "qwen3:1.7b")
    assert model_router._auto_pick("medium", "cost_estimation") == ("ollama", "qwen2.5:7b")
    # Prompt sizing, the call itself and the check before Start agree.
    assert model_router.profile_for(RoutingMode.AUTO, None, "medium", role="product_manager").model == "qwen3:1.7b"
    resp = model_router.complete([ChatMessage(role="user", content="hi")], mode=RoutingMode.AUTO, role="product_manager")
    assert resp.model == "qwen3:1.7b"
    ready = model_router.readiness(RoutingMode.AUTO, None, roles=["product_manager", "cost_estimation"])
    assert ready.ok
    picks = {p["role"]: p for p in ready.picks}
    assert picks["product_manager"]["name"] == "qwen3:1.7b"
    assert picks["product_manager"]["reason"] == "valid first try in 5 of 5 runs"
    assert picks["cost_estimation"]["name"] == "qwen2.5:7b" and picks["cost_estimation"]["basis"] == "record"
    assert {c["model"] for c in ready.checks} == {"qwen3:1.7b", "qwen2.5:7b"}


def test_an_agent_pointed_at_a_model_in_settings_still_leads(auto_router):
    class Pinned(_NoRoles):
        def get(self, role):
            return "ollama:qwen2.5:7b" if role == "product_manager" else None

    records = _record("product_manager", "ollama:qwen3:1.7b", 5, schema_status="valid")
    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen2.5:7b", roles=Pinned(), records=records)
    chain = model_router._resolve_chain(RoutingMode.AUTO, None, "medium", "product_manager")
    assert chain[0] == ("ollama", "qwen2.5:7b")
    found = model_router.explain_pick(RoutingMode.AUTO, None, "medium", "product_manager")
    assert found["reason"] == "set for this agent in Settings" and found["basis"] == "settings"


def test_a_model_on_another_computer_or_a_hosted_one_is_never_picked_unasked(auto_router):
    hosted = ModelEntry(name="giant:480b-cloud", kind="chat", is_local=False, size_bytes=900 * GB)
    remote = _source("desk", [ModelEntry(name="huge:70b", kind="chat", size_bytes=40 * GB)], same_machine=False)
    model_router = auto_router(_source("ollama", [SMALL, hosted]), remote, chosen="ollama:qwen3:1.7b")
    assert model_router._auto_pick("high", "system_design") == ("ollama", "qwen3:1.7b")
    assert [c["name"] for c in model_router.auto_choices()] == ["qwen3:1.7b"]


def test_a_hard_step_goes_to_a_working_cloud_key_until_the_record_says_otherwise(auto_router, monkeypatch):
    from app.router.router import router as model_router

    monkeypatch.setitem(model_router._default_model, "anthropic", "claude-x")
    monkeypatch.setattr(model_router._cloud["anthropic"], "available", lambda: True)
    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen2.5:7b")

    assert model_router._auto_pick("high", "system_design") == ("anthropic", "claude-x")
    found = model_router.explain_pick(RoutingMode.AUTO, None, "high", "system_design")
    assert found["reason"] == "no record yet, so the strongest cloud model for a hard step"
    assert found["where"] == "Anthropic" and not found["local"]
    assert model_router._auto_pick("medium", "product_manager") == ("ollama", "qwen2.5:7b")
    assert {c["name"] for c in model_router.auto_choices()} == {"claude-x", "qwen2.5:7b", "qwen3:1.7b"}

    records = (
        _record("system_design", "anthropic:claude-x", 3, schema_status="invalid")
        + _record("system_design", "ollama:qwen2.5:7b", 3, schema_status="valid")
    )
    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen2.5:7b", records=records)
    assert model_router._auto_pick("high", "system_design") == ("ollama", "qwen2.5:7b")


def test_the_preflight_names_each_agents_model_and_what_auto_can_choose(client, monkeypatch, auto_router):
    from app.router.router import ModelRouter

    # The suite's stubbed readiness says nothing about picks; this test wants the real one.
    monkeypatch.setattr(ModelRouter, "readiness", _REAL_READINESS)
    auto_router(_source("ollama", [BIG, SMALL, VECTORS]), chosen="ollama:qwen2.5:7b")
    body = client.post("/api/projects/preflight", json={"routing_mode": "auto"}).json()
    assert body["ok"]
    roles = {p["role"]: p for p in body["picks"]}
    assert {"product_manager", "system_design", "cost_estimation", "debate", "planner"} <= set(roles)
    assert "embeddings" not in roles
    assert roles["product_manager"]["name"] == "qwen2.5:7b"
    assert [c["name"] for c in body["auto"]] == ["qwen2.5:7b", "qwen3:1.7b"]


# ── a run records why ────────────────────────────────────────────────────────
def test_every_phase_row_records_its_pick(client, monkeypatch):
    from app.router.router import ModelRouter

    def explain(self, mode, preferred_model, complexity, role, resolved=None, *, escalated=False):
        return {"mode": mode.value, "model": "ollama:qwen2.5:7b", "name": "qwen2.5:7b", "where": "Ollama",
                "local": True, "reason": f"no record yet, so your default ({complexity})", "sample": 0, "basis": "prior"}

    monkeypatch.setattr(ModelRouter, "explain_pick", explain)
    r = client.post("/api/projects", json={"idea": "A pick for every phase", "routing_mode": "auto",
                                           "require_approval": False})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = through_database_gate(client, pid)
    assert project["status"] == "completed"
    picks = {p["phase"]: p["model_pick"] for p in project["phases"]}
    assert len(picks) == 8 and all(picks.values())
    assert picks["system_design"]["reason"].endswith("(high)") and picks["product_manager"]["reason"].endswith("(medium)")
    assert all(p["mode"] == "auto" and p["fell_back"] is False and p["fell_back_from"] == [] for p in picks.values())


def test_a_stop_is_not_put_down_to_the_model(client, monkeypatch):
    from app.router.base import RequestCancelled

    def stopped(*_args, **_kwargs):
        raise RequestCancelled("Stopped.")

    stub(monkeypatch, "complete", stopped)
    r = client.post("/api/projects", json={"idea": "Stopped right away", "routing_mode": "local_only"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    with SessionLocal() as db:
        rows = db.query(PhaseResult).filter(PhaseResult.project_id == pid).all()
    assert rows and all(row.feedback == "Stopped before this phase finished." for row in rows if row.status == "failed")


def test_the_column_is_added_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE phase_results (id VARCHAR(32) PRIMARY KEY, phase TEXT)"))
        conn.execute(text("INSERT INTO phase_results (id, phase) VALUES ('r', 'product_manager')"))
    assert "phase_results.model_pick" in run_migrations(engine)
    assert "model_pick" in {c["name"] for c in inspect(engine).get_columns("phase_results")}
    assert run_migrations(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT phase, model_pick FROM phase_results")).fetchall() == [("product_manager", None)]


def test_the_fix_loops_last_round_records_the_model_it_starts_on(auto_router, monkeypatch):
    from app.router.router import ModelRouter

    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen3:1.7b")
    monkeypatch.setattr(ModelRouter, "strongest_for", lambda self, *a, **k: "ollama:qwen2.5:7b")
    found = model_router.explain_pick(RoutingMode.AUTO, None, "medium", "backend_engineer", escalated=True)
    assert (found["name"], found["basis"]) == ("qwen2.5:7b", "escalated")
    assert found["reason"] == "the strongest model it can reach, for the last fix round"
    # Nothing stronger: the round runs at the hard-step difficulty, and says why.
    monkeypatch.setattr(ModelRouter, "strongest_for", lambda self, *a, **k: None)
    found = model_router.explain_pick(RoutingMode.AUTO, None, "medium", "backend_engineer", escalated=True)
    assert found["model"] == model_router.explain_pick(RoutingMode.AUTO, None, "high", "backend_engineer")["model"]


def test_a_head_from_the_fallback_chain_is_not_called_the_default(auto_router, monkeypatch):
    from app.router import router as router_module
    from app.router.router import ModelRouter

    model_router = auto_router(_source("ollama", [BIG]), chosen="ollama:qwen2.5:7b")
    monkeypatch.setattr(ModelRouter, "_auto_choice", lambda self, complexity, role: None)
    monkeypatch.setattr(type(router_module.settings), "fallback_pairs", property(lambda self: [("openai", "gpt-x")]))
    found = model_router.explain_pick(RoutingMode.AUTO, None, "medium", "product_manager")
    assert (found["model"], found["basis"]) == ("openai:gpt-x", "fallback")
    assert found["reason"] == "the first model in the fallback chain"


def test_an_auto_phase_holds_its_calls_to_one_model(client, monkeypatch):
    """Auto's choice can change while a phase runs (another build's phase finishing
    changes the record); every call of the phase, sizing included, keeps to one."""
    from app.router.router import ModelRouter
    from tests.conftest import _fake_complete

    pins: list = []

    def complete(messages, **kwargs):
        pins.append(kwargs.get("pin"))
        return _fake_complete(messages, **kwargs)

    def explain(self, mode, preferred_model, complexity, role, resolved=None, *, escalated=False):
        return {"mode": mode.value, "model": f"ollama:held-{role}", "name": f"held-{role}", "where": "Ollama",
                "local": True, "reason": "held", "sample": 0, "basis": "prior"}

    stub(monkeypatch, "complete", complete)
    monkeypatch.setattr(ModelRouter, "explain_pick", explain)
    pid = client.post("/api/projects", json={"idea": "Held to one model", "routing_mode": "auto",
                                             "require_approval": False}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    held = [p for p in pins if p and p.startswith("ollama:held-")]
    assert held and {"ollama:held-product_manager", "ollama:held-backend_engineer"} <= set(held)


def test_a_kept_attempt_and_carried_failures_are_read_right():
    # Re-checked without a model call (#76): nothing to say about the model.
    assert history.outcomes(_row(phase="qa_engineer", test_status="failed", model_pick={"calls": 0})) == []
    # A retried phase's dead attempts are carried on the row that replaced them.
    carried = history.outcomes(_row(model_pick={"model": "ollama:big", "calls": 2, "fell_back_from": [],
                                                "failed_before": ["ollama:big", "ollama:small"]}))
    assert [(o.model, o.failed) for o in carried] == [
        ("ollama:big", False), ("ollama:big", True), ("ollama:small", True)]


def test_a_retried_phase_keeps_the_failure_of_the_attempt_it_clears(stub_router):
    from app.orchestration.runner import runner

    with SessionLocal() as db:
        project = Project(idea="history", owner_id=TEST_USER_ID, status="failed", routing_mode="auto")
        db.add(project)
        db.flush()
        db.add(PhaseResult(project_id=project.id, phase="product_manager", agent="Scope", status="failed",
                           feedback="The model provider failed during this phase.",
                           model_pick={"model": "ollama:big", "failed_before": ["ollama:older"]}))
        db.add(PhaseResult(project_id=project.id, phase="product_manager", agent="Scope", status="failed",
                           feedback="Stopped before this phase finished.", model_pick={"model": "ollama:stopped"}))
        db.commit()
        row = runner._begin_phase(db, project, "product_manager")
        assert row.model_pick["failed_before"] == ["ollama:big", "ollama:older"]
        db.delete(project)
        db.commit()


def test_an_account_or_provider_problem_is_not_held_against_the_model(auto_router):
    from app.router.base import ProviderError

    class Refusing(FakeAdapter):
        def __init__(self, models, kind):
            super().__init__(models)
            self.kind = kind

        def chat(self, request):
            raise ProviderError("refused", retryable=False, kind=self.kind)

    for kind, tried in (("no_credit", False), (None, True)):
        first = SourceProvider(Source(id="first", label="First", base_url="http://127.0.0.1:1111",
                                      runtime="openai-compatible", origin="detected"), Refusing([SMALL], kind))
        second = _source("ollama", [BIG])
        model_router = auto_router(first, second, chosen="ollama:qwen2.5:7b")
        resp = model_router.complete([ChatMessage(role="user", content="hi")], mode=RoutingMode.MANUAL,
                                     preferred_model="first:qwen3:1.7b")
        assert resp.fallback_used and resp.attempts[0]["tried"] is tried


def test_a_refused_run_still_names_each_agents_model(auto_router):
    hosted = ModelEntry(name="giant:480b-cloud", kind="chat", is_local=False)
    model_router = auto_router(_source("ollama", [hosted]), chosen="ollama:giant:480b-cloud")
    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"])
    assert not ready.ok and [p["name"] for p in ready.picks] == ["giant:480b-cloud"]


def test_one_model_spelled_two_ways_is_one_candidate(auto_router, monkeypatch):
    def resolves(self, model, names=None):
        names = self.list_models() if names is None else names
        bare = model[: -len(":latest")] if model.endswith(":latest") else model
        return any(n in (bare, f"{bare}:latest") for n in names)

    monkeypatch.setattr(SourceProvider, "resolves", resolves)
    latest = ModelEntry(name="qwen3:latest", kind="chat", size_bytes=2 * GB)
    model_router = auto_router(_source("ollama", [latest, SMALL]), chosen="ollama:qwen3")
    assert [c["name"] for c in model_router.auto_choices()] == ["qwen3:latest", "qwen3:1.7b"]
    found = model_router.explain_pick(RoutingMode.AUTO, None, "medium", "product_manager")
    assert found["reason"] == "no record yet, so your default"


def test_a_phase_stopped_by_a_key_or_a_bill_is_not_the_models_failure(client, monkeypatch):
    from app.router.base import ProviderError

    def broke(*_args, **_kwargs):
        raise ProviderError("Out of credit.", retryable=False, kind="no_credit", provider="openai")

    stub(monkeypatch, "complete", broke)
    pid = client.post("/api/projects", json={"idea": "Out of credit", "routing_mode": "local_only"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    with SessionLocal() as db:
        rows = db.query(PhaseResult).filter(PhaseResult.project_id == pid, PhaseResult.status == "failed").all()
    assert rows and all(row.feedback == "The provider stopped this phase over a key, credit or rate limit." for row in rows)
    assert all(history.outcomes({"phase": r.phase, "status": r.status, "feedback": r.feedback,
                                 "model_pick": {"model": "openai:gpt-x"}}) == [] for r in rows)


def test_a_model_that_runs_with_caveats_is_a_candidate_but_never_leads_a_cold_start(auto_router):
    """#96's own case: qwen3:1.7b is "a small model, the content will be thin" — a
    guess about quality, which the record measures. It is chosen when its record
    beats the default's, never on no record at all."""
    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen2.5:7b", caveats={"qwen3:1.7b"})
    assert [c["name"] for c in model_router.auto_choices()] == ["qwen2.5:7b", "qwen3:1.7b"]
    assert model_router._auto_pick("medium", "product_manager") == ("ollama", "qwen2.5:7b")
    records = (
        _record("product_manager", "ollama:qwen3:1.7b", 4, schema_status="valid", latency_ms=700)
        + _record("product_manager", "ollama:qwen2.5:7b", 4, schema_status="repaired")
    )
    model_router = auto_router(_source("ollama", [BIG, SMALL]), chosen="ollama:qwen2.5:7b",
                               caveats={"qwen3:1.7b"}, records=records)
    assert model_router._auto_pick("medium", "product_manager") == ("ollama", "qwen3:1.7b")
