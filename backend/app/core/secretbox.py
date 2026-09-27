"""Encrypting the API keys the settings files hold.

A saved key is written as `enc:v1:<Fernet token>` — authenticated encryption, so a
key that was tampered with, or encrypted under another key, fails to open rather
than opening as garbage. Anything without the prefix is a key saved before this
existed; it is still read, and `secrets_store.migrate_all` rewrites it encrypted.

Where the encryption key comes from, first match wins:

1. `SECRETS_ENCRYPTION_KEY` — one Fernet key, or several comma-separated to rotate:
   the first encrypts, all of them decrypt (`MultiFernet`).
2. `SECRETS_KEY_FILE` (default `~/.config/aiteam/secrets.key`) — generated owner-only
   on first use, and said so in the log. Outside `data/` on purpose.

The threat this answers is a copied `data/` folder, a backup of it, or a stray file:
none of those carry the encryption key. It does not protect against someone who
controls the running backend — that process has to be able to read the keys to
use them.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

PREFIX = "enc:v1:"

_LOCK = threading.Lock()
_BOX = None  # MultiFernet, built once


class SecretsLocked(RuntimeError):
    """A saved key that cannot be decrypted with the encryption key(s) available."""


def _key_file() -> Path:
    return Path(os.path.expanduser(settings.secrets_key_file))


def _load_or_create_file() -> bytes:
    from cryptography.fernet import Fernet

    path = _key_file()
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    key = Fernet.generate_key()
    try:
        # Owner-only from the first byte, and never over a file another process
        # created a moment ago — that one is the key, and this one would orphan it.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    log.warning(
        "Generated the key that encrypts saved API keys, at %s. Back it up somewhere other "
        "than data/ — without it, saved keys can't be read and have to be entered again. "
        "Or set SECRETS_ENCRYPTION_KEY to manage it yourself.",
        path,
    )
    return key


def _box():
    global _BOX
    if _BOX is not None:
        return _BOX
    with _LOCK:
        if _BOX is not None:
            return _BOX
        from cryptography.fernet import Fernet, MultiFernet

        configured = settings.secrets_encryption_key
        raw = configured.get_secret_value().strip() if configured else ""
        keys = [k.strip().encode() for k in raw.split(",") if k.strip()] or [_load_or_create_file()]
        try:
            _BOX = MultiFernet([Fernet(k) for k in keys])
        except (ValueError, TypeError) as e:
            raise SecretsLocked(
                "The encryption key for saved API keys isn't a valid Fernet key "
                "(32 url-safe base64-encoded bytes)."
            ) from e
        return _BOX


def reset() -> None:
    """Forget the loaded key — for tests that point it somewhere else."""
    global _BOX
    with _LOCK:
        _BOX = None


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    if is_encrypted(plain):
        return plain
    return PREFIX + _box().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(value: Optional[str]) -> Optional[str]:
    """The key inside `value`. Plaintext from before encryption is returned as is."""
    if value is None or not isinstance(value, str):
        return None
    if not is_encrypted(value):
        return value
    from cryptography.fernet import InvalidToken

    try:
        return _box().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as e:
        raise SecretsLocked(
            "This key was saved under a different encryption key. Restore the "
            "SECRETS_ENCRYPTION_KEY (or key file) it was saved with, or enter the key again."
        ) from e


def rotate(value: str) -> str:
    """Re-encrypt under the current (first) key. Plaintext is encrypted."""
    if not is_encrypted(value):
        return encrypt(value)
    return PREFIX + _box().rotate(value[len(PREFIX):].encode("ascii")).decode("ascii")
