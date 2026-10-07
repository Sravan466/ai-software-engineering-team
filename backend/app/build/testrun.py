"""Running the tests QA wrote, for real, and reading what they did (#76).

The Test Cases phase used to end with test files nobody ran and a coverage figure the
model invented. This runs them — in the sandbox the build step uses (#75), after the
same install — and reads the runner's own machine-readable report:

  * **Jest** — `--json --outputFile` for the results, `--coverage
    --coverageReporters=json-summary` for `coverage-summary.json`;
  * **Vitest** — its JSON reporter (the same shape as Jest's) and v8 coverage;
  * **Mocha** — its JSON reporter. Nothing measures coverage, and the result says so;
  * **pytest** — `pytest-json-report` and `pytest-cov`'s JSON report.

The reports stay in the container, so a few lines of Node or Python run after the
suite and print a condensed copy on one marked line of output, which is all that
comes back. A suite that can't be collected — a syntax error, an import that doesn't
resolve, a test for a runner the project doesn't have — counts as failed: a test that
cannot run is a defect of the test, never a pass and never "unchecked".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.build import buildlog
from app.build.check import Problem
from app.build.sandbox import NODE_IMAGE, PYTHON_IMAGE, Step, StepResult
from app.core.config import settings
from app.core.constants import TestStatus

#: The line the in-sandbox reader prints its condensed report on.
MARK = "@@AITEAM-TESTS@@"
_RESULTS = "/tmp/aiteam-results.json"
_COVERAGE_DIR = "/tmp/aiteam-coverage"
_PY_COVERAGE = "/tmp/aiteam-coverage.json"
#: Failures kept per run, each cut to its assertion and first frames, so the report
#: stays well inside the output the sandbox keeps. The rest are counted, not described.
MAX_FAILURES = 100
_TAIL_LINES = 40


def is_test(rel: str) -> bool:
    from app.build.scaffold import _is_test

    return _is_test(rel)


# ── the result ───────────────────────────────────────────────────────────────
@dataclass
class TestFailure:
    """One test that ran and failed."""

    __test__ = False

    path: str
    name: str
    message: str
    line: Optional[int] = None
    #: assertion | error | environment — `environment` is a test that reached for the
    #: network or a database the sandbox doesn't have: the test's to fix, not the code's.
    kind: str = "assertion"

    def as_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "message": self.message, "line": self.line, "kind": self.kind}


@dataclass
class Coverage:
    lines_pct: Optional[float]
    branches_pct: Optional[float]
    #: What measured it: jest | vitest (v8) | pytest-cov.
    tool: str

    def as_dict(self) -> dict:
        return {"lines_pct": self.lines_pct, "branches_pct": self.branches_pct, "tool": self.tool}


@dataclass
class TestRun:
    """One side's suite: what ran, what passed, and how much of the code it touched."""

    __test__ = False

    status: str
    side: str
    framework: Optional[str] = None
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    failures: list[TestFailure] = field(default_factory=list)
    coverage: Optional[Coverage] = None
    #: Why it didn't run, or why the coverage wasn't measured.
    reason: Optional[str] = None
    runner: Optional[str] = None
    image: Optional[str] = None
    seconds: float = 0.0
    steps: list[dict] = field(default_factory=list)
    #: What stops the suite running at all — a file that won't collect, a runner that
    #: crashed — as problems for QA, the way a compile error is a problem for its phase.
    problems: list[Problem] = field(default_factory=list)
    #: The test files this run was for, as tree paths.
    files: list[str] = field(default_factory=list)

    @classmethod
    def not_run(cls, side: str, reason: str, **kw) -> "TestRun":
        return cls(status=TestStatus.NOT_RUN.value, side=side, reason=reason, **kw)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errored + self.skipped

    def summary(self) -> str:
        """"24 passed · 2 failed · 61% lines (jest, 38 s)" — never a number nobody measured."""
        if self.status == TestStatus.NOT_RUN.value:
            return f"Not run: {_lower_first(self.reason or 'no test runner was available.')}"
        if self.total == 0:
            return f"Couldn't run: {_lower_first(self.reason or 'the suite failed before any test ran.')}"
        parts = []
        if self.passed or not (self.failed or self.errored):
            parts.append(f"{self.passed} passed")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.errored:
            parts.append(f"{self.errored} couldn't run")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.coverage is not None and self.coverage.lines_pct is not None:
            parts.append(f"{_pct(self.coverage.lines_pct)} lines")
        else:
            parts.append("coverage not measured")
        how = ", ".join(x for x in (self.framework, f"{round(self.seconds)} s" if self.seconds else None) if x)
        return " · ".join(parts) + (f" ({how})" if how else "")

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "side": self.side,
            "framework": self.framework,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "skipped": self.skipped,
            "total": self.total,
            "failures": [f.as_dict() for f in self.failures],
            "coverage": self.coverage.as_dict() if self.coverage else None,
            "summary": self.summary(),
            "reason": self.reason,
            "runner": self.runner,
            "image": self.image,
            "seconds": round(self.seconds, 1),
            "steps": self.steps,
            "problems": [p.as_dict() for p in self.problems],
            "files": self.files,
        }


