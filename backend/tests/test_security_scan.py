"""The security review backed by scanners (#77): findings have a rule and a line, and
"fixed" means a clean rescan."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

from app.build import scan
from app.build.sandbox import StepResult
from app.core.config import settings
from app.core.constants import ApprovalMode, GateKind, Phase
from app.orchestration import remediation
from app.orchestration.approval import decide_gate
from tests.conftest import (
    SCAN_OWNERS,
    SCAN_TREE,
    _fake_complete,
    scripted_scan,
    semgrep_hit,
    stub,
    through_database_gate,
)

SQLI = semgrep_hit(
    "aiteam.javascript.sql-built-from-request", "backend/app/index.js", 5, cwe="CWE-89",
    message="SQL text is built from the request, so a crafted value can change the query. Pass it as a parameter.",
)


# ── severity, per tool ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,cwe,confidence,expected",
    [
        ("ERROR", "CWE-89: SQL Injection", "HIGH", "critical"),  # injection, reported with confidence
        ("ERROR", "CWE-89: SQL Injection", "LOW", "medium"),  # …but not when the rule itself doubts it
        ("ERROR", "CWE-327: Broken crypto", "MEDIUM", "high"),
        ("WARNING", "CWE-79: XSS", "HIGH", "high"),  # Top 25, high confidence
        ("WARNING", "CWE-79: XSS", "MEDIUM", "medium"),
        ("WARNING", "CWE-1004: Cookie", "HIGH", "medium"),  # not Top 25
        ("INFO", "CWE-352: CSRF", "LOW", "low"),
        ("CRITICAL", None, None, "critical"),  # the newer spellings
        ("HIGH", None, None, "high"),
    ],
)
def test_semgrep_severities_map_to_the_platforms_four(raw, cwe, confidence, expected):
    assert scan.semgrep_severity(raw, cwe, confidence) == expected


@pytest.mark.parametrize(
    "raw,cwe,confidence,expected",
    [
        ("HIGH", 78, "HIGH", "critical"),
        ("HIGH", 78, "MEDIUM", "high"),
        ("MEDIUM", 89, "MEDIUM", "medium"),
        ("LOW", 259, "MEDIUM", "low"),
        ("MEDIUM", 502, "LOW", "low"),
    ],
)
def test_bandit_severities_map_to_the_platforms_four(raw, cwe, confidence, expected):
    assert scan.bandit_severity(raw, cwe, confidence) == expected


# ── reading what the tools printed ───────────────────────────────────────────
_SEMGREP_JSON = {
    "version": "1.139.0",
    "results": [
        {
            "check_id": "tmp.aiteam-rules.aiteam.javascript.sql-built-from-request",
            "path": "/work/backend/app/index.js",
            "start": {"line": 5, "col": 33},
            "end": {"line": 5, "col": 90},
            "extra": {
                "severity": "ERROR",
                "message": "SQL text is built from the request, so a crafted value can change the query. Pass it as a parameter.",
                "metadata": {"cwe": ["CWE-89: Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')"],
                             "confidence": "HIGH"},
                "lines": "requires login",
            },
        },
        {
            "check_id": "cache.rules.python.lang.security.audit.subprocess-shell-true.subprocess-shell-true",
            "path": "/work/backend/main.py",
            "start": {"line": 6},
            "end": {"line": 6},
            "extra": {
                "severity": "ERROR",
                "message": "Found 'subprocess' function 'check_output' with 'shell=True'. This is dangerous.",
                "metadata": {
                    "cwe": ["CWE-78: Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection')"],
                    "confidence": "MEDIUM",
                    "source": "https://semgrep.dev/r/python.lang.security.audit.subprocess-shell-true.subprocess-shell-true",
                },
            },
        },
    ],
    "errors": [{"type": "Syntax error", "message": "Syntax error at line backend/broken.py:3", "path": "/work/backend/broken.py"}],
}
_BANDIT_JSON = {
    "errors": [],
    "results": [
        {
            "test_id": "B602", "test_name": "subprocess_popen_with_shell_equals_true",
            "filename": "/work/backend/main.py", "line_number": 6, "line_range": [6],
            "issue_severity": "HIGH", "issue_confidence": "HIGH", "issue_cwe": {"id": 78, "link": "x"},
            "issue_text": "subprocess call with shell=True identified, security issue.",
            "more_info": "https://bandit.readthedocs.io/en/1.8.6/plugins/b602_subprocess_popen_with_shell_equals_true.html",
        }
    ],
}
_PIP_AUDIT_JSON = {
    "dependencies": [
        {"name": "jinja2", "version": "3.1.2", "vulns": [
            {"id": "PYSEC-2026-1473", "fix_versions": ["3.1.3"], "aliases": ["CVE-2024-22195"], "description": "xmlattr"},
            {"id": "PYSEC-2026-1471", "fix_versions": ["3.1.6"], "aliases": ["CVE-2025-27516"], "description": "sandbox"},
        ]},
        {"name": "fastapi", "version": "0.142.2", "vulns": []},
    ],
    "fixes": [],
}
_TREE = {
    "backend/app/index.js": SCAN_TREE["backend/app/index.js"],
    "backend/main.py": "import subprocess\nfrom flask import Flask, request\napp = Flask(__name__)\n@app.route('/run')\n"
    "def run():\n    return subprocess.check_output(request.args['cmd'], shell=True)\n",
    "backend/requirements.txt": "flask>=3,<4\njinja2==3.1.2\n",
}


def _read(tmp_path, kind: str, report: dict, *args: str) -> str:
    """Run the in-sandbox reader here, on a report shaped like the tool's own."""
    reader = tmp_path / "read.py"
    reader.write_text(scan._PY_READER)
    data = tmp_path / f"{kind}.json"
    data.write_text(json.dumps(report))
    if kind == "pip-audit":
        argv = [kind, args[0], str(data), "0", "0"]
    elif kind == "semgrep":
        argv = [kind, str(data), "0", "cache.rules.,tmp.aiteam-rules.", "1.139.0"]
    else:
        argv = [kind, str(data), "0", "1.8.6"]
    done = subprocess.run([sys.executable, "-I", str(reader), *argv], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _plan(*tools: str) -> scan.ScanPlan:
    return scan.ScanPlan(scan.PYTHON_IMAGE, [], {}, tools)


def test_the_readers_turn_each_tools_json_into_findings_with_a_rule_and_a_line(tmp_path):
    outputs = [
        StepResult("install", "install scanners", 0, 1.0, "scanners already installed"),
        StepResult("scan", "semgrep", 0, 1.0, _read(tmp_path, "semgrep", _SEMGREP_JSON)),
        StepResult("scan", "bandit", 0, 1.0, _read(tmp_path, "bandit", _BANDIT_JSON)),
        StepResult("scan", "pip-audit", 0, 1.0, _read(tmp_path, "pip-audit", _PIP_AUDIT_JSON, "backend")),
    ]
    owners = {"backend/app/index.js": "backend_engineer", "backend/main.py": "backend_engineer"}
    result = scan.judge([(_plan(scan.SEMGREP, scan.BANDIT, scan.PIP_AUDIT), outputs, None)], _TREE, owners)

    assert result.status == "ok" and result.ran(scan.SEMGREP) and result.ran(scan.BANDIT)
    # A file Semgrep couldn't parse is one it says nothing about.
    assert result.tools[scan.SEMGREP]["unscanned"] == ["backend/broken.py"]
    assert not result.covers(scan.SEMGREP, "backend/broken.py") and result.covers(scan.SEMGREP, "backend/main.py")
    # pip-audit ran for the backend, so the backend's manifest is covered.
    assert result.covers(scan.PIP_AUDIT, "backend/requirements.txt")
    sqli = next(f for f in result.findings if f.cwe == "CWE-89")
    # The config path Semgrep prefixes every rule id with is gone.
    assert sqli.rule_id == "aiteam.javascript.sql-built-from-request"
    assert (sqli.path, sqli.line, sqli.severity, sqli.category) == ("backend/app/index.js", 5, "critical", "SQL injection")
    assert sqli.owner_phase == "backend_engineer" and sqli.fingerprint
    # Semgrep and Bandit reporting one shell=True are one finding, with the other kept.
    shell = [f for f in result.findings if f.cwe == "CWE-78"]
    assert len(shell) == 1 and shell[0].tool == scan.SEMGREP
    assert shell[0].also == ["bandit:B602:subprocess_popen_with_shell_equals_true"]
    assert shell[0].url.startswith("https://semgrep.dev/r/")
    # pip-audit: one finding per package, the newest fix in the hint, the manifest line.
    jinja = next(f for f in result.findings if f.tool == scan.PIP_AUDIT)
    assert (jinja.path, jinja.line, jinja.rule_id) == ("backend/requirements.txt", 2, "jinja2")
    assert "3.1.6" in jinja.fix_hint and "PYSEC-2026-1473" in jinja.message
    assert result.summary().startswith("semgrep 1.139.0 · bandit 1.8.6 · pip-audit 2.9.0")


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_the_npm_audit_reader_keeps_fix_available(tmp_path):
    side = f"t{uuid.uuid4().hex[:8]}"
    audit = {"vulnerabilities": {"lodash": {
        "name": "lodash", "severity": "high", "isDirect": True, "range": "<=4.17.20",
        "fixAvailable": {"name": "lodash", "version": "4.18.1", "isSemVerMajor": False},
        "via": [{"title": "Command Injection in lodash", "url": "https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
                 "cwe": ["CWE-77", "CWE-94"], "range": "<4.17.21", "severity": "high"}],
    }}}
    reader = tmp_path / "read.cjs"
    reader.write_text(scan._NODE_READER)
    path = f"/tmp/audit-{side}.json"
    with open(path, "w") as f:
        json.dump(audit, f)
    try:
        done = subprocess.run(["node", str(reader), side, "0", "0", "10.8.2"], capture_output=True, text=True, timeout=30)
    finally:
        os.remove(path)
    assert done.returncode == 0, done.stderr
    files = {f"{side}/package.json": '{\n  "dependencies": {\n    "lodash": "4.17.15"\n  }\n}\n'}
    result = scan.judge(
        [(scan.ScanPlan(scan.NODE_IMAGE, [], {}, (scan.NPM_AUDIT,)),
          [StepResult("scan", f"npm audit ({side})", 0, 1.0, done.stdout)], None)],
        files, {},
    )
    (f,) = result.findings
    assert (f.tool, f.rule_id, f.severity, f.line, f.cwe) == ("npm audit", "lodash", "high", 3, "CWE-77")
    assert "fixAvailable: lodash@4.18.1" in f.fix_hint
    assert f.url == "https://github.com/advisories/GHSA-35jh-r3h4-6jhm"


def test_a_dependency_finding_is_never_the_crews_to_fix():
    """The platform owns the manifests: no agent can bump a version, so a dependency's
    finding is a person's call — and a critical one still holds the build."""
    assert not remediation.finding_is_serious("tool", scan.NPM_AUDIT, "critical", "Vulnerable dependency")
    assert not remediation.finding_is_serious("tool", scan.PIP_AUDIT, "high", "Vulnerable dependency")
    assert remediation.finding_is_serious("tool", scan.SEMGREP, "high", "SQL injection")
    assert not remediation.finding_is_serious("model", None, "critical", "SQL injection")


def test_a_scan_with_no_report_says_why_per_tool():
    outputs = [
        StepResult("install", "install scanners", 1, 3.0, "ERROR: Could not install packages: network unreachable"),
        StepResult("scan", "semgrep", None, 0.0, skipped=True),
    ]
    result = scan.judge([(_plan(scan.SEMGREP), outputs, None)], {}, {})
    assert result.status == "skipped"
    assert "couldn't be" in result.tools[scan.SEMGREP]["reason"]
    assert result.tools[scan.NPM_AUDIT]["status"] == "not_needed"
    assert result.summary().startswith("Scanners not available")


# ── what runs where ──────────────────────────────────────────────────────────
def test_only_the_dependency_audits_get_the_network_and_versions_are_pinned():
    plan = scan.plan_python(_TREE)
    by = {s.label: s for s in plan.steps}
    assert set(by) == {"install scanners", "semgrep", "bandit", "pip-audit"}
    # Semgrep and Bandit read the code with no network at all.
    assert not by["semgrep"].network and not by["bandit"].network
    assert by["install scanners"].network and by["pip-audit"].network
    assert f"semgrep=={scan.SEMGREP_VERSION}" in by["install scanners"].command
    assert f"bandit=={scan.BANDIT_VERSION}" in by["install scanners"].command
    # Requirements are resolved from wheels only, and pip-audit installs nothing.
    assert "--only-binary :all:" in by["install scanners"].command
    assert "--disable-pip" in by["pip-audit"].command and "--metrics=off" in by["semgrep"].command
    # The box holds the code and the manifest, nothing else.
    assert set(plan.files) == set(_TREE)
    node = scan.plan_node({"frontend/package.json": "{}", "frontend/app/page.tsx": "x"})
    (step,) = node.steps
    assert "--package-lock-only --ignore-scripts" in step.command and set(node.files) == {"frontend/package.json"}


def test_the_scan_tree_leaves_out_tests_and_the_platforms_scaffold():
    prior = {
        "backend_engineer": {"files": [
            {"path": "backend/routes/users.js", "content": "x"},
            {"path": "backend/tests/users.test.js", "content": "const password = 'fake-pass-123';"},
        ]},
        "qa_engineer": {"test_files": [{"path": "backend/tests/api.test.js", "framework": "jest", "targets": "", "code": "x"}]},
    }
    files, owners = scan.scan_tree(prior)
    assert "backend/routes/users.js" in files
    assert not any("test" in p for p in files)
    assert owners["backend/routes/users.js"] == "backend_engineer"
    # The platform's manifest is audited, and owned by the side's engineer.
    assert "backend/package.json" in files
    assert scan.owner_of("backend/package.json", owners) == "backend_engineer"


def test_scanners_use_a_cache_no_build_mounts(monkeypatch):
    seen = []

    class Engine:
        kind = "docker"

        def run(self, image, files, steps, limits, on_cancel, on_step):
            seen.append((image, limits.cache))
            return [StepResult(s.name, s.label, 0, 0.1, "") for s in steps]

    from app.build import runner

    monkeypatch.setattr(settings, "security_scan_enabled", True)
    monkeypatch.setattr(runner, "engine", Engine())
    scan.run_scan({**_TREE, "frontend/package.json": "{}"}, {})
    assert ("python:3.12-slim", "scan-tools") in seen
    assert any(image == "node:20-alpine" and cache.startswith("scan-") for image, cache in seen)


# ── the platform's own rules ─────────────────────────────────────────────────
def test_the_secrets_rule_catches_a_password_in_a_connection_string_and_not_a_placeholder():
    import re

    import yaml

    rules = {r["id"]: r for r in yaml.safe_load(scan.OWN_RULES)["rules"]}
    pattern = re.compile(rules["aiteam.secrets.credentials-in-connection-string"]["pattern-regex"])
    assert pattern.search("const MONGO = 'mongodb+srv://admin:hunter2pass@cluster0.example.net/app'")
    assert pattern.search('DATABASE_URL = "postgresql://app:s3cr3t@db:5432/app"')
    assert not pattern.search("const uri = `mongodb+srv://${USER}:${PASS}@cluster0`")
    assert not pattern.search("process.env.MONGODB_URI")
    # The same shape the scrubber takes the password out of before anything is stored.
    from app.core import scrub

    assert "hunter2pass" not in scrub.scrub("mongodb+srv://admin:hunter2pass@cluster0.example.net/app")


# ── what Warden is told ──────────────────────────────────────────────────────
def test_warden_is_handed_known_findings_capped_five_a_file_and_twenty_four_in_all():
    findings = [
        scan.ToolFinding(tool="semgrep", rule_id=f"r{i}", severity="high", path=f"backend/f{i % 7}.js", line=i,
                         category="SQL injection")
        for i in range(60)
    ]
    block = scan.prompt_block(scan.ScanResult(status="ok", findings=findings, tools={"semgrep": {"status": "ran"}}))
    rows = [line for line in block.splitlines() if line.startswith("- [")]
    assert len(rows) == 24
    assert max(sum(f"backend/f{n}.js" in r for r in rows) for n in range(7)) <= 5
    assert "…and 36 more" in block and "don't repeat them" in block


def test_with_no_scanners_warden_is_told_to_look_for_everything():
    block = scan.prompt_block(scan.ScanResult.skipped("Docker isn't running."))
    assert "couldn't run" in block and "injection" in block and "Docker isn't running" in block


def test_warden_still_fits_a_small_window_with_a_huge_scan_block():
    from app.agents import get_agent
    from app.agents.base import AgentContext
    from tests.test_schema_conformance import _profile

    agent = get_agent(Phase.SECURITY_ENGINEER.value)
    profile = _profile(4096)
    ctx = AgentContext(
        idea="A team standup bot " * 50,
        prior_outputs={d: {"files": [{"code": "x" * 400_000}]} for d in agent.depends_on},
        scan_context="# Scanner findings\n" + "- [high] backend/x.js:1: SQL injection (semgrep)\n" * 5000,
    )
    built = sum(len(m.content) for m in agent._build_messages(ctx, profile).messages)
    assert built <= profile.prompt_char_budget


# ── identity, and what "fixed" means ─────────────────────────────────────────
def _project(db, client):
    from app.db.models import Project

    pid = client.post("/api/projects", json={"idea": "An identity check", "routing_mode": "local_only"}).json()["id"]
    return db.get(Project, pid)


def _scan(*findings: scan.ToolFinding, ran=True) -> dict:
    tools = {t: {"status": "ran" if ran else "skipped", "reason": None if ran else "Docker isn't running."}
             for t in scan.TOOLS}
    return scan.ScanResult(status="ok" if ran else "skipped", findings=list(findings), tools=tools).as_dict()


def _hit(line: int, fingerprint: str = "fp-1", rule: str = "aiteam.javascript.sql-built-from-request") -> scan.ToolFinding:
    return scan.ToolFinding(tool="semgrep", rule_id=rule, severity="critical", path="backend/app/index.js", line=line,
                            cwe="CWE-89", category="SQL injection", title="SQL text is built from the request.",
                            fingerprint=fingerprint, owner_phase="backend_engineer")


def test_a_finding_a_fix_moved_down_the_file_is_the_same_finding(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(_hit(5)))
        key = row.finding_key
        row.status = FindingStatus.FIX_REQUESTED.value
        db.commit()
        # The fix added twenty lines of imports above it and changed nothing else.
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(_hit(25)))
        assert row.finding_key == key and row.status == "open" and row.line == 25
        # A different code at that rule's new place, far from the old one: a new finding.
        rows = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(_hit(25), _hit(80, "fp-2")))
        assert len(rows) == 2


