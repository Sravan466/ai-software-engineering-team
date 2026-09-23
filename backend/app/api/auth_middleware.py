"""Every request is signed in, or refused — decided here, before any route runs.

A middleware rather than a dependency on each route, so a route added later is
protected without anyone remembering to protect it. Only health and signing in
itself are open. For everything else the session cookie is looked up, the account
is bound to the request (`app.core.identity`), and a request with no valid session
gets a 401 before it reaches any code.

It also refuses a state-changing request whose `Origin` is a site this backend
doesn't serve. The session cookie is `SameSite=Lax`, which already keeps it off
cross-site requests — but every port on `localhost` is the same *site*, so any other
app running on this machine could otherwise post to this one as the person
signed in.
"""
from __future__ import annotations

import json
from typing import Optional
from urllib.parse import urlparse

import anyio
from starlette.requests import cookie_parser

from app.core import auth, identity
from app.core.config import settings
from app.db.base import SessionLocal

#: Reachable without signing in. Exact paths: nothing beneath these is open.
PUBLIC = frozenset(
    {
        "/health",
        "/",
        "/api/auth/status",
        "/api/auth/signin",
        "/api/auth/signup",
        "/api/auth/signout",
    }
)

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


def _trusted_origins() -> set[str]:
    out = {_origin(o) for o in [*settings.cors_origin_list, settings.frontend_base_url, settings.backend_public_url]}
    return {o for o in out if o}


def _header(scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _cookie(scope) -> Optional[str]:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    # Starlette's parser, the one `request.cookies` uses: lenient on purpose. Every
    # app on localhost shares this browser's cookies for the name, and the strict
    # stdlib parser stops at the first one it can't read — so another dev server's
    # cookie would silently sign everyone out of this one.
    return cookie_parser(raw).get(auth.COOKIE)


def _lookup(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    db = SessionLocal()
    try:
        user = auth.user_for_token(db, token)
        return user.id if user is not None else None
    finally:
        db.close()


async def _refuse(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        if method in _UNSAFE:
            origin = _header(scope, b"origin")
            if origin and _origin(origin) not in _trusted_origins():
                await _refuse(send, 403, "This request came from a site this backend doesn't serve.")
                return

        if method == "OPTIONS":
            # A CORS preflight carries no cookie by design; CORS answers it.
            await self.app(scope, receive, send)
            return

        user_id = await anyio.to_thread.run_sync(_lookup, _cookie(scope))
        if user_id is None and path not in PUBLIC:
            await _refuse(send, 401, "Sign in to continue.")
            return

        scope.setdefault("state", {})["user_id"] = user_id
        token = identity.bind(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            identity.unbind(token)
