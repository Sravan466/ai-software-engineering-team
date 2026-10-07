"""Putting a finished build online, in the user's own accounts.

    GET    /api/deploy/connections             who GitHub and Vercel are connected as
    PUT    /api/deploy/vercel/token            check a pasted Vercel token, then save it
    DELETE /api/deploy/vercel/token            forget it
    GET    /api/projects/{id}/ship             what "Deploy it" will do for this build
    POST   /api/projects/{id}/deploy           deploy (Vercel) or hand off (Render)
    GET    /api/projects/{id}/deploy           the deploy's state, polled — and, when
                                               Vercel fails the build, the crew is sent
                                               its errors to fix (#75)
    PUT    /api/projects/{id}/deploy/url       the live URL pasted back from Render

A frontend-only build goes straight to Vercel through its API: no GitHub needed.
Anything with a backend goes to the user's GitHub with a `render.yaml`, and then to
Render's own Blueprint page (`render.com/deploy?repo=…`), where Render signs the
user in and creates every service in their account. We never take a Render key, and
nothing is ever hosted on ours.

No token is ever returned: the page sees a username and `…last4`. A database value
the user saved (#54) goes to Vercel only when it is public by design (a Supabase URL,
its publishable key) and the frontend reads it; to Render it goes as a *name*, for
Render to ask for.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.api.deps import get_project
from app.api.routes import github as github_routes
from app.api.routes.settings import _trusted_host
from app.build import blueprint, buildlog, dbconnect, scaffold
from app.build.layout import FRONTEND
from app.core import artifacts, deploy_store, project_secrets, scrub, secretbox, vercel
from app.core import github_publish as gh
from app.core.config import settings
from app.core.constants import Phase, PipelineStatus
from app.orchestration import autofix
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import Project
from app.router import keycheck

log = get_logger(__name__)

router = APIRouter(tags=["deploy"])

#: A deploy that is still ours to finish: nothing else may start one beside it.
IN_FLIGHT = ("queued", "uploading", "building")
#: Project ids whose upload is running in this process. A row that says `queued` or
#: `uploading` with no entry here was interrupted by a restart.
_uploading: set[str] = set()
_uploading_lock = threading.Lock()

#: Values that are public by design: a browser is handed them anyway.
_PUBLIC_BY_DESIGN = frozenset({"SUPABASE_ANON_KEY", "FIREBASE_API_KEY"})
_PUBLIC_PREFIXES = ("NEXT_PUBLIC_", "VITE_", "REACT_APP_", "PUBLIC_")


class HourlyLimiter:
    """At most `settings.deploys_per_hour` deploys per account per rolling hour."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        with self._lock:
            now = time.monotonic()
            hits = [t for t in self._hits.get(who, []) if now - t < 3600]
            allowed = len(hits) < max(settings.deploys_per_hour, 1)
            if allowed:
                hits.append(now)
            self._hits[who] = hits
            return allowed

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = HourlyLimiter()


