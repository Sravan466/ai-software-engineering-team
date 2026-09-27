"""Pairing a computer to an account, and proving it is that computer afterwards.

The flow is RFC 8628's, typed the other way round — the website shows the code and
the computer types it — because the computer is where the connector runs:

  1. A signed-in account asks for a code (`create_code`). Single-use, ten minutes,
     only its hash stored.
  2. The connector asks who the code belongs to (`lookup`) and shows it: "This
     connects <computer> to <account> on <host>. Continue?" (§5.4, remote phishing).
  3. It makes an Ed25519 keypair and claims the code with the public half (`claim`).
     The device starts `pending`.
  4. The account approves it on the website. Until then it is sent nothing.

Every connection afterwards is opened with a signature only the private key can
make (`verify_handshake`), and a nonce is never accepted twice.

Attempts are limited per address and per code. The limits are counted in this
process, like the sign-in limits: one process is how this backend is deployed.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.connector import protocol as P
from app.core.auth import RateLimiter
from app.db.models import Device, PairingCode, User, _aware

#: Per address: lookups and claims, whatever code they name.
IP_LIMIT, IP_WINDOW = 20, 600.0
#: Codes one account may have waiting at once. Asking again retires the oldest.
MAX_OPEN_CODES = 3

limits = RateLimiter()


class PairingError(ValueError):
    """A pairing step that can't go ahead. The message is for a person."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


_INVALID = "That code isn't valid, has already been used, or has expired. Make a new one on the website."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def create_code(db: Session, user: User) -> tuple[PairingCode, str]:
    """A fresh code for `user`: (its row, the code itself — shown once, never stored)."""
    db.execute(delete(PairingCode).where(PairingCode.expires_at < _now() - timedelta(days=1)))
    open_codes = db.execute(
        select(PairingCode)
        .where(PairingCode.user_id == user.id, PairingCode.used_at.is_(None), PairingCode.expires_at > _now())
        .order_by(PairingCode.created_at)
    ).scalars().all()
    for stale in open_codes[: max(len(open_codes) - MAX_OPEN_CODES + 1, 0)]:
        stale.expires_at = _now()
    code = "".join(secrets.choice(P.CODE_ALPHABET) for _ in range(P.CODE_LENGTH))
    row = PairingCode(
        code_hash=digest(code),
        user_id=user.id,
        expires_at=_now() + timedelta(seconds=P.CODE_TTL_SECONDS),
    )
    db.add(row)
    db.commit()
    return row, code


def _check_ip(ip: str) -> None:
    wait = limits.blocked(f"ip:{ip}", IP_LIMIT, IP_WINDOW)
    if wait:
        raise PairingError(f"Too many attempts from this network. Try again in {int(wait)} seconds.", 429)
    limits.hit(f"ip:{ip}", IP_WINDOW)


def _live_code(db: Session, raw: str) -> PairingCode:
    """The code `raw` names, if it can still be used — counting this attempt against it."""
    code = P.normalise_code(raw)
    if not P.valid_code(code):
        raise PairingError(_INVALID)
    row = db.execute(select(PairingCode).where(PairingCode.code_hash == digest(code))).scalars().first()
    if row is None:
        raise PairingError(_INVALID)
    # Counted before anything else is checked, and written straight away, so a
    # failed claim still spends an attempt.
    db.execute(
        update(PairingCode).where(PairingCode.id == row.id).values(attempts=PairingCode.attempts + 1)
    )
    db.commit()
    db.refresh(row)
    expires = _aware(row.expires_at)
    if row.used_at is not None or expires is None or expires <= _now() or row.attempts > P.CODE_MAX_ATTEMPTS:
        raise PairingError(_INVALID)
    return row


def account_label(user: User) -> str:
    return user.email or user.display_name or "your account"


def lookup(db: Session, raw_code: str, ip: str) -> dict:
    """Who a code belongs to, for the connector's "Continue?" prompt."""
    _check_ip(ip)
    row = _live_code(db, raw_code)
    user = db.get(User, row.user_id)
    if user is None:
        raise PairingError(_INVALID)
    expires = _aware(row.expires_at)
    return {
        "account": account_label(user),
        "expires_in": max(int((expires - _now()).total_seconds()), 0) if expires else 0,
    }


