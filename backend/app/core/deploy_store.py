"""One account's GitHub and Vercel connections, encrypted, owner-only, on disk.

    data/users/<user id>/deploy.local.json

    {"github": {"token": "enc:v1:…", "login": "ada", "name": "Ada", "avatar": "…",
                "scopes": "repo", "connected_at": "…"},
     "vercel": {"token": "enc:v1:…", "username": "ada", "hint": "…a1b2",
                "checked_at": "…"}}

A sibling of `secrets_store.ProviderStore`, not a part of it: that file's names are
model providers, and a deploy token is a different kind of fact. Same encryption
(`app.core.secretbox`), same atomic owner-only writes, same rule that a file which
can't be read is never saved over.

The GitHub token used to live in a process-local dict behind a browser cookie, so a
restart, or another browser, meant connecting again. Here it belongs to the account.

A token never leaves this module except to the call that uses it: `public()` gives
the page a login, a username and a `…last4` hint. Every token is registered with
`app.core.scrub` as it is saved or read, so one quoted back in an error is redacted.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core import scrub, secretbox, userdata
from app.core.atomic import write_private
from app.core.logging import get_logger

log = get_logger(__name__)

_NAME = "deploy.local.json"
GITHUB = "github"
VERCEL = "vercel"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class StoreUnreadable(RuntimeError):
    """The file exists and can't be read. It is left exactly as it is."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hint(token: str) -> str:
    return f"…{token[-4:]}" if token and len(token) >= 8 else "…"


class DeployStore:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    @property
    def path(self) -> Path:
        return userdata.path(self.user_id, _NAME)

    def _lock(self) -> threading.RLock:
        key = str(self.path)
        with _LOCKS_GUARD:
            lock = _LOCKS.get(key)
            if lock is None:
                lock = _LOCKS[key] = threading.RLock()
            return lock

    def _read(self, *, strict: bool = False) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("it doesn't hold a JSON object")
            return data
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001 - reported, never treated as empty on write
            message = (
                f"The saved deploy connections ({_NAME}) can't be read, so nothing is being "
                f"saved over them: {type(e).__name__}. Move the file aside to start again."
            )
            log.error("%s", message)
            if strict:
                raise StoreUnreadable(message) from e
            return {}

    # ── reading ──────────────────────────────────────────────────────────────
    def entry(self, kind: str) -> dict:
        """The saved entry for `kind`, token still encrypted. Empty when none."""
        found = self._read().get(kind)
        return dict(found) if isinstance(found, dict) else {}

    def token(self, kind: str) -> Optional[str]:
        """The token, decrypted — for the one call that uses it. None when there is
        none, or when it was saved under another encryption key (connect again)."""
        sealed = self.entry(kind).get("token")
        if not isinstance(sealed, str) or not sealed:
            return None
        try:
            value = secretbox.decrypt(sealed)
        except secretbox.SecretsLocked:
            log.warning("A saved %s token can't be decrypted with the current key.", kind)
            return None
        scrub.register(value)
        return value

    def public(self) -> dict:
        """What the page may know: who each connection is, never a token."""
        gh = self.entry(GITHUB)
        vc = self.entry(VERCEL)
        return {
            GITHUB: {
                "connected": bool(gh.get("token")),
                "login": gh.get("login"),
                "name": gh.get("name"),
                "avatar": gh.get("avatar"),
                "connected_at": gh.get("connected_at"),
            },
            VERCEL: {
                "connected": bool(vc.get("token")),
                "username": vc.get("username"),
                "hint": vc.get("hint"),
                "checked_at": vc.get("checked_at"),
            },
        }

    # ── writing ──────────────────────────────────────────────────────────────
    def save(self, kind: str, token: str, **facts: object) -> None:
        """Replace the `kind` connection with `token` (encrypted) and its facts."""
        with self._lock():
            data = self._read(strict=True)
            data[kind] = {
                "token": secretbox.encrypt(token),
                **{k: v for k, v in facts.items() if v is not None},
            }
            write_private(self.path, json.dumps(data, indent=2))
        # A replaced token is not forgotten by the scrubber: a call already in flight
        # with it can still quote it back in an error.
        scrub.register(token)

    def remove(self, kind: str) -> bool:
        """Forget the `kind` connection. True if there was one."""
        with self._lock():
            data = self._read(strict=True)
            old = data.pop(kind, None)
            if old is None:
                return False
            write_private(self.path, json.dumps(data, indent=2))
        # Still scrubbed for the rest of this process, for the same reason as `save`.
        return True


def for_user(user_id: str) -> DeployStore:
    return DeployStore(user_id)


def register_all() -> int:
    """At startup: tell the scrubber every saved deploy token. Returns how many files."""
    root = userdata.ROOT
    if not root.is_dir():
        return 0
    count = 0
    for path in root.glob(f"*/{_NAME}"):
        store = DeployStore(path.parent.name)
        try:
            for kind in (GITHUB, VERCEL):
                store.token(kind)
            count += 1
        except (ValueError, OSError):
            continue
    return count