def _pct(value: float) -> str:
    return f"{value:.0f}%" if value >= 10 or value == 0 else f"{value:.1f}%"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def combine(runs: list[TestRun], reason: Optional[str] = None) -> dict:
    """Every side's run as the one record a phase keeps: `{status, summary, runs}`.

    Failed if any side failed, ok only if every side that has tests ran green, and
    not run when nothing ran — with the reason, so the card never shows a figure.
    """
    if not runs:
        why = reason or "QA wrote no test files."
        return {
            "status": TestStatus.NOT_RUN.value,
            "summary": f"Not run: {_lower_first(why)}",
            "reason": why,
            "runs": [],
            "at": _now(),
        }
    statuses = {r.status for r in runs}
    if TestStatus.FAILED.value in statuses:
        status = TestStatus.FAILED.value
    elif statuses == {TestStatus.OK.value}:
        status = TestStatus.OK.value
    elif TestStatus.OK.value in statuses:
        # One side ran green and the other couldn't run: what ran passed, and the
        # rest says why it didn't.
        status = TestStatus.OK.value
    else:
        status = TestStatus.NOT_RUN.value
    ran = [r for r in runs if r.status != TestStatus.NOT_RUN.value]
    passed = sum(r.passed for r in ran)
    failed = sum(r.failed for r in ran)
    errored = sum(r.errored for r in ran)
    if status == TestStatus.NOT_RUN.value:
        summary = runs[0].summary()
        why = runs[0].reason
    elif len(runs) == 1:
        summary = runs[0].summary()
        why = None
    else:
        bits = [f"{passed} passed"]
        if failed:
            bits.append(f"{failed} failed")
        if errored:
            bits.append(f"{errored} couldn't run")
        summary = " · ".join(bits) + f" across {len(ran)} suite{'s' if len(ran) != 1 else ''}"
        why = None
    return {
        "status": status,
        "summary": summary,
        "reason": why,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "runs": [r.as_dict() for r in runs],
        "at": _now(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── what to run ──────────────────────────────────────────────────────────────
@dataclass
class TestPlan:
    __test__ = False

    framework: str
    image: str
    steps: list[Step]
    manifest: str


#: Prints the condensed report: counts, failures, suites that never ran, coverage.
#: Reads Jest's and Vitest's shared JSON shape, and Mocha's.
_NODE_READER = r"""
const fs = require('fs');
const [kind, results, coverage] = process.argv.slice(2);
const read = (p) => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch (e) { return null; } };
const clip = (s, n) => String(s || '').slice(0, n);
const out = { framework: kind, ran: false, passed: 0, failed: 0, errored: 0, skipped: 0, failures: [], suites: [], coverage: null };
const r = read(results);
if (r && kind === 'mocha') {
  out.ran = true;
  const st = r.stats || {};
  out.passed = st.passes || 0; out.failed = st.failures || 0; out.skipped = st.pending || 0;
  for (const f of (r.failures || []).slice(0, MAX)) {
    out.failures.push({ path: f.file || '', name: f.fullTitle || f.title || '', message: clip(f.err && (f.err.stack || f.err.message), 600) });
  }
} else if (r) {
  out.ran = true;
  out.passed = r.numPassedTests || 0; out.failed = r.numFailedTests || 0;
  out.skipped = (r.numPendingTests || 0) + (r.numTodoTests || 0);
  for (const s of r.testResults || []) {
    const asserts = s.assertionResults || [];
    if (s.status === 'failed' && !asserts.some((a) => a.status === 'failed')) {
      out.errored += 1;
      if (out.suites.length < 10) out.suites.push({ path: s.name || '', message: clip(s.message || s.failureMessage, 1500) });
    }
    for (const a of asserts) {
      if (a.status !== 'failed' || out.failures.length >= MAX) continue;
      out.failures.push({ path: s.name || '', name: a.fullName || a.title || '', message: clip((a.failureMessages || []).join('\n'), 600), line: a.location ? a.location.line : null });
    }
  }
}
const c = read(coverage);
if (c && c.total) {
  const pct = (m) => (m && typeof m.pct === 'number' && m.total > 0 ? m.pct : null);
  out.coverage = { lines_pct: pct(c.total.lines), branches_pct: pct(c.total.branches) };
}
process.stdout.write('\nMARK' + JSON.stringify(out) + '\n');
""".replace("MAX", str(MAX_FAILURES)).replace("MARK", MARK)

#: The same for pytest-json-report and pytest-cov.
_PY_READER = r"""
import json, sys
results, coverage = sys.argv[1:3]
def read(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None
out = {"framework": "pytest", "ran": False, "passed": 0, "failed": 0, "errored": 0, "skipped": 0,
       "failures": [], "suites": [], "coverage": None}
r = read(results)
if r:
    out["ran"] = True
    s = r.get("summary") or {}
    out["passed"] = int(s.get("passed") or 0) + int(s.get("xpassed") or 0)
    out["failed"] = int(s.get("failed") or 0)
    out["skipped"] = int(s.get("skipped") or 0) + int(s.get("xfailed") or 0)
    for t in r.get("tests") or []:
        if t.get("outcome") not in ("failed", "error"):
            continue
        stage = next((t.get(k) for k in ("call", "setup", "teardown") if (t.get(k) or {}).get("outcome") == "failed"), None) or {}
        crash = stage.get("crash") or {}
        node = str(t.get("nodeid") or "")
        if len(out["failures"]) < MAX:
            out["failures"].append({
                "path": node.split("::")[0], "name": node.split("::", 1)[-1],
                "message": str(stage.get("longrepr") or crash.get("message") or "")[-600:],
                "line": crash.get("lineno") if str(crash.get("path") or "").endswith(node.split("::")[0]) else
                        ((t.get("lineno") or 0) + 1 or None),
                "error": t.get("outcome") == "error",
            })
    errors = sum(1 for t in r.get("tests") or [] if t.get("outcome") == "error")
    for c in r.get("collectors") or []:
        if c.get("outcome") == "failed":
            errors += 1
            if len(out["suites"]) < 10:
                out["suites"].append({"path": str(c.get("nodeid") or ""), "message": str(c.get("longrepr") or "")[-1500:]})
    out["errored"] = max(errors, int(s.get("error") or 0))
c = read(coverage)
if c and c.get("totals"):
    t = c["totals"]
    lines = t.get("num_statements") or 0
    branches = t.get("num_branches") or 0
    out["coverage"] = {
        "lines_pct": round(100.0 * (t.get("covered_lines") or 0) / lines, 1) if lines else None,
        "branches_pct": round(100.0 * (t.get("covered_branches") or 0) / branches, 1) if branches else None,
    }
sys.stdout.write("\nMARK" + json.dumps(out) + "\n")
""".replace("MAX", str(MAX_FAILURES)).replace("MARK", MARK)

#: What a JavaScript suite's report is read with, written into the box's /tmp first.
_WRITE_NODE_READER = f"cat > /tmp/aiteam-read.cjs <<'AITEAM_EOF'\n{_NODE_READER}\nAITEAM_EOF\n"
_WRITE_PY_READER = f"cat > /tmp/aiteam-read.py <<'AITEAM_EOF'\n{_PY_READER}\nAITEAM_EOF\n"

#: Coverage of the backend's own modules: not the packages installed beside them, not
#: the tests.
_COVERAGE_RC = "printf '[run]\\nomit =\\n    .deps/*\\n    tests/*\\n    */tests/*\\n    conftest.py\\n' > /tmp/aiteam-cov.rc\n"

_COMMANDS = {
    "jest": (
        "npm test --silent -- --ci --json --outputFile={results} --testLocationInResults "
        "--coverage --coverageReporters=json-summary --coverageDirectory={cov} --forceExit"
    ),
    "vitest": (
        "npm test --silent -- --reporter=default --reporter=json --outputFile.json={results} "
        "--coverage.enabled=true --coverage.provider=v8 --coverage.reporter=json-summary "
        # Vitest reports coverage only for a green run unless told otherwise.
        "--coverage.reportsDirectory={cov} --coverage.reportOnFailure=true"
    ),
    # The JSON reporter's own file: stdout is the app's, and a `console.log` in it
    # mustn't corrupt the report.
    "mocha": "npm test --silent -- --reporter json --reporter-option output={results}",
}


def _test_files(files: dict[str, str]) -> list[str]:
    return sorted(
        p for p in files
        if is_test(p) and not p.endswith("conftest.py") and p.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))
    )


