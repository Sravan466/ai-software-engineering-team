"""Signing in, signing out, and setting up the install's first account.

Email and password. The first account created on an install is its **owner**; when
the backend already held projects from before accounts existed, that account was
made by the migration without a password, and setting up claims it — every existing
build included. After that, new accounts can only be made when `ALLOW_SIGNUP` is on.

Setting up is taken only from this machine, or with a setup token — `SETUP_TOKEN`,
or the one-time token the backend prints in its log at startup — because on a fresh
backend reachable from a network, whoever arrives first would otherwise own it.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import auth
from app.core.auth import AuthError
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import get_db
from app.db.models import User

log = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: One setup at a time, so two people racing to claim a fresh install can't both win.
_SETUP_LOCK = threading.Lock()


class SignIn(BaseModel):
    email: str
    password: str


class SignUp(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None
    setup_token: Optional[str] = None


def _public(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_owner": user.is_owner,
    }


def _needs_setup(db: Session) -> bool:
    return (
        db.execute(select(func.count()).select_from(User).where(User.password_hash.is_not(None))).scalar_one()
        == 0
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


#: Made at startup when the install still needs setting up and no SETUP_TOKEN is
#: configured, and printed in the backend's log — so an install whose browser isn't
#: on loopback (Docker's bridge, another computer) can still be set up, by whoever
#: can read the server's output.
_GENERATED_SETUP_TOKEN: Optional[str] = None


def announce_setup_token(db: Session) -> Optional[str]:
    """Make and log a one-time setup token, when setting up is still to be done."""
    global _GENERATED_SETUP_TOKEN
    if settings.setup_token or not _needs_setup(db):
        _GENERATED_SETUP_TOKEN = None
        return None
    if _GENERATED_SETUP_TOKEN is None:
        _GENERATED_SETUP_TOKEN = secrets.token_urlsafe(18)
    log.warning(
        "This install has no account yet. Set it up at %s/signin. From anywhere but this "
        "machine, enter this one-time setup token: %s",
        settings.frontend_base_url.rstrip("/"),
        _GENERATED_SETUP_TOKEN,
    )
    return _GENERATED_SETUP_TOKEN


def _setup_token_ok(given: Optional[str]) -> bool:
    given = (given or "").strip()
    if not given:
        return False
    for expected in (settings.setup_token, _GENERATED_SETUP_TOKEN):
        if expected and hmac.compare_digest(given.encode(), expected.encode()):
            return True
    return False


#: Headers a reverse proxy adds. A request carrying one came from somewhere else,
#: whatever address it reached this process from.
_PROXIED = ("x-forwarded-for", "x-real-ip", "forwarded")


def _from_this_machine(request: Request) -> bool:
    if any(request.headers.get(h) for h in _PROXIED):
        return False
    host = _client(request)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _secure(request: Request) -> bool:
    if settings.session_cookie_secure is not None:
        return bool(settings.session_cookie_secure)
    return request.url.scheme == "https"


def _sign_in(response: Response, request: Request, db: Session, user: User) -> dict:
    token, expires = auth.start_session(db, user)
    response.set_cookie(
        auth.COOKIE,
        token,
        max_age=max(settings.session_ttl_hours, 1) * 3600,
        expires=expires,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )
    response.headers["Cache-Control"] = "no-store"
    return {"user": _public(user)}


def _limited(key: str, limit: int) -> None:
    wait = auth.attempts.blocked(key, limit, settings.signin_window_seconds)
    if wait:
        minutes = max(1, round(wait / 60))
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in about {minutes} minute{'s' if minutes != 1 else ''}.",
            headers={"Retry-After": str(int(wait))},
        )


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)) -> dict:
    """Who is signed in, and what the sign-in page should offer."""
    user_id = getattr(request.state, "user_id", None)
    user = db.get(User, user_id) if user_id else None
    needs_setup = _needs_setup(db)
    return {
        "user": _public(user) if user is not None and user.claimed else None,
        "needs_setup": needs_setup,
        #: Whether this browser can set the install up without a token.
        "setup_needs_token": needs_setup and not _from_this_machine(request),
        #: Whether an existing install takes new accounts.
        "signup_open": (not needs_setup) and settings.allow_signup,
        #: Whether this install held projects before accounts, waiting to be claimed.
        "has_unclaimed_work": needs_setup
        and db.execute(select(func.count()).select_from(User).where(User.is_owner.is_(True))).scalar_one() > 0,
    }


@router.post("/signin")
def signin(body: SignIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip_key = f"ip:{_client(request)}"
    _limited(ip_key, settings.signin_attempts_per_ip)
    auth.attempts.hit(ip_key, settings.signin_window_seconds)
    try:
        email = auth.normalise_email(body.email)
    except AuthError:
        raise HTTPException(status_code=401, detail="That email and password don't match an account.")
    # Failures are counted per email *from one address*: counted per email alone,
    # anyone could lock the owner out by guessing wrong eight times. The limit per
    # address still caps how fast any one place can guess.
    email_key = f"email:{email}|{_client(request)}"
    _limited(email_key, settings.signin_failures_per_email)
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not auth.verify_or_waste(body.password, user):
        auth.attempts.hit(email_key, settings.signin_window_seconds)
        raise HTTPException(status_code=401, detail="That email and password don't match an account.")
    auth.attempts.clear(email_key)
    return _sign_in(response, request, db, user)


@router.post("/signup", status_code=201)
def signup(body: SignUp, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip_key = f"ip:{_client(request)}"
    _limited(ip_key, settings.signin_attempts_per_ip)
    auth.attempts.hit(ip_key, settings.signin_window_seconds)
    try:
        email = auth.normalise_email(body.email)
        auth.check_password_rules(body.password)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = (body.display_name or "").strip()[:120] or None

    with _SETUP_LOCK:
        setting_up = _needs_setup(db)
        # Closed first, then taken: "that email has an account" is only ever said to
        # someone who could have made one.
        if not setting_up and not settings.allow_signup:
            raise HTTPException(
                status_code=403,
                detail="This install isn't taking new accounts. Ask its owner to turn on sign-ups.",
            )
        if setting_up and not _from_this_machine(request) and not _setup_token_ok(body.setup_token):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Set this install up from the machine it runs on, or with the setup token — "
                    "SETUP_TOKEN in its configuration, or the one-time token the backend printed "
                    "in its log when it started."
                ),
            )
        if db.execute(select(User).where(User.email == email)).scalars().first() is not None:
            raise HTTPException(status_code=409, detail="An account with that email already exists. Sign in instead.")
        if setting_up:
            user = _claim_or_create_owner(db, email, name, auth.hash_password(body.password))
        else:
            user = User(email=email, display_name=name, password_hash=auth.hash_password(body.password))
            db.add(user)
            db.commit()
            db.refresh(user)
    response.status_code = 201
    return _sign_in(response, request, db, user)


def _claim_or_create_owner(db: Session, email: str, name: Optional[str], password_hash: str) -> User:
    """The owner the migration made, now with an email and password — or a new one."""
    placeholder = db.execute(
        select(User)
        .where(User.is_owner.is_(True), User.password_hash.is_(None))
        .order_by(User.created_at)
    ).scalars().first()
    if placeholder is not None:
        claimed = db.execute(
            update(User)
            .where(User.id == placeholder.id, User.password_hash.is_(None))
            .values(email=email, display_name=name or placeholder.display_name, password_hash=password_hash)
        )
        db.commit()
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="This install was just set up. Sign in instead.")
        db.refresh(placeholder)
        return placeholder
    user = User(email=email, display_name=name, password_hash=password_hash, is_owner=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/signout")
def signout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    auth.end_session(db, request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/", httponly=True, samesite="lax", secure=_secure(request))
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)) -> dict:
    user_id = getattr(request.state, "user_id", None)
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return {"user": _public(user)}
