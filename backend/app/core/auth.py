"""Passwords, sessions and sign-in rate limits.

**Passwords** are hashed with Argon2id, the memory-hard KDF OWASP recommends first:
64 MiB and a random salt per hash, stored with its parameters so they can be raised
later without breaking a single existing account. Checking one takes a comparable
time whether or not the account exists (`verify_or_waste`), so a sign-in's timing
doesn't say which emails have accounts.

**Sessions** are 32 random bytes in an `HttpOnly`, `SameSite=Lax` cookie — `Secure`
whenever the request came over HTTPS. Only a SHA-256 of the token is stored, so a
copy of the database is not a way in. Each lasts `SESSION_TTL_HOURS`, and signing
out deletes it.

**Rate limits** are counted in this process: attempts per address, and failures per
email, in a sliding window. A backend run as several processes would need them in
a shared store; one process is how this one is deployed.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AuthSession, User, _aware

COOKIE = "aiteam_session"

# ── passwords ────────────────────────────────────────────────────────────────
#: Argon2id at the RFC 9106 "low memory" parameters — 64 MiB, 3 passes, 4 lanes —
#: which is above OWASP's minimum. The parameters are stored in each hash, so they
#: can be raised later: `needs_rehash` says when a stored hash is below them.
_HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

MIN_PASSWORD = 10
#: Past this a password is refused rather than hashed: nothing a person types needs
#: more, and a megabyte "password" is only ever a probe.
MAX_PASSWORD = 256

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    """A sign-in or sign-up that can't go ahead. The message is for a person."""


def normalise_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 320 or not _EMAIL.match(email):
        raise AuthError("Enter an email address like name@example.com.")
    return email


def check_password_rules(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters for your password.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Use at most {MAX_PASSWORD} characters for your password.")


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not password or len(password) > MAX_PASSWORD:
        return False
    try:
        return _HASHER.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _HASHER.check_needs_rehash(stored)
    except InvalidHashError:
        return True


#: Hashed once, lazily: checked against when an email has no account, so the answer
#: takes as long as a real check.
_DUMMY: Optional[str] = None


def verify_or_waste(password: str, user: Optional[User]) -> bool:
    global _DUMMY
    if user is None or not user.password_hash:
        if _DUMMY is None:
            _DUMMY = hash_password(secrets.token_urlsafe(16))
        verify_password(password or "x", _DUMMY)
        return False
    return verify_password(password, user.password_hash)


# ── sessions ─────────────────────────────────────────────────────────────────
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_session(db: Session, user: User) -> tuple[str, datetime]:
    """A new session for `user`: (the token for the cookie, when it expires)."""
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(hours=max(settings.session_ttl_hours, 1))
    db.add(AuthSession(token_hash=_digest(token), user_id=user.id, expires_at=expires))
    # Expired sessions are cleared as new ones start, so the table stays the size
    # of the people actually signed in.
    db.execute(delete(AuthSession).where(AuthSession.expires_at < _now()))
    db.commit()
    return token, expires


def user_for_token(db: Session, token: Optional[str]) -> Optional[User]:
    """The account a cookie's token signs in, or None when it signs in nobody."""
    if not token or len(token) > 128:
        return None
    row = db.execute(
        select(AuthSession).where(AuthSession.token_hash == _digest(token))
    ).scalars().first()
    if row is None:
        return None
    expires = _aware(row.expires_at)
    if expires is None or expires <= _now():
        db.delete(row)
        db.commit()
        return None
    user = db.get(User, row.user_id)
    return user if user is not None and user.claimed else None


def end_session(db: Session, token: Optional[str]) -> None:
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _digest(token)))
        db.commit()


def end_all_sessions(db: Session, user_id: str) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    db.commit()


# ── rate limits ──────────────────────────────────────────────────────────────
class RateLimiter:
    """A sliding-window counter per key. `hit` records; `blocked` says for how long."""

    def __init__(self) -> None:
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, window: float, now: float) -> deque:
        events = self._events.setdefault(key, deque())
        while events and now - events[0] > window:
            events.popleft()
        return events

    def blocked(self, key: str, limit: int, window: float) -> float:
        """Seconds until `key` may try again — 0 when it may now."""
        now = time.monotonic()
        with self._lock:
            events = self._trim(key, window, now)
            if len(events) < max(limit, 1):
                return 0.0
            return max(window - (now - events[0]), 1.0)

    def hit(self, key: str, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._trim(key, window, now).append(now)
            if len(self._events) > 50_000:
                # Bounded: a flood of distinct addresses can't grow this without end.
                for stale in [k for k, v in self._events.items() if not v][:10_000]:
                    self._events.pop(stale, None)

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


attempts = RateLimiter()