#: Why there is no plan, when there is none: nothing to run, the tests' own fault (a
#: suite in the wrong language — QA can fix it), or no runner set up for this side,
#: which QA can't change.
NOTHING, TESTS_FAULT, NO_RUNNER = "nothing", "tests", "no_runner"


def plan_tests(files: dict[str, str], side: str) -> tuple[Optional[TestPlan], Optional[str], str]:
    """The steps that install one side and run its suite: (plan, why none, whose fault).

    `files` are relative to the side. Both faults count as failed — a suite that can't
    run is never a pass — but only `TESTS_FAULT` is sent back to QA.
    """
    from app.build.runner import _NPM_INSTALL, _NPM_SCRIPTS, _env_example, _json

    tests = _test_files(files)
    if not tests:
        return None, "There are no tests for this side.", NOTHING
    env = _env_example(files)
    budget = max(int(settings.build_run_timeout_seconds), 30)
    if "package.json" in files:
        js = [t for t in tests if not t.endswith(".py")]
        if not js:
            return None, f"The tests here are Python, but the {side} is JavaScript.", TESTS_FAULT
        script = str((_json(files["package.json"]).get("scripts") or {}).get("test") or "")
        framework = next((f for f in ("vitest", "mocha", "jest") if re.search(rf"\b{f}\b", script)), None)
        if framework is None:
            return None, f"The {side} has no test runner set up, so its tests can't run.", NO_RUNNER
        command = (
            _WRITE_NODE_READER
            + _COMMANDS[framework].format(results=_RESULTS, cov=_COVERAGE_DIR)
            + f"; code=$?; node /tmp/aiteam-read.cjs {framework} {_RESULTS} {_COVERAGE_DIR}/coverage-summary.json"
            + "; exit $code"
        )
        steps = [_NPM_INSTALL, _NPM_SCRIPTS,
                 Step("test", f"{framework} (tests)", command, timeout=budget, env={**env, "NODE_ENV": "test"})]
        return TestPlan(framework, NODE_IMAGE, steps, "package.json"), None, NOTHING
    if "requirements.txt" in files:
        py = [t for t in tests if t.endswith(".py")]
        if not py:
            return None, f"The tests here are JavaScript, but the {side} is Python.", TESTS_FAULT
        if not re.search(r"(?im)^pytest\b", files["requirements.txt"]):
            return None, f"The {side} doesn't install pytest, so its tests can't run.", NO_RUNNER
        command = (
            _WRITE_PY_READER
            + _COVERAGE_RC
            # One file that won't import mustn't hide what every other file's tests say.
            + f"python -m pytest -p no:cacheprovider -q --continue-on-collection-errors "
            f"--json-report --json-report-file={_RESULTS} "
            "--json-report-omit log streams warnings keywords "
            f"--cov=. --cov-branch --cov-config=/tmp/aiteam-cov.rc --cov-report=json:{_PY_COVERAGE}"
            f"; code=$?; python /tmp/aiteam-read.py {_RESULTS} {_PY_COVERAGE}; exit $code"
        )
        steps = [
            Step("install", "pip install", "pip install --no-input --prefer-binary --target /work/.deps -r requirements.txt",
                 network=True),
            Step("test", "pytest (tests)", command, timeout=budget,
                 env={**env, "PYTHONPATH": "/work:/work/.deps"}),
        ]
        return TestPlan("pytest", PYTHON_IMAGE, steps, "requirements.txt"), None, NOTHING
    return None, f"Nothing here can run the {side}'s tests yet.", NO_RUNNER


