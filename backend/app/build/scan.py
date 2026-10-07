"""Security scanners, run on the generated tree in the sandbox (#77).

Warden used to be one model's opinion of the code, judged again by the same model: a
finding was "fixed" the moment the next audit happened to word it differently. This
runs the tools whose findings have a rule and a line, and which say the same thing
twice about the same code:

  * **Semgrep** — the registry's `p/default`, `p/owasp-top-ten` and `p/secrets` packs
    (fetched into the scanner cache, refreshed daily) plus this platform's own rules
    (`OWN_RULES`), over the JavaScript, TypeScript and Python the crew wrote;
  * **Bandit** — the Python;
  * **npm audit** — each side's `package.json`, resolved to a lockfile without running
    anything;
  * **pip-audit** — each side's `requirements.txt`, resolved from wheel metadata only.

Every tool runs in the sandbox (#75), never on the API process, in containers that
never execute the generated code: the tools are installed into a scanner cache of
their own that no build mounts, so code a build ran can't tamper with them. The two
dependency audits have the network (they ask the advisory databases); Semgrep and
Bandit don't.

Each tool's JSON report is condensed inside the box and printed on one marked line,
then read here into `ToolFinding`s: a tool, a rule id, a CWE, a severity from the
tables below, and a path and line. One problem reported by three rules is one finding.
A finding's identity is its rule, its file and where in the file — never the words a
model used — and "fixed" means a rescan no longer reports it there
(`remediation.sync_dispositions`).

`check.secret_leaks` is not folded in here: it catches a server-only key *read* in
browser code, a compile-gate failure fixed before Warden runs. The scanners' secrets
rules catch a credential *written* into the code. The two never report the same line.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.build import buildlog, layout
from app.build.sandbox import NODE_IMAGE, PYTHON_IMAGE, Step, StepResult
from app.core import scrub
from app.core.config import settings
from app.core.constants import Phase
from app.core.logging import get_logger

log = get_logger(__name__)

# ── what runs ────────────────────────────────────────────────────────────────
#: Pinned: a scanner upgrade changes what is reported, so it is a code change.
SEMGREP_VERSION = "1.139.0"
BANDIT_VERSION = "1.8.6"
PIP_AUDIT_VERSION = "2.9.0"
#: The Semgrep registry packs. Fetched with the network into the scanner cache and
#: refreshed when older than `RULES_MAX_AGE_HOURS`; a fetch that fails keeps the last
#: copy, and with no copy at all Semgrep runs on `OWN_RULES` alone, and says so.
REGISTRY_PACKS = ("default", "owasp-top-ten", "secrets")
RULES_MAX_AGE_HOURS = 24

SEMGREP, BANDIT, NPM_AUDIT, PIP_AUDIT = "semgrep", "bandit", "npm audit", "pip-audit"
TOOLS = (SEMGREP, BANDIT, NPM_AUDIT, PIP_AUDIT)
#: Tools whose findings are about a dependency's version, not a line anyone wrote.
#: The platform owns the manifests (`packages.py`), so the crew can't fix these: they
#: are a person's call, never the fix loop's.
DEPENDENCY_TOOLS = frozenset({NPM_AUDIT, PIP_AUDIT})

#: The line each in-sandbox reader prints its condensed report on.
MARK = "@@AITEAM-SCAN@@"
#: Findings a reader keeps per tool. The rest are counted, not described.
MAX_PER_TOOL = 300
#: The longest a report line may be: the sandbox keeps the last 96,000 bytes of a
#: step's output, and a line cut at its head has lost its marker.
REPORT_LIMIT = 80_000
#: Findings a scan keeps once deduplicated, most severe first.
MAX_KEPT = 200
#: The compile gate's caps, for anything that goes into a prompt: five a file, twenty-
#: four in all.
PROMPT_PER_FILE = 5
PROMPT_TOTAL = 24
#: Two reports this many lines apart are about the same code.
LINE_WINDOW = 3

_TOOLS_DIR = f"/cache/tools-semgrep{SEMGREP_VERSION}-bandit{BANDIT_VERSION}-pipaudit{PIP_AUDIT_VERSION}"
_RULES_DIR = "/cache/rules"
_OWN_RULES_DIR = "/tmp/aiteam-rules"
_SCRATCH = "/work/.aiteam-scan"

#: The platform's own Semgrep rules: the shapes the registry packs miss on the code
#: this crew writes — a query string built from `req.params` against a database
#: object of its own, and a connection string with its password in it.
OWN_RULES = r"""
rules:
  - id: aiteam.javascript.sql-built-from-request
    languages: [javascript, typescript]
    severity: ERROR
    message: >-
      SQL text is built from the request, so a crafted value can change the query.
      Pass the value as a query parameter (a placeholder like $1 or ?) instead of
      putting it into the string.
    metadata:
      cwe: ["CWE-89: Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')"]
      confidence: HIGH
    pattern-either:
      - pattern: $DB.$QUERY("..." + $REQ.$PART.$X, ...)
      - pattern: $DB.$QUERY("..." + $REQ.$PART[$X], ...)
      - pattern: $DB.$QUERY(`...${$REQ.$PART.$X}...`, ...)
      - pattern: $DB.$QUERY(`...${$REQ.$PART[$X]}...`, ...)
      - patterns:
          - pattern: $DB.$QUERY($SQL, ...)
          - pattern-inside: |
              $SQL = "..." + $REQ.$PART.$X;
              ...
      - patterns:
          - pattern: $DB.$QUERY($SQL, ...)
          - pattern-inside: |
              $SQL = `...${$REQ.$PART.$X}...`;
              ...
  - id: aiteam.python.sql-built-from-request
    languages: [python]
    severity: ERROR
    message: >-
      SQL text is built with string formatting, so a crafted value can change the
      query. Pass the values as parameters to execute() instead.
    metadata:
      cwe: ["CWE-89: Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')"]
      confidence: MEDIUM
    pattern-either:
      - pattern: $CUR.execute(f"...", ...)
      - pattern: $CUR.execute("..." % $X, ...)
      - pattern: $CUR.execute("..." + $X, ...)
      - pattern: $CUR.execute("...".format(...), ...)
      - pattern: text(f"...")
  - id: aiteam.secrets.credentials-in-connection-string
    languages: [generic]
    severity: ERROR
    message: >-
      A connection string with a username and password is written into the code, so
      anyone with the code has the database. Read it from an environment variable.
    metadata:
      cwe: ["CWE-798: Use of Hard-coded Credentials"]
      confidence: HIGH
    paths:
      exclude: ["*.md", ".env.example", "*.example"]
    pattern-regex: (?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|mariadb|mssql|redis|rediss|amqps?)://[^\s:/@'"`$]+:[^\s@/'"`${}<>]{3,}@
