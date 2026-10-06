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

from app.core.constants import CODE_PHASES, PHASE_LABELS, PHASE_ORDER, BuildStatus, PhaseStatus
from app.db.models import Project

#: The phase name the platform's own files are attributed to.
PLATFORM = "platform"



def iter_files(output: dict) -> Iterator[Tuple[str, str, str]]:
    """Yield (path, content, language) for every file-like item in an agent output."""
    if not isinstance(output, dict):
        return
    for value in output.values():
        # A lone file (DevOps's `ci_workflow`) is a file too.
        if isinstance(value, dict) and "path" in value:
            value = [value]
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

    try:
        session = object_session(project)
    except Exception:  # noqa: BLE001 - not an ORM instance (a scoring stand-in)
        session = None
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


def devops_may_write(path: str, output: dict) -> bool:
    """Whether a DevOps file may ship. Never one the platform writes — `render.yaml`
    above all, which used to replace the platform's Blueprint — and never a workflow
    that runs in the person's repository unless it is the checked `ci_workflow`."""
    from app.agents.devops_engineer import workflow_problems
    from app.build.scaffold import platform_owned

    p = path.strip().lstrip("/")
    name = p.rsplit("/", 1)[-1]
    if p == "render.yaml" or name in ("package.json", "package-lock.json") or platform_owned(p):
        return False
    if p.startswith(".github/"):
        ci = output.get("ci_workflow") if isinstance(output, dict) else None
        return (
            isinstance(ci, dict)
            and str(ci.get("path") or "").strip().lstrip("/") == p
            and not workflow_problems(ci)
        )
    return True


def assemble(project: Project) -> dict:
    """Collapse the current attempt at each phase into a placed, scaffolded build."""
    from app.build import layout
    from app.build.scaffold import build as scaffold_build, platform_owned, superseded
    from app.orchestration.charter import Charter

    charter = Charter.from_dict(project.charter)
    backend_language = charter.get("language").token if charter and charter.get("language") else None

    files: dict[str, dict] = {}  # placed path -> record (later phases win)
    setup: list[str] = []
    docs: list[dict] = []
    design: Optional[dict] = None
    replaced: list[str] = []

    # Placed in pipeline order, not the order the rows were written: QA's tests follow
    # the folders the Frontend phase was placed in, and a Frontend re-run is newer
    # than the QA it came before. The compile check places them the same way.
    order = {p.value: i for i, p in enumerate(PHASE_ORDER)}
    phases = sorted(current_phases(project), key=lambda ph: order.get(ph.phase, len(order)))
    placer = layout.Placer(backend_language)
    for ph in phases:
        out = ph.output if isinstance(ph.output, dict) else {}
        if ph.phase == "system_design":
            design = out
        for placed, path, content, lang in placer.place_all(ph.phase, iter_files(out)):
            if not placed:
                continue
            if ph.phase == "devops_engineer" and not devops_may_write(placed, out):
                replaced.append(placed)
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
    # An agent's lockfile beside a manifest the platform rewrote, or its tsconfig
    # beside the platform's jsconfig, is a second copy that contradicts the first, so
    # it goes. Nothing else the platform merely *claims* is dropped: a file the
    # scaffold did not write — a Vite project's jest.config, a backend's own
    # migrate.py — is the only copy there is, and deleting it would lose it from the
    # archive while reporting it as replaced.
    written = scaffold.paths()
    for path in [p for p, f in files.items() if f["phase"] != PLATFORM and platform_owned(p)]:
        if superseded(path, written):
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
            "The compile check still reports these after the crew's fix rounds:",
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


def project_kind(assembled: dict) -> Optional[str]:
    """`frontend`, `fullstack` or `backend`: which way "Deploy it" goes. None with no code."""
    from app.build import blueprint

    return blueprint.kind(assembled.get("scaffold") or {})


def saved_env_names(project: Project) -> tuple[str, ...]:
    """The names (never the values) of the variables saved for this project: its
    database's, and its app connectors' (from the account or the project)."""
    from app.core import project_secrets
    from app.orchestration import connectors

    if not project.owner_id:
        return ()
    try:
        record = project_secrets.load(project.owner_id, project.id)
    except ValueError:
        return ()
    names = set((record.get("values") or {}).keys())
    try:
        names |= set(connectors.saved_names(project))
    except (ValueError, OSError):
        pass
    return tuple(sorted(names))


