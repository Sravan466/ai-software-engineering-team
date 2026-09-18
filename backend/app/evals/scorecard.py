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
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

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


def score(db: Session, project: Project) -> dict:
    phases = artifacts.current_phases(project)
    statuses = [ph.schema_status for ph in phases]
    conforming = sum(1 for s in statuses if s in (SchemaStatus.VALID.value, SchemaStatus.REPAIRED.value))
    assembled = artifacts.assemble(project)
    agent_files = [f for f in assembled["files"] if f["phase"] != artifacts.PLATFORM]
    code_paths = [
        f["path"] for f in agent_files if f["phase"] in CODE_PHASES
    ]
    check = check_tree({f["path"]: f["content"] for f in assembled["files"]}, code_paths)
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
        "mockup": _mockup(db, project),
        "tokens": sum(ph.total_tokens or 0 for ph in phases),
    }