# ── reading what it did ──────────────────────────────────────────────────────
def _report(output: str) -> Optional[dict]:
    for line in reversed(buildlog.clean(output).splitlines()):
        if line.startswith(MARK):
            try:
                data = json.loads(line[len(MARK):])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _tail(output: str) -> str:
    kept = [l.rstrip() for l in buildlog.clean(output).splitlines() if not l.startswith(MARK)]
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept[-_TAIL_LINES:])


def _side_path(path: str, side: str) -> str:
    """A path from a report, as a tree path: `/work/__tests__/a.test.js` → `frontend/__tests__/a.test.js`."""
    rel = buildlog.rel(path or "")
    return f"{side}/{rel}" if rel and not rel.startswith(f"{side}/") else rel


def _message(raw: str, limit: int = 600) -> str:
    """The part of a failure a person (and a model) needs: the assertion, not the frames."""
    text = buildlog.clean(raw or "").strip()
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    # pytest: the `E   assert 404 == 201` lines say it.
    e_lines = [l.strip()[1:].strip() for l in lines if re.match(r"^\s*E\s", l)]
    if e_lines:
        text = "\n".join(e_lines[:4])
    else:
        kept = [l for l in lines if not re.match(r"^\s*at\s|^\s*\d+\s*\|", l)]
        text = "\n".join(kept[:10])
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line(raw: str, rel: str) -> Optional[int]:
    """The line of the test file the failure points at, from its stack."""
    base = re.escape(rel.rsplit("/", 1)[-1])
    m = re.search(rf"{base}:(\d+)(?::\d+)?", buildlog.clean(raw or ""))
    return int(m.group(1)) if m else None


