"""One account's app connectors — Stripe, Resend, Clerk, OpenAI — encrypted, on disk.

    data/users/<user id>/connectors.local.json

    {"connections": {"stripe": {"values": {"STRIPE_SECRET_KEY": "enc:v1:…", …},
                                "check": {"status": "connected", "at": "…", …},
                                "mode": "test", "connected_at": "…"}},
     "defaults": {"payments": "stripe"}}

A sibling of `deploy_store`, in the same style: the same encryption
(`app.core.secretbox`), the same atomic owner-only writes, the same rule that a file
which can't be read is never saved over. Connected here once, a connector is linked
by reference into every build that needs it — so rotating a key here reaches all of
them, and nothing is copied into a project.

A value never leaves this module except to the one place allowed to use it (the
opt-in download, a deploy's public env): `public()` gives the page names, `…last4`
hints and the last check. Every secret value is registered with `app.core.scrub` as
it is saved or read, so one quoted back in an error is redacted.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.build import integrations
from app.core import scrub, secretbox, userdata
from app.core.atomic import write_private
from app.core.logging import get_logger

log = get_logger(__name__)

_NAME = "connectors.local.json"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_WARNED: set[str] = set()


class StoreUnreadable(RuntimeError):
    """The file exists and can't be read. It is left exactly as it is."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_record(checked: integrations.Checked) -> dict:
    """What is kept of a check: its outcome and when — never what was sent."""
    r = checked.result
    return {
        "status": r.status,
        "reason": r.reason,
        "message": r.message,
        "host": r.host,
        "latency_ms": r.latency_ms,
        "advice": r.advice,
        "at": _now(),
    }


