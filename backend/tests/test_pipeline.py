"""End-to-end pipeline tests using the stubbed LLM (see conftest)."""
from __future__ import annotations

PHASES = [
    "product_manager",
    "system_design",
    "backend_engineer",
    "frontend_engineer",
    "qa_engineer",
    "security_engineer",
    "devops_engineer",
    "cost_estimation",
]


def _create(client, **overrides) -> str:
    body = {"idea": "Build a food delivery platform for college students", "routing_mode": "local_only"}
    body.update(overrides)
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health_and_pipeline_shape(client):
    assert client.get("/health").json()["status"] == "ok"
    shape = client.get("/api/models/pipeline").json()
    assert [p["key"] for p in shape["phases"]] == PHASES
    assert "mermaid" in shape


def test_full_run_with_approvals(client):
    pid = _create(client, require_approval=True)

    # Start -> first phase produced, awaiting approval.
    run = client.post(f"/api/projects/{pid}/run").json()
    assert run["status"] == "awaiting_approval"
    assert run["current_phase"] == "product_manager"

    # Approve through to completion.
    seen_phases = []
    for _ in range(20):  # generous cap; pipeline is 8 phases
        proj = client.get(f"/api/projects/{pid}").json()
        if proj["status"] == "completed":
            break
        assert proj["status"] == "awaiting_approval"
        seen_phases.append(proj["current_phase"])
        resp = client.post(f"/api/projects/{pid}/approve").json()
        assert resp["status"] in ("awaiting_approval", "completed")

    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["status"] == "completed"
    assert proj["current_phase"] is None

    # Every phase ran and was approved.
    approved = {p["phase"] for p in proj["phases"] if p["status"] == "approved"}
    assert approved == set(PHASES)
    assert seen_phases == PHASES

    # A debate was recorded before the backend phase.
    debates = client.get(f"/api/analytics/projects/{pid}/debates").json()
    assert len(debates) >= 1
    assert debates[0]["decision"]

    # Analytics captured usage for every LLM call (8 phases + >=1 debate).
    summary = client.get(f"/api/analytics/projects/{pid}").json()
    assert summary["calls"] >= 9
    assert summary["total_tokens"] > 0


def test_reject_regenerates_phase(client):
    pid = _create(client, require_approval=True)
    client.post(f"/api/projects/{pid}/run")

    before = client.get(f"/api/projects/{pid}").json()
    assert before["current_phase"] == "product_manager"
    pm_rows_before = [p for p in before["phases"] if p["phase"] == "product_manager"]

    # Reject with feedback -> regenerates the same phase, stays awaiting approval.
    resp = client.post(
        f"/api/projects/{pid}/reject", json={"feedback": "Tighten the MVP scope"}
    ).json()
    assert resp["status"] == "awaiting_approval"
    assert resp["current_phase"] == "product_manager"

    after = client.get(f"/api/projects/{pid}").json()
    pm_rows_after = [p for p in after["phases"] if p["phase"] == "product_manager"]
    # One row was rejected and a fresh pending one was produced.
    assert len(pm_rows_after) == len(pm_rows_before) + 1
    assert any(p["status"] == "rejected" and p["feedback"] for p in pm_rows_after)
    assert any(p["status"] == "pending_approval" for p in pm_rows_after)


def test_auto_run_without_approvals(client):
    pid = _create(client, require_approval=False)
    # Approvals disabled -> runs to completion in a background task.
    run = client.post(f"/api/projects/{pid}/run").json()
    assert run["status"] == "running"
    # TestClient runs the background task synchronously after the response,
    # so by the time we poll, it has completed.
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["status"] == "completed"
    assert proj["current_phase"] is None
    assert {p["phase"] for p in proj["phases"]} == set(PHASES)
    # Auto-run auto-approves everything — no phase is left pending a (never-coming) human.
    assert {p["status"] for p in proj["phases"]} == {"approved"}

    # Approving a finished auto-run is a clean 400, not a crash (this is the error the
    # old phantom gate produced when the UI offered an approve button it shouldn't have).
    err = client.post(f"/api/projects/{pid}/approve")
    assert err.status_code == 400
    assert "Nothing to approve" in err.text


def test_auto_run_stays_running_between_phases(stub_router):
    """Regression: auto-run must never park in `awaiting_approval` mid-pipeline.

    That status is what the frontend renders as an approval gate; if auto-run used it
    between phases, the UI would show (and let the user click) an approve button that the
    backend then rejects with `400 Nothing to approve` once the background run finishes.
    """
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration.runner import runner

    db = SessionLocal()
    try:
        project = Project(idea="Build a landing site", routing_mode="local_only", require_approval=False)
        db.add(project)
        db.commit()
        db.refresh(project)

        runner.start(db, project, pause=False)
        assert project.status == "running"  # mid-run, not a phantom gate
        assert project.current_phase == "product_manager"

        runner.advance(db, project, pause=False)
        assert project.status == "running"
        assert project.current_phase == "system_design"
    finally:
        db.close()
