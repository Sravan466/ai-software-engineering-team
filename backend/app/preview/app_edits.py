"""Changes made on the app preview, as changes to the code (#78).

The sketch's edits rewrote its HTML; the app's rewrite the files the crew wrote. This
is the translation: an element's `data-src` address to the file in the build that
draws it, and a direct edit, a site style, an "Ask the crew" or an undo to the change
`runner.revise_frontend` makes as a new Frontend attempt.

Only the Frontend's own files can be changed this way. A file the platform writes —
the scaffold's root layout, the Tailwind config — or another phase wrote is named
and refused, so the person can ask the crew instead.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.build import theme as site
from app.core import artifacts
from app.core.constants import PipelineStatus
from app.db.models import Project
from app.preview import app_runtime, app_state, source

FRONTEND = "frontend_engineer"
#: What a person may change the app from: a build waiting on them, or a finished one.
EDITABLE = (PipelineStatus.AWAITING_APPROVAL.value, PipelineStatus.COMPLETED.value)
_OWNERS = {
    "platform": "the platform",
    "backend_engineer": "the Backend Engineer",
    "qa_engineer": "QA",
    "devops_engineer": "DevOps",
}


def edit_block(project: Project) -> Optional[str]:
    """Why the app can't be changed from the preview right now, or None."""
    from app.orchestration.runner import runner

    ok, why = app_runtime.available()
    if not ok:
        return why
    row, busy = app_runtime.current_frontend(project)
    if row is None:
        return "The crew hasn't written the frontend yet."
    if busy or project.status == PipelineStatus.RUNNING.value:
        return "The crew is working on this build right now. Change the app once it stops."
    if project.status not in EDITABLE:
        return f"Resume the build first — it's {project.status.replace('_', ' ')}."
    if runner.at_database_gate(project) or runner.at_integrations_gate(project):
        return "This build is waiting on a question about its services. Answer it first."
    return None


def guard(project: Project, built_from: Optional[str]):
    """The current Frontend attempt, or 409 with why the app can't be changed now."""
    why = edit_block(project)
    if why:
        raise HTTPException(409, why)
    row, _ = app_runtime.current_frontend(project)
    if built_from and built_from != row.id:
        raise HTTPException(
            409,
            "The code changed since this preview was built, so it's starting again with the new code. "
            "Try again once it's up.",
        )
    return row


def records(project: Project) -> dict[str, dict]:
    """Every placed file of the build, as the preview was built from it."""
    return {f["path"]: f for f in artifacts.assemble(project).get("files") or []}


def address(oid: Optional[str]) -> source.Address:
    found = source.parse(oid)
    if found is None:
        raise HTTPException(422, "That element can't be traced to the code. Select it again.")
    return found


def frontend_file(files: dict[str, dict], addr: source.Address) -> dict:
    """The file an address points into — one the Frontend wrote — or 422 saying who did."""
    record = files.get(addr.path)
    if record is None:
        raise HTTPException(422, f"{addr.path} isn't in the current build. Wait for the preview to restart, then try again.")
    owner = record.get("phase")
    if owner != FRONTEND:
        who = _OWNERS.get(str(owner), str(owner))
        raise HTTPException(
            422,
            f"{addr.path} is written by {who}, not by the crew's frontend, so it can't be changed from here.",
        )
    if not record.get("source_path"):
        raise HTTPException(422, f"{addr.path} can't be traced back to the file the crew wrote.")
    return record


def locate(project: Project, oid: str) -> dict:
    addr = address(oid)
    files = records(project)
    record = files.get(addr.path)
    owner = record.get("phase") if record else None
    out = {"path": addr.path, "line": addr.line, "owner": None if owner == FRONTEND else (owner or "unknown")}
    if record is None:
        out["why"] = f"{addr.path} isn't in the current build."
        return out
    try:
        info = source.locate(addr, record["content"])
    except source.Unavailable as e:
        out["why"] = str(e)
        return out
    if not info.get("found"):
        out["why"] = "That element has moved in the code since this preview was built."
        return out
    if owner != FRONTEND:
        who = _OWNERS.get(str(owner), str(owner))
        out["why"] = f"{addr.path} is written by {who}, so it can't be changed from here."
    out.update(
        start_line=info.get("start_line"), end_line=info.get("end_line"), tag=info.get("tag"),
        text=info.get("text") or {}, classes=info.get("classes") or {},
    )
    return out


