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

    # Start returns immediately with `running` — the phase itself is a background
    # task. TestClient drains it before the next request, so the poll below sees
    # the gate the same way a real client's 2.5s poll would.
    run = client.post(f"/api/projects/{pid}/run").json()
    assert run["status"] == "running"

    # Approve through to completion.
    seen_phases = []
    for _ in range(20):  # generous cap; pipeline is 8 phases
        proj = client.get(f"/api/projects/{pid}").json()
        if proj["status"] == "completed":
            break
        assert proj["status"] == "awaiting_approval"
        seen_phases.append(proj["current_phase"])
        resp = client.post(f"/api/projects/{pid}/approve").json()
        # Approve hands the next phase to a background task and returns at once.
        assert resp["status"] == "running"

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

    # Reject with feedback -> regenerates the same phase in the background.
    resp = client.post(
        f"/api/projects/{pid}/reject", json={"feedback": "Tighten the MVP scope"}
    ).json()
    assert resp["status"] == "running"

    after = client.get(f"/api/projects/{pid}").json()
    assert after["status"] == "awaiting_approval"
    assert after["current_phase"] == "product_manager"
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


def test_a_phase_is_announced_before_it_runs(stub_router):
    """Regression: `current_phase` used to be written only *after* an agent finished.

    That is why the first phase rendered as eight queued nodes with no elapsed time —
    there was nothing to say who had the work. The row and the timestamps now exist
    from the moment generation starts.
    """
    from app.core.constants import PhaseStatus
    from app.db.base import SessionLocal
    from app.db.models import PhaseResult, Project
    from app.orchestration.runner import runner

    db = SessionLocal()
    try:
        project = Project(idea="Build a landing site", routing_mode="local_only")
        db.add(project)
        db.commit()
        db.refresh(project)

        row = runner._begin_phase(db, project, "product_manager")
        assert project.status == "running"
        assert project.current_phase == "product_manager"
        assert project.phase_started_at is not None
        assert project.heartbeat_at is not None
        assert row.status == PhaseStatus.RUNNING.value
        assert row.started_at is not None

        # A live phase is not stalled; a run whose heartbeat never lands is.
        assert project.stalled is False
        project.heartbeat_at = None
        project.phase_started_at = None
        db.commit()
        assert project.stalled is True

        # The placeholder is filled in, not duplicated, when the phase completes.
        assert db.query(PhaseResult).filter_by(project_id=project.id).count() == 1
    finally:
        db.close()


def test_approve_is_idempotent_across_tabs(client):
    """A stale second tab must not advance two phases on one click's worth of intent."""
    pid = _create(client, require_approval=True)
    client.post(f"/api/projects/{pid}/run")
    assert client.get(f"/api/projects/{pid}").json()["current_phase"] == "product_manager"

    first = client.post(f"/api/projects/{pid}/approve")
    assert first.status_code == 200

    # The project is `awaiting_approval` again (system_design), but this tab is
    # replaying the click it made against product_manager. It wins its own claim —
    # what must never happen is two claims landing on the *same* pending phase.
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.core.constants import PipelineStatus

    db = SessionLocal()
    try:
        project = db.get(Project, pid)
        project.status = PipelineStatus.RUNNING.value  # pretend a run is in flight
        db.commit()
    finally:
        db.close()

    stale = client.post(f"/api/projects/{pid}/approve")
    assert stale.status_code == 409
    assert "already" in stale.json()["detail"]


def test_stop_then_resume(client):
    """Any run can be got out of, and back into, from the UI."""
    pid = _create(client, require_approval=True)
    client.post(f"/api/projects/{pid}/run")
    assert client.get(f"/api/projects/{pid}").json()["status"] == "awaiting_approval"

    stopped = client.post(f"/api/projects/{pid}/stop", json={}).json()
    assert stopped["status"] == "cancelled"
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["status"] == "cancelled"
    assert proj["last_error"]

    # Everything generated before the stop survives it.
    assert any(p["phase"] == "product_manager" for p in proj["phases"])

    resumed = client.post(f"/api/projects/{pid}/resume").json()
    assert resumed["status"] == "running"
    after = client.get(f"/api/projects/{pid}").json()
    # The unapproved phase is offered again rather than skipped.
    assert after["status"] == "awaiting_approval"
    assert after["current_phase"] == "product_manager"


def test_delete_removes_the_build(client):
    pid = _create(client)
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


