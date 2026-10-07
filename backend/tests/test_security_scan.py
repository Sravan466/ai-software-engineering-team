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
        argv = [kind, str(data), "0", "cache.rules.,tmp.aiteam-rules.", "1.139.0", "default,owasp-top-ten,secrets,"]
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
        StepResult("scan", "pip-audit (backend)", 0, 1.0, _read(tmp_path, "pip-audit", _PIP_AUDIT_JSON, "backend")),
    ]
    owners = {"backend/app/index.js": "backend_engineer", "backend/main.py": "backend_engineer"}
    result = scan.judge([(_plan(scan.SEMGREP, scan.BANDIT, scan.PIP_AUDIT), outputs, None)], _TREE, owners)

    assert result.status == "ok" and result.ran(scan.SEMGREP) and result.ran(scan.BANDIT)
    # A file Semgrep couldn't parse is one it says nothing about.
    assert result.tools[scan.SEMGREP]["unscanned"] == ["backend/broken.py"]
    assert not result.covers(scan.SEMGREP, "backend/broken.py") and result.covers(scan.SEMGREP, "backend/main.py")
    assert result.tools[scan.SEMGREP]["missing_packs"] == []
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
    assert set(by) == {"install scanners", "semgrep", "bandit", "pip-audit (backend)"}
    # Semgrep and Bandit read the code with no network at all.
    assert not by["semgrep"].network and not by["bandit"].network
    assert by["install scanners"].network and by["pip-audit (backend)"].network
    assert f"semgrep=={scan.SEMGREP_VERSION}" in by["install scanners"].command
    assert f"bandit=={scan.BANDIT_VERSION}" in by["install scanners"].command
    # Requirements are resolved from wheels only, and pip-audit installs nothing.
    assert "--only-binary :all:" in by["install scanners"].command
    assert "--disable-pip" in by["pip-audit (backend)"].command and "--metrics=off" in by["semgrep"].command
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
    files, owners, tree = scan.scan_tree(prior)
    assert "backend/routes/users.js" in files
    assert not any("test" in p for p in files)
    # Left out of the scan, still in the tree: a finding there is unread, not gone.
    assert "backend/tests/users.test.js" in tree
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
    monkeypatch.setattr(settings, "build_run_enabled", True)
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
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(runner, "engine", None)
    monkeypatch.setattr(settings, "build_runner_url", "")
    monkeypatch.setattr(scan, "scan_tree", lambda prior, charter=None, **kw: (dict(SCAN_TREE), dict(SCAN_OWNERS), sorted(SCAN_TREE)))
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
    # One builder on its own model, the other on the default Warden runs on: Warden
    # reviews that one's code on the model that wrote it.
    rows[0]["assigned"] = "ollama:qwen3:1.7b"
    assert ModelRouter._auditor_shares_builders(rows, "ollama:qwen2.5:7b")
    rows[1]["assigned"] = "ollama:llama3.2:3b"
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
    monkeypatch.setattr(settings, "build_run_enabled", True)
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
    monkeypatch.setattr(settings, "build_run_enabled", True)
    monkeypatch.setattr(settings, "security_scan_timeout_seconds", 420)
    monkeypatch.setattr(runner, "engine", Slow())
    scan.run_scan({**_TREE, "frontend/package.json": "{}"}, {})
    assert budgets[0] == 420 and budgets[1] <= 120


