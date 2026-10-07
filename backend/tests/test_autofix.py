"""The crew fixes its own serious problems, and only asks about small ones (#50).

Since #77 "serious" is a scanner's word, not the model's: the loop runs on what the
security scanners report, with a rule and a line, and "fixed" is proven by a scripted
*rescan* no longer reporting it — never by a scripted audit leaving it out.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.orchestration import autofix, remediation
from tests.conftest import _fake_complete, fenced, scripted_scan, semgrep_hit, stub, through_database_gate

#: What Semgrep reports: a connection string with its password in the backend, and an
#: XSS sink in a frontend page — critical and high, so the crew's to fix.
SECRET = semgrep_hit(
    "aiteam.secrets.credentials-in-connection-string", "backend/app/index.js", 3, cwe="CWE-798",
    message="A connection string with a username and password is written into the code.",
)
XSS = semgrep_hit(
    "typescript.react.security.audit.react-dangerouslysetinnerhtml.react-dangerouslysetinnerhtml",
    "frontend/pages/groups/new.jsx", 12, severity="WARNING", cwe="CWE-79",
    message="Detection of dangerouslySetInnerHTML from non-constant definition.",
)
#: Warden's own review notes (#77): the model's opinion, never the crew's to fix.
POLISH = {
    "title": "Low contrast on the sign-in button",
    "severity": "high",
    "category": "UI/UX",
    "path": "frontend/pages/signin.jsx",
    "line": 4,
    "description": "d",
    "recommendation": "Raise the contrast.",
}
IDOR = {
    "title": "Any user can read any expense",
    "severity": "critical",
    "category": "Authorization",
    "path": "backend/app/index.js",
    "line": 40,
    "description": "GET /expenses/:id never checks the owner.",
    "recommendation": "Check req.user owns the expense.",
}


class Crew:
    """A fake model: Warden reports whatever `audits` says next; everyone else
    answers their schema. Records every prompt each agent was sent."""

    def __init__(self, audits: list[list[dict]]):
        self.audits = audits
        self.asked: dict[str, list[str]] = {}

    def __call__(self, messages, **kwargs):
        system = messages[0].content
        role = system.split(" on an AI", 1)[0].replace("You are the ", "")
        self.asked.setdefault(role, []).append(messages[-1].content)
        resp = _fake_complete(messages, **kwargs)
        if role == "Security Engineer":
            findings = self.audits.pop(0) if len(self.audits) > 1 else self.audits[0]
            payload = json.loads(resp.text)
            payload["findings"] = findings
            return resp.model_copy(update={"text": json.dumps(payload)})
        return resp


def _build(client, monkeypatch, scans, audits=None, mode="unattended"):
    """A build whose scanners report `scans` in turn, and whose Warden says `audits`."""
    scanner = scripted_scan(monkeypatch, scans)
    crew = Crew(audits or [[]])
    stub(monkeypatch, "complete", crew)
    pid = client.post(
        "/api/projects",
        json={"idea": "A group expenses app", "routing_mode": "local_only", "approval_mode": mode},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    return pid, crew, scanner


def _fix_notes(crew: Crew, role: str) -> list[str]:
    """The fix notes a phase was handed, once per attempt: a code phase's plan call.
    Its write calls carry the same note (#81), and are not counted again."""
    return [
        p for p in crew.asked.get(role, []) if remediation.FIX_NOTE_PREFIX in p and "# Write now" not in p
    ]


# ── the loop ─────────────────────────────────────────────────────────────────
def test_serious_findings_are_fixed_and_proven_by_a_clean_rescan(client, monkeypatch):
    """The build from the issue: a leaked URI and an XSS sink, fixed in one rewind —
    and called fixed because the rescan stopped reporting them, nothing else."""
    pid, crew, scanner = _build(client, monkeypatch, [[SECRET, XSS], []])
    project = client.get(f"/api/projects/{pid}").json()

    # Never asked "send back or waive" — it finished.
    assert project["status"] == "completed", project["last_error"]
    # Both owners got their own findings in the same round, by rule and line.
    backend, frontend = _fix_notes(crew, "Backend Engineer"), _fix_notes(crew, "Frontend Engineer")
    assert len(backend) == 1 and "credentials-in-connection-string" in backend[0]
    assert "backend/app/index.js:3" in backend[0] and "dangerouslysetinnerhtml" not in backend[0].split("Trusted")[0]
    assert len(frontend) == 1 and "frontend/pages/groups/new.jsx:12" in frontend[0]
    rounds = project["auto_fix"]["tracks"]["security"]["rounds"]
    assert len(rounds) == 1 and set(rounds[0]["phases"]) == {"backend_engineer", "frontend_engineer"}
    assert {p["tool"] for p in rounds[0]["problems"]} == {"semgrep"}
    # Two scans: the first audit and the rescan of the rebuilt code.
    assert len(scanner.trees) == 2

    security = client.get(f"/api/projects/{pid}/security").json()
    assert {f["status"] for f in security["findings"]} == {"fixed"}
    assert {f["fixed_round"] for f in security["findings"]} == {1}
    assert {f["source"] for f in security["findings"]} == {"tool"}
    secret = next(f for f in security["findings"] if f["cwe"] == "CWE-798")
    assert secret["severity"] == "critical" and secret["path"] == "backend/app/index.js" and secret["line"] == 3
    assert secret["rule_id"] == "aiteam.secrets.credentials-in-connection-string"


def test_a_model_that_forgets_a_finding_cannot_close_it(client, monkeypatch):
    """The scanner still reports it; Warden's re-review says nothing about it at all.
    The rescan is the only judge: the round fixed nothing, and the crew asks for help."""
    pid, crew, _ = _build(client, monkeypatch, [[SECRET]], audits=[[]])
    project = client.get(f"/api/projects/{pid}").json()

    assert project["status"] == "awaiting_approval"
    assert project["gate_kind"] == "needs_help"
    track = project["auto_fix"]["tracks"]["security"]
    assert len(track["rounds"]) == 1 and track["stopped"]["reason"] == "no_progress"
    assert track["rounds"][0]["fixed"] == []
    assert "fixed none" in project["gate_note"]
    finding = client.get(f"/api/projects/{pid}/security").json()["findings"][0]
    assert finding["status"] == "open" and finding["source"] == "tool"
    # And it cannot be approved past.
    assert client.post(f"/api/projects/{pid}/approve").status_code == 409


def test_the_loop_stops_at_its_round_limit(client, monkeypatch):
    """Progress every round, but never all the way: bounded all the same."""
    monkeypatch.setattr(settings, "auto_fix_max_rounds", 3)
    many = [dict(SECRET, line=3 + 10 * i) for i in range(5)]
    # Each rescan reports one fewer; five findings, three rounds.
    pid, crew, _ = _build(client, monkeypatch, [many[i:] for i in range(5)])
    project = client.get(f"/api/projects/{pid}").json()

    track = project["auto_fix"]["tracks"]["security"]
    assert [r["strategy"] for r in track["rounds"]] == ["guided", "with_code", "stronger_model"]
    assert [len(r["fixed"]) for r in track["rounds"]] == [1, 1, 1]
    assert track["stopped"]["reason"] == "limit"
    assert project["gate_kind"] == "needs_help"
    # Round two says the last attempt failed; round three asked for a stronger model.
    notes = _fix_notes(crew, "Backend Engineer")
    assert "previous attempt did not fix" in notes[1]
    assert "scanner runs again" in notes[0]


def test_keep_trying_grants_more_rounds_and_a_waiver_needs_a_kind(client, monkeypatch):
    pid, crew, _ = _build(client, monkeypatch, [[SECRET]])
    key = client.get(f"/api/projects/{pid}/security").json()["findings"][0]["key"]

    r = client.post(f"/api/projects/{pid}/auto-fix/retry", json={"rounds": 1})
    assert r.status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    track = project["auto_fix"]["tracks"]["security"]
    assert len(track["rounds"]) == 2 and project["gate_kind"] == "needs_help"
    # The round Keep trying bought moves on to the next approach, not back to the first.
    assert [r["strategy"] for r in track["rounds"]] == ["guided", "with_code"]
    assert "previous attempt did not fix" in _fix_notes(crew, "Backend Engineer")[-1]

    # A serious finding is not waived on a free-text shrug.
    bad = client.post(f"/api/projects/{pid}/security/{key}/waive", json={"reason": "fine"})
    assert bad.status_code == 422
    ok = client.post(
        f"/api/projects/{pid}/security/{key}/waive",
        json={"reason": "Rotated and moved to the vault", "kind": "mitigated"},
    )
    assert ok.status_code == 200 and ok.json()["waive_kind"] == "mitigated"

    # With nothing left to fix, carrying on finishes the build.
    assert client.post(f"/api/projects/{pid}/auto-fix/retry").status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"


def test_review_notes_ask_the_reviewer_and_never_block(client, monkeypatch):
    """Warden's own findings — polish or a critical IDOR alike — are review notes: no
    automatic round, a Security stop for a person to read, and Approve goes through
    without waiving them."""
    pid, crew, _ = _build(client, monkeypatch, [[]], audits=[[POLISH, IDOR]], mode="checkpoints")
    # Past the plan review.
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()

    assert project["gate_kind"] == "security"
    assert "review note" in project["gate_note"]
    assert not _fix_notes(crew, "Frontend Engineer") and not _fix_notes(crew, "Backend Engineer")
    security = client.get(f"/api/projects/{pid}/security").json()
    assert {f["source"] for f in security["findings"]} == {"model"}
    assert not any(f["serious"] or f["blocks"] for f in security["findings"])
    assert security["unresolved"] == 0
    idor = next(f for f in security["findings"] if f["category"] == "Authorization")
    assert idor["path"] == "backend/app/index.js" and idor["line"] == 40
    # Read, not waived: the build moves on.
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200
    # A note can still be waived exactly as before: a reason, no kind needed.
    waived = client.post(
        f"/api/projects/{pid}/security/{idor['key']}/waive", json={"reason": "Single-user tool"}
    )
    assert waived.status_code == 200


# ── the fix note ─────────────────────────────────────────────────────────────
def test_advice_a_skill_contradicts_is_replaced_by_the_skill():
    finding = remediation.Finding(
        key="k", title="No CSRF protection on forms", severity="high", category="CSRF",
        location="frontend/pages/groups/new.jsx", recommendation="Use Helmet to add CSRF protection.",
        owner_phase="frontend_engineer",
    )
    note = remediation.fix_instruction([finding])
    assert "Use Helmet to add CSRF" not in note
    assert "csrf-csrf" in note and "Trusted procedure" in note


def test_an_unowned_serious_finding_goes_to_the_backend_as_app_wide():
    finding = remediation.Finding(
        key="k", title="Sessions never expire", severity="high", category="Misc",
        location="", recommendation="Expire them.", owner_phase=None,
    )
    assert remediation.route_owner(finding) == "backend_engineer"
    assert "app-wide concern" in remediation.fix_instruction([finding])


@pytest.mark.parametrize(
    "severity,category,serious",
    [
        ("critical", "Secrets", True),
        ("high", "CSRF", True),
        ("medium", "CSRF", False),
        ("low", "XSS", False),
        ("high", "UI/UX", False),
        ("critical", "Accessibility", False),
        # UI words in the *title* do not make a security finding polish.
        ("critical", "Authorization", True),
    ],
)
def test_what_counts_as_serious(severity, category, serious):
    assert remediation.is_serious(severity, category) is serious


def test_no_progress_is_judged_only_since_the_last_keep_trying():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.SECURITY)
    autofix.start_round(t, "guided", ["backend_engineer"], [{"key": "a"}])
    assert autofix.close_round(t, ["a"]) == []
    assert autofix.next_step(t) == autofix.STOP_NO_PROGRESS
    autofix.stop(t, autofix.STOP_NO_PROGRESS, ["a"])
    assert autofix.keep_trying(data, 2) == [autofix.SECURITY]
    assert autofix.next_step(t) == "fix" and t["allowed"] == 3