def _suite_problem(path: str, raw: str, side: str) -> Problem:
    text = buildlog.clean(raw or "").strip()
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    # pytest's `E   ModuleNotFoundError: …` says it; Jest's and Vitest's first error line.
    e_lines = [l.strip()[1:].strip() for l in lines if re.match(r"^\s*E\s", l)]
    lead = e_lines[-1] if e_lines else next(
        (l.strip() for l in lines if re.search(r"Error|Cannot find|Could not|not defined|No module", l)
         and not l.strip().startswith(("at ", "●"))),
        lines[0].strip() if lines else "The test file failed before any test in it ran.",
    )
    tree = _side_path(path, side)
    return Problem(
        tree,
        f"The test file couldn't run: {lead[:300]}",
        "test",
        _line(raw, tree),
        "test",
    )


def judge(plan: TestPlan, results: list[StepResult], side: str, files: list[str]) -> TestRun:
    """The steps' outcome read into a `TestRun`: counts, failures, coverage, problems."""
    steps = [
        {"name": r.name, "label": r.label, "exit_code": r.exit_code, "seconds": round(r.seconds, 1),
         "ok": r.ok, "timed_out": r.timed_out, "skipped": r.skipped, "tail": _tail(r.output)}
        for r in results
    ]
    run = TestRun(status=TestStatus.FAILED.value, side=side, framework=plan.framework, image=plan.image,
                  steps=steps, seconds=sum(r.seconds for r in results), files=files)
    first = files[0] if files else f"{side}/{plan.manifest}"
    for r in results:
        if r.name != "install":
            continue
        if r.skipped:
            return _not_run(run, r.output or "The tests were stopped before they ran.")
        if r.timed_out:
            return _not_run(run, f"`{r.label}` ran out of time, so the tests weren't run.")
        if r.ok:
            continue
        if r.exit_code == 125 and re.search(r"^docker: |Error response from daemon", r.output, re.MULTILINE):
            return _not_run(run, f"Docker couldn't start `{r.label}`, so the tests weren't run.")
        if buildlog.environmental(r.output) and not r.label.startswith("install scripts"):
            return _not_run(run, "The package registry couldn't be reached, so the tests weren't run.")
        found = (buildlog.pip_problems(r.output, plan.manifest) if plan.framework == "pytest"
                 else buildlog.npm_problems(r.output, plan.manifest)) or [
            buildlog.tail_problem(r.output, plan.manifest, f"`{r.label}`")]
        run.problems = [Problem(_side_path(p.path, side), f"With the tests' packages added, {_lower_first(p.message)}",
                                "test", p.line, "test") for p in found]
        run.reason = "The tests' packages didn't install."
        return run
    test = next((r for r in results if r.name == "test"), None)
    if test is None or test.skipped:
        return _not_run(run, (test.output if test is not None else "") or "The tests were stopped before they ran.")
    if test.exit_code == 125 and re.search(r"^docker: |Error response from daemon", test.output, re.MULTILINE):
        return _not_run(run, "Docker couldn't start the test run, so the tests weren't run.")
    report = _report(test.output)
    if test.timed_out and not (report and report.get("ran")):
        run.problems = [Problem(first, f"The suite didn't finish in {round(test.seconds)} s. A test that never "
                                       "ends — an open server, a timer, a real network call — counts as failed.",
                                "test", None, "test")]
        run.reason = "The suite ran out of time."
        return run
    if test.exit_code == 137 and not (report and report.get("ran")):
        return _not_run(run, f"The test run ran out of memory ({settings.build_run_memory_mb} MB).")
    if not report or not report.get("ran"):
        # The runner never got as far as a report: a broken config, a missing runner,
        # a crash on start. The suite can't run, which is a failure, not "unchecked".
        run.problems = [Problem(first, f"The {plan.framework} run crashed before any test ran. Its last "
                                       f"lines:\n{_tail(test.output)[-1600:] or '(no output)'}",
                                "test", None, "test")]
        run.reason = "The test runner crashed before any test ran."
        return run

    run.passed = int(report.get("passed") or 0)
    run.failed = int(report.get("failed") or 0)
    run.errored = int(report.get("errored") or 0)
    run.skipped = int(report.get("skipped") or 0)
    for s in report.get("suites") or []:
        run.problems.append(_suite_problem(str(s.get("path") or first), str(s.get("message") or ""), side))
    for f in report.get("failures") or []:
        raw = str(f.get("message") or "")
        path = _side_path(str(f.get("path") or first), side)
        env = buildlog.environmental(raw)
        if env:
            kind = "environment"
        elif f.get("error") or _broke_itself(raw, path, side):
            # A pytest *error* is a fixture or setup that broke before the test body;
            # an undefined name thrown from the test file is the test's own mistake.
            # Either way it is the test's to fix, not the code's.
            kind = "error"
        else:
            kind = "assertion"
        # The failing line in the test file, from its stack; else where the test starts.
        line = _line(raw, path) or (f.get("line") if isinstance(f.get("line"), int) and f.get("line") else None)
        run.failures.append(TestFailure(path, str(f.get("name") or "a test"), _message(raw) or "failed", line, kind))
    cov = report.get("coverage")
    if isinstance(cov, dict) and (cov.get("lines_pct") is not None or cov.get("branches_pct") is not None):
        tool = {"pytest": "pytest-cov", "vitest": "vitest (v8)"}.get(plan.framework, plan.framework)
        run.coverage = Coverage(_num(cov.get("lines_pct")), _num(cov.get("branches_pct")), tool)
    elif plan.framework == "mocha":
        run.reason = "Mocha has no coverage tool in this build, so coverage wasn't measured."
    if run.total == 0 and not run.problems:
        run.problems = [Problem(first, f"{plan.framework} found no tests to run. Name them so it finds them "
                                       "(test_*.py with test_ functions, or *.test.js with it()/test()).",
                                "test", None, "test")]
    if run.failed or run.errored or run.problems or run.total == 0:
        run.status = TestStatus.FAILED.value
    else:
        run.status = TestStatus.OK.value
    return run


