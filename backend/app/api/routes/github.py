"""GitHub publishing: OAuth "Connect" flow + push a project to the user's account.

The OAuth App (client id/secret) is the operator's, registered once — every user
logs in through it into *their own* GitHub. The token that comes back belongs to
that user and is kept in a per-browser server-side session (an opaque cookie maps
to an in-memory entry), never written to disk and never sent to the browser. So
two different people using the same instance push to two different accounts.
"""
from __future__ import annotations

import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import get_project
from app.core import github_publish as gh
from app.core.config import settings
from app.db.models import Project

router = APIRouter(prefix="/api/github", tags=["github"])

_COOKIE = "gh_session"
_SESSION_TTL = 8 * 60 * 60  # 8h
_STATE_TTL = 10 * 60  # 10 min to complete the OAuth round-trip

# Process-local stores. Fine for a self-hosted single-process app; tokens live
# only here (not on disk), so a restart simply asks the user to reconnect.
_sessions: dict[str, dict] = {}
_pending: dict[str, dict] = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _configured() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


def _prune() -> None:
    now = time.time()
    for store, ttl in ((_sessions, _SESSION_TTL), (_pending, _STATE_TTL)):
        for key in [k for k, v in store.items() if now - v.get("ts", 0) > ttl]:
            store.pop(key, None)


def _session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(_COOKIE)
    if not sid:
        return None
    s = _sessions.get(sid)
    if s and time.time() - s.get("ts", 0) > _SESSION_TTL:
        _sessions.pop(sid, None)
        return None
    return s


def _safe_return(url: str) -> str:
    """Only ever redirect back to our own frontend origin (no open redirect)."""
    base = settings.frontend_base_url.rstrip("/")
    if url and url.startswith(base):
        return url
    return base


def _with_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != key]
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# ── status ───────────────────────────────────────────────────────────────────
@router.get("/status")
def status(request: Request) -> dict:
    _prune()
    s = _session(request)
    return {
        "configured": _configured(),
        "connected": bool(s),
        "login": s.get("login") if s else None,
        "name": s.get("name") if s else None,
        "avatar": s.get("avatar") if s else None,
    }


# ── OAuth round-trip ─────────────────────────────────────────────────────────
@router.get("/oauth/start")
def oauth_start(return_to: str = ""):
    if not _configured():
        raise HTTPException(
            400,
            "GitHub publishing isn't configured. Add a free OAuth App and set "
            "GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET in the backend .env.",
        )
    _prune()
    state = secrets.token_urlsafe(24)
    _pending[state] = {"return_to": _safe_return(return_to), "ts": time.time()}
    redirect_uri = settings.backend_public_url.rstrip("/") + "/api/github/oauth/callback"
    authorize = "https://github.com/login/oauth/authorize?" + urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": redirect_uri,
            "scope": settings.github_scope,
            "state": state,
            "allow_signup": "true",
        }
    )
    return RedirectResponse(authorize, status_code=302)


@router.get("/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    pend = _pending.pop(state, None)
    return_to = (pend or {}).get("return_to") or settings.frontend_base_url.rstrip("/")

    if error or not code or pend is None:
        return RedirectResponse(_with_param(return_to, "github", "error"), status_code=302)

    try:
        token = gh.exchange_code(code)
        user = gh.get_user(token)
    except Exception:  # noqa: BLE001 - any failure → a clean "error" back on the UI
        return RedirectResponse(_with_param(return_to, "github", "error"), status_code=302)

    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {
        "token": token,
        "login": user.get("login"),
        "name": user.get("name"),
        "avatar": user.get("avatar_url"),
        "ts": time.time(),
    }
    resp = RedirectResponse(_with_param(return_to, "github", "connected"), status_code=302)
    resp.set_cookie(
        _COOKIE,
        sid,
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@router.post("/disconnect")
def disconnect(request: Request, response: Response) -> dict:
    sid = request.cookies.get(_COOKIE)
    if sid:
        _sessions.pop(sid, None)
    response.delete_cookie(_COOKIE, path="/")
    return {"connected": False}


# ── push ─────────────────────────────────────────────────────────────────────
class PushRequest(BaseModel):
    name: Optional[str] = None
    private: bool = True
    description: Optional[str] = None


@router.post("/push/{project_id}")
def push(
    body: PushRequest,
    request: Request,
    project: Project = Depends(get_project),
) -> dict:
    s = _session(request)
    if not s:
        raise HTTPException(401, "Connect your GitHub account first.")
    try:
        return gh.push_project(
            s["token"],
            project,
            name=body.name,
            private=body.private,
            description=body.description,
        )
    except gh.GitHubError as e:
        raise HTTPException(400, str(e))