def _user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "Sign in first.")
    return user_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """UTC, always with its offset: SQLite hands back naive datetimes, and a browser
    reads a naive ISO string as local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# ── connections ──────────────────────────────────────────────────────────────
@router.get("/api/deploy/connections")
def connections(request: Request) -> dict:
    user_id = _user_id(request)
    return {
        "github": github_routes.status_for(user_id),
        "vercel": deploy_store.for_user(user_id).public()[deploy_store.VERCEL],
    }


class VercelToken(BaseModel):
    token: str


@router.put("/api/deploy/vercel/token")
def save_vercel_token(body: VercelToken, request: Request) -> dict:
    """Check the token with Vercel, then save it encrypted. A token Vercel rejects is
    never saved, and never replaces one that works."""
    user_id = _user_id(request)
    if not _trusted_host(request):
        raise HTTPException(
            403,
            "Tokens can only be saved from an address this backend is served at "
            "(localhost, or BACKEND_PUBLIC_URL).",
        )
    token = body.token.strip()
    if not token:
        raise HTTPException(422, "Paste the token first.")
    if len(token) > 200 or any(ch.isspace() for ch in token):
        raise HTTPException(422, "That doesn't look like a Vercel token — copy it again, all of it.")
    if not keycheck.limiter.allow(user_id):
        raise HTTPException(429, "Too many token checks in a short time. Wait a few minutes and try again.")
    found = vercel.check_token(token)
    store = deploy_store.for_user(user_id)
    if found.ok:
        try:
            store.save(
                deploy_store.VERCEL,
                token,
                username=found.username,
                hint=deploy_store.hint(token),
                checked_at=_now().isoformat(),
            )
        except (deploy_store.StoreUnreadable, secretbox.SecretsLocked) as e:
            raise HTTPException(409, str(e))
    return {
        "applied": found.ok,
        "reason": found.reason,
        "message": found.message or (f"Connected as {found.username}." if found.ok else ""),
        "vercel": store.public()[deploy_store.VERCEL],
    }


@router.delete("/api/deploy/vercel/token")
def remove_vercel_token(request: Request) -> dict:
    user_id = _user_id(request)
    if not _trusted_host(request):
        raise HTTPException(403, "Tokens can only be removed from an address this backend is served at.")
    store = deploy_store.for_user(user_id)
    try:
        store.remove(deploy_store.VERCEL)
    except deploy_store.StoreUnreadable as e:
        raise HTTPException(409, str(e))
    return {"vercel": store.public()[deploy_store.VERCEL]}


# ── what "Deploy it" does for this build ─────────────────────────────────────
def _label(info: dict, database: Optional[str]) -> str:
    parts = [p for p in (info.get("frontend"), info.get("backend_framework") or info.get("backend"), database) if p]
    return " + ".join(parts)


@router.get("/api/projects/{project_id}/ship")
def ship(request: Request, project: Project = Depends(get_project)) -> dict:
    user_id = _user_id(request)
    assembled = artifacts.assemble(project)
    info = assembled.get("scaffold") or {}
    kind = blueprint.kind(info) if assembled["files"] else None
    if project.deploy_target == "vercel" and project.deploy_status in ("fixing", "fixed") and kind is None:
        # Mid-fix the frontend being rebuilt has no current output yet, so the build
        # assembles as empty. It was a Vercel build, and it still is (#75).
        kind = blueprint.FRONTEND_ONLY
    files = {f["path"]: f["content"] for f in assembled["files"]}
    render = (
        blueprint.summary(
            files,
            info,
            saved_env=artifacts.saved_env_names(project),
            required_env=tuple(info.get("required_env") or ()),
        )
        if kind in (blueprint.FULLSTACK, blueprint.BACKEND_ONLY)
        else None
    )
    _settle_interrupted(project)
    return {
        "kind": kind,
        "target": blueprint.target(kind),
        "stack": _label(info, info.get("database")),
        "frontend": info.get("frontend"),
        "ready": project.status == PipelineStatus.COMPLETED.value and bool(assembled["files"]),
        "render": render,
        "github_repo": project.github_repo,
        "github_branch": project.github_branch,
        "github_pushed_at": _iso(project.github_pushed_at),
        "deploy": _deploy_state(project),
        "connections": {
            "github": github_routes.status_for(user_id),
            "vercel": deploy_store.for_user(user_id).public()[deploy_store.VERCEL],
        },
    }


#: Lines of a failed build's log the page is sent; the whole log stays on the row.
_LOG_SHOWN = 40
#: Deploy states that come with the failed build's log.
_WITH_LOG = ("error", "fixing", "fixed")


def _deploy_state(project: Project, log_lines: Optional[list[str]] = None) -> dict:
    kept = project.deploy_log if project.deploy_status in _WITH_LOG else None
    return {
        "target": project.deploy_target,
        "status": project.deploy_status,
        "url": project.deploy_url,
        "error": project.deploy_error,
        "deployed_at": _iso(project.deployed_at),
        "log": log_lines if log_lines is not None else list(kept or [])[-_LOG_SHOWN:],
        "fix": _fix_state(project),
    }


def _fix_state(project: Project) -> Optional[dict]:
    """The crew's fix of a failed Vercel build (#75): which round, of how many, and how
    it ended — `fixing`, `stuck` (it parked asking for help) or `fixed`."""
    if project.deploy_status not in ("fixing", "fixed"):
        return None
    data = autofix.load(project)
    t = data["tracks"].get(autofix.build_track(Phase.FRONTEND_ENGINEER.value)) or {}
    rounds = t.get("rounds") or []
    start = max((i for i, r in enumerate(rounds) if r.get("source") == "vercel"), default=None)
    since = rounds[start:] if start is not None else []
    # The budget the round started with: the track's own moves on once it settles.
    budget = rounds[start].get("of") if start is not None else None
    allowed = max(int(budget or (int(t.get("allowed") or 0) - (start or 0))), len(since))
    stuck = bool(t.get("stopped")) and not t.get("accepted")
    if project.deploy_status == "fixed":
        state = "fixed"
    elif project.status == PipelineStatus.RUNNING.value:
        state = "fixing"
    elif project.status == PipelineStatus.AWAITING_APPROVAL.value:
        # Parked: asking for help, or — fixed — waiting at a review before it finishes.
        state = "stuck" if stuck else "review"
    else:
        state = "stopped"
    return {
        "state": state,
        "round": len(since),
        "of": allowed,
        "problems": len(rounds[start].get("problems") or []) if start is not None else 0,
        "attempt": int((data.get("vercel") or {}).get("attempts") or 0),
    }


def _settle_interrupted(project: Project) -> None:
    """An upload this process isn't running was cut off by a restart: say so."""
    if project.deploy_status in ("queued", "uploading"):
        with _uploading_lock:
            running = project.id in _uploading
        if not running:
            with SessionLocal() as db:
                row = db.get(Project, project.id)
                if row is not None and row.deploy_status in ("queued", "uploading"):
                    row.deploy_status = "error"
                    row.deploy_error = "The deploy was interrupted before Vercel had the files — retry it."
                    db.commit()
            project.deploy_status = "error"
            project.deploy_error = "The deploy was interrupted before Vercel had the files — retry it."