# ── what the re-review found (#77, PR #88) ───────────────────────────────────
def test_a_note_beside_a_scanner_finding_about_something_else_is_kept(client):
    from app.db.base import SessionLocal

    idor = {"title": "Any visitor can read any user's record", "severity": "critical", "category": "Authorization",
            "path": "backend/app/index.js", "line": 4, "description": "d", "recommendation": "r"}
    sqli = {"title": "SQL injection in the lookup", "severity": "critical", "category": "SQL injection",
            "path": "backend/app/index.js", "line": 6, "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        rows = remediation.sync_dispositions(db, project, {"findings": [idor, sqli]}, scan=_scan(_hit(5)))
        # The SQL note repeats the scanner's finding; the IDOR one is about something else.
        assert sorted((r.source, r.category) for r in rows) == [("model", "Authorization"), ("tool", "SQL injection")]


def test_a_rescan_led_by_another_rule_is_the_same_finding_and_keeps_its_waiver(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    lead = _hit(40, rule="a.sql")
    lead.also = ["semgrep:b.sql"]
    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(lead))
        assert set(row.rules) == {"semgrep:a.sql", "semgrep:b.sql"}
        row.status = FindingStatus.WAIVED.value
        db.commit()
        # The rebuild changed the code so only the second rule still matches it.
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(_hit(40, "fp-2", rule="b.sql")))
        assert row.status == "waived"


def test_a_waived_note_does_not_stop_the_next_audit(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    note = {"title": "IDOR", "severity": "high", "category": "Authorization", "path": "routes.js", "line": 3,
            "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": [note]}, scan=_scan())
        assert remediation.open_notes(db, project) == [row]
        row.status = FindingStatus.WAIVED.value
        db.commit()
        remediation.sync_dispositions(db, project, {"findings": [note]}, scan=_scan())
        assert remediation.open_notes(db, project) == []

    class P:
        effective_approval_mode = ApprovalMode.CHECKPOINTS.value
        cost_cap_usd = None

    # The report still says it; the dispositions say it's settled. The dispositions win.
    assert decide_gate(P(), Phase.SECURITY_ENGINEER.value, {"findings": [note]}, "valid",
                       tool_findings=[], review_notes=[]) is None


def test_with_only_the_dependency_audits_run_warden_still_checks_the_code():
    result = scan.ScanResult(status="ok", tools={"npm audit": {"status": "ran"}, "semgrep": {"status": "failed"}})
    block = scan.prompt_block(result)
    assert "npm audit ran" in block and "code scanners couldn't run" in block and "injection" in block
    ran = scan.ScanResult(status="ok", tools={"semgrep": {"status": "ran"}})
    assert "code scanners" not in scan.prompt_block(ran)


def test_npm_audit_audits_what_ships_and_installs_take_a_lock():
    (step,) = scan.plan_node({"frontend/package.json": "{}"}).steps
    assert "npm audit --omit=dev --json" in step.command
    install = scan.plan_python(_TREE).steps[0].command
    assert 'mkdir "$T.lock"' in install and 'rm -rf "$T" "$T".part-*' in install
    assert install.index('mkdir "$T.lock"') < install.index("pip install --no-input")


def test_one_helper_marks_a_round_nobody_could_judge():
    from app.orchestration import autofix

    record = {"problems": [{"key": "a"}, {"key": "b"}]}
    autofix.mark_unjudged(record, ["a"], "Docker isn't running.")
    assert record["unjudged"] == "Docker isn't running." and record["unjudged_all"] is False
    autofix.mark_unjudged(record, ["a", "b"], "Docker isn't running.")
    assert record["unjudged_all"] is True