"""


# ── the result ───────────────────────────────────────────────────────────────
@dataclass
class ToolFinding:
    """One thing a scanner reported, with where it is and who wrote it."""

    tool: str
    rule_id: str
    severity: str
    path: str
    line: Optional[int] = None
    end_line: Optional[int] = None
    cwe: Optional[str] = None
    #: A name for the class of problem: the CWE's, or "Vulnerable dependency".
    category: str = ""
    title: str = ""
    message: str = ""
    fix_hint: str = ""
    #: The rule's page: Semgrep's registry, Bandit's docs, the advisory.
    url: Optional[str] = None
    #: The code it is about, normalised and hashed: how the same finding is recognised
    #: after a fix moved it down the file.
    fingerprint: Optional[str] = None
    owner_phase: Optional[str] = None
    #: Other rules that reported the same problem at the same place: `tool:rule`.
    also: list[str] = field(default_factory=list)

    @property
    def dependency(self) -> bool:
        return self.tool in DEPENDENCY_TOOLS

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    def as_dict(self) -> dict:
        return {
            "tool": self.tool, "rule_id": self.rule_id, "severity": self.severity, "path": self.path,
            "line": self.line, "end_line": self.end_line, "cwe": self.cwe, "category": self.category,
            "title": self.title, "message": self.message, "fix_hint": self.fix_hint, "url": self.url,
            "fingerprint": self.fingerprint, "owner_phase": self.owner_phase, "also": list(self.also),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ToolFinding":
        line = d.get("line")
        end = d.get("end_line")
        return cls(
            tool=str(d.get("tool") or ""), rule_id=str(d.get("rule_id") or ""),
            severity=str(d.get("severity") or "low"), path=str(d.get("path") or ""),
            line=int(line) if isinstance(line, int) or (isinstance(line, str) and line.isdigit()) else None,
            end_line=int(end) if isinstance(end, int) else None,
            cwe=d.get("cwe"), category=str(d.get("category") or ""), title=str(d.get("title") or ""),
            message=str(d.get("message") or ""), fix_hint=str(d.get("fix_hint") or ""), url=d.get("url"),
            fingerprint=d.get("fingerprint"), owner_phase=d.get("owner_phase"),
            also=[str(a) for a in d.get("also") or []],
        )


#: A tool's state in one scan.
RAN, SKIPPED, FAILED, NOT_NEEDED = "ran", "skipped", "failed", "not_needed"


@dataclass
class ScanResult:
    #: ok — at least one tool ran; skipped — none could.
    status: str
    findings: list[ToolFinding] = field(default_factory=list)
    #: name -> {status: ran|skipped|failed|not_needed, version, reason, count, seconds}
    tools: dict[str, dict] = field(default_factory=dict)
    reason: Optional[str] = None
    runner: Optional[str] = None
    seconds: float = 0.0
    #: More findings were reported than are kept here.
    truncated: bool = False
    #: The rule packs Semgrep had, and how old the oldest was: "fetched 3 h ago".
    rules: Optional[str] = None

    @classmethod
    def skipped(cls, reason: str, **kw) -> "ScanResult":
        tools = {t: {"status": SKIPPED, "reason": reason} for t in TOOLS}
        return cls(status=SKIPPED, reason=reason, tools=tools, **kw)

    def ran(self, tool: str) -> bool:
        return (self.tools.get(tool) or {}).get("status") == RAN

    def summary(self) -> str:
        """"semgrep 1.139.0 · bandit 1.8.6 · npm audit · 9.1 s" — the Warden card's line."""
        if self.status == SKIPPED:
            return f"Scanners not available: {_lower_first(self.reason or 'no sandbox could run them.')}"
        parts = []
        for name in TOOLS:
            t = self.tools.get(name) or {}
            if t.get("status") == RAN:
                parts.append(f"{name} {t['version']}" if t.get("version") else name)
        return " · ".join(parts + [f"{self.seconds:.1f} s"]) if parts else "No scanner ran."

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "summary": self.summary(),
            "reason": self.reason,
            "runner": self.runner,
            "seconds": round(self.seconds, 1),
            "tools": self.tools,
            "findings": [f.as_dict() for f in self.findings],
            "truncated": self.truncated,
            "rules": self.rules,
            "at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["ScanResult"]:
        if not isinstance(data, dict):
            return None
        return cls(
            status=str(data.get("status") or SKIPPED),
            findings=[ToolFinding.from_dict(f) for f in data.get("findings") or [] if isinstance(f, dict)],
            tools={str(k): dict(v) for k, v in (data.get("tools") or {}).items() if isinstance(v, dict)},
            reason=data.get("reason"),
            runner=data.get("runner"),
            seconds=float(data.get("seconds") or 0),
            truncated=bool(data.get("truncated")),
            rules=data.get("rules"),
        )


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


# ── severity, per tool ───────────────────────────────────────────────────────
#: The platform's four severities, most severe first (`remediation.SEVERITY_ORDER`).
_ORDER = ("critical", "high", "medium", "low")

#: Semgrep's severities, old and new spellings. INFO is a note, filed as low.
SEMGREP_SEVERITY = {
    "CRITICAL": "critical", "HIGH": "high", "ERROR": "high",
    "MEDIUM": "medium", "WARNING": "medium",
    "LOW": "low", "INFO": "low", "INVENTORY": "low", "EXPERIMENT": "low",
}
BANDIT_SEVERITY = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low", "UNDEFINED": "low"}
NPM_SEVERITY = {"critical": "critical", "high": "high", "moderate": "medium", "low": "low", "info": "low"}
#: pip-audit's advisories carry no severity. Medium: known and fixable, not rated.
PIP_AUDIT_SEVERITY = "medium"

#: An injection or a written-in credential, reported with confidence, is critical: the
#: one-line fix is known and the hole is open to anyone who reads the code or the URL.
CRITICAL_CWES = frozenset({77, 78, 89, 94, 798})
#: CWE Top 25 (2024). A Semgrep WARNING on one of these, reported with high
#: confidence, is high rather than medium (#77, open question 2).
TOP_25_CWES = frozenset({
    79, 787, 89, 352, 22, 125, 78, 416, 862, 434, 94, 20, 77, 287, 269, 502, 200, 863,
    918, 119, 476, 798, 190, 400, 306,
})
#: Short names for the CWEs these tools report most, for the finding's category.
CWE_NAMES = {
    20: "Input validation", 22: "Path traversal", 77: "Command injection", 78: "OS command injection",
    79: "Cross-site scripting (XSS)", 89: "SQL injection", 94: "Code injection", 200: "Data exposure",
    209: "Error message exposure", 259: "Hard-coded password", 284: "Access control",
    287: "Authentication", 295: "Certificate validation", 306: "Missing authentication",
    319: "Cleartext transmission", 326: "Weak encryption", 327: "Broken cryptography",
    328: "Weak hash", 330: "Weak randomness", 338: "Weak randomness", 352: "CSRF",
    377: "Insecure temporary file", 400: "Resource exhaustion", 502: "Unsafe deserialization",
    601: "Open redirect", 605: "Binding to all interfaces", 611: "XML external entities",
    703: "Error handling", 732: "Permissions", 798: "Hard-coded credentials",
    862: "Missing authorization", 863: "Authorization", 915: "Mass assignment",
    918: "Server-side request forgery (SSRF)", 1004: "Cookie without HttpOnly",
    1321: "Prototype pollution", 1333: "Regular expression DoS",
}


