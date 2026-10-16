"""One number per property the issues were written about, for one finished build.

There was no way to tell whether a prompt change made builds better or worse — every
figure in the issues that started this was measured by hand, once. These are the same
measurements, taken the same way every time:

  schema conformance   share of phases whose output matched its declared shape
  charter coherence    phases that contradict the frozen stack (0 is coherent)
  files / bytes        what the agents wrote, apart from the platform's scaffold
  compiles             the compile gate, re-run over the whole assembled tree
  mockup               pages, sections, how many fell back, whether every check held
  console errors       from the mockup's headless render, when one ran

and, since #80, whether each agent used what it was handed:

  names_used_pct        registry names (entities, endpoint paths) present in the code
  endpoints_wired_pct   frontend calls a backend route actually serves
  criteria_covered_pct  P0 acceptance criteria with a test named after them
  digest_present        per phase: was every dependency handed over with its digest
  truncated_replies     replies the output limit cut off, across every phase

and, since #81, how the code phases wrote their code:

  generation_mode       per code phase: one | batch | whole (file by file, in batches,
                        or the old single JSON reply)
  calls_per_phase       model calls each phase made, repairs included
  files_planned         files the code phases' plans listed
  files_written         of those, files that were written
  compile_by_path       ok | failed for every file the code phases wrote

and, since #101, code that parses and is not finished:

  placeholders          stubs, comments standing in for code, TODOs, lorem ipsum,
                        stand-in values, empty files and refusals, over every code file
  weak_tests            tests that assert nothing (or only what is always true), are
                        skipped, or mock the module they test

  export_mismatches     imports of a name a module doesn't export, or of a default it
                        doesn't have (TypeScript 2305/2724/2613/2614/1192) — what showing
                        each write call the exports a file really has is meant to cut

and, since #76, what running QA's tests measured — never the model's own estimate:

  tests_passed_pct      passed / tests that ran, across every suite; None if none ran
  coverage_lines_pct    line coverage the runner measured (the lowest suite's, when
                        there are two); None when nothing measured it

and, since #103, whether the crew's fix rounds on code converge:

  fix_rounds            code fix rounds the build took, across every phase
  fix_trend             per code phase, its latest episode: the problems before the first
                        round, then after each one — 3 → 6 → 4 is a loop that isn't
                        converging
"""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session

from app.agents import handoff
from app.build import layout, placeholders, routes
from app.build.check import check_tree
from app.core import artifacts
from app.core.constants import CODE_PHASES, SchemaStatus, StackStatus
from app.db.models import PreviewRevision, Project


def _mockup(db: Session, project: Project) -> Optional[dict]:
    row = (
        db.query(PreviewRevision)
        .filter(PreviewRevision.project_id == project.id, PreviewRevision.report.isnot(None))
        .order_by(PreviewRevision.created_at.desc())
        .first()
    )
    if row is None:
        return None
    report = row.report or {}
    checks = report.get("checks") or []
    render = report.get("render") or {}
    return {
        "bytes": report.get("bytes"),
        "routes": len(report.get("routes") or []),
        "sections": len(report.get("sections") or []),
        "fallback_sections": (report.get("counts") or {}).get("fallback"),
        "checks_passed": sum(1 for c in checks if c.get("ok")),
        "checks_total": len(checks),
        "renders": bool(checks) and all(c.get("ok") for c in checks),
        "console_errors": render.get("console_errors") if render.get("ran") else None,
        "calls": report.get("calls"),
    }