# ── deploying ────────────────────────────────────────────────────────────────
class DeployRequest(BaseModel):
    #: For a Render deploy whose build isn't on GitHub yet: the repo to create.
    name: Optional[str] = None
    private: bool = True


def _public_env(project: Project, frontend_files: dict[str, str]) -> dict[str, str]:
    """The saved values the frontend reads that are public by design: the database's
    publishable ones, and app connectors' client variables (#59) — taken from the
    registry, never a hardcoded list. A connector's secret never goes here."""
    if not project.owner_id:
        return {}
    out = _public_connector_env(project, frontend_files)
    record = project_secrets.load(project.owner_id, project.id)
    if not record.get("values"):
        return out
    contract = dbconnect.contract_for(record.get("database"), record.get("provider"))
    try:
        values = project_secrets.reveal(project.owner_id, project.id)
    except secretbox.SecretsLocked:
        return out
    for name in scaffold.env_vars(frontend_files.items()):
        base = next((name[len(p):] for p in _PUBLIC_PREFIXES if name.startswith(p)), name)
        for candidate in (name, base):
            value = values.get(candidate)
            var = contract.var(candidate) if contract else None
            if value and var is not None and (not var.secret or candidate in _PUBLIC_BY_DESIGN):
                out[name] = value
                break
    return out