def load_public_key(text: str) -> Ed25519PublicKey:
    try:
        raw = P.b64d(text)
    except Exception as e:  # noqa: BLE001
        raise PairingError("That public key isn't valid.") from e
    if len(raw) != 32:
        raise PairingError("That public key isn't valid.")
    return Ed25519PublicKey.from_public_bytes(raw)


def claim(
    db: Session,
    raw_code: str,
    ip: str,
    *,
    public_key: str,
    name: str,
    os: str,
    connector_version: str,
) -> tuple[Device, User]:
    """Use a code: make a pending device holding `public_key`. The code dies here."""
    _check_ip(ip)
    load_public_key(public_key)
    row = _live_code(db, raw_code)
    user = db.get(User, row.user_id)
    if user is None:
        raise PairingError(_INVALID)
    if db.execute(select(Device.id).where(Device.public_key == public_key)).first():
        raise PairingError("This key is already paired. Make a new one with `aiteam-connect forget`.", 409)
    # Single use, decided by the database rather than by the read above: of two
    # claims racing on one code, exactly one update matches.
    used = db.execute(
        update(PairingCode)
        .where(PairingCode.id == row.id, PairingCode.used_at.is_(None))
        .values(used_at=_now())
    )
    if not used.rowcount:
        db.rollback()
        raise PairingError(_INVALID)
    device = Device(
        owner_id=user.id,
        name=(name or "My computer").strip()[:120] or "My computer",
        public_key=public_key,
        status=P.STATE_PENDING,
        os=(os or "")[:64] or None,
        connector_version=(connector_version or "")[:32] or None,
        paired_from=ip[:64],
    )
    db.add(device)
    db.flush()
    db.execute(update(PairingCode).where(PairingCode.id == row.id).values(device_id=device.id))
    db.commit()
    return device, user


# ── proving it is the paired computer ───────────────────────────────────────
class _Nonces:
    """Nonces seen in the last few minutes: a signed handshake opens one connection."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_use(self, nonce: str) -> bool:
        now = time.monotonic()
        keep = 2 * P.HANDSHAKE_SKEW_SECONDS + 5
        with self._lock:
            self._seen = {n: t for n, t in self._seen.items() if now - t <= keep}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


nonces = _Nonces()


def verify(public_key: str, signature: str, message: bytes) -> bool:
    try:
        load_public_key(public_key).verify(P.b64d(signature), message)
        return True
    except (InvalidSignature, PairingError, ValueError):
        return False


def verify_handshake(db: Session, header: Optional[str], host: str) -> Optional[Device]:
    """The device a connection's `Authorization` header proves it is, or None."""
    fields = P.parse_auth_header(header)
    if fields is None:
        return None
    if abs(time.time() - fields["ts"]) > P.HANDSHAKE_SKEW_SECONDS:
        return None
    device = db.get(Device, fields["id"])
    if device is None:
        return None
    message = P.handshake_message(device.id, fields["ts"], fields["nonce"], host)
    if not verify(device.public_key, fields["sig"], message):
        return None
    # Checked after the signature, so only the key's holder can spend a nonce.
    if not nonces.first_use(fields["nonce"]):
        return None
    return device


# ── what a computer can run ─────────────────────────────────────────────────
def size_advice(ram_bytes: Optional[int]) -> Optional[dict]:
    """What size of model fits in this much memory — sizes and quantizations,
    never a model name. A 4-bit quantization weighs about 0.6 GB per billion
    parameters, and the OS, the context cache and the connector need the rest."""
    if not ram_bytes:
        return None
    gib = ram_bytes / 2**30
    if gib < 7:
        size, quant = "up to about 3B parameters", "Q4_K_M"
    elif gib < 12:
        size, quant = "up to about 7–8B parameters", "Q4_K_M"
    elif gib < 20:
        size, quant = "up to about 8B at Q5–Q6, or 12–14B at Q4", "Q4_K_M or Q5_K_M"
    elif gib < 36:
        size, quant = "up to about 14B at Q8, or 27–32B at Q4", "Q4_K_M to Q8_0"
    elif gib < 72:
        size, quant = "up to about 32B at Q8, or 70B at Q4", "Q4_K_M to Q8_0"
    else:
        size, quant = "70B and larger", "Q4_K_M or higher"
    return {
        "ram_gib": round(gib, 1),
        "size": size,
        "quantization": quant,
        "note": (
            "Leave room for the context window: a long build needs several GB on top of the "
            "model's own weight. Smaller quantizations (Q3 and below) lose noticeable quality."
        ),
    }