def test_the_new_columns_migrate_onto_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(
            text("CREATE TABLE security_dispositions (id VARCHAR(32) PRIMARY KEY, status VARCHAR(16))")
        )
        conn.execute(text("INSERT INTO security_dispositions VALUES ('x', 'open')"))
    applied = run_migrations(engine)
    assert "projects.auto_fix" in applied
    assert {"security_dispositions.fixed_round", "security_dispositions.waive_kind"} <= set(applied)
    cols = {c["name"] for c in inspect(engine).get_columns("security_dispositions")}
    assert {"fixed_round", "waive_kind"} <= cols
    assert run_migrations(engine) == []


def test_a_ui_word_in_the_title_does_not_make_a_finding_small():
    assert remediation.is_serious("critical", "Authorization", "Admin API publicly accessible")
    assert remediation.is_serious("high", "Secrets", "API key exposed in the client UI")
    assert remediation.is_serious("high", "Secrets", "Hardcoded secret in app/layout.tsx")
    assert not remediation.is_serious("high", "UI/UX", "Low contrast")


def test_a_fix_round_that_never_generated_is_not_judged(client, monkeypatch):
    """A provider failure inside the fix is not "a round that fixed nothing"."""
    from app.router.base import ProviderError

    scripted_scan(monkeypatch, [[SECRET], []])
    crew = Crew([[]])
    failing = {"on": True}

    def flaky(messages, **kwargs):
        if failing["on"] and remediation.FIX_NOTE_PREFIX in messages[-1].content:
            raise ProviderError("the runtime went away", retryable=False)
        return crew(messages, **kwargs)

    stub(monkeypatch, "complete", flaky)
    pid = client.post(
        "/api/projects",
        json={"idea": "A group expenses app", "routing_mode": "local_only", "approval_mode": "unattended"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "failed"
    assert project["auto_fix"]["tracks"]["security"]["rounds"] == []

    failing["on"] = False
    assert client.post(f"/api/projects/{pid}/resume").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project["gate_note"]
    assert len(project["auto_fix"]["tracks"]["security"]["rounds"]) == 1


def test_a_cleared_track_gets_a_fresh_budget_and_accepts_are_scoped():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    for i in range(3):
        autofix.start_round(t, "guided", ["backend_engineer"], [{"key": f"k{i}"}])
        autofix.close_round(t, [])
    assert autofix.next_step(t) == autofix.STOP_LIMIT
    autofix.settle(t)
    assert autofix.next_step(t) == "fix"

    autofix.stop(t, autofix.STOP_LIMIT, ["a", "b"])
    autofix.accept(data, "accepted_risk", "stub")
    assert autofix.covers(t, ["a"]) and not autofix.covers(t, ["a", "c"])


def test_a_pinned_model_is_tried_first():
    from app.router.router import ModelRouter

    r = ModelRouter.__new__(ModelRouter)
    r._saved_pair = lambda spec: tuple(spec.split(":", 1))
    chain = [("ollama", "small"), ("gemini", "g")]
    assert r._pinned(chain, "ollama:big") == [("ollama", "big"), ("ollama", "small"), ("gemini", "g")]
    assert r._pinned(chain, None) == chain


@pytest.mark.parametrize(
    "category,small",
    [
        ("UI / UX", True),
        ("Accessibility (a11y)", True),
        ("Usability & Accessibility", True),
        ("Design", False),  # OWASP "Insecure Design", shortened
        ("Insecure Design", False),
        ("Content injection", False),
        ("", False),
    ],
)
def test_ui_ux_is_read_from_every_word_of_the_category(category, small):
    assert remediation.is_ui_ux(category) is small


def test_a_compile_fix_that_works_closes_its_round_and_frees_the_budget(client, monkeypatch):
    """Round 1 fixes the code: the round is judged a success, not left open."""
    from tests.conftest import _fake_complete

    calls = {"backend": 0}

    def fake(messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if messages[0].content.startswith("You are the Backend Engineer"):
            if getattr(kwargs.get("options"), "json_schema", None):  # the plan
                payload = json.loads(resp.text)
                payload["files"] = [{"path": "main.py", "purpose": "app"}]
                return resp.model_copy(update={"text": json.dumps(payload)})
            calls["backend"] += 1
            code = "from fastapi import FastAPI\napp = FastAPI()\n"
            if calls["backend"] <= 2:  # the first write and its own repair round
                code = "from fastapi import FastAPI\napp = FastAPI(\n"
            return resp.model_copy(update={"text": fenced({"backend/main.py": code})})
        return resp

    stub(monkeypatch, "complete", fake)
    pid = client.post(
        "/api/projects",
        json={"idea": "An API", "routing_mode": "local_only", "approval_mode": "unattended"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project["gate_note"]
    track = project["auto_fix"]["tracks"]["build:backend_engineer"]
    assert len(track["rounds"]) == 1 and track["rounds"][0]["fixed"]
    assert track["resumed_after"] == 1 and track["allowed"] == 1 + settings.auto_fix_max_rounds


def test_a_standing_security_note_survives_every_compile_round():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.SECURITY)
    record = autofix.start_round(t, "guided", ["backend_engineer"], [{"key": "a"}])
    record["notes"] = {"backend_engineer": remediation.FIX_NOTE_PREFIX + " … MongoDB"}
    from app.orchestration.runner import PipelineRunner

    note = PipelineRunner._standing_fix_note(data, "backend_engineer")
    assert "MongoDB" in note
    assert "MongoDB" in autofix.code_note([{"key": "k", "title": "x", "kind": "build"}], 2, None, note)