class ConnectorsStore:
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
                f"The saved connectors ({_NAME}) can't be read, so nothing is being saved "
                f"over them: {type(e).__name__}. Move the file aside to start again."
            )
            # Once per file per process: a page polls, and the same line every few
            # seconds buries everything else in the log.
            if strict or str(self.path) not in _WARNED:
                _WARNED.add(str(self.path))
                log.error("%s", message)
            if strict:
                raise StoreUnreadable(message) from e
            return {}

    def _write(self, data: dict) -> None:
        write_private(self.path, json.dumps(data, indent=2))

    # ── reading ──────────────────────────────────────────────────────────────
    def connections(self) -> dict[str, dict]:
        found = self._read().get("connections")
        if not isinstance(found, dict):
            return {}
        return {k: dict(v) for k, v in found.items() if isinstance(v, dict) and integrations.connectable(k)}

    def entry(self, iid: str) -> dict:
        return self.connections().get(iid, {})

    def connected_ids(self) -> list[str]:
        """Every connector with something saved, in catalog order."""
        held = self.connections()
        return [i.id for i in integrations.catalog() if i.id in held and held[i.id].get("values")]

    def recent(self) -> dict[str, str]:
        return {k: str(v.get("connected_at") or "") for k, v in self.connections().items()}

    def defaults(self) -> dict[str, str]:
        found = self._read().get("defaults")
        return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}

    def values(self, iid: str) -> dict[str, str]:
        """The saved values, decrypted — for the one call that uses them.

        Raises `secretbox.SecretsLocked` when the encryption key can't open them.
        """
        sealed = self.entry(iid).get("values") or {}
        out: dict[str, str] = {}
        for name, value in sealed.items():
            plain = secretbox.decrypt(value) if isinstance(value, str) else None
            if plain:
                out[name] = plain
        scrub.register_many(integrations.secret_parts(integrations.get(iid), out))
        return out

    def public(self, iid: str) -> dict:
        """What the page may know about one connection: hints, mode, last check."""
        found = integrations.get(iid)
        entry = self.entry(iid)
        if found is None or not entry.get("values"):
            return {"connected": False}
        try:
            saved = integrations.hints(found, self.values(iid))
        except secretbox.SecretsLocked:
            saved = [{"name": n, "hint": "(can't be read — connect it again)", "side": "server"} for n in entry["values"]]
        return {
            "connected": True,
            "saved": saved,
            "check": entry.get("check"),
            "mode": entry.get("mode"),
            "connected_at": entry.get("connected_at"),
            "models": entry.get("models") or [],
        }

    # ── writing ──────────────────────────────────────────────────────────────
    def save(
        self,
        iid: str,
        values: dict[str, str],
        checked: integrations.Checked,
        mode: Optional[str],
    ) -> None:
        """Replace `iid`'s connection with `values` (encrypted) and its check."""
        found = integrations.get(iid)
        with self._lock():
            data = self._read(strict=True)
            connections = data.get("connections") if isinstance(data.get("connections"), dict) else {}
            previous = connections.get(iid) if isinstance(connections.get(iid), dict) else {}
            connections[iid] = {
                "values": {n: secretbox.encrypt(v) for n, v in values.items() if v},
                "check": check_record(checked),
                "mode": checked.mode or mode,
                # Kept across a replace: "connected since" is when it was first connected.
                "connected_at": previous.get("connected_at") or _now(),
                "models": checked.models[:400] if checked.models else previous.get("models") or [],
            }
            data["connections"] = connections
            self._write(data)
        # A replaced value is not forgotten: a call in flight may still quote it.
        scrub.register_many(integrations.secret_parts(found, values))
        log.info(
            "Connector %s saved for an account: %s (%s)",
            iid,
            ", ".join(sorted(n for n, v in values.items() if v)),
            checked.result.status,
        )

    def readable(self) -> None:
        """Raise `StoreUnreadable` when the file is there and can't be read — so a
        page says so, rather than showing nothing connected."""
        self._read(strict=True)

    def record_check(self, iid: str, checked: integrations.Checked, tested: Optional[dict[str, str]] = None) -> bool:
        """A re-test's result, on the values already saved — and only if they are
        still the values that were tested (a Replace may have landed meanwhile)."""
        with self._lock():
            data = self._read(strict=True)
            connections = data.get("connections") or {}
            entry = connections.get(iid)
            if not isinstance(entry, dict):
                return False
            if tested is not None:
                try:
                    if self.values(iid) != tested:
                        return False
                except secretbox.SecretsLocked:
                    return False
            entry["check"] = check_record(checked)
            if checked.mode:
                entry["mode"] = checked.mode
            if checked.models:
                entry["models"] = checked.models[:400]
            self._write(data)
        return True

    def set_default(self, capability: str, iid: Optional[str]) -> None:
        with self._lock():
            data = self._read(strict=True)
            defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
            if iid:
                defaults[capability] = iid
            else:
                defaults.pop(capability, None)
            data["defaults"] = defaults
            self._write(data)

    def remove(self, iid: str) -> bool:
        with self._lock():
            data = self._read(strict=True)
            connections = data.get("connections") or {}
            if iid not in connections:
                return False
            connections.pop(iid)
            data["connections"] = connections
            defaults = data.get("defaults") or {}
            data["defaults"] = {k: v for k, v in defaults.items() if v != iid}
            self._write(data)
        log.info("Connector %s disconnected for an account", iid)
        return True


def for_user(user_id: str) -> ConnectorsStore:
    return ConnectorsStore(user_id)


def connected_ids(user_id: Optional[str]) -> list[str]:
    """Best effort, for the pipeline: an unreadable store reads as nothing connected."""
    if not user_id:
        return []
    try:
        return ConnectorsStore(user_id).connected_ids()
    except (ValueError, OSError):
        return []


def register_all() -> int:
    """At startup: tell the scrubber every saved connector value. Returns how many files."""
    root = userdata.ROOT
    if not root.is_dir():
        return 0
    count = 0
    for path in root.glob(f"*/{_NAME}"):
        store = ConnectorsStore(path.parent.name)
        try:
            for iid in store.connections():
                try:
                    store.values(iid)
                except secretbox.SecretsLocked:
                    log.warning("A saved %s connector can't be decrypted with the current key.", iid)
            count += 1
        except (ValueError, OSError):
            continue
    return count