_STOP = frozenset(
    "that this with from into when then they them their there have has will should "
    "would could each every user users page item items shows show able given".split()
)
_TEST_NAME = re.compile(
    r"""def\s+(test_\w+)|\b(?:it|test|describe)\(\s*['"`]([^'"`]{3,200})['"`]"""
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def _pct(hit: int, total: int) -> Optional[float]:
    return round(hit / total, 3) if total else None


def names_used(outputs: dict, files: dict[str, str]) -> Optional[float]:
    """Share of registry names the code uses: entities by name, endpoints as served."""
    reg = handoff.registry(outputs)
    if not reg:
        return None
    code = "\n".join(c for p, c in files.items() if layout.side_of(p)).lower()
    served = routes.served(files, layout.BACKEND)
    hits = sum(1 for e in reg.entities if e.lower() in code)
    for path in reg.paths:
        call = routes.Call(path, "", 0)
        hits += 1 if routes.is_served(call, served) else 0
    return _pct(hits, len(reg.entities) + len(reg.paths))


def endpoints_wired(files: dict[str, str]) -> Optional[float]:
    """Share of frontend calls a backend (or the frontend's own API) route serves."""
    found = routes.calls(files)
    if not found:
        return None
    known = routes.served(files, layout.BACKEND) + routes.frontend_api(files)
    return _pct(sum(1 for c in found if routes.is_served(c, known)), len(found))


def criteria_covered(outputs: dict, files: dict[str, str]) -> Optional[float]:
    """Share of P0 acceptance criteria with a test whose name says most of it."""
    pm = handoff.digest("product_manager", outputs.get("product_manager") or {})
    criteria = [c for s in pm.get("p0_stories", []) for c in s.get("acceptance_criteria", [])]
    if not criteria:
        return None
    tests = []
    for path, content in files.items():
        if "test" not in path.lower():
            continue
        for m in _TEST_NAME.finditer(content or ""):
            tests.append(_words((m.group(1) or m.group(2) or "").replace("_", " ")))
    covered = 0
    for criterion in criteria:
        want = _words(criterion)
        if len(want) < 2:
            continue
        if any(len(want & t) >= max(2, (len(want) + 1) // 2) for t in tests):
            covered += 1
    return _pct(covered, len(criteria))


def score(db: Session, project: Project) -> dict:
    phases = artifacts.current_phases(project)
    statuses = [ph.schema_status for ph in phases]
    conforming = sum(1 for s in statuses if s in (SchemaStatus.VALID.value, SchemaStatus.REPAIRED.value))
    assembled = artifacts.assemble(project)
    agent_files = [f for f in assembled["files"] if f["phase"] != artifacts.PLATFORM]
    code_paths = [
        f["path"] for f in agent_files if f["phase"] in CODE_PHASES
    ]
    tree = {f["path"]: f["content"] for f in assembled["files"]}
    check = check_tree(tree, code_paths)
    outputs = {ph.phase: ph.output for ph in phases if isinstance(ph.output, dict)}
    agent_tree = {f["path"]: f["content"] for f in agent_files}
    return {
        "project_id": project.id,
        "idea": project.idea,
        "status": project.status,
        "schema_conformance": round(conforming / len(statuses), 3) if statuses else None,
        "schema_invalid": [ph.phase for ph in phases if ph.schema_status == SchemaStatus.INVALID.value],
        "charter_violations": sum(1 for ph in phases if ph.stack_status == StackStatus.VIOLATED.value),
        "files": len(agent_files),
        "bytes": sum(len(f["content"].encode("utf-8")) for f in agent_files),
        "platform_files": len(assembled["files"]) - len(agent_files),
        "compiles": check.status,
        "compile_problems": len(check.problems),
        "compile_unchecked": len(check.unchecked),
        "export_mismatches": check.export_misses,
        # Uncapped, unlike the gate's list: a count, not a repair prompt.
        **placeholders.counts(placeholders.placeholder_problems(tree, code_paths, cap=None)),
        "mockup": _mockup(db, project),
        "tokens": sum(ph.total_tokens or 0 for ph in phases),
        "names_used_pct": names_used(outputs, agent_tree),
        "endpoints_wired_pct": endpoints_wired(agent_tree),
        "criteria_covered_pct": criteria_covered(outputs, agent_tree),
        "digest_present": {
            ph.phase: all(d.get("digest") for d in (ph.handoff or {}).get("deps", []))
            for ph in phases
            if getattr(ph, "handoff", None) is not None
        },
        "truncated_replies": sum(
            int((getattr(ph, "handoff", None) or {}).get("truncated_replies") or 0) for ph in phases
        ),
        **generation(db, project, phases, code_paths, check),
        **tests(phases),
        **fix_rounds(project),
    }


def fix_rounds(project: Project) -> dict:
    """How many code fix rounds the build took, and the problems left after each (#103).

    A round's problems are every problem the attempt before it had, so round n+1's are
    what round n left. After the last: what it stopped on, what a person accepted, or
    none once the phase came back clean. The trend is the latest episode's: a problem
    that came back after the phase was clean starts a new one."""
    from app.orchestration import autofix

    data = autofix.load(project)
    trend: dict[str, list] = {}
    total = 0
    for name, t in data["tracks"].items():
        rounds = t.get("rounds") or []
        if not name.startswith(autofix.BUILD_PREFIX) or not rounds:
            continue
        total += len(rounds)
        episode = rounds[int(t.get("episode_start") or 0):] or rounds[-1:]
        counts: list = [len(r.get("problems") or []) for r in episode]
        last = episode[-1]
        if t.get("stopped"):
            counts.append(int(t["stopped"].get("left") or 0))
        elif t.get("accepted"):
            counts.append(len(t["accepted"].get("keys") or []))
        elif last.get("fixed") is not None:
            counts.append(0)
        trend[name[len(autofix.BUILD_PREFIX):]] = counts
    return {"fix_rounds": total, "fix_trend": trend}


def tests(phases: list) -> dict:
    """What running QA's suite measured (#76). None where nothing was measured."""
    qa = next((ph for ph in phases if ph.phase == "qa_engineer"), None)
    run = getattr(qa, "test_run", None) if qa is not None else None
    runs = [r for r in (run or {}).get("runs") or [] if r.get("status") != "not_run"] if isinstance(run, dict) else []
    total = sum(int(r.get("passed") or 0) + int(r.get("failed") or 0) + int(r.get("errored") or 0) for r in runs)
    passed = sum(int(r.get("passed") or 0) for r in runs)
    measured = [
        float(r["coverage"]["lines_pct"])
        for r in runs
        if isinstance(r.get("coverage"), dict) and r["coverage"].get("lines_pct") is not None
    ]
    return {
        "tests_passed_pct": _pct(passed, total),
        "coverage_lines_pct": min(measured) if measured else None,
    }


def generation(db: Session, project: Project, phases: list, code_paths: list[str], check) -> dict:
    """How the code phases wrote their code (#81), and what each call bought."""
    from sqlalchemy import func

    from app.db.models import UsageEvent

    records = {
        ph.phase: ((getattr(ph, "handoff", None) or {}).get("generation") or {})
        for ph in phases
        if ph.phase in CODE_PHASES
    }
    try:
        rows = (
            db.query(UsageEvent.phase, func.count(UsageEvent.id))
            .filter(UsageEvent.project_id == project.id)
            .group_by(UsageEvent.phase)
            .all()
        )
        calls = {phase: n for phase, n in rows if phase}
    except Exception:  # noqa: BLE001 - a scoring stand-in with no database
        calls = {}
    failed = {p.path for p in check.problems}
    return {
        "generation_mode": {phase: r.get("mode") for phase, r in records.items() if r.get("mode")},
        "calls_per_phase": calls,
        "files_planned": sum(int(r.get("files_planned") or 0) for r in records.values()),
        "files_written": sum(int(r.get("files_written") or 0) for r in records.values()),
        "compile_by_path": {path: ("failed" if path in failed else "ok") for path in code_paths},
    }