def _public_connector_env(project: Project, frontend_files: dict[str, str]) -> dict[str, str]:
    from app.build import integrations
    from app.orchestration import connectors

    public = integrations.client_names()
    try:
        values = connectors.values_for(project, client_only=True)
    except secretbox.SecretsLocked:
        return {}
    read = set(scaffold.env_vars(frontend_files.items()))
    return {name: value for name, value in values.items() if name in public and name in read}


def _claim_deploy(db: Session, project: Project, target: str, status: str) -> bool:
    """Atomically mark a deploy as started — refused while another is in flight."""
    result = db.execute(
        update(Project)
        .where(
            Project.id == project.id,
            or_(Project.deploy_status.is_(None), Project.deploy_status.notin_(IN_FLIGHT)),
        )
        .values(deploy_target=target, deploy_status=status, deploy_error=None, deploy_id=None, deploy_log=None)
    )
    db.commit()
    db.refresh(project)
    return result.rowcount == 1


def _run_vercel(project_id: str, user_id: str) -> None:
    """Background: upload the frontend and start the deployment. The route has
    already put `project_id` in `_uploading`; this takes it out when done."""
    try:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            if project is None:
                return
            token = deploy_store.for_user(user_id).token(deploy_store.VERCEL)
            if not token:
                project.deploy_status, project.deploy_error = "error", "Connect Vercel first."
                db.commit()
                return
            assembled = artifacts.assemble(project)
            prefix = FRONTEND + "/"
            # The frontend's own folder, sent as the project root: its package.json is
            # complete on its own, and Vercel needs no `rootDirectory`.
            files = {
                f["path"][len(prefix):]: f["content"]
                for f in assembled["files"]
                if f["path"].startswith(prefix)
            }
            info = assembled.get("scaffold") or {}
            project.deploy_status = "uploading"
            db.commit()
            try:
                found = vercel.deploy(
                    token,
                    artifacts.slug(project.name or project.idea)[:52],
                    files,
                    blueprint.VERCEL_FRAMEWORK.get(info.get("frontend") or ""),
                    public_env=_public_env(project, files),
                )
            except vercel.VercelError as e:
                if e.reason == "rejected":
                    deploy_store.for_user(user_id).remove(deploy_store.VERCEL)
                project.deploy_status, project.deploy_error = "error", str(e)
                db.commit()
                return
            except Exception as e:  # noqa: BLE001 - a clean sentence, never a stack trace
                log.warning("Vercel deploy failed: %s", scrub.scrub(e))
                project.deploy_status = "error"
                project.deploy_error = "Vercel didn't answer. Check the connection and retry."
                db.commit()
                return
            project.deploy_id = str(found.get("id") or "")
            project.deploy_status = "building"
            project.deploy_url = vercel.live_url(found)
            db.commit()
    finally:
        with _uploading_lock:
            _uploading.discard(project_id)