def test_nothing_is_fixed_by_a_rescan_whose_tool_did_not_run(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(_hit(5)))
        row.status = FindingStatus.FIX_REQUESTED.value
        db.commit()
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(ran=False))
        assert row.status == "fix_requested"
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan())
        assert row.status == "fixed"


def test_rewording_a_note_at_the_same_line_does_not_mark_it_fixed(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    first = {"title": "Missing ownership check", "severity": "high", "category": "Authorization",
             "path": "routes/orders.js", "line": 18, "description": "d", "recommendation": "r"}
    again = {**first, "title": "IDOR on GET /orders/:id", "category": "Broken access control",
             "path": "backend/routes/orders.js", "line": 19}
    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": [first]}, scan=_scan())
        key = row.finding_key
        row.status = FindingStatus.FIX_REQUESTED.value
        db.commit()
        (row,) = remediation.sync_dispositions(db, project, {"findings": [again]}, scan=_scan())
        assert row.finding_key == key and row.status == "open" and row.source == "model"
        # Gone from the next review entirely: closed by the model's re-review, as before.
        row.status = FindingStatus.FIX_REQUESTED.value
        db.commit()
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan())
        assert row.status == "fixed"


def test_a_note_that_repeats_a_scanner_finding_is_not_tracked_twice(client):
    from app.db.base import SessionLocal

    repeat = {"title": "SQL injection in user lookup", "severity": "critical", "category": "SQL injection",
              "path": "backend/app/index.js", "line": 6, "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        rows = remediation.sync_dispositions(db, project, {"findings": [repeat]}, scan=_scan(_hit(5)))
        assert [r.source for r in rows] == ["tool"]


def test_the_gate_asks_about_a_scanners_small_severe_findings(client):
    from app.core.constants import ApprovalMode as Mode

    class P:
        effective_approval_mode = Mode.CHECKPOINTS.value
        cost_cap_usd = None

    (dep,) = remediation.tool_findings(_scan(scan.ToolFinding(
        tool="npm audit", rule_id="next", severity="critical", path="frontend/package.json",
        line=4, category="Vulnerable dependency", title="next has a known vulnerability")))
    gate = decide_gate(P(), Phase.SECURITY_ENGINEER.value, {"findings": []}, "valid", tool_findings=[dep])
    assert gate.kind == GateKind.SECURITY.value and "scanners raised 1 finding" in gate.note
    # Nothing unsettled for a person: no stop.
    assert decide_gate(P(), Phase.SECURITY_ENGINEER.value, {"findings": []}, "valid", tool_findings=[]) is None


# ── the whole loop, end to end ───────────────────────────────────────────────
def _tool_findings(client, pid: str) -> list[dict]:
    """The scanners' findings (the stub Warden adds a "mock deliverable" note)."""
    return [f for f in client.get(f"/api/projects/{pid}/security").json()["findings"] if f["source"] == "tool"]


def test_sql_injection_is_routed_to_forge_and_fixed_only_by_a_clean_rescan(client, monkeypatch):
    """The issue's acceptance test: `db.query("select … id=" + req.params.id)` is a
    Semgrep finding with a path, a line, a rule id and a CWE, sent to the Backend
    Engineer — and Warden's re-review leaving it out closes nothing. It is fixed in the
    round whose rescan is clean, and that round only."""
    scripted_scan(monkeypatch, [[SQLI], [SQLI], []])
    stub(monkeypatch, "complete", _fake_complete)  # Warden never mentions it at all
    pid = client.post(
        "/api/projects",
        json={"idea": "A user directory", "routing_mode": "local_only", "approval_mode": "unattended"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)

    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "needs_help"  # round 1's rescan still reported it
    (finding,) = _tool_findings(client, pid)
    assert (finding["tool"], finding["rule_id"], finding["cwe"]) == (
        "semgrep", "aiteam.javascript.sql-built-from-request", "CWE-89")
    assert (finding["path"], finding["line"], finding["owner_phase"]) == ("backend/app/index.js", 5, "backend_engineer")
    assert finding["status"] == "open" and finding["serious"] and finding["blocks"]
    (sent,) = project["auto_fix"]["tracks"]["security"]["rounds"][0]["problems"]
    assert sent["phase"] == "backend_engineer" and sent["where"] == "backend/app/index.js:5"

    assert client.post(f"/api/projects/{pid}/auto-fix/retry").status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed", project["gate_note"]
    (finding,) = _tool_findings(client, pid)
    assert finding["status"] == "fixed" and finding["fixed_round"] == 2


def test_a_rescan_that_cannot_run_fixes_nothing_and_says_so(client, monkeypatch):
    scanner = scripted_scan(monkeypatch, [[SQLI]])
    scanner.down_after = 1
    stub(monkeypatch, "complete", _fake_complete)
    pid = client.post(
        "/api/projects",
        json={"idea": "A user directory", "routing_mode": "local_only", "approval_mode": "unattended"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)

    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "needs_help"
    track = project["auto_fix"]["tracks"]["security"]
    assert track["stopped"]["reason"] == "unchecked" and track["rounds"][0]["unjudged_all"]
    assert "scanners couldn't run again" in project["gate_note"]
    (finding,) = _tool_findings(client, pid)
    assert finding["status"] == "fix_requested"
    warden = next(p for p in project["phases"] if p["phase"] == "security_engineer")
    assert warden["scan"]["status"] == "skipped" and "Docker isn't running" in warden["scan"]["reason"]
    # The poll carries the scan's count, never its findings: those are `/security`'s.
    assert "findings" not in warden["scan"] and warden["scan"]["found"] == 0


def test_runner_unavailable_means_scanners_skipped_and_the_card_says_so(client, monkeypatch):
    from app.build import runner

    monkeypatch.setattr(settings, "security_scan_enabled", True)
    monkeypatch.setattr(runner, "engine", None)
    monkeypatch.setattr(settings, "build_runner_url", "")
    monkeypatch.setattr(scan, "scan_tree", lambda prior, charter=None: (dict(SCAN_TREE), dict(SCAN_OWNERS)))
    from app.build import sandbox

    monkeypatch.setattr(sandbox, "available", lambda refresh=False: (False, "Docker isn't installed on the computer running the backend.", None))
    asked = []

    def crew(messages, **kwargs):
        asked.append(messages[-1].content)
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", crew)
    pid = client.post(
        "/api/projects",
        json={"idea": "A user directory", "routing_mode": "local_only", "approval_mode": "unattended"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed"
    warden = next(p for p in project["phases"] if p["phase"] == "security_engineer")
    assert warden["scan"]["status"] == "skipped"
    assert warden["scan"]["summary"].startswith("Scanners not available")
    assert all(t["status"] == "skipped" for t in warden["scan"]["tools"].values())
    # Warden was told, and asked to look for everything.
    assert any("The scanners couldn't run on this build" in p for p in asked)
    security = client.get(f"/api/projects/{pid}/security").json()
    assert security["scan"]["status"] == "skipped"
    assert not any(f["source"] == "tool" for f in security["findings"])


def test_a_vulnerable_dependency_stops_the_review_with_fix_available(client, monkeypatch):
    lodash = {"package": "lodash", "severity": "high", "direct": True, "range": "<=4.17.20",
              "fix": {"name": "lodash", "version": "4.18.1", "isSemVerMajor": False},
              "advisories": [{"title": "Prototype Pollution in lodash", "url": "https://github.com/advisories/GHSA-p6mc-m468-83gw",
                              "cwe": ["CWE-1321"], "range": "<4.17.19", "severity": "high"}], "through": []}
    scripted_scan(monkeypatch, [[]], npm=[lodash])
    stub(monkeypatch, "complete", _fake_complete)
    pid = client.post(
        "/api/projects",
        json={"idea": "A user directory", "routing_mode": "local_only", "approval_mode": "checkpoints"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200  # the plan review
    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "security" and "Vulnerable dependency" in project["gate_note"]
    (dep,) = _tool_findings(client, pid)
    assert (dep["tool"], dep["rule_id"], dep["path"], dep["line"]) == ("npm audit", "lodash", "frontend/package.json", 3)
    assert "fixAvailable: lodash@4.18.1" in dep["recommendation"]
    assert dep["blocks"] and not dep["serious"]
    # No agent can change a version the platform sets: sending it back is refused.
    refused = client.post(f"/api/projects/{pid}/security/{dep['key']}/fix")
    assert refused.status_code == 400 and "platform sets package versions" in refused.json()["detail"]
    # Blocks the way any severe finding a person decides does: waive it, or it stays.
    assert client.post(f"/api/projects/{pid}/approve").status_code == 409
    waived = client.post(f"/api/projects/{pid}/security/{dep['key']}/waive", json={"reason": "No user input reaches lodash"})
    assert waived.status_code == 200
    # Waived holds: the gate reads what is unsettled, not what the scanner reported.
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration.runner import runner

    with SessionLocal() as db:
        project = db.get(Project, pid)
        warden = runner.latest_row(db, project, Phase.SECURITY_ENGINEER.value)
        assert runner.gate_for(project, warden) is None
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200


# ── upgrades ─────────────────────────────────────────────────────────────────
def test_existing_dispositions_still_load_after_the_columns_are_added(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE phase_results (id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE security_dispositions (id VARCHAR(32) PRIMARY KEY, finding_key VARCHAR(64), "
            "status VARCHAR(16), severity VARCHAR(16), title TEXT, category VARCHAR(128))"
        ))
        conn.execute(text("INSERT INTO security_dispositions VALUES ('x', 'k', 'open', 'critical', 'SQLi', 'SQL injection')"))
    applied = set(run_migrations(engine))
    assert {f"security_dispositions.{c}" for c in ("source", "tool", "rule_id", "cwe", "path", "line", "fingerprint")} <= applied
    assert "phase_results.scan" in applied
    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, source, tool FROM security_dispositions")).one()
    assert tuple(row) == ("open", None, None)
    cols = {c["name"] for c in inspect(engine).get_columns("security_dispositions")}
    assert {"source", "rule_url", "line"} <= cols
    assert run_migrations(engine) == []


def test_a_legacy_row_is_a_review_note():
    class Row:
        source = None
        tool = None
        severity = "critical"
        category = "Secrets"
        title = "Hardcoded key"

    assert remediation.row_source(Row()) == "model"
    assert not remediation.row_blocks(Row()) and not remediation.row_is_serious(Row())


# ── Settings ─────────────────────────────────────────────────────────────────
def test_settings_say_when_warden_reviews_on_the_builders_model():
    from app.router.router import ModelRouter

    rows = [{"role": "backend_engineer", "assigned": None}, {"role": "frontend_engineer", "assigned": None},
            {"role": "security_engineer", "assigned": None}]
    assert ModelRouter._auditor_shares_builders(rows, "ollama:qwen2.5:7b")
    rows[2]["assigned"] = "ollama:qwen3:1.7b"
    assert not ModelRouter._auditor_shares_builders(rows, "ollama:qwen2.5:7b")
    rows[2]["assigned"] = None
    rows[0]["assigned"] = "ollama:qwen3:1.7b"
    assert not ModelRouter._auditor_shares_builders(rows, "ollama:qwen2.5:7b")
    assert not ModelRouter._auditor_shares_builders(rows, None)


# ── the real tools, in Docker (opt-in) ───────────────────────────────────────
_DOCKER = pytest.mark.skipif(
    os.environ.get("SECURITY_SCAN_DOCKER_TESTS") != "1",
    reason="runs the real scanners in Docker: set SECURITY_SCAN_DOCKER_TESTS=1",
)


@_DOCKER
def test_the_real_scanners_find_the_injection_the_secret_and_the_dependency(monkeypatch):
    from app.build import runner

    monkeypatch.setattr(settings, "security_scan_enabled", True)
    monkeypatch.setattr(runner, "engine", None)
    files = {
        **_TREE,
        "frontend/package.json": '{\n  "name": "web",\n  "dependencies": {\n    "lodash": "4.17.15"\n  }\n}\n',
    }
    result = scan.run_scan(files, {"backend/app/index.js": "backend_engineer", "backend/main.py": "backend_engineer"})
    assert result.ran(scan.SEMGREP) and result.ran(scan.BANDIT) and result.ran(scan.NPM_AUDIT), result.tools
    by = {(f.rule_id, f.path): f for f in result.findings}
    sqli = by[("aiteam.javascript.sql-built-from-request", "backend/app/index.js")]
    assert (sqli.line, sqli.cwe, sqli.owner_phase) == (5, "CWE-89", "backend_engineer")
    secret = next(f for f in result.findings if f.cwe == "CWE-798" and f.path == "backend/app/index.js")
    assert secret.line == 3 and "hunter2pass" not in json.dumps(secret.as_dict())
    lodash = next(f for f in result.findings if f.tool == scan.NPM_AUDIT and f.rule_id == "lodash")
    assert "fixAvailable" in lodash.fix_hint
    # Three Semgrep rules and Bandit's B602 on the one `shell=True` line: one finding.
    shell = [f for f in result.findings if f.path == "backend/main.py" and f.line == 6]
    assert len(shell) == 1 and len(shell[0].also) >= 2, [f.as_dict() for f in shell]


# ── what the review found (#77, PR #88) ──────────────────────────────────────
def test_a_side_that_wont_resolve_never_stops_semgrep_or_bandit():
    plan = scan.plan_python(_TREE)
    install = plan.steps[0].command
    # The tools' own install is the only thing allowed to fail the step.
    assert install.index("set +e") < install.index("pip install --dry-run")
    assert install.index("set -e") < install.index(f"semgrep=={scan.SEMGREP_VERSION}") < install.index("set +e")


def test_a_rescan_only_speaks_for_what_it_covered():
    result = scan.ScanResult(status="ok", tools={
        "semgrep": {"status": "ran", "unscanned": ["backend/broken.py"]},
        "bandit": {"status": "ran", "truncated": True},
        "npm audit": {"status": "ran", "sides": {"backend": "ran", "frontend": "failed"}},
        "pip-audit": {"status": "failed"},
    })
    assert result.covers("semgrep", "backend/app.py")
    assert not result.covers("semgrep", "backend/broken.py")  # it couldn't parse that file
    assert not result.covers("bandit", "backend/app.py")  # its report was cut short
    assert result.covers("npm audit", "backend/package.json")
    assert not result.covers("npm audit", "frontend/package.json")  # the other side failed
    assert not result.covers("pip-audit", "backend/requirements.txt")


def test_a_failed_side_or_a_cut_report_resolves_nothing(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    dep = scan.ToolFinding(tool="npm audit", rule_id="lodash", severity="high", path="frontend/package.json",
                           line=3, category="Vulnerable dependency", title="lodash has a known vulnerability")
    with SessionLocal() as db:
        project = _project(db, client)
        rows = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(dep, _hit(5)))
        assert len(rows) == 2
        rescan = _scan()
        rescan["tools"]["npm audit"]["sides"] = {"backend": "ran", "frontend": "failed"}
        rescan["tools"]["semgrep"]["truncated"] = True
        rows = remediation.sync_dispositions(db, project, {"findings": []}, scan=rescan)
        assert {r.status for r in rows} == {FindingStatus.OPEN.value}


def test_two_new_findings_on_identical_lines_are_two_findings(client):
    from app.db.base import SessionLocal

    with SessionLocal() as db:
        project = _project(db, client)
        rows = remediation.sync_dispositions(
            db, project, {"findings": []}, scan=_scan(_hit(10, "same-code"), _hit(40, "same-code"))
        )
        assert sorted(r.line for r in rows) == [10, 40]


def test_a_dotfile_keeps_its_dot():
    assert remediation._path_and_line(".env")[0] == ".env"
    assert remediation._path_and_line("./backend/.env:3") == ("backend/.env", 3)
    assert remediation._same_file(".env", "backend/.env")
    assert not remediation._same_file("env", "backend/.env")


def test_the_scan_shares_one_budget_across_its_sandboxes(monkeypatch):
    from app.build import runner

    budgets = []

    class Slow:
        kind = "docker"

        def run(self, image, files, steps, limits, on_cancel, on_step):
            budgets.append(limits.seconds)
            return [StepResult(s.name, s.label, 0, 0.1, "") for s in steps]

    clock = iter([0.0, 0.0, 300.0, 300.0, 300.0, 300.0])
    monkeypatch.setattr(scan.time, "monotonic", lambda: next(clock, 300.0))
    monkeypatch.setattr(settings, "security_scan_enabled", True)
    monkeypatch.setattr(settings, "security_scan_timeout_seconds", 420)
    monkeypatch.setattr(runner, "engine", Slow())
    scan.run_scan({**_TREE, "frontend/package.json": "{}"}, {})
    assert budgets[0] == 420 and budgets[1] <= 120