#: Mistakes a test makes in its own body: a name it never defined or imported, code
#: that doesn't parse, a module it can't import. Not a TypeError — "undefined is not
#: an object" in a test is as often the code returning the wrong thing.
_OWN_JS = re.compile(r"^\s*(?:Error:\s*)?(?:Uncaught\s+)?(ReferenceError|SyntaxError)\b")
_OWN_PY = frozenset({"NameError", "SyntaxError", "ImportError", "ModuleNotFoundError", "IndentationError"})
_PY_CRASH = re.compile(r"^(?P<path>[\w./\-]+\.py):(?P<line>\d+): (?P<exc>\w+)\s*$", re.MULTILINE)


def _broke_itself(raw: str, path: str, side: str) -> bool:
    """Whether a failure was thrown by the test file's own code, before any assertion."""
    text = buildlog.clean(raw or "")
    if path.endswith(".py"):
        crashes = list(_PY_CRASH.finditer(text))
        if not crashes:
            return False
        last = crashes[-1]
        return last.group("exc") in _OWN_PY and _side_path(last.group("path"), side) == path
    first = next((l for l in text.splitlines() if l.strip()), "")
    if not _OWN_JS.match(first):
        return False
    for m in buildlog._JS_FRAME.finditer(text):
        frame = buildlog.rel(m.group("path"))
        if frame.startswith("node_modules/") or "/node_modules/" in frame:
            continue
        return _side_path(frame, side) == path
    return False