@router.post("/api/projects/{project_id}/deploy")
def deploy(
    body: DeployRequest,
    request: Request,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    user_id = _user_id(request)
    if project.status != PipelineStatus.COMPLETED.value:
        raise HTTPException(409, "Deploy is ready once the build is complete.")
    assembled = artifacts.assemble(project)
    kind = blueprint.kind(assembled.get("scaffold") or {}) if assembled["files"] else None
    target = blueprint.target(kind)
    if target is None:
        raise HTTPException(409, "This build has no frontend or backend code to deploy.")
    _settle_interrupted(project)
    if project.deploy_status in IN_FLIGHT:
        raise HTTPException(409, "A deploy of this build is already running — wait for it to finish.")

    if target == "vercel":
        if not deploy_store.for_user(user_id).token(deploy_store.VERCEL):
            return JSONResponse(
                status_code=409,
                content={"detail": "Connect your Vercel account first.", "needs": "vercel"},
            )
        if not limiter.allow(user_id):
            raise HTTPException(429, f"That's {settings.deploys_per_hour} deploys this hour — try again later.")
        # Marked as ours before the row says `queued`: a poll arriving before the
        # background task starts would otherwise read it as cut off by a restart.
        with _uploading_lock:
            _uploading.add(project.id)
        if not _claim_deploy(db, project, "vercel", "queued"):
            with _uploading_lock:
                _uploading.discard(project.id)
            raise HTTPException(409, "A deploy of this build is already running — wait for it to finish.")
        background.add_task(_run_vercel, project.id, user_id)
        return {"target": "vercel", "deploy": _deploy_state(project)}

    # Render: from the build's GitHub repo, pushed again first so it is current.
    if not github_routes._configured():  # noqa: SLF001
        raise HTTPException(
            400,
            "Full-stack apps deploy from GitHub, and GitHub isn't configured on this server. "
            "Set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET in the backend .env.",
        )
    if not github_routes.token_for(user_id):
        return JSONResponse(
            status_code=409,
            content={"detail": "Full-stack apps deploy from a GitHub repo. Connect GitHub to continue.", "needs": "github"},
        )
    if not project.github_repo and not body.name:
        return JSONResponse(
            status_code=409,
            content={"detail": "Push this build to a GitHub repo first.", "needs": "push"},
        )
    if not limiter.allow(user_id):
        raise HTTPException(429, f"That's {settings.deploys_per_hour} deploys this hour — try again later.")
    try:
        pushed = github_routes.push_for(
            user_id, project, db, github_routes.PushRequest(name=body.name, private=body.private)
        )
    except gh.GitHubConflict as e:
        return github_routes.conflict_response(e)
    repo_url = f"https://github.com/{pushed['full_name']}"
    if pushed["branch"] not in ("main", "master"):
        repo_url += f"/tree/{quote(pushed['branch'])}"
    handoff = f"https://render.com/deploy?repo={quote(repo_url, safe=':/')}"
    project.deploy_target = "render"
    project.deploy_status = "handed_off"
    project.deploy_error = None
    db.commit()
    return {"target": "render", "handoff_url": handoff, "push": pushed, "deploy": _deploy_state(project)}


@router.get("/api/projects/{project_id}/deploy")
def deploy_status(
    request: Request,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    user_id = _user_id(request)
    _settle_interrupted(project)
    if project.deploy_target != "vercel" or project.deploy_status != "building" or not project.deploy_id:
        return _deploy_state(project)
    token = deploy_store.for_user(user_id).token(deploy_store.VERCEL)
    if not token:
        return _deploy_state(project)
    try:
        found = vercel.status(token, project.deploy_id)
    except vercel.VercelError as e:
        if e.reason == "rejected":
            deploy_store.for_user(user_id).remove(deploy_store.VERCEL)
            project.deploy_status, project.deploy_error = "error", str(e)
            db.commit()
        return _deploy_state(project)
    except Exception:  # noqa: BLE001 - try again on the next poll
        return _deploy_state(project)
    state = str(found.get("readyState") or found.get("status") or "").upper()
    if state == "READY":
        project.deploy_status = "ready"
        project.deploy_url = vercel.live_url(found) or project.deploy_url
        project.deployed_at = _now()
        data = autofix.load(project)
        if data.get("vercel"):
            # It built: the next failure, if there is one, starts the count again.
            data["vercel"]["attempts"] = 0
            autofix.save(project, data)
        db.commit()
    elif state in ("ERROR", "CANCELED"):
        # The page polls every few seconds, and a slow poll can still be reading the
        # log when the next one arrives. The move off `building` is one conditional
        # write: only the poll that wins it reads the log and may send the build back.
        won = db.execute(
            update(Project)
            .where(Project.id == project.id, Project.deploy_status == "building",
                   Project.deploy_id == project.deploy_id)
            .values(deploy_status="error")
        ).rowcount == 1
        db.commit()
        db.refresh(project)
        if not won:
            return _deploy_state(project)
        lines = vercel.events(token, project.deploy_id)
        if project.deployed_at is None:
            # Never went live: its address would only ever show Vercel's error page.
            project.deploy_url = None
        reason = vercel.failure(found) or ("The build was cancelled." if state == "CANCELED" else "")
        project.deploy_error = scrub.scrub(f"Build failed on Vercel — see the log. {reason}".strip())[:500]
        project.deploy_log = lines or None
        db.commit()
        if state == "ERROR":
            _send_back(db, project, lines, background)
        return _deploy_state(project, lines[-_LOG_SHOWN:])
    return _deploy_state(project)


def _send_back(db: Session, project: Project, lines: list[str], background: BackgroundTasks) -> bool:
    """Vercel failed the build: hand its errors to the crew, as a fix round (#75).

    The same loop a compile error goes through — `build:frontend_engineer` — with
    Vercel's own errors as the problems. Only for a finished build the platform ran
    (a checkpoint to rewind), only when an error is in a file the crew wrote (a
    missing variable in the Vercel project isn't the code's fault), and at most
    `auto_fix_max_rounds` times in a row without a deploy that builds.
    """
    from app.api.routes.projects import _claim, _drive_deploy_fix
    from app.orchestration.runner import PipelineRunner

    if project.status != PipelineStatus.COMPLETED.value or not lines:
        return False
    started, _ = PipelineRunner._graph_position(project.id)
    if not started:
        return False
    assembled = artifacts.assemble(project)
    prefix = FRONTEND + "/"
    crew = {
        f["path"] for f in assembled["files"]
        if f["path"].startswith(prefix) and f.get("phase") == Phase.FRONTEND_ENGINEER.value
    }
    names = [f["path"][len(prefix):] for f in assembled["files"] if f["path"].startswith(prefix)]
    problems = buildlog.prefixed(buildlog.vercel_problems(lines, names), FRONTEND)
    # The manifest is the platform's, written from the crew's imports: a package npm
    # can't install is fixed by importing something else, which is the crew's to do.
    manifest = f"{FRONTEND}/package.json"
    if not any(p.path in crew or (p.path == manifest and p.kind == "package") for p in problems):
        project.deploy_error = (
            f"{project.deploy_error} The error isn't in a file the crew wrote, so it wasn't sent back."
        )[:500]
        db.commit()
        return False
    attempts = int((autofix.load(project).get("vercel") or {}).get("attempts") or 0)
    if attempts >= max(int(settings.auto_fix_max_rounds), 0):
        project.deploy_error = (
            f"Vercel still fails after the crew fixed it {attempts} time{'s' if attempts != 1 else ''}. "
            "Read the log, or send the frontend back with a note."
        )
        db.commit()
        return False
    token = _claim(db, project, {PipelineStatus.COMPLETED.value})
    if not token:
        return False  # resumed or redone a moment ago: the deploy stays failed, with its log
    # Counted once the crew has the build: an attempt that never started isn't one.
    data = autofix.load(project)
    data.setdefault("vercel", {}).update(attempts=attempts + 1, deploy_id=project.deploy_id)
    autofix.save(project, data)
    project.deploy_status = "fixing"
    db.commit()
    log.info("Vercel failed %s; sending %d problem(s) back to the Frontend Engineer.", project.id, len(problems))
    background.add_task(_drive_deploy_fix, project.id, [p.as_dict() for p in problems], token)
    return True


class LiveUrl(BaseModel):
    url: str = ""


@router.put("/api/projects/{project_id}/deploy/url")
def set_live_url(body: LiveUrl, project: Project = Depends(get_project), db: Session = Depends(get_db)) -> dict:
    """The URL Render gave the user, kept with the project. Blank clears it."""
    url = body.url.strip()
    if url:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme != "https" or not parsed.hostname or "." not in parsed.hostname:
            raise HTTPException(422, "Paste the full https:// address Render shows for your app.")
        url = f"https://{parsed.hostname}{parsed.path.rstrip('/')}"
    project.deploy_url = url or None
    if url:
        project.deployed_at = _now()
    db.commit()
    return _deploy_state(project)
