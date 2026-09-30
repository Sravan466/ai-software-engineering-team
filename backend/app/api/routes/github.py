"""GitHub publishing: OAuth "Connect" flow + push a project to the user's account.

The OAuth App (client id/secret) is the operator's, registered once — every user
logs in through it into *their own* GitHub. The token that comes back belongs to
that user and is kept, encrypted, in the account's `deploy.local.json`
(`app.core.deploy_store`) — never sent to the browser. It survives a restart and
follows the account to any browser it signs in from.

The round trip is tied to the signed-in account (`state`, 10 minutes) and uses PKCE
(S256): the verifier never leaves this process, so a `code` intercepted on the way
back can't be exchanged by anyone else.

A GitHub 401 means the user removed the app's access: the token is forgotten and
the page is told `reason: "revoked"` so it can say "reconnect".
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_project
from app.core import deploy_store, github_publish as gh
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import get_db
from app.db.models import Project

log = get_logger(__name__)

router = APIRouter(prefix="/api/github", tags=["github"])

_STATE_TTL = 10 * 60  # 10 min to complete the OAuth round-trip

#: state -> {return_to, ts, user_id, verifier}. Process-local and short-lived: a
#: restart in the middle of a sign-in just means clicking Connect again.
_pending: dict[str, dict] = {}
#: Accounts whose token GitHub turned down, so `/status` can say why it's gone.
_revoked: set[str] = set()


# ── helpers ──────────────────────────────────────────────────────────────────
def _configured() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


def _prune() -> None:
    now = time.time()
    for key in [k for k, v in _pending.items() if now - v.get("ts", 0) > _STATE_TTL]:
        _pending.pop(key, None)


def _user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "Sign in first.")
    return user_id


def _safe_return(url: str) -> str:
    """Only ever redirect back to our own frontend origin (no open redirect)."""
    base = settings.frontend_base_url.rstrip("/")
    if url and (url == base or url.startswith(base + "/") or url.startswith(base + "?")):
        return url
    return base


def _with_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != key]
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def token_for(user_id: str) -> Optional[str]:
    return deploy_store.for_user(user_id).token(deploy_store.GITHUB)


def forget_revoked(user_id: str) -> None:
    """GitHub turned the token down: forget it, and remember why for `/status`."""
    deploy_store.for_user(user_id).remove(deploy_store.GITHUB)
    _revoked.add(user_id)
    log.info("GitHub access was revoked for an account; its token was forgotten.")


def status_for(user_id: str) -> dict:
    public = deploy_store.for_user(user_id).public()[deploy_store.GITHUB]
    return {
        "configured": _configured(),
        "connected": public["connected"],
        "login": public["login"],
        "name": public["name"],
        "avatar": public["avatar"],
        "reason": "revoked" if not public["connected"] and user_id in _revoked else None,
    }


# ── status ───────────────────────────────────────────────────────────────────
@router.get("/status")
def status(request: Request) -> dict:
    _prune()
    return status_for(_user_id(request))


# ── OAuth round-trip ─────────────────────────────────────────────────────────
@router.get("/oauth/start")
def oauth_start(request: Request, return_to: str = ""):
    if not _configured():
        raise HTTPException(
            400,
            "GitHub publishing isn't configured. Add a free OAuth App and set "
            "GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET in the backend .env.",
        )
    _prune()
    state = secrets.token_urlsafe(24)
    verifier, challenge = gh.pkce_pair()
    _pending[state] = {
        "return_to": _safe_return(return_to),
        "ts": time.time(),
        "user_id": getattr(request.state, "user_id", None),
        "verifier": verifier,
    }
    return RedirectResponse(gh.authorize_url(state, challenge), status_code=302)


@router.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    _prune()
    pend = _pending.pop(state, None)
    return_to = (pend or {}).get("return_to") or settings.frontend_base_url.rstrip("/")
    user_id = getattr(request.state, "user_id", None)

    # The round trip has to end in the account it started in.
    if error or not code or pend is None or not user_id or pend.get("user_id") != user_id:
        return RedirectResponse(_with_param(return_to, "github", "error"), status_code=302)

    try:
        token, scopes = gh.exchange_code(code, pend["verifier"])
        user = gh.get_user(token)
        deploy_store.for_user(user_id).save(
            deploy_store.GITHUB,
            token,
            login=user.get("login"),
            name=user.get("name"),
            avatar=user.get("avatar_url"),
            scopes=scopes,
            connected_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:  # noqa: BLE001 - any failure → a clean "error" back on the UI
        log.warning("GitHub sign-in didn't complete: %s", type(e).__name__)
        return RedirectResponse(_with_param(return_to, "github", "error"), status_code=302)

    _revoked.discard(user_id)
    return RedirectResponse(_with_param(return_to, "github", "connected"), status_code=302)


@router.post("/disconnect")
def disconnect(request: Request) -> dict:
    user_id = _user_id(request)
    deploy_store.for_user(user_id).remove(deploy_store.GITHUB)
    _revoked.discard(user_id)
    return {"connected": False}


# ── push ─────────────────────────────────────────────────────────────────────
class PushRequest(BaseModel):
    name: Optional[str] = None
    private: bool = True
    description: Optional[str] = None
    #: Push into the empty repository of that name the user already has.
    use_existing: bool = False


def push_for(user_id: str, project: Project, db: Session, body: PushRequest) -> dict:
    """Push `project` with the account's token and remember where it went.

    Raises `HTTPException` for a missing or revoked connection and any other GitHub
    failure, and lets `GitHubConflict` through for the caller to answer with the
    name's owner (`conflict_response`). Shared by the push route and the Render deploy.
    """
    token = token_for(user_id)
    if not token:
        raise HTTPException(409, "Connect your GitHub account first.")
    try:
        result = gh.push_project(
            token,
            project,
            name=body.name,
            private=body.private,
            description=body.description,
            use_existing=body.use_existing,
        )
    except gh.GitHubRevoked as e:
        forget_revoked(user_id)
        raise HTTPException(409, str(e))
    except gh.GitHubConflict:
        raise
    except gh.GitHubError as e:
        raise HTTPException(400, str(e))
    project.github_repo = result["full_name"]
    project.github_branch = result["branch"]
    project.github_pushed_at = datetime.now(timezone.utc)
    db.commit()
    return result


@router.post("/push/{project_id}")
def push(
    body: PushRequest,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    user_id = _user_id(request)
    if not token_for(user_id):
        # 409, not 401: a 401 means "sign in to this app", and the page would take
        # the person to the sign-in screen instead of saying what's missing.
        return JSONResponse(
            status_code=409,
            content={"detail": "Connect your GitHub account first.", "needs": "github"},
        )
    try:
        return push_for(user_id, project, db, body)
    except gh.GitHubConflict as e:
        return conflict_response(e)


def conflict_response(e: gh.GitHubConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": str(e), "conflict": {"full_name": e.full_name, "usable": e.usable}},
    )
