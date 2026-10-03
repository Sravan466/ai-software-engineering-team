"""Stop → Resume during a model call leaves one driver, not two (#41).

Stop can't take back a model call already in flight, and Resume claims the build
straight back. The first driver's call still returns; these tests make sure that,
when it does, it finds the build is no longer its own and writes nothing — whether
its call returns while the resumed run is waiting for it, or after the resumed run
has already finished the phase.
"""
from __future__ import annotations

import threading
import time

from app.api.routes import projects as routes
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project, UsageEvent
from app.orchestration import claim, runner as runner_module
from app.orchestration.runner import _config
from app.orchestration.graph import graph
from tests.conftest import _fake_complete, stub


IDEA = "A tool-lending library for a neighbourhood"


def _create(client) -> str:
    r = client.post(
        "/api/projects",
        json={"idea": IDEA, "routing_mode": "local_only", "require_approval": True},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


#: What the stopped driver's call answers with, so its work is told apart from the
#: resumed run's wherever it lands.
STALE = "written by the stopped run"


class _HeldFirstCall:
    """A model whose first call waits to be released; every later call answers at once.

    Counts only this build's calls. The stub replaces the router for the whole process,
    and a mockup an earlier test queued may still be drawing — through it.
    """

    def __init__(self) -> None:
        self.generating = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, messages, **kwargs):
        if IDEA not in str(messages):
            return _fake_complete(messages, **kwargs)
        with self._lock:
            self.calls += 1
            first = self.calls == 1
        response = _fake_complete(messages, **kwargs)
        if first:
            self.generating.set()
            assert self.release.wait(timeout=30), "the held call was never released"
            response.text = response.text.replace("mock deliverable", STALE)
        return response


def _start(pid: str) -> tuple[threading.Thread, str]:
    """Claim the build and drive it on a thread — what `POST /run` hands its task."""
    db = SessionLocal()
    try:
        token = routes._claim(db, db.get(Project, pid), {"created"})
    finally:
        db.close()
    assert token
    driver = threading.Thread(target=routes._drive, args=(pid, token), name="first-driver")
    driver.start()
    return driver, token


def _resume_on_thread(client, pid: str) -> tuple[threading.Thread, list]:
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(client.post(f"/api/projects/{pid}/resume")), name="resume"
    )
    t.start()
    return t, out


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _row(pid: str):
    with SessionLocal() as db:
        project = db.get(Project, pid)
        rows = db.query(PhaseResult).filter(PhaseResult.project_id == pid, PhaseResult.phase == "product_manager").all()
        usage = (
            db.query(UsageEvent)
            .filter(UsageEvent.project_id == pid, UsageEvent.phase == "product_manager")
            .count()
        )
        return project, rows, usage


def _assert_one_driver_finished_it(client, pid: str, model: _HeldFirstCall) -> None:
    project, rows, usage = _row(pid)
    # The resumed run's decision stands: the first driver did not settle it as stopped.
    assert project.status == "awaiting_approval", project.last_error
    assert project.current_phase == "product_manager"
    # One attempt at the phase, completed once…
    assert len(rows) == 1, [(r.status, r.created_at) for r in rows]
    assert rows[0].status == "pending_approval"
    # …and paid for once: every call except the first driver's, which was discarded.
    assert usage == model.calls - 1, (usage, model.calls)
    # The checkpoint stands where the resumed run left it — after the first phase, once.
    snapshot = graph.get_state(_config(pid))
    assert snapshot.values["last_result"]["phase"] == "product_manager"
    assert snapshot.next and snapshot.next[0] != "product_manager"
    # Holding the resumed run's work, which the row shows — not the stopped run's.
    assert STALE not in str(snapshot.values["prior_outputs"]["product_manager"])
    assert STALE not in str(rows[0].output)
    assert snapshot.values["prior_outputs"]["product_manager"] == rows[0].output
    # And the build is shown as the resumed run left it, through the API too.
    assert client.get(f"/api/projects/{pid}").json()["status"] == "awaiting_approval"


def test_a_stopped_driver_whose_call_returns_after_resume_writes_nothing(client, monkeypatch):
    """The call returns while the resumed run waits for the checkpoint."""
    model = _HeldFirstCall()
    stub(monkeypatch, "complete", model)
    pid = _create(client)

    first, token = _start(pid)
    try:
        assert model.generating.wait(timeout=15), "the first driver never reached its model call"
        assert client.post(f"/api/projects/{pid}/stop", json={}).json()["status"] == "cancelled"

        resume, out = _resume_on_thread(client, pid)
        # The resume has claimed the build: the stopped driver's token is no longer current.
        assert _wait_for(lambda: _row(pid)[0].run_token not in (None, token))
    finally:
        model.release.set()
    first.join(timeout=30)
    resume.join(timeout=30)
    assert not first.is_alive() and not resume.is_alive()
    assert out and out[0].status_code == 200, out and out[0].text

    _assert_one_driver_finished_it(client, pid, model)


def test_a_stopped_driver_whose_call_returns_after_the_new_run_finished_writes_nothing(
    client, monkeypatch
):
    """The call returns only after the resumed run has already finished the phase.

    In one process the checkpoint lock makes the resumed run wait for the stopped
    one's call, so this ordering needs two processes. Each with a lock of its own is
    the same as none being shared, which is what this stands in for.
    """
    model = _HeldFirstCall()
    stub(monkeypatch, "complete", model)
    monkeypatch.setattr(runner_module, "_checkpoint_lock", lambda _pid: threading.RLock())
    pid = _create(client)

    first, token = _start(pid)
    try:
        assert model.generating.wait(timeout=15), "the first driver never reached its model call"
        assert client.post(f"/api/projects/{pid}/stop", json={}).json()["status"] == "cancelled"

        resume, out = _resume_on_thread(client, pid)
        resume.join(timeout=30)
        assert not resume.is_alive(), "the resumed run waited on the stopped one"
        assert out and out[0].status_code == 200, out and out[0].text
        # The new run finished the phase while the old call was still out.
        project, rows, _ = _row(pid)
        assert project.status == "awaiting_approval"
        assert [r.status for r in rows] == ["pending_approval"]
    finally:
        model.release.set()
    first.join(timeout=30)
    assert not first.is_alive()

    _assert_one_driver_finished_it(client, pid, model)


def test_a_superseded_session_cannot_write_and_its_transaction_is_left_untouched(client):
    """The write guard itself: once another claim lands, nothing this session flushes
    reaches the database — not the row it was completing, not the project."""
    pid = _create(client)
    with SessionLocal() as other:
        stale = routes._claim(other, other.get(Project, pid), {"created"})
        assert stale

    db = SessionLocal()
    try:
        project = db.get(Project, pid)
        with claim.holding(db, project, stale):
            project.last_error = "written by a run that no longer owns the build"
            db.commit()  # still current: allowed
            with SessionLocal() as other:
                other.query(Project).filter(Project.id == pid).update({"run_token": claim.new_token()})
                other.commit()
            project.last_error = "a stale write"
            try:
                db.commit()
            except claim.Superseded:
                db.rollback()
            else:
                raise AssertionError("a superseded session's commit went through")
    finally:
        db.close()

    with SessionLocal() as db:
        assert db.get(Project, pid).last_error == "written by a run that no longer owns the build"