def _num(value: object) -> Optional[float]:
    try:
        return round(float(value), 1) if value is not None else None
    except (TypeError, ValueError):
        return None


def _not_run(run: TestRun, reason: str) -> TestRun:
    run.status = TestStatus.NOT_RUN.value
    run.reason = reason
    return run


# ── the test a fix note quotes ───────────────────────────────────────────────
def test_source(files: dict[str, str], path: str, name: str, limit: int = 1400) -> Optional[str]:
    """The body of one failing test, from the file QA wrote, for the engineer's note.

    `files` is the phase's own `{path: code}`; `path` is a tree path, so it is matched
    on its tail (QA may have written `tests/test_api.py` for `backend/tests/test_api.py`).
    """
    content = next(
        (c for p, c in files.items() if path == p or path.endswith("/" + p.lstrip("/")) or p.endswith("/" + path)),
        None,
    )
    if not content:
        return None
    lines = content.splitlines()
    if path.endswith(".py"):
        return _python_test(lines, name, limit)
    return _js_test(lines, name, limit)


def _python_test(lines: list[str], name: str, limit: int) -> Optional[str]:
    func = re.sub(r"\[.*\]$", "", name.split("::")[-1]).strip()
    for i, line in enumerate(lines):
        m = re.match(rf"^(\s*)(?:async\s+)?def\s+{re.escape(func)}\s*\(", line)
        if not m:
            continue
        indent = len(m.group(1))
        body = [line]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent and not nxt.lstrip().startswith(("#", ")")):
                break
            body.append(nxt)
        return _clip("\n".join(body).rstrip(), limit)
    return None


def _js_test(lines: list[str], name: str, limit: int) -> Optional[str]:
    best: Optional[tuple[int, int]] = None
    for i, line in enumerate(lines):
        m = re.search(r"\b(?:it|test)(?:\.\w+)?\(\s*(['\"`])(.+?)\1", line)
        if m and name.endswith(m.group(2)) and (best is None or len(m.group(2)) > best[1]):
            best = (i, len(m.group(2)))
    if best is None:
        return None
    start = best[0]
    depth, body = 0, []
    for line in lines[start:start + 60]:
        body.append(line)
        depth += line.count("{") + line.count("(") - line.count("}") - line.count(")")
        if depth <= 0 and len(body) > 1:
            break
    return _clip("\n".join(body).rstrip(), limit)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