def _cwe_number(cwe: Optional[str]) -> Optional[int]:
    m = re.search(r"CWE-(\d+)", str(cwe or ""), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _down(severity: str) -> str:
    i = _ORDER.index(severity) if severity in _ORDER else len(_ORDER) - 1
    return _ORDER[min(i + 1, len(_ORDER) - 1)]


def semgrep_severity(raw: Optional[str], cwe: Optional[str], confidence: Optional[str]) -> str:
    sev = SEMGREP_SEVERITY.get(str(raw or "").upper(), "medium")
    n = _cwe_number(cwe)
    conf = str(confidence or "").upper()
    if sev == "high" and n in CRITICAL_CWES and conf in ("HIGH", "MEDIUM", ""):
        return "critical"
    if sev == "medium" and n in TOP_25_CWES and conf == "HIGH":
        return "high"
    if conf == "LOW":
        return _down(sev)
    return sev


def bandit_severity(raw: Optional[str], cwe: Optional[int], confidence: Optional[str]) -> str:
    sev = BANDIT_SEVERITY.get(str(raw or "").upper(), "low")
    conf = str(confidence or "").upper()
    if sev == "high" and conf == "HIGH" and cwe in CRITICAL_CWES:
        return "critical"
    if conf == "LOW":
        return _down(sev)
    return sev


def _category(cwe: Optional[str]) -> str:
    n = _cwe_number(cwe)
    if n in CWE_NAMES:
        return CWE_NAMES[n]
    text = str(cwe or "")
    m = re.search(r"\('([^']+)'\)", text)
    if m:
        return m.group(1)
    name = text.split(":", 1)[-1].strip()
    return name[:60] or (f"CWE-{n}" if n else "")


def _first_sentence(text: str, limit: int = 120) -> str:
    flat = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", flat)
    head = m.group(1) if m else flat
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


# ── which files, and whose ───────────────────────────────────────────────────
_CODE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py")
_MANIFESTS = ("package.json", "requirements.txt")
#: Who owns a file nobody in the crew wrote — the platform's manifest, say: the code
#: phase on that side of the tree.
_SIDE_OWNER = {layout.BACKEND: Phase.BACKEND_ENGINEER.value, layout.FRONTEND: Phase.FRONTEND_ENGINEER.value}
#: The phases whose files are scanned: the ones that write code and run before Warden.
SCANNED_PHASES = (Phase.BACKEND_ENGINEER.value, Phase.FRONTEND_ENGINEER.value, Phase.QA_ENGINEER.value)


def scan_tree(prior_outputs: dict, charter=None) -> tuple[dict[str, str], dict[str, str]]:
    """(the files to scan, who wrote each) — the build as Warden is about to review it.

    The crew's own source, not its tests (a fake password in a fixture is not a leak)
    and not the platform's scaffold (nobody in the crew can change it) — apart from
    each side's manifest, which is what the dependency audits read.
    """
    from app.build.check import phase_tree
    from app.build.scaffold import _is_test, platform_owned
    from app.core.artifacts import iter_files

    files, _mine = phase_tree(prior_outputs, Phase.SECURITY_ENGINEER.value, {}, charter)
    backend_language = charter.get("language").token if charter is not None and charter.get("language") else None
    placer = layout.Placer(backend_language)
    owners: dict[str, str] = {}
    from app.core.constants import PHASE_ORDER

    for phase in [p.value for p in PHASE_ORDER]:
        if phase == Phase.SECURITY_ENGINEER.value:
            break
        for placed, _path, _content, _lang in placer.place_all(phase, iter_files(prior_outputs.get(phase) or {})):
            if phase in SCANNED_PHASES:
                owners.setdefault(placed, phase)
    out: dict[str, str] = {}
    for path, content in files.items():
        side = layout.side_of(path)
        rel = layout.relative_to_side(path)
        if rel in _MANIFESTS and side in _SIDE_OWNER:
            out[path] = content
            continue
        if not path.endswith(_CODE_EXT) or path not in owners:
            continue
        if _is_test(rel) or platform_owned(path):
            continue
        out[path] = content
    return out, owners


def owner_of(path: str, owners: dict[str, str]) -> Optional[str]:
    return owners.get(path) or _SIDE_OWNER.get(layout.side_of(path) or "")


# ── the readers that run inside the box ──────────────────────────────────────
#: Condenses Semgrep's, Bandit's and pip-audit's JSON into the shape `judge` reads.
_PY_READER = r"""
import json, sys
MARK, LIMIT, MAX = "@@MARK@@", @@LIMIT@@, @@MAX@@
kind = sys.argv[1]
def read(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None
def clip(s, n):
    s = str(s if s is not None else "")
    return s if len(s) <= n else s[: n - 1] + "\u2026"
def rel(p):
    p = str(p or "")
    return p[6:] if p.startswith("/work/") else p.lstrip("./")
def tail(p, n=600):
    try:
        with open(p, errors="replace") as f:
            return f.read()[-n:]
    except Exception:
        return ""
out = {"tool": kind, "ran": False, "findings": [], "errors": 0, "reason": None, "version": None, "total": 0}
if kind == "semgrep":
    report, code, prefixes, version = sys.argv[2], sys.argv[3], sys.argv[4].split(","), sys.argv[5]
    d = read(report)
    if not isinstance(d, dict):
        out["reason"] = "Semgrep stopped without a report (exit %s): %s" % (code, tail("/tmp/semgrep.err", 300))
    else:
        out["ran"], out["version"] = True, d.get("version") or version
        errs = d.get("errors") or []
        out["errors"] = len(errs)
        if errs:
            out["error_sample"] = clip((errs[0] or {}).get("message"), 300)
        results = d.get("results") or []
        out["total"] = len(results)
        for r in results[:MAX]:
            rid = str(r.get("check_id") or "")
            for p in prefixes:
                if p and rid.startswith(p):
                    rid = rid[len(p):]
                    break
            ex = r.get("extra") or {}
            md = ex.get("metadata") or {}
            cwe = md.get("cwe")
            cwe = cwe[0] if isinstance(cwe, list) and cwe else cwe
            out["findings"].append({
                "rule": rid, "path": rel(r.get("path")),
                "line": (r.get("start") or {}).get("line"), "end": (r.get("end") or {}).get("line"),
                "severity": ex.get("severity"), "confidence": md.get("confidence"),
                "cwe": clip(cwe, 200) if cwe else None, "message": clip(ex.get("message"), 600),
                "url": md.get("source") or md.get("shortlink"),
                "fix": clip(ex.get("fix"), 300) if ex.get("fix") else None,
            })
elif kind == "bandit":
    report, code, version = sys.argv[2], sys.argv[3], sys.argv[4]
    d = read(report)
    if not isinstance(d, dict):
        out["reason"] = "Bandit stopped without a report (exit %s): %s" % (code, tail("/tmp/bandit.err", 300))
    else:
        out["ran"], out["version"] = True, version
        out["errors"] = len(d.get("errors") or [])
        results = d.get("results") or []
        out["total"] = len(results)
        for r in results[:MAX]:
            cwe = r.get("issue_cwe") or {}
            lines = r.get("line_range") or []
            out["findings"].append({
                "rule": r.get("test_id"), "name": r.get("test_name"), "path": rel(r.get("filename")),
                "line": r.get("line_number"), "end": lines[-1] if lines else r.get("line_number"),
                "severity": r.get("issue_severity"), "confidence": r.get("issue_confidence"),
                "cwe": cwe.get("id") if isinstance(cwe, dict) else None,
                "message": clip(r.get("issue_text"), 600), "url": r.get("more_info"),
            })
elif kind == "pip-audit":
    side, report, code, resolved = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    out["side"] = side
    d = read(report)
    if resolved != "0":
        out["reason"] = "The %s's requirements couldn't be resolved from wheels: %s" % (side, tail("/tmp/resolve-%s.err" % side, 300))
    elif not isinstance(d, dict):
        out["reason"] = "pip-audit stopped without a report (exit %s): %s" % (code, tail("/tmp/pip-audit-%s.err" % side, 300))
    else:
        out["ran"], out["version"] = True, "@@PIPAUDIT@@"
        deps = d.get("dependencies") or []
        out["checked"] = len(deps)
        for dep in deps:
            vulns = [v for v in dep.get("vulns") or [] if isinstance(v, dict)]
            if not vulns:
                continue
            out["total"] += 1
            if len(out["findings"]) >= MAX:
                continue
            out["findings"].append({
                "package": dep.get("name"), "version": dep.get("version"),
                "vulns": [{"id": v.get("id"), "fix": v.get("fix_versions") or [], "aliases": (v.get("aliases") or [])[:4],
                           "description": clip(v.get("description"), 300)} for v in vulns[:8]],
                "more": max(len(vulns) - 8, 0),
            })
while len(json.dumps(out)) > LIMIT and out["findings"]:
    out["findings"].pop()
    out["truncated"] = True
sys.stdout.write("\n" + MARK + json.dumps(out) + "\n")
""".replace("@@MARK@@", MARK).replace("@@LIMIT@@", str(REPORT_LIMIT)).replace("@@MAX@@", str(MAX_PER_TOOL)).replace(
    "@@PIPAUDIT@@", PIP_AUDIT_VERSION
)

#: The same for `npm audit --json`.
_NODE_READER = r"""
const fs = require('fs');
const [side, lockCode, auditCode, version] = process.argv.slice(2);
const MARK = '@@MARK@@', LIMIT = @@LIMIT@@, MAX = @@MAX@@;
const read = (p) => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch (e) { return null; } };
const tail = (p, n) => { try { return fs.readFileSync(p, 'utf8').slice(-n); } catch (e) { return ''; } };
const clip = (s, n) => { s = String(s == null ? '' : s); return s.length <= n ? s : s.slice(0, n - 1) + '\u2026'; };
const out = { tool: 'npm audit', side, ran: false, findings: [], errors: 0, reason: null, version, total: 0 };
const d = read('/tmp/audit-' + side + '.json');
if (lockCode !== '0') {
  out.reason = "npm couldn't resolve the " + side + "'s dependencies: " + clip(tail('/tmp/lock-' + side + '.log', 400), 400);
} else if (!d || (d.error && !d.vulnerabilities)) {
  out.reason = 'npm audit had no answer: ' + clip((d && d.error && (d.error.summary || d.error.code)) || tail('/tmp/audit-' + side + '.err', 300), 300);
} else {
  out.ran = true;
  const vulns = Object.entries(d.vulnerabilities || {});
  out.total = vulns.length;
  for (const [name, v] of vulns.slice(0, MAX)) {
    const via = (v.via || []).filter((x) => x && typeof x === 'object').slice(0, 6)
      .map((x) => ({ title: clip(x.title, 200), url: x.url, cwe: x.cwe || [], range: x.range, severity: x.severity }));
    const through = (v.via || []).filter((x) => typeof x === 'string').slice(0, 6);
    out.findings.push({ package: name, severity: v.severity, direct: !!v.isDirect, range: v.range,
      fix: v.fixAvailable, advisories: via, through });
  }
}
while (JSON.stringify(out).length > LIMIT && out.findings.length) { out.findings.pop(); out.truncated = true; }
process.stdout.write('\n' + MARK + JSON.stringify(out) + '\n');
""".replace("@@MARK@@", MARK).replace("@@LIMIT@@", str(REPORT_LIMIT)).replace("@@MAX@@", str(MAX_PER_TOOL))

#: Fetches the registry packs into the scanner cache, keeping the last good copy.
_RULES_FETCH = r"""
import os, sys, time, urllib.request
d, packs, max_age = sys.argv[1], sys.argv[2].split(","), float(sys.argv[3]) * 3600
os.makedirs(d, exist_ok=True)
for p in packs:
    path = os.path.join(d, p + ".yml")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        continue
    try:
        req = urllib.request.Request("https://semgrep.dev/c/p/" + p, headers={"User-Agent": "aiteam-scan"})
        body = urllib.request.urlopen(req, timeout=60).read()
        if not body.lstrip().startswith(b"rules:"):
            raise ValueError("not a rule pack")
        tmp = path + ".part-%d" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        print("fetched p/%s" % p)
    except Exception as e:
        print("couldn't fetch p/%s (%s)%s" % (p, e.__class__.__name__, ", keeping the last copy" if os.path.exists(path) else ""))
"""


def _heredoc(path: str, body: str) -> str:
    return f"cat > {path} <<'AITEAM_EOF'\n{body.strip()}\nAITEAM_EOF\n"


def _q(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


# ── what to run ──────────────────────────────────────────────────────────────
@dataclass
class ScanPlan:
    image: str
    steps: list[Step]
    files: dict[str, str]
    #: Tools this plan runs, for the result's "not needed" entries.
    tools: tuple[str, ...]


def _budget() -> int:
    return max(int(settings.security_scan_timeout_seconds), 60)


def _python_sides(files: dict[str, str]) -> list[str]:
    return sorted({layout.side_of(p) for p in files if layout.relative_to_side(p) == "requirements.txt"} - {None})


def _node_sides(files: dict[str, str]) -> list[str]:
    return sorted({layout.side_of(p) for p in files if layout.relative_to_side(p) == "package.json"} - {None})


def plan_python(files: dict[str, str]) -> Optional[ScanPlan]:
    """Semgrep, Bandit and pip-audit, in the Python image. None with nothing to scan."""
    code = [p for p in files if p.endswith(_CODE_EXT)]
    py_sides = _python_sides(files)
    if not code and not py_sides:
        return None
    tools: list[str] = []
    budget = _budget()
    env = {"PYTHONPATH": _TOOLS_DIR, "PATH": f"{_TOOLS_DIR}/bin:/usr/local/bin:/usr/bin:/bin"}
    install = (
        "set -e\n"
        f"T={_TOOLS_DIR}\n"
        'if [ ! -f "$T/.ok" ]; then\n'
        '  P="$T.part-$(cat /proc/sys/kernel/random/uuid)"\n'
        '  pip install --no-input --prefer-binary --disable-pip-version-check --quiet --target "$P" '
        f"semgrep=={SEMGREP_VERSION} bandit=={BANDIT_VERSION} pip-audit=={PIP_AUDIT_VERSION}\n"
        '  touch "$P/.ok"\n'
        '  if [ ! -f "$T/.ok" ]; then rm -rf "$T"; mv "$P" "$T"; else rm -rf "$P"; fi\n'
        "  echo 'installed the scanners'\n"
        "else echo 'scanners already installed'; fi\n"
        + _heredoc("/tmp/aiteam-fetch.py", _RULES_FETCH)
        + f"python /tmp/aiteam-fetch.py {_RULES_DIR} {','.join(REGISTRY_PACKS)} {RULES_MAX_AGE_HOURS} || true\n"
        f"mkdir -p {_SCRATCH}\n"
    )
    for side in py_sides:
        # Resolved from wheel metadata only: nothing a package ships is run to find
        # out what it depends on. No cache: one account's package names stay its own.
        install += (
            f"pip install --dry-run --ignore-installed --only-binary :all: --no-cache-dir --quiet "
            f"--disable-pip-version-check --report {_SCRATCH}/{side}-resolved.json -r {side}/requirements.txt "
            f">/dev/null 2>{_SCRATCH}/{side}-resolve.err; echo $? > {_SCRATCH}/{side}-resolve.code\n"
        )
    steps = [Step("install", "install scanners", install, network=True, timeout=min(budget, 420))]

    if code:
        tools.append(SEMGREP)
        prefixes = ",".join(
            d.strip("/").replace("/", ".") + "." for d in (_RULES_DIR, _OWN_RULES_DIR)
        )
        semgrep = (
            _heredoc(f"{_OWN_RULES_DIR}/aiteam.yml", OWN_RULES).replace("cat >", f"mkdir -p {_OWN_RULES_DIR} && cat >", 1)
            + _heredoc("/tmp/aiteam-read.py", _PY_READER)
            + f"C='--config {_OWN_RULES_DIR}/aiteam.yml'\n"
            + f"for f in {_RULES_DIR}/*.yml; do [ -f \"$f\" ] && C=\"$C --config $f\"; done\n"
            + "semgrep scan --metrics=off --disable-version-check --json --quiet --timeout 30 "
            "--max-target-bytes 1000000 --exclude .aiteam-scan $C --output /tmp/semgrep.json /work "
            "2>/tmp/semgrep.err; code=$?\n"
            + f"python /tmp/aiteam-read.py semgrep /tmp/semgrep.json $code {_q(prefixes)} {SEMGREP_VERSION}\n"
            "exit 0\n"
        )
        steps.append(Step("scan", "semgrep", semgrep, timeout=min(budget, 240), env=env))
    py_code = [p for p in code if p.endswith(".py")]
    if py_code:
        tools.append(BANDIT)
        dirs = " ".join(sorted({_q(p.split("/", 1)[0]) for p in py_code}))
        bandit = (
            _heredoc("/tmp/aiteam-read.py", _PY_READER)
            + f"bandit -r {dirs} -f json -o /tmp/bandit.json -q --exit-zero -x ./.aiteam-scan 2>/tmp/bandit.err; code=$?\n"
            + f"python /tmp/aiteam-read.py bandit /tmp/bandit.json $code {BANDIT_VERSION}\n"
            "exit 0\n"
        )
        steps.append(Step("scan", "bandit", bandit, timeout=min(budget, 120), env=env))
    if py_sides:
        tools.append(PIP_AUDIT)
        audit = _heredoc("/tmp/aiteam-read.py", _PY_READER)
        for side in py_sides:
            # The resolved versions, pinned, audited as they are: --disable-pip installs
            # nothing. The network is for the advisory database.
            audit += (
                f"r=$(cat {_SCRATCH}/{side}-resolve.code 2>/dev/null || echo 1); cp {_SCRATCH}/{side}-resolve.err /tmp/resolve-{side}.err 2>/dev/null\n"
                f"if [ \"$r\" = 0 ]; then python -c 'import json,sys; d=json.load(open(sys.argv[1])); "
                "print(\"\\n\".join(i[\"metadata\"][\"name\"]+\"==\"+i[\"metadata\"][\"version\"] for i in d.get(\"install\") or []))' "
                f"{_SCRATCH}/{side}-resolved.json > /tmp/{side}-pinned.txt; "
                f"pip-audit --no-deps --disable-pip -r /tmp/{side}-pinned.txt -f json -o /tmp/pip-audit-{side}.json "
                f"--progress-spinner off 2>/tmp/pip-audit-{side}.err; code=$?; else code=0; fi\n"
                f"python /tmp/aiteam-read.py pip-audit {side} /tmp/pip-audit-{side}.json $code $r\n"
            )
        steps.append(Step("scan", "pip-audit", audit + "exit 0\n", network=True, timeout=min(budget, 180), env=env))
    mine = {p: c for p, c in files.items() if p.endswith(_CODE_EXT) or layout.relative_to_side(p) == "requirements.txt"}
    return ScanPlan(PYTHON_IMAGE, steps, mine, tuple(tools))


def plan_node(files: dict[str, str]) -> Optional[ScanPlan]:
    """npm audit, one step per side with a `package.json`, in the Node image."""
    sides = _node_sides(files)
    if not sides:
        return None
    steps = []
    for side in sides:
        # A lockfile is resolved, never installed: no package is downloaded or run.
        command = (
            _heredoc("/tmp/aiteam-read.cjs", _NODE_READER)
            + f"cd /work/{side} && npm install --package-lock-only --ignore-scripts --no-audit --no-fund "
            f"--loglevel=error >/tmp/lock-{side}.log 2>&1; lc=$?\n"
            + f"if [ $lc = 0 ]; then npm audit --json > /tmp/audit-{side}.json 2>/tmp/audit-{side}.err; fi\n"
            + f"node /tmp/aiteam-read.cjs {side} $lc 0 \"$(npm --version)\"\n"
            "exit 0\n"
        )
        steps.append(Step("scan", f"npm audit ({side})", command, network=True, timeout=180))
    mine = {p: c for p, c in files.items() if layout.relative_to_side(p) == "package.json"}
    return ScanPlan(NODE_IMAGE, steps, mine, (NPM_AUDIT,))


# ── reading what it did ──────────────────────────────────────────────────────
def _reports(output: str) -> list[dict]:
    found = []
    for line in buildlog.clean(output or "").splitlines():
        if line.startswith(MARK):
            try:
                data = json.loads(line[len(MARK):])
            except ValueError:
                continue
            if isinstance(data, dict):
                found.append(data)
    return found


def _norm(code: str) -> str:
    return re.sub(r"\s+", " ", code).strip()


def _fingerprint(files: dict[str, str], path: str, line: Optional[int], end: Optional[int]) -> Optional[str]:
    content = files.get(path)
    if content is None or not line:
        return None
    lines = content.splitlines()
    chosen = _norm("\n".join(lines[line - 1 : max(end or line, line)]))
    if not chosen:
        return None
    return hashlib.sha256(f"{path}\0{chosen}".encode("utf-8")).hexdigest()[:24]


def _manifest_line(files: dict[str, str], path: str, package: str) -> Optional[int]:
    """Where a dependency is declared in its manifest, when it is declared there."""
    content = files.get(path) or ""
    if path.endswith("package.json"):
        pattern = re.compile(rf'^\s*"{re.escape(package)}"\s*:', re.MULTILINE)
    else:
        pattern = re.compile(rf"^\s*{re.escape(package)}\s*(?:[<>=!~;\[ ]|$)", re.MULTILINE | re.IGNORECASE)
    m = pattern.search(content)
    return content.count("\n", 0, m.start()) + 1 if m else None


def _semgrep(r: dict, files: dict[str, str]) -> Optional[ToolFinding]:
    path, rule = str(r.get("path") or ""), str(r.get("rule") or "")
    if not path or not rule:
        return None
    line = r.get("line") if isinstance(r.get("line"), int) else None
    end = r.get("end") if isinstance(r.get("end"), int) else line
    cwe = r.get("cwe")
    message = str(r.get("message") or "")
    hint = f"Semgrep's suggested fix: `{r['fix']}`" if r.get("fix") else ""
    url = r.get("url") or (f"https://semgrep.dev/r/{rule}" if not rule.startswith("aiteam.") else None)
    return ToolFinding(
        tool=SEMGREP, rule_id=rule, severity=semgrep_severity(r.get("severity"), cwe, r.get("confidence")),
        path=path, line=line, end_line=end, cwe=_cwe_id(cwe), category=_category(cwe) or "Security",
        title=_first_sentence(message), message=message, fix_hint=hint, url=url,
        fingerprint=_fingerprint(files, path, line, end),
    )


def _cwe_id(cwe: object) -> Optional[str]:
    n = _cwe_number(str(cwe)) if cwe is not None else None
    if n is None and isinstance(cwe, int):
        n = cwe
    return f"CWE-{n}" if n else None


def _bandit(r: dict, files: dict[str, str]) -> Optional[ToolFinding]:
    path, rule = str(r.get("path") or ""), str(r.get("rule") or "")
    if not path or not rule:
        return None
    line = r.get("line") if isinstance(r.get("line"), int) else None
    end = r.get("end") if isinstance(r.get("end"), int) else line
    cwe = r.get("cwe") if isinstance(r.get("cwe"), int) else None
    message = str(r.get("message") or "")
    return ToolFinding(
        tool=BANDIT, rule_id=rule if not r.get("name") else f"{rule}:{r['name']}",
        severity=bandit_severity(r.get("severity"), cwe, r.get("confidence")), path=path, line=line,
        end_line=end, cwe=f"CWE-{cwe}" if cwe else None, category=CWE_NAMES.get(cwe or -1) or "Security",
        title=_first_sentence(message), message=message, url=r.get("url"),
        fingerprint=_fingerprint(files, path, line, end),
    )


def _npm(r: dict, side: str, files: dict[str, str]) -> Optional[ToolFinding]:
    package = str(r.get("package") or "")
    if not package:
        return None
    path = f"{side}/package.json"
    advisories = [a for a in r.get("advisories") or [] if isinstance(a, dict)]
    cwes = [c for a in advisories for c in (a.get("cwe") or [])]
    titles = list(dict.fromkeys(str(a.get("title") or "") for a in advisories if a.get("title")))
    fix = r.get("fix")
    if isinstance(fix, dict) and fix.get("name"):
        major = " (a major version: check what it breaks)" if fix.get("isSemVerMajor") else ""
        hint = f"fixAvailable: {fix['name']}@{fix.get('version')}{major}."
    elif fix is True:
        hint = "fixAvailable: `npm audit fix` resolves it within the declared range."
    else:
        hint = "No fixed version is available yet."
    through = [str(t) for t in r.get("through") or []]
    if not r.get("direct") and through:
        hint += f" It comes in through {', '.join(through[:3])}."
    title = f"{package} has {'a known vulnerability' if len(titles) <= 1 else f'{len(titles)} known vulnerabilities'}"
    message = "; ".join(titles[:4]) or f"{package} is affected by a vulnerable dependency."
    line = _manifest_line(files, path, package)
    return ToolFinding(
        tool=NPM_AUDIT, rule_id=package, severity=NPM_SEVERITY.get(str(r.get("severity") or "").lower(), "medium"),
        path=path, line=line, end_line=line, cwe=_cwe_id(cwes[0]) if cwes else None,
        category="Vulnerable dependency", title=title, message=message, fix_hint=hint,
        url=next((a.get("url") for a in advisories if a.get("url")), None),
    )


def _pip(r: dict, side: str, files: dict[str, str]) -> Optional[ToolFinding]:
    package = str(r.get("package") or "")
    if not package:
        return None
    path = f"{side}/requirements.txt"
    vulns = [v for v in r.get("vulns") or [] if isinstance(v, dict)]
    ids = [str(v.get("id")) for v in vulns if v.get("id")]
    fixes = [str(x) for v in vulns for x in v.get("fix") or []]
    best = max(fixes, key=_version_key) if fixes else None
    hint = f"Upgrade {package} to {best} or later." if best else "No fixed version is listed yet."
    more = int(r.get("more") or 0)
    count = len(ids) + more
    title = f"{package} {r.get('version') or ''} has {'a known vulnerability' if count == 1 else f'{count} known vulnerabilities'}".replace("  ", " ")
    message = ", ".join(ids[:6]) + (f" and {more} more" if more else "")
    first = (vulns[0].get("description") or "").strip() if vulns else ""
    if first:
        message += f". {first}"
    line = _manifest_line(files, path, package)
    return ToolFinding(
        tool=PIP_AUDIT, rule_id=package.lower(), severity=PIP_AUDIT_SEVERITY, path=path, line=line,
        end_line=line, category="Vulnerable dependency", title=title, message=message, fix_hint=hint,
        url=f"https://osv.dev/vulnerability/{ids[0]}" if ids else None,
    )


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", v)[:4])


def _near(a: Optional[int], b: Optional[int]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= LINE_WINDOW


_TOOL_ORDER = {t: i for i, t in enumerate(TOOLS)}


def _rank(f: ToolFinding) -> tuple:
    sev = _ORDER.index(f.severity) if f.severity in _ORDER else len(_ORDER)
    return (sev, _TOOL_ORDER.get(f.tool, 9), f.rule_id)


def dedupe(found: Iterable[ToolFinding]) -> list[ToolFinding]:
    """One finding per problem.

    The same rule at the same place twice is one finding (`rule_id`, `path`, a line
    within ±3). Different rules — or Bandit and Semgrep — reporting the same CWE at the
    same place are one problem too: the most severe report leads and the others are
    kept under `also`, so the dispositions list says "SQL injection" once, not four
    times.
    """
    ordered = sorted(found, key=lambda f: (f.path, f.line or 0, _rank(f)))
    groups: list[list[ToolFinding]] = []
    for f in ordered:
        home = None
        for g in groups:
            lead = g[0]
            if lead.path != f.path or not _near(lead.line, f.line):
                continue
            same_rule = any(m.tool == f.tool and m.rule_id == f.rule_id for m in g)
            same_cwe = bool(f.cwe) and not f.dependency and any(m.cwe == f.cwe and not m.dependency for m in g)
            if same_rule or same_cwe:
                home = g
                break
        if home is None:
            groups.append([f])
        elif not any(m.tool == f.tool and m.rule_id == f.rule_id for m in home):
            home.append(f)
    out = []
    for g in groups:
        g.sort(key=_rank)
        lead = g[0]
        lead.also = list(dict.fromkeys(f"{m.tool}:{m.rule_id}" for m in g[1:]))
        if not lead.fix_hint:
            lead.fix_hint = next((m.fix_hint for m in g[1:] if m.fix_hint), "")
        out.append(lead)
    out.sort(key=lambda f: (_rank(f), f.path, f.line or 0))
    return out


def judge(results_by_plan: list[tuple[ScanPlan, list[StepResult], Optional[str]]], files: dict[str, str],
          owners: dict[str, str]) -> ScanResult:
    """Every tool's report, read into findings, a per-tool status, and a summary.

    Each plan comes with its step results and, when the sandbox itself couldn't run
    it (Docker, the builder service), why — every tool it holds is then skipped."""
    found: list[ToolFinding] = []
    tools: dict[str, dict] = {}
    truncated = False
    rules_note: Optional[str] = None
    for plan, results, couldnt in results_by_plan:
        install_failed = scrub.scrub(couldnt)[:300] if couldnt else None
        for r in results:
            if r.label == "install scanners":
                if r.skipped or r.timed_out or not r.ok:
                    install_failed = install_failed or _why_install_failed(r)
                else:
                    rules_note = _rules_note(r.output)
                continue
            name = SEMGREP if r.label == SEMGREP else BANDIT if r.label == BANDIT else (
                PIP_AUDIT if r.label == PIP_AUDIT else NPM_AUDIT
            )
            entry = tools.setdefault(name, {"status": SKIPPED, "version": None, "reason": None, "count": 0,
                                             "seconds": 0.0})
            entry["seconds"] = round(entry["seconds"] + r.seconds, 1)
            if r.skipped or r.timed_out:
                entry["reason"] = entry["reason"] or (
                    install_failed or (f"{name} ran out of time." if r.timed_out else "The scan was stopped.")
                )
                continue
            reports = _reports(r.output)
            if not reports:
                entry["status"] = FAILED if entry["status"] != RAN else RAN
                entry["reason"] = entry["reason"] or f"{name} printed no report: {_tail(r.output)}"
                continue
            for rep in reports:
                if rep.get("ran"):
                    entry["status"] = RAN
                    entry["version"] = rep.get("version") or entry["version"]
                elif entry["status"] != RAN:
                    entry["status"] = FAILED
                    entry["reason"] = scrub.scrub(str(rep.get("reason") or f"{name} didn't run."))[:400]
                if rep.get("truncated") or int(rep.get("total") or 0) > len(rep.get("findings") or []):
                    truncated = True
                if rep.get("errors"):
                    entry["errors"] = int(entry.get("errors") or 0) + int(rep["errors"])
                side = str(rep.get("side") or "")
                for raw in rep.get("findings") or []:
                    if not isinstance(raw, dict):
                        continue
                    if name == SEMGREP:
                        f = _semgrep(raw, files)
                    elif name == BANDIT:
                        f = _bandit(raw, files)
                    elif name == NPM_AUDIT:
                        f = _npm(raw, side, files)
                    else:
                        f = _pip(raw, side, files)
                    if f is not None:
                        found.append(f)
        for name in plan.tools:
            tools.setdefault(name, {"status": SKIPPED, "reason": install_failed or "It didn't run.", "count": 0})
    for f in found:
        f.owner_phase = owner_of(f.path, owners)
        f.title = scrub.scrub(f.title)
        f.message = scrub.scrub(f.message)
        f.fix_hint = scrub.scrub(f.fix_hint)
    kept = dedupe(found)
    if len(kept) > MAX_KEPT:
        kept, truncated = kept[:MAX_KEPT], True
    for name, entry in tools.items():
        entry["count"] = sum(1 for f in kept if f.tool == name)
    for name in TOOLS:
        tools.setdefault(name, {"status": NOT_NEEDED, "reason": _not_needed(name), "count": 0})
    ran = any(t.get("status") == RAN for t in tools.values())
    reasons = [t.get("reason") for t in tools.values() if t.get("status") in (SKIPPED, FAILED) and t.get("reason")]
    return ScanResult(
        status="ok" if ran else SKIPPED,
        findings=kept,
        tools=tools,
        reason=None if ran else (reasons[0] if reasons else "No scanner could run."),
        truncated=truncated,
        rules=rules_note,
    )


def _not_needed(name: str) -> str:
    return {
        SEMGREP: "There's no code to scan.",
        BANDIT: "There's no Python to scan.",
        NPM_AUDIT: "There's no package.json.",
        PIP_AUDIT: "There's no requirements.txt.",
    }[name]


def _why_install_failed(r: StepResult) -> str:
    if r.timed_out:
        return "Installing the scanners ran out of time."
    if r.skipped:
        return "The scan was stopped."
    if buildlog.environmental(r.output):
        return "The scanners couldn't be downloaded (no network)."
    return f"The scanners couldn't be installed: {_tail(r.output)}"


def _rules_note(output: str) -> Optional[str]:
    failed = re.findall(r"couldn't fetch p/([\w-]+)", output or "")
    if not failed:
        return None
    missing = [p for p in failed if not re.search(rf"couldn't fetch p/{re.escape(p)} \([^)]*\), keeping", output)]
    if missing:
        return f"Semgrep ran without p/{', p/'.join(missing)}: the registry couldn't be reached."
    return "Semgrep used yesterday's rule packs: the registry couldn't be reached."


def _tail(output: str, n: int = 200) -> str:
    text = " ".join(buildlog.clean(output or "").split())
    return scrub.scrub(text[-n:]) or "no output"


# ── running it ───────────────────────────────────────────────────────────────
def run_scan(files: dict[str, str], owners: dict[str, str]) -> ScanResult:
    """Run every scanner that applies to `files`. Never raises for the scan's sake:
    what can't run comes back `skipped`, with the reason."""
    from app.build import runner
    from app.core import identity

    if not settings.security_scan_enabled:
        return ScanResult.skipped("Security scanners are switched off (SECURITY_SCAN_ENABLED=false).")
    plans = [p for p in (plan_python(files), plan_node(files)) if p is not None]
    if not plans:
        result = ScanResult(status=SKIPPED, reason="There's no code or manifest to scan.")
        result.tools = {t: {"status": NOT_NEEDED, "reason": _not_needed(t), "count": 0} for t in TOOLS}
        return result
    chosen, why = runner._engine()
    if chosen is None:
        return ScanResult.skipped(why or "No sandbox is available to run the scanners in.")
    started = time.monotonic()
    done: list[tuple[ScanPlan, list[StepResult], Optional[str]]] = []
    user = identity.current_user_id() or "shared"
    for plan in plans:
        # The scanners' own cache, which no build mounts: code a build ran can never
        # reach the tools that judge it. The Python tools are the same for everyone;
        # npm's metadata cache is the account's own.
        cache = "scan-tools" if plan.image == PYTHON_IMAGE else f"scan-{user}"
        unrun = [StepResult(s.name, s.label, None, 0.0, skipped=True) for s in plan.steps]
        try:
            results = runner._execute(plan.image, plan.files, plan.steps, "security", "scanning", chosen,
                                      cache=cache, seconds=_budget())
        except runner._CouldntRun as e:
            log.warning("The scanners couldn't run: %s", e)
            done.append((plan, unrun, str(e)))
            continue
        except runner._Stopped:
            done.append((plan, unrun, "The scan was stopped."))
            continue
        done.append((plan, results, None))
    result = judge(done, files, owners)
    result.runner = chosen.kind
    result.seconds = time.monotonic() - started
    log.info("Security scan (%s): %s, %d finding(s)", chosen.kind, result.summary(), len(result.findings))
    return result


def scan_build(prior_outputs: dict, charter=None) -> ScanResult:
    """The build as Warden is about to review it, scanned. Never raises for the scan's
    sake (a Stop still stops it)."""
    from app.orchestration.claim import Superseded
    from app.router.base import RequestCancelled

    try:
        files, owners = scan_tree(prior_outputs, charter)
        return run_scan(files, owners)
    except (RequestCancelled, Superseded):
        raise
    except Exception as e:  # noqa: BLE001 - the scanners must never become the failure
        log.warning("The security scan couldn't run: %s", e)
        return ScanResult.skipped(f"The security scan couldn't run: {e}")


# ── what Warden is told ──────────────────────────────────────────────────────
def capped(findings: Iterable[ToolFinding], per_file: int = PROMPT_PER_FILE, total: int = PROMPT_TOTAL) -> list[ToolFinding]:
    """The compile gate's caps: five a file, twenty-four in all, most severe first."""
    seen: dict[str, int] = {}
    out = []
    for f in sorted(findings, key=_rank):
        if seen.get(f.path, 0) >= per_file:
            continue
        seen[f.path] = seen.get(f.path, 0) + 1
        out.append(f)
        if len(out) >= total:
            break
    return out


def prompt_block(result: Optional[ScanResult]) -> str:
    """"# Scanner findings" for Warden's prompt: what is already known, capped.

    Parsed and capped, never the tools' raw output: a rule message is a sentence, and a
    hundred of them would push the code Warden is reviewing out of its window.
    """
    if result is None or result.status == SKIPPED:
        why = (result.reason if result is not None else None) or "no sandbox could run them"
        return (
            "# Scanner findings\nThe scanners couldn't run on this build "
            f"({why.rstrip('.')}). Nothing is known yet: also check for injection, XSS, "
            "CSRF and secrets written into the code."
        )
    shown = capped(result.findings)
    ran = ", ".join(n for n in TOOLS if result.ran(n))
    if not shown:
        return f"# Scanner findings\n{ran} ran and reported nothing. Don't re-report what they check for."
    lines = [f"# Scanner findings (known — don't repeat them)\n{ran} ran. Already reported:"]
    for f in shown:
        lines.append(f"- [{f.severity}] {f.location}: {f.category or f.rule_id} ({f.tool})")
    hidden = len(result.findings) - len(shown)
    if hidden > 0:
        lines.append(f"- …and {hidden} more.")
    return "\n".join(lines)


def findings_of(scan: object) -> list[ToolFinding]:
    """A phase row's stored scan, as findings."""
    result = ScanResult.from_dict(scan if isinstance(scan, dict) else None)
    return result.findings if result is not None else []


__all__ = [
    "BANDIT",
    "DEPENDENCY_TOOLS",
    "NPM_AUDIT",
    "PIP_AUDIT",
    "SEMGREP",
    "TOOLS",
    "ScanResult",
    "ToolFinding",
    "capped",
    "dedupe",
    "findings_of",
    "judge",
    "plan_node",
    "plan_python",
    "prompt_block",
    "run_scan",
    "scan_build",
    "scan_tree",
]