# ── two builds, two model calls, one process ─────────────────────────────────
def test_two_builds_on_two_projects_generate_at_the_same_time(client, monkeypatch):
    """The runner's lock used to wrap the model call, so builds ran one at a time.

    A rendezvous is the assertion: both runs have to be *inside* a model call at the
    same moment for it to clear. Serialised, the second never arrives, the first
    times out, and `overlapped` stays unset — which is what this test does against
    the code before the fix.

    The checkpoints are then read back per project. Concurrency that let one build's
    writes land in another's thread would be a far worse bug than the one being
    fixed, so both halves are asserted, not just the fast one.
    """
    import threading

    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration.graph import graph
    from app.orchestration.runner import _config, runner
    from app.router.router import router as model_router
    from tests.conftest import _fake_complete

    rendezvous = threading.Barrier(2, timeout=15)
    overlapped = threading.Event()
    guard = threading.Lock()
    done = False

    def meeting_complete(messages, **kwargs):
        # Only the first call from each run waits: once the two have met there is
        # nothing left to prove, and a second wait would hang on a reset barrier.
        nonlocal done
        with guard:
            wait = not done
        if wait:
            try:
                rendezvous.wait()
                overlapped.set()
            except threading.BrokenBarrierError:
                pass  # serialised — the assertion below is what reports it
            with guard:
                done = True
        return _fake_complete(messages, **kwargs)

    monkeypatch.setattr(model_router, "complete", meeting_complete)

    ideas = {
        _create(client, idea="A parking app for a university campus", require_approval=False):
            "A parking app for a university campus",
        _create(client, idea="A reading tracker for a book club", require_approval=False):
            "A reading tracker for a book club",
    }

    def drive(project_id: str) -> None:
        db = SessionLocal()
        try:
            runner.continue_run(db, db.get(Project, project_id))
        finally:
            db.close()

    threads = [
        threading.Thread(target=drive, args=(pid,), name=f"build-{pid[:8]}")
        for pid in ideas
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "a build never finished"

    assert overlapped.is_set(), "the two builds never generated at the same time"

    for project_id, idea in ideas.items():
        proj = client.get(f"/api/projects/{project_id}").json()
        assert proj["status"] == "completed", proj.get("last_error")
        values = graph.get_state(_config(project_id)).values
        assert values["idea"] == idea, "one build's checkpoint holds another's idea"
        assert set(values["prior_outputs"]) == set(PHASES)


def test_a_redo_holds_its_own_build_while_it_generates_and_no_other(client, monkeypatch):
    """The read-modify-write in `redo` stays one critical section, per project.

    A redo builds its checkpoint patch from a snapshot read before the model call.
    Stop marks a build cancelled without interrupting that call and Resume can claim
    it straight back, so a second driver on the *same* build is possible mid-call —
    and releasing the lock across generation let that driver's writes be overwritten
    by a patch built from a snapshot that no longer existed. The same lock must not
    hold up any *other* build, or this is the process-wide lock over again.
    """
    import threading

    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration.runner import _checkpoint_lock, runner
    from app.router.router import router as model_router
    from tests.conftest import _fake_complete

    busy = _create(client, require_approval=True)
    other = _create(client, require_approval=True)
    client.post(f"/api/projects/{busy}/run")
    assert client.get(f"/api/projects/{busy}").json()["status"] == "awaiting_approval"

    generating = threading.Event()
    release = threading.Event()

    def held_complete(messages, **kwargs):
        generating.set()
        release.wait(timeout=15)
        return _fake_complete(messages, **kwargs)

    monkeypatch.setattr(model_router, "complete", held_complete)

    def drive_redo() -> None:
        db = SessionLocal()
        try:
            runner.redo(db, db.get(Project, busy), "product_manager", "Tighten the scope")
        finally:
            db.close()

    redo = threading.Thread(target=drive_redo, name="redo")
    redo.start()
    try:
        assert generating.wait(timeout=15), "the redo never reached its model call"

        def try_lock(project_id: str, out: list) -> None:
            # From another thread: the lock is re-entrant, so asking from the redo's
            # own thread would always succeed and prove nothing.
            lock = _checkpoint_lock(project_id)
            got = lock.acquire(timeout=0.3)
            if got:
                lock.release()
            out.append(got)

        same: list = []
        elsewhere: list = []
        for project_id, out in ((busy, same), (other, elsewhere)):
            t = threading.Thread(target=try_lock, args=(project_id, out))
            t.start()
            t.join()
        assert same == [False], "another driver could write this build's checkpoint mid-redo"
        assert elsewhere == [True], "a redo on one build held up a different build"
    finally:
        release.set()
        redo.join(timeout=30)
    assert not redo.is_alive(), "the redo never finished"
