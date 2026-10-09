"""Each agent's record across an account's builds (#91): `GET /api/analytics/crew`."""
from __future__ import annotations

from app.core.constants import PHASE_ORDER
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project, UsageEvent
from tests.test_accounts import two  # noqa: F401 — the fixture


def _build(owner_id: str, idea: str) -> str:
    with SessionLocal() as db:
        p = Project(owner_id=owner_id, idea=idea, status="completed")
        db.add(p)
        db.commit()
        return p.id


def _row(project_id: str, phase: str, status: str, **extra) -> None:
    with SessionLocal() as db:
        db.add(PhaseResult(project_id=project_id, phase=phase, agent=phase, status=status, **extra))
        db.commit()


def _call(owner_id: str, project_id: str, phase: str, **extra) -> None:
    with SessionLocal() as db:
        db.add(UsageEvent(owner_id=owner_id, project_id=project_id, phase=phase, provider="p", model="m", **extra))
        db.commit()


def test_an_account_with_two_builds_and_one_rejection(two):
    a, a_user, b, b_user = two
    first = _build(a_user.id, "A recipe box")
    second = _build(a_user.id, "A habit tracker")

    # FORGE wrote both backends; the first was sent back once, then approved.
    _row(first, "backend_engineer", "rejected", schema_status="repaired", build_status="failed")
    _row(first, "backend_engineer", "approved", schema_status="valid", build_status="ok")
    _row(second, "backend_engineer", "approved", schema_status="invalid", build_status="ok")
    # A restored version's copy of a row is the same work, not more of it.
    _row(second, "backend_engineer", "approved", handoff={"restored_row": "x"})
    _call(a_user.id, first, "backend_engineer", total_tokens=1000, cost_usd=0.02, cost_known=True,
          is_local=False, latency_ms=3000)
    _call(a_user.id, first, "backend_engineer", total_tokens=400, cost_usd=0.0, cost_known=False,
          is_local=True, latency_ms=1000)
    _call(a_user.id, second, "backend_engineer", total_tokens=600, is_local=True, latency_ms=2000)
    # Calls outside the eight phases (a debate, a preview edit) belong to no agent here.
    _call(a_user.id, first, "debate", total_tokens=9999)
    # Someone else's build never counts.
    other = _build(b_user.id, "B's build")
    _row(other, "backend_engineer", "rejected")
    _call(b_user.id, other, "backend_engineer", total_tokens=5)

    r = a.get("/api/analytics/crew")
    assert r.status_code == 200, r.text
    forge = r.json()["phases"]["backend_engineer"]
    assert forge["builds"] == 2
    assert forge["approved"] == 2 and forge["rejected"] == 1 and forge["failed"] == 0
    assert forge["schema_repaired"] == 1 and forge["schema_invalid"] == 1
    assert forge["build_ok"] == 2 and forge["build_failed"] == 1
    assert forge["calls"] == 3 and forge["tokens"] == 2000
    assert forge["cost_usd"] == 0.02 and forge["unpriced_calls"] == 1
    assert forge["avg_latency_ms"] == 2000.0
    assert forge["local_calls"] == 2 and forge["located_calls"] == 3
    assert forge["local_share"] == round(2 / 3, 3)

    # Every phase is present, in order, even ones that never ran.
    phases = r.json()["phases"]
    assert list(phases) == [p.value for p in PHASE_ORDER]
    assert phases["product_manager"]["calls"] == 0 and phases["product_manager"]["builds"] == 0

    # B sees only B's work.
    mine = b.get("/api/analytics/crew").json()["phases"]["backend_engineer"]
    assert mine["builds"] == 1 and mine["rejected"] == 1 and mine["tokens"] == 5


def test_an_account_with_no_builds_gets_zeros_not_an_error(two):
    a, *_ = two
    r = a.get("/api/analytics/crew")
    assert r.status_code == 200, r.text
    for rec in r.json()["phases"].values():
        assert rec["builds"] == 0 and rec["calls"] == 0 and rec["tokens"] == 0
        assert rec["cost_usd"] == 0.0 and rec["avg_latency_ms"] == 0.0
        # Nothing recorded where it ran, so there is no share to show, not 0%.
        assert rec["local_share"] is None


def test_the_running_phase_says_what_it_was_handed_before_its_row_is_saved():
    """The crew floor's courier names what the next agent got at the hand-off (#91)."""
    from app.orchestration import activity
    from app.router import inflight

    with SessionLocal() as db:
        p = Project(owner_id="someone", idea="A tide clock", status="running", current_phase="backend_engineer")
        db.add(p)
        db.commit()
        pid = p.id
    deps = [{"phase": "system_design", "digest": True, "full": "whole", "omitted": []},
            {"phase": "product_manager", "digest": True, "full": "digest_only", "omitted": []}]
    try:
        activity.given("backend_engineer", deps)  # outside a build: nothing to record
        assert activity.given_for(pid) is None
        with inflight.building(pid):
            activity.given("backend_engineer", deps)
        with SessionLocal() as db:
            p = db.get(Project, pid)
            assert p.given == {"phase": "backend_engineer", "deps": deps}
            # Another phase's record is not this one's.
            p.current_phase = "frontend_engineer"
            assert p.given is None
            p.current_phase = "backend_engineer"
            p.status = "completed"
            assert p.given is None
        # A new phase starts clean.
        activity.clear(pid)
        assert activity.given_for(pid) is None
    finally:
        activity.clear(pid)


def test_a_deleted_builds_calls_leave_the_record_with_it(two):
    """Usage events have no foreign key and outlive their build; the record must not."""
    a, a_user, *_ = two
    kept = _build(a_user.id, "A plant watering log")
    _row(kept, "frontend_engineer", "approved")
    _call(a_user.id, kept, "frontend_engineer", total_tokens=100, latency_ms=1000)
    r = a.post("/api/projects", json={"idea": "A build to delete"})
    gone = r.json()["id"]
    _row(gone, "frontend_engineer", "approved")
    _call(a_user.id, gone, "frontend_engineer", total_tokens=900, latency_ms=3000)
    assert a.get("/api/analytics/crew").json()["phases"]["frontend_engineer"]["tokens"] == 1000

    assert a.delete(f"/api/projects/{gone}").status_code == 204
    prism = a.get("/api/analytics/crew").json()["phases"]["frontend_engineer"]
    assert prism["builds"] == 1 and prism["tokens"] == 100 and prism["calls"] == 1
    assert prism["avg_latency_ms"] == 1000.0


def test_what_a_phase_was_handed_is_bounded_and_goes_with_the_build(two, monkeypatch):
    from app.orchestration import activity
    from app.router import inflight

    a, a_user, *_ = two
    deps = [{"phase": "product_manager", "digest": True, "full": "whole", "omitted": []}]
    pid = _build(a_user.id, "A reading list")
    with inflight.building(pid):
        activity.given("system_design", deps)
    # A run that stops driving leaves it: a planning phase opens no board, so a newer
    # run mid-phase looks the same from here.
    activity.drop_ended(pid)
    assert activity.given_for(pid)
    # Deleting the build takes it.
    assert a.delete(f"/api/projects/{pid}").status_code == 204
    assert activity.given_for(pid) is None

    # Bounded: the oldest builds' records go first, the one writing now stays.
    monkeypatch.setattr(activity, "GIVEN_MAX", 3)
    ids = [f"bounded-{i}" for i in range(5)]
    try:
        for i in ids:
            with inflight.building(i):
                activity.given("system_design", deps)
        assert [activity.given_for(i) is not None for i in ids] == [False, False, True, True, True]
    finally:
        for i in ids:
            activity.clear(i)