# ── the changes ──────────────────────────────────────────────────────────────
def patch_spec(project: Project, ops: list[dict]) -> Optional[dict]:
    """Direct edits — text, classes, links — as files changed. None when nothing changes."""
    files = records(project)
    edits: list[dict] = []
    used: dict[str, dict] = {}
    for op in ops:
        addr = address(op.get("oid"))
        record = frontend_file(files, addr)
        used[addr.path] = record
        edit = {"path": addr.path, "line": addr.line, "col": addr.col, "kind": op.get("kind")}
        for key in ("text", "add", "remove", "name", "value"):
            if op.get(key) is not None:
                edit[key] = op[key]
        edits.append(edit)
    try:
        changed, refused = source.edit({p: r["content"] for p, r in used.items()}, edits)
    except source.Unavailable as e:
        raise HTTPException(503, f"The code can't be read here, so the app can't be changed from the preview: {e}")
    if refused:
        reasons = list(dict.fromkeys(r.get("reason") or "" for r in refused if r.get("reason")))
        raise HTTPException(422, " ".join(reasons[:2]) or "That change can't be made to the code.")
    out = {
        used[p]["source_path"]: content
        for p, content in changed.items()
        if p in used and content != used[p]["content"]
    }
    return {"files": out} if out else None


def ask_spec(project: Project, oid: str, instruction: str) -> tuple[dict, str]:
    """"Ask the crew" about one element: the file, its lines, and the change. (spec, path)"""
    addr = address(oid)
    files = records(project)
    record = frontend_file(files, addr)
    try:
        info = source.locate(addr, record["content"])
    except source.Unavailable as e:
        raise HTTPException(503, f"The code can't be read here: {e}")
    if not info.get("found"):
        raise HTTPException(409, "That element has moved in the code since this preview was built. Select it again.")
    text = (info.get("text") or {}).get("value") if (info.get("text") or {}).get("editable") else None
    what = f"the <{info.get('tag')}>" + (f" “{text[:60]}”" if text else "")
    return {
        "ask": {
            "path": record["source_path"],
            "content": record["content"],
            "lines": [info.get("start_line"), info.get("end_line")],
            "what": what,
            "instruction": instruction.strip(),
        }
    }, addr.path


def theme_spec(row, changes: dict) -> dict:
    output = row.output if isinstance(row.output, dict) else {}
    front = {
        f["path"]: (f.get("code") or f.get("content") or "")
        for f in output.get("files") or []
        if isinstance(f, dict) and isinstance(f.get("path"), str)
    }
    try:
        merged = site.merge(output.get("app_theme"), changes, front)
    except site.Refused as e:
        raise HTTPException(422, str(e))
    return {"theme": merged}


def restore_spec(project: Project, row, forward: bool) -> dict:
    undo, redo = app_state.targets(project, row.id)
    target = redo if forward else undo
    if target is None:
        raise HTTPException(409, "Nothing to redo." if forward else "Nothing to undo — this is the code the crew wrote.")
    return {"restore_row": target}


# ── what the page is told ────────────────────────────────────────────────────
def app_theme(row) -> Optional[dict]:
    """The app's Site style panel: what it has now, and the choices it offers."""
    from app.preview import design as D

    if row is None or not isinstance(row.output, dict):
        return None
    front = {
        f["path"]: (f.get("code") or f.get("content") or "")
        for f in row.output.get("files") or []
        if isinstance(f, dict) and isinstance(f.get("path"), str)
    }
    chosen = row.output.get("app_theme") if isinstance(row.output.get("app_theme"), dict) else None
    families = (chosen or {}).get("families") if isinstance((chosen or {}).get("families"), dict) else site.families(front)
    now = site.current(chosen, front)
    primary = D._hex(now["primary"]) or "#4f46e5"
    accent = D._hex(now["accent"]) or "#d97706"
    return {
        "scope": "app",
        "current": now,
        "families": families,
        "fonts": [
            {"id": key, "label": spec["label"], "display": spec["display"], "body": spec["body"]}
            for key, spec in D.FONT_PAIRS.items()
        ],
        "tints": list(D._TINTS),
        "radii": list(D._RADIUS),
        "shadows": list(D._SHADOW),
        "densities": [],
        "palette": {
            "primary": {str(k): v for k, v in D._scale(primary).items()},
            "accent": {str(k): v for k, v in D._scale(accent).items()},
            "neutral": {str(k): v for k, v in D._neutral_scale(primary, now["tint"] or "neutral").items()},
        },
    }


def out(project: Project, *, touch: bool = False) -> dict:
    """`PreviewOut.app`."""
    data = app_runtime.state(project, touch=touch)
    row, _busy = app_runtime.current_frontend(project)
    record = app_state.edit(project)
    if record is not None:
        record = {
            **record,
            "active": record.get("status") in ("running", "landed") and project.status == PipelineStatus.RUNNING.value,
        }
    undo, redo = app_state.targets(project, row.id if row is not None else None)
    block = edit_block(project)
    data.update(
        editing=record,
        can_edit=block is None and data["status"] in ("running", "starting"),
        edit_block=block,
        can_undo=bool(undo) and block is None,
        can_redo=bool(redo) and block is None,
        theme=app_theme(row) if row is not None else None,
    )
    return data