def ship_files(project: Project, assembled: dict) -> dict[str, str]:
    """What a repository gets: the .zip's files, plus a Render Blueprint for a build
    with a backend. Never a real `.env` — `assemble` has only `.env.example`."""
    from app.build import blueprint

    files: dict[str, str] = {"README.md": readme_md(project, assembled)}
    for f in assembled["files"]:
        path = "docs/README.from-agents.md" if f["path"] == "README.md" else f["path"]
        files[path] = f["content"]
    for d in assembled["docs"]:
        files[d["path"]] = d["content"]
    scaffold_info = assembled.get("scaffold") or {}
    render = blueprint.blueprint(
        {f["path"]: f["content"] for f in assembled["files"]},
        scaffold_info,
        project.name or project.idea or "app",
        saved_env=saved_env_names(project),
        required_env=tuple(scaffold_info.get("required_env") or ()),
    )
    if render:
        # The platform's, always. An agent's copy never reaches here (`devops_may_write`),
        # and a Blueprint a model wrote would deploy something the charter never chose.
        files["render.yaml"] = render
    return files


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "project")[:48]).strip("-").lower()
    return s or "project"


def env_file(example: str, values: dict[str, str]) -> str:
    """`.env.example` with the saved values filled in, and any it lacks appended."""
    lines: list[str] = []
    placed: set[str] = set()
    for line in (example or "").splitlines():
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if name in values:
            lines.append(f"{name}={_env_value(values[name])}")
            placed.add(name)
        else:
            lines.append(line)
    missing = [n for n in values if n not in placed]
    if missing:
        lines += ["", "# Your saved connections."]
        lines += [f"{n}={_env_value(values[n])}" for n in missing]
    header = [
        "# Real credentials, added because you asked for them in this download.",
        "# Never commit this file. It is in .gitignore.",
        "",
    ]
    return "\n".join(header + lines) + "\n"


#: Characters a bare `.env` value can hold and mean the same to every reader.
_BARE = re.compile(r"^[A-Za-z0-9_./:@%+=,~?&!*()\[\]{}^|;<>-]*$")


def _env_value(value: str) -> str:
    """A value python-dotenv and Node's dotenv both read back unchanged, when one exists.

    Bare when every character is ordinary; single-quoted when it holds no backslash,
    single quote or `${` — both readers take that literally. Connection strings always
    land here: their user and password are fully percent-encoded when saved.

    Past that no quoting means the same thing to both (python-dotenv expands `${…}`
    even in single quotes and unescapes `\\'`, Node's dotenv does neither), so the
    line says so rather than pretending: python-dotenv's form, and a comment.
    """
    if _BARE.match(value) and "${" not in value:
        return value
    if "\\" not in value and "'" not in value and "${" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"'{escaped}'  # holds a quote, backslash or ${{ — check it survived your dotenv "
        "reader (python-dotenv: load with interpolate=False)"
    )


def frontend_env(project: Project, assembled: dict) -> dict[str, str]:
    """The connector values the frontend's `.env.local` gets in an opt-in download.

    Publishable ones always. Server ones too when the build has no backend of its
    own: then the frontend's API routes are the server, and a key in a
    `backend/.env` nothing reads is a key the app never sees.
    """
    from app.orchestration import connectors

    has_backend = any(f["path"] == "backend/.env.example" for f in assembled["files"])
    return connectors.values_for(project, client_only=has_backend)


def build_zip(
    project: Project,
    assembled: dict,
    env: Optional[dict[str, str]] = None,
    client_env: Optional[dict[str, str]] = None,
) -> bytes:
    """A real, unzippable project archive: README + code files + docs.

    `env` — the saved database values — adds a real `backend/.env`. Only the
    download does this, and only when the person asked; `assemble` never does,
    because `/artifacts` and the GitHub push both send what it returns.
    """
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
        if env:
            example = next(
                (f["content"] for f in assembled["files"] if f["path"] == "backend/.env.example"),
                "",
            )
            z.writestr("backend/.env", env_file(example, env))
        if client_env:
            # The frontend's own values — chosen by the caller (`frontend_env`): the
            # publishable ones, plus the server ones in a build whose server *is* the
            # frontend. A build with no frontend `.env.example` has no frontend.
            example = next(
                (f["content"] for f in assembled["files"] if f["path"] == "frontend/.env.example"),
                None,
            )
            if example is not None:
                z.writestr("frontend/.env.local", env_file(example, client_env))
    return buf.getvalue()
