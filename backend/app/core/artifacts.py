"""Assemble a downloadable project from the agents' phase outputs.

Different agents emit files under different keys — backend/frontend use
`files: [{path, code}]`, QA uses `test_files`, DevOps uses `dockerfiles` /
`compose_or_manifests` / `ci_cd` with `content`. Rather than special-casing each,
we generically treat *any* list of `{path, code|content}` objects as files.

What comes out is a *build*, not a pile of files: each agent's file is placed under
the side it belongs to (`backend/`, `frontend/`), the platform's scaffold is added —
manifests, configs, the migration runner — and the mechanical fixes the scaffold makes
are recorded against the files they were made in. Every file says who wrote it, so
the file browser can offer a redo to its author and label the platform's own.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Iterator, Optional, Tuple

from app.core.constants import CODE_PHASES, PHASE_LABELS, BuildStatus, PhaseStatus
from app.db.models import Project

#: The phase name the platform's own files are attributed to.
PLATFORM = "platform"


def iter_files(output: dict) -> Iterator[Tuple[str, str, str]]:
    """Yield (path, content, language) for every file-like item in an agent output."""
    if not isinstance(output, dict):
        return
    for value in output.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            content = item.get("code") or item.get("content")
            if isinstance(path, str) and path.strip() and isinstance(content, str) and content.strip():
                lang = item.get("language") or item.get("framework") or item.get("tool") or ""
                yield path.strip().lstrip("/"), content, lang


#: Attempts that were superseded. Their output stays on the row — the history of
#: what a reviewer turned down is worth keeping — but it is not part of the build.
_SUPERSEDED = frozenset({PhaseStatus.REJECTED.value, PhaseStatus.FAILED.value})


def current_phases(project: Project) -> list:
    """The attempt that counts for each phase, newest first-class one per phase.

    A phase re-runs when it is sent back, so a project can hold several attempts at
    one phase. Merging them all was harmless while a rejection only happened at the
    phase you were looking at; per-file redo makes it routine, and the result was a
    .zip containing both the layout the reviewer rejected and the one that replaced
    it — `files` is keyed on path, so a renamed file does not overwrite its ghost.
    """
    return _current(project.phases)


def _current(rows) -> list:
    best: dict[str, object] = {}
    for ph in rows:
        if ph.status in _SUPERSEDED:
            continue
        current = best.get(ph.phase)
        if current is None or (ph.created_at, ph.id) >= (current.created_at, current.id):
            best[ph.phase] = ph
    kept = set(id(v) for v in best.values())
    # Keep the order the phases ran in, which is the order the rows are loaded.
    return [ph for ph in rows if id(ph) in kept]


def build_problems(project: Project) -> list[dict]:
    """Every compile problem still outstanding in the current build, phase by phase.

    Read from the database, not from `project.phases`. The runner's session loads that
    collection once and, with `expire_on_commit=False`, never sees a row added after —
    so asking it at the last phase answered for a build without its backend, and a
    build that did not compile was waved through as finished.
    """
    from sqlalchemy.orm import object_session

    from app.db.models import PhaseResult

    session = object_session(project)
    rows = (
        session.query(PhaseResult)
        .filter(PhaseResult.project_id == project.id)
        .order_by(PhaseResult.created_at, PhaseResult.id)
        .all()
        if session is not None
        else list(project.phases)
    )
    out: list[dict] = []
    for ph in _current(rows):
        if ph.phase not in CODE_PHASES or ph.build_status != BuildStatus.FAILED.value:
            continue
        for problem in ph.build_note or []:
            if isinstance(problem, dict):
                out.append({**problem, "phase": ph.phase})
    return out


def _language(path: str, given: str) -> str:
    if given:
        return given
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python", "js": "javascript", "jsx": "javascript", "mjs": "javascript",
        "cjs": "javascript", "ts": "typescript", "tsx": "typescript", "json": "json",
        "css": "css", "sql": "sql", "md": "markdown", "yml": "yaml", "yaml": "yaml",
    }.get(ext, "")


def assemble(project: Project) -> dict:
    """Collapse the current attempt at each phase into a placed, scaffolded build."""
    from app.build import layout
    from app.build.scaffold import build as scaffold_build, platform_owned
    from app.orchestration.charter import Charter

    charter = Charter.from_dict(project.charter)
    backend_language = charter.get("language").token if charter and charter.get("language") else None

    files: dict[str, dict] = {}  # placed path -> record (later phases win)
    setup: list[str] = []
    docs: list[dict] = []
    design: Optional[dict] = None
    replaced: list[str] = []

    phases = current_phases(project)
    for ph in phases:
        out = ph.output if isinstance(ph.output, dict) else {}
        if ph.phase == "system_design":
            design = out
        for path, content, lang in iter_files(out):
            placed = layout.place(ph.phase, path, content, backend_language)
            if not placed:
                continue
            files[placed] = {
                "path": placed,
                "content": content,
                "language": _language(placed, lang),
                "phase": ph.phase,
                "source_path": path,
                "notes": [],
            }

        instructions = out.get("setup_instructions")
        if isinstance(instructions, list):
            for step in instructions:
                if isinstance(step, str) and step.strip() and step not in setup:
                    setup.append(step.strip())

        if ph.content_md:
            docs.append(
                {
                    "path": f"docs/{ph.phase}.md",
                    "title": PHASE_LABELS.get(ph.phase, ph.phase),
                    "content": ph.content_md,
                }
            )

    pm = next((ph.output for ph in phases if ph.phase == "product_manager" and isinstance(ph.output, dict)), {})
    product = str(pm.get("product_name") or project.name or project.idea or "app")
    scaffold = scaffold_build(
        {p: f["content"] for p, f in files.items()}, charter, design, product
    )

    for path, (content, notes) in scaffold.rewrites.items():
        if path in files:
            files[path]["content"] = content
            files[path]["notes"] = [f"Platform {n}" for n in notes]

    for f in scaffold.files:
        existing = files.get(f.path)
        if existing is not None and existing["phase"] != PLATFORM:
            replaced.append(f.path)
        files[f.path] = {
            "path": f.path,
            "content": f.content,
            "language": _language(f.path, ""),
            "phase": PLATFORM,
            "source_path": None,
            "notes": [f.purpose]
            + (["Replaces the copy an agent wrote — the platform owns this file."] if existing else []),
        }
    # A platform-owned file the scaffold did not write (a `yarn.lock` next to the
    # platform's npm manifest) contradicts the manifest it sits beside.
    for path in [p for p, f in files.items() if f["phase"] != PLATFORM and platform_owned(p)]:
        replaced.append(path)
        del files[path]

    problems = build_problems(project)
    statuses = {ph.phase: ph.build_status for ph in phases if ph.phase in CODE_PHASES}
    for problem in problems:
        record = files.get(problem.get("path") or "")
        if record is not None:
            record.setdefault("problems", []).append(problem)
    if problems:
        state = BuildStatus.FAILED.value
    elif any(s == BuildStatus.UNCHECKED.value for s in statuses.values()):
        state = BuildStatus.UNCHECKED.value
    elif any(s == BuildStatus.OK.value for s in statuses.values()):
        state = BuildStatus.OK.value
    else:
        state = None  # nothing was checked: a build from before the gate, or no code yet

    return {
        "files": sorted(files.values(), key=lambda f: f["path"]),
        "setup_instructions": setup,
        "docs": docs,
        "scaffold": {**scaffold.as_dict(), "replaced": sorted(set(replaced))},
        "build": {
            "status": state,
            "phases": statuses,
            "problems": problems,
        },
    }


def readme_md(project: Project, assembled: dict) -> str:
    lines = [
        f"# {project.name or project.idea}",
        "",
        f"> {project.idea}",
        "",
        "Generated by the **AI Software Engineering Team** — a multi-agent build pipeline.",
        "",
    ]
    scaffold = assembled.get("scaffold") or {}
    if scaffold.get("commands"):
        lines += ["## Run it", "", "```sh"]
        lines += scaffold["commands"]
        lines += ["```", ""]
    if scaffold.get("notes"):
        lines += [f"> {note}" for note in scaffold["notes"]]
        lines.append("")
    build = assembled.get("build") or {}
    if build.get("status") == BuildStatus.FAILED.value:
        lines += [
            "## Known build problems",
            "",
            "The compile check still reports these after each was sent back once:",
            "",
        ]
        lines += [
            f"- `{p.get('path')}`" + (f" line {p.get('line')}" if p.get("line") else "") + f" — {p.get('message')}"
            for p in build.get("problems", [])
        ]
        lines.append("")
    if assembled["setup_instructions"]:
        lines += ["## Setup notes from the team", ""]
        lines += [f"{i}. {s}" for i, s in enumerate(assembled["setup_instructions"], 1)]
        lines.append("")
    if assembled["files"]:
        lines += ["## Files", ""]
        lines += [
            f"- `{f['path']}`" + (f" — {f['language']}" if f["language"] else "")
            + (" (platform)" if f.get("phase") == PLATFORM else "")
            for f in assembled["files"]
        ]
        lines.append("")
    if assembled["docs"]:
        lines += ["## Documentation", ""]
        lines += [f"- `{d['path']}` — {d['title']}" for d in assembled["docs"]]
        lines.append("")
    return "\n".join(lines)


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "project")[:48]).strip("-").lower()
    return s or "project"


def build_zip(project: Project, assembled: dict) -> bytes:
    """A real, unzippable project archive: README + code files + docs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", readme_md(project, assembled))
        for f in assembled["files"]:
            # An agent's own top-level README would be a second entry of the same
            # name, and which one an unzip keeps depends on the tool.
            path = "docs/README.from-agents.md" if f["path"] == "README.md" else f["path"]
            z.writestr(path, f["content"])
        for d in assembled["docs"]:
            z.writestr(d["path"], d["content"])
    return buf.getvalue()