# ── what the third review found (#77, PR #88) ────────────────────────────────
def test_a_note_keeps_its_own_row_and_a_neighbours_waiver_stays_with_the_neighbour(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    rate = {"title": "Missing rate limit", "severity": "medium", "category": "Business logic",
            "path": "routes/users.js", "line": 10, "description": "d", "recommendation": "r"}
    idor = {"title": "IDOR on GET /users/:id", "severity": "critical", "category": "Authorization",
            "path": "routes/users.js", "line": 12, "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        rows = remediation.sync_dispositions(db, project, {"findings": [rate, idor]}, scan=_scan())
        by = {r.category: r for r in rows}
        by["Business logic"].status = FindingStatus.WAIVED.value
        db.commit()
        # Only the IDOR is reported this time: it keeps its own row, open; the waived
        # rate-limit note is the one that went.
        rows = remediation.sync_dispositions(db, project, {"findings": [idor]}, scan=_scan())
        by = {r.category: r for r in rows}
        assert by["Authorization"].status == "open" and by["Authorization"].severity == "critical"
        assert by["Business logic"].status == "waived"
        assert remediation.open_notes(db, project) == [by["Authorization"]]


def test_a_dependency_is_its_package_wherever_the_manifest_puts_it(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    def dep(line):
        return scan.ToolFinding(tool="npm audit", rule_id="lodash", severity="high", path="backend/package.json",
                                line=line, category="Vulnerable dependency", title="lodash has a known vulnerability")

    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(dep(14)))
        row.status = FindingStatus.WAIVED.value
        db.commit()
        (row,) = remediation.sync_dispositions(db, project, {"findings": []}, scan=_scan(dep(18)))
        assert row.status == "waived" and row.line == 18


def test_a_fatal_semgrep_run_is_not_a_clean_one(tmp_path):
    """Exit 7 (a rule it can't parse) still writes a report — with no results."""
    reader = tmp_path / "read.py"
    reader.write_text(scan._PY_READER)
    data = tmp_path / "semgrep.json"
    data.write_text(json.dumps({"version": "1.139.0", "results": [], "errors": [{"message": "Invalid rule schema"}]}))
    done = subprocess.run([sys.executable, "-I", str(reader), "semgrep", str(data), "7", "x.", "1.139.0", ""],
                          capture_output=True, text=True, timeout=30)
    result = scan.judge([(_plan(scan.SEMGREP), [StepResult("scan", "semgrep", 0, 1.0, done.stdout)], None)], {}, {})
    assert result.tools[scan.SEMGREP]["status"] == "failed" and "exit 7" in result.tools[scan.SEMGREP]["reason"]
    assert not result.covers(scan.SEMGREP, "backend/main.py", "aiteam.javascript.sql-built-from-request")


def test_a_rescan_without_a_registry_pack_only_speaks_for_the_platforms_rules():
    result = scan.ScanResult(status="ok", tools={"semgrep": {"status": "ran", "missing_packs": ["default"]}})
    assert result.covers("semgrep", "backend/a.js", "aiteam.secrets.credentials-in-connection-string")
    assert not result.covers("semgrep", "backend/a.js", "javascript.express.security.injection.tainted-sql-string")


def test_scans_honour_the_sandbox_switch(monkeypatch):
    monkeypatch.setattr(settings, "security_scan_enabled", True)
    monkeypatch.setattr(settings, "build_run_enabled", False)
    result = scan.run_scan(dict(_TREE), {})
    assert result.status == "skipped" and "BUILD_RUN_ENABLED=false" in result.reason


def test_the_same_tree_is_scanned_once_within_the_hour(monkeypatch):
    calls = []

    def fake_run(files, owners, tree=None):
        calls.append(1)
        tools = {t: {"status": "ran"} for t in scan.TOOLS}
        return scan.ScanResult(status="ok", tools=tools)

    monkeypatch.setattr(scan, "run_scan", fake_run)
    monkeypatch.setattr(scan, "scan_tree", lambda prior, charter=None, **kw: ({"backend/a.py": "x"}, {}, ["backend/a.py"]))
    monkeypatch.setattr(scan, "_recent", {})
    first = scan.scan_build({})
    again = scan.scan_build({})
    assert len(calls) == 1 and not first.reused and again.reused
    # A different tree is scanned for real.
    monkeypatch.setattr(scan, "scan_tree", lambda prior, charter=None, **kw: ({"backend/a.py": "y"}, {}, ["backend/a.py"]))
    scan.scan_build({})
    assert len(calls) == 2


# ── what the fourth review found (#77, PR #88) ───────────────────────────────
def test_a_waiver_never_passes_to_a_more_severe_note_beside_it(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    verbose = {"title": "Verbose error message", "severity": "low", "category": "Information exposure",
               "path": "routes/orders.js", "line": 40, "description": "d", "recommendation": "r"}
    idor = {"title": "IDOR on GET /orders/:id", "severity": "critical", "category": "Authorization",
            "path": "routes/orders.js", "line": 42, "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": [verbose]}, scan=_scan())
        row.status = FindingStatus.WAIVED.value
        db.commit()
        rows = remediation.sync_dispositions(db, project, {"findings": [idor]}, scan=_scan())
        by = {r.title: r for r in rows}
        assert by["IDOR on GET /orders/:id"].status == "open"
        assert remediation.open_notes(db, project) == [by["IDOR on GET /orders/:id"]]


def test_a_repeat_note_keeps_its_record_while_the_scanner_still_reports_the_problem(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal

    note = {"title": "SQL injection in the user lookup", "severity": "critical", "category": "SQL injection",
            "path": "backend/app/index.js", "line": 5, "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        # Tracked before a scanner reported it (a build from before #77), then sent back.
        (row,) = remediation.sync_dispositions(db, project, {"findings": [note]}, scan=_scan())
        row.status = FindingStatus.FIX_REQUESTED.value
        db.commit()
        rows = remediation.sync_dispositions(db, project, {"findings": [note]}, scan=_scan(_hit(5)))
        note_row = next(r for r in rows if r.source == "model")
        # Not "fixed" (the scanner still reports it), and not reopened beside the
        # scanner's finding: superseded by it.
        assert note_row.status == "gone"
        assert note_row.rule_id == "semgrep:aiteam.javascript.sql-built-from-request"
        assert remediation.open_notes(db, project) == []
        assert len(rows) == 2


def test_a_pip_audit_report_per_side_and_reuse_needs_every_side():
    plan = scan.plan_python({**_TREE, "frontend/requirements.txt": "flask\n"})
    assert [s.label for s in plan.steps if s.label.startswith("pip-audit")] == ["pip-audit (backend)", "pip-audit (frontend)"]
    tools = {t: {"status": "ran"} for t in scan.TOOLS}
    tools["npm audit"]["sides"] = {"backend": "ran", "frontend": "failed"}
    assert not scan._complete(scan.ScanResult(status="ok", tools=tools))
    tools["npm audit"]["sides"]["frontend"] = "ran"
    assert scan._complete(scan.ScanResult(status="ok", tools=tools))


def test_the_install_lock_goes_stale_when_an_install_could_no_longer_be_running(monkeypatch):
    monkeypatch.setattr(settings, "security_scan_timeout_seconds", 120)
    install = scan.plan_python(_TREE).steps[0]
    assert install.timeout == 120 and "-mmin +2" in install.command


def test_a_rescan_that_reports_a_finding_again_judges_it(client, monkeypatch):
    """The rescan ran but its report was cut short — and still reported the finding:
    that is a verdict (not fixed), not "couldn't check"."""
    from app.build import runner as _runner

    scanner = scripted_scan(monkeypatch, [[SQLI]])
    real_judge = scan.judge

    def cut(*a, **k):
        result = real_judge(*a, **k)
        result.tools["semgrep"]["truncated"] = True
        return result

    monkeypatch.setattr(scan, "judge", cut)
    stub(monkeypatch, "complete", _fake_complete)
    pid = client.post(
        "/api/projects",
        json={"idea": "A user directory", "routing_mode": "local_only", "approval_mode": "unattended"},
    ).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    track = client.get(f"/api/projects/{pid}").json()["auto_fix"]["tracks"]["security"]
    assert track["stopped"]["reason"] == "no_progress"
    assert not track["rounds"][0].get("unjudged_all")


def test_a_legacy_round_of_model_findings_is_not_counted_as_fixed(client):
    from app.core.constants import FindingStatus
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.orchestration import autofix
    from app.orchestration.runner import runner

    legacy = {"title": "Hardcoded key", "severity": "critical", "category": "Secrets", "path": "app.js", "line": 3,
              "description": "d", "recommendation": "r"}
    with SessionLocal() as db:
        project = _project(db, client)
        (row,) = remediation.sync_dispositions(db, project, {"findings": [legacy]}, scan=_scan())
        row.source = None  # written before #77
        row.status = FindingStatus.FIX_REQUESTED.value
        data = autofix.load(project)
        t = autofix.track(data, autofix.SECURITY)
        autofix.start_round(t, "guided", ["backend_engineer"], [{"key": row.finding_key, "kind": "security"}])
        autofix.save(project, data)
        db.commit()
        # The re-audit after the upgrade still reports it.
        remediation.sync_dispositions(db, project, {"findings": [legacy]}, scan=_scan())
        unsettled = runner._unsettled(db, project, [row.finding_key])
        assert unsettled == {row.finding_key}
        fixed = autofix.close_round(autofix.track(autofix.load(db.get(Project, project.id)), autofix.SECURITY), list(unsettled))
        assert fixed == []


# ── what the final review found (#77, PR #88) ────────────────────────────────
def test_approving_a_security_stop_reads_its_notes(client, monkeypatch):
    from app.db.base import SessionLocal
    from app.db.models import Project

    idor = {"title": "IDOR", "severity": "critical", "category": "Authorization", "path": "backend/app/index.js",
            "line": 40, "description": "d", "recommendation": "r"}
    from tests.test_autofix import _build

    pid, crew, _ = _build(client, monkeypatch, [[]], audits=[[idor]], mode="checkpoints")
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200  # the plan review
    assert client.get(f"/api/projects/{pid}").json()["gate_kind"] == "security"
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200  # read and accepted
    note = next(f for f in client.get(f"/api/projects/{pid}/security").json()["findings"] if f["category"] == "Authorization")
    assert note["read"] is True and note["status"] == "open"
    with SessionLocal() as db:
        assert remediation.open_notes(db, db.get(Project, pid)) == []


def test_a_file_the_rescan_wasnt_given_gets_no_verdict_and_a_deleted_one_is_gone():
    result = scan.ScanResult(status="ok", tools={"semgrep": {"status": "ran"}},
                             scanned=["backend/app.js"], tree=["backend/app.js", "backend/tests/app.test.js"])
    assert result.covers("semgrep", "backend/app.js", "aiteam.x")
    assert not result.covers("semgrep", "backend/tests/app.test.js", "aiteam.x")  # still there, not scanned
    assert result.covers("semgrep", "backend/old.js", "aiteam.x")  # gone from the tree


def test_the_sql_rule_only_matches_sql_and_a_fatal_run_falls_back_to_own_rules():
    import yaml

    rule = yaml.safe_load(scan.OWN_RULES)["rules"][0]
    assert rule["id"] == "aiteam.javascript.sql-built-from-request"
    text = json.dumps(rule)
    assert '\\"$SQL\\" +' in text and "select|insert|update|delete" in text and "^(params|query|body|headers|cookies)$" in text
    assert "oneOrNone" in text  # template SQL only through methods that run SQL
    semgrep = next(s for s in scan.plan_python(_TREE).steps if s.label == "semgrep").command
    assert "P=''; fi" in semgrep and semgrep.count("semgrep scan") == 2


def test_a_planned_side_with_no_report_is_not_a_side_that_ran():
    plan = scan.ScanPlan(scan.NODE_IMAGE, [], {}, (scan.NPM_AUDIT,))
    ran = scan.MARK + json.dumps({"tool": "npm audit", "side": "backend", "ran": True, "findings": []})
    result = scan.judge([(plan, [StepResult("scan", "npm audit (backend)", 0, 1.0, ran),
                                 StepResult("scan", "npm audit (frontend)", None, 180.0, timed_out=True)], None)], {}, {})
    assert result.tools[scan.NPM_AUDIT]["sides"] == {"backend": "ran", "frontend": "skipped"}
    assert not result.covers(scan.NPM_AUDIT, "frontend/package.json") and not scan._complete(result)


def test_paths_below_a_renamed_root_are_the_same_file():
    assert remediation._same_file("server/app/db.py", "backend/app/db.py")
    assert not remediation._same_file("a/x.js", "b/x.js")
    # Two sides of the tree are two files, however alike below their roots.
    assert not remediation._same_file("backend/src/index.js", "frontend/src/index.js")


# ── the re-review of the final round's fixes (#77, PR #88) ───────────────────
def test_a_note_is_only_read_where_and_as_severe_as_it_was_read(client, monkeypatch):
    from app.db.base import SessionLocal
    from app.db.models import Project
    from tests.test_autofix import _build

    sqli = {"title": "SQL Injection", "severity": "high", "category": "Injection", "path": "routes/users.js",
            "line": 9, "description": "d", "recommendation": "r"}
    pid, crew, _ = _build(client, monkeypatch, [[]], audits=[[sqli], [{**sqli, "path": "routes/payments.js"}]],
                          mode="checkpoints")
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200  # the plan review
    assert client.post(f"/api/projects/{pid}/approve").status_code == 200  # read at the Security stop
    with SessionLocal() as db:
        project = db.get(Project, pid)
        from app.db.models import SecurityDisposition

        row = db.query(SecurityDisposition).filter_by(project_id=pid, category="Injection").one()
        read = remediation.notes_read(project)
        assert remediation.is_read(read, row)
        # The same note, somewhere else: not what was read.
        row.path = "routes/payments.js"
        assert not remediation.is_read(read, row)
        row.path, row.severity = "routes/users.js", "critical"
        assert not remediation.is_read(read, row)


def test_a_multi_place_or_more_severe_note_is_not_a_repeat():
    note = remediation.Finding(key="k", title="SQL injection", severity="critical", category="SQL injection",
                               location="backend/app/index.js:5", recommendation="", owner_phase=None,
                               path="backend/app/index.js", line=5)
    medium = remediation.Finding(key="t", title="SQL text is built", severity="medium", category="SQL injection",
                                 location="backend/app/index.js:5", recommendation="", owner_phase=None,
                                 source="tool", tool="semgrep", rule_id="r", path="backend/app/index.js", line=5)
    assert not remediation.repeats(note, medium)
    critical = remediation.Finding(**{**medium.__dict__, "severity": "critical"})
    assert remediation.repeats(note, critical)
    spread = remediation.Finding(**{**note.__dict__, "location": "backend/app/index.js:5, backend/app/other.js"})
    assert not remediation.repeats(spread, critical)


def test_rule_packs_are_staged_where_the_scan_never_reads_them():
    assert '".staging"' in scan._RULES_FETCH and "timeout=60" in scan._RULES_FETCH
    assert ".part-" not in scan._RULES_FETCH


def test_a_stop_marks_the_plans_after_it_as_stopped(monkeypatch):
    from app.build import runner

    monkeypatch.setattr(settings, "security_scan_enabled", True)
    monkeypatch.setattr(settings, "build_run_enabled", True)

    class Engine:
        kind = "docker"

    monkeypatch.setattr(runner, "engine", Engine())

    def stopped(*a, **k):
        raise runner._Stopped()

    monkeypatch.setattr(runner, "_execute", stopped)
    result = scan.run_scan({**_TREE, "frontend/package.json": "{}"}, {})
    assert result.tools[scan.NPM_AUDIT]["status"] == "skipped"
    assert result.tools[scan.NPM_AUDIT]["reason"] == "The scan was stopped."


def test_the_reuse_key_changes_with_the_tree():
    files = {"backend/a.py": "x"}
    assert scan._tree_key(files, ["backend/a.py", "backend/tests/t.py"]) != scan._tree_key(files, ["backend/a.py"])


def test_a_note_about_two_lines_of_one_file_can_still_be_a_repeat():
    assert remediation._files_in("backend/app/index.js:5, 7") == {"backend/app/index.js"}
    assert len(remediation._files_in("a.js:5, b.js")) == 2
