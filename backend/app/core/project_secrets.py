"""A project's database credentials: encrypted, owner-only, and write-only.

One file per project, beside the account's other settings:

    data/users/<user id>/projects/<project id>/secrets.local.json

    {"database": "mongodb", "provider": "atlas",
     "values": {"MONGODB_URI": "enc:v1:…"},
     "check": {"status": "connected", "host": "…", "latency_ms": 84, "at": "…"}}

Every value is encrypted with the same key as the cloud API keys
(`app.core.secretbox`). Nothing here is ever returned to the browser: `summary`
gives each variable's name and a hint (a host, or `…last4`), and `reveal` is for the
one place a value is allowed to leave — the download, when the person asked for it.

Values are registered with `app.core.scrub` as they are saved and read, so a
fragment of one that turns up in a log line or an error is redacted.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.build import dbconnect
from app.core import scrub, secretbox, userdata
from app.core.atomic import write_private
from app.core.logging import get_logger

log = get_logger(__name__)

_NAME = "secrets.local.json"
_PROJECT_ID = re.compile(r"^[0-9a-f]{32}$")
#: Re-entrant: a save forgets, writes and registers as one step, so two saves racing
#: on one project can't leave the file holding values the scrubber doesn't know.
_LOCK = threading.RLock()
#: What this process has told the scrubber, per project — so a value is registered
#: once however many times it is read, and forgotten exactly once when replaced.
_REGISTERED: dict[str, list[str]] = {}


def _dir(owner_id: str, project_id: str) -> Path:
    if not _PROJECT_ID.match(project_id or ""):
        raise ValueError("That isn't a project id.")
    return userdata.directory(owner_id) / "projects" / project_id


def _path(owner_id: str, project_id: str) -> Path:
    return _dir(owner_id, project_id) / _NAME


def load(owner_id: str, project_id: str) -> dict:
    """The stored record, still encrypted. Empty when nothing is saved."""
    path = _path(owner_id, project_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("The database credentials file for project %s can't be read.", project_id)
        return {}
    return data if isinstance(data, dict) else {}


def save(
    owner_id: str,
    project_id: str,
    contract: dbconnect.Contract,
    values: dict[str, str],
    check: dbconnect.CheckResult,
) -> dict:
    """Replace whatever was saved with `values`, encrypted. Returns the new record."""
    record = {
        "database": contract.database,
        "provider": contract.provider,
        "values": {name: secretbox.encrypt(value) for name, value in values.items() if value},
        "check": _check_record(check),
    }
    with _LOCK:
        target = _path(owner_id, project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_private(target, json.dumps(record, indent=2))
        _forget(project_id)
        _register(project_id, dbconnect.secret_parts(contract, values))
    # Names only. The values are never in a log line.
    log.info(
        "Database credentials saved for project %s: %s (%s)",
        project_id,
        ", ".join(sorted(record["values"])),
        check.status,
    )
    return record


def _check_record(check: dbconnect.CheckResult) -> dict:
    return {
        "status": check.status,
        "reason": check.reason,
        "host": check.host,
        "latency_ms": check.latency_ms,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _register(project_id: str, parts: list[str]) -> None:
    with _LOCK:
        if project_id in _REGISTERED:
            return
        _REGISTERED[project_id] = parts
        scrub.register_many(parts)


def _forget(project_id: str) -> None:
    with _LOCK:
        parts = _REGISTERED.pop(project_id, [])
        for part in parts:
            scrub.forget(part)


@contextmanager
def held(parts: list[str]):
    """Scrub `parts` while something that might echo them runs — a connection test
    of values not saved yet, whose driver may quote them back in an error."""
    for part in parts:
        scrub.register(part)
    try:
        yield
    finally:
        for part in parts:
            scrub.forget(part)


def _decrypted(record: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, sealed in (record.get("values") or {}).items():
        value = secretbox.decrypt(sealed)
        if value:
            out[name] = value
    return out


def _parts(record: dict, values: dict[str, str]) -> list[str]:
    contract = dbconnect.contract_for(record.get("database"), record.get("provider"))
    return dbconnect.secret_parts(contract, values) if contract is not None else []


def holds_saved_secret(owner_id: Optional[str], project_id: str, text: str) -> bool:
    """Whether `text` contains a secret saved for this project — whatever its shape.

    Read from the file, not from what this process happens to have registered: a
    40-character AWS secret has no pattern of its own, and after a restart nothing
    else would know it.
    """
    if not owner_id or not text:
        return False
    # Under the lock, so a remove() landing between the read and the register can't
    # leave a deleted secret registered for the rest of the process.
    with _LOCK:
        record = load(owner_id, project_id)
        try:
            values = _decrypted(record)
        except secretbox.SecretsLocked:
            return False
        parts = _parts(record, values)
        _register(project_id, parts)
    return any(p in text for p in parts)


def register_all() -> int:
    """At startup: tell the scrubber every saved secret, so a log line or an error
    after a restart is scrubbed as well as one before it. Returns how many projects."""
    count = 0
    root = userdata.ROOT
    if not root.is_dir():
        return 0
    for path in root.glob(f"*/projects/*/{_NAME}"):
        project_id = path.parent.name
        owner_id = path.parent.parent.parent.name
        try:
            record = load(owner_id, project_id)
            _register(project_id, _parts(record, _decrypted(record)))
            count += 1
        except (secretbox.SecretsLocked, ValueError, OSError):
            log.warning("Couldn't read the saved database credentials for project %s.", project_id)
    return count


def snapshot(owner_id: str, project_id: str) -> Optional[str]:
    """The file as it is now (None if absent) — to put back if a save's transaction fails."""
    try:
        return _path(owner_id, project_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def restore(owner_id: str, project_id: str, previous: Optional[str]) -> None:
    """Put back what `snapshot` returned, and the scrubber's view of it."""
    with _LOCK:
        path = _path(owner_id, project_id)
        _forget(project_id)
        if previous is None:
            if path.exists():
                path.unlink()
            return
        write_private(path, previous)
        try:
            record = json.loads(previous)
            _register(project_id, _parts(record, _decrypted(record)))
        except (ValueError, secretbox.SecretsLocked):
            pass


def remove(owner_id: str, project_id: str) -> bool:
    """Delete the saved credentials. True if there were any."""
    with _LOCK:
        path = _path(owner_id, project_id)
        existed = path.exists()
        if existed:
            path.unlink()
    _forget(project_id)
    if existed:
        log.info("Database credentials removed for project %s", project_id)
    return existed


def remove_project(owner_id: Optional[str], project_id: str) -> None:
    """Everything stored for a project that is being deleted."""
    if not owner_id:
        return
    try:
        remove(owner_id, project_id)
        folder = _dir(owner_id, project_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
    except ValueError:
        pass


def reveal(owner_id: str, project_id: str) -> dict[str, str]:
    """The saved values, decrypted — for the opt-in download only.

    Raises `secretbox.SecretsLocked` when the encryption key can't open them.
    """
    record = load(owner_id, project_id)
    out = _decrypted(record)
    _register(project_id, _parts(record, out))
    return out


def summary(owner_id: str, project_id: str, contract: Optional[dbconnect.Contract]) -> dict:
    """What the page may know: which names are saved, a hint for each, the last check."""
    record = load(owner_id, project_id)
    sealed = record.get("values") or {}
    same = contract is not None and record.get("database") == contract.database
    saved: list[dict] = []
    if same:
        for name, value in sealed.items():
            var = contract.var(name) if contract else None
            try:
                shown = dbconnect.hint(var, secretbox.decrypt(value) or "")
            except secretbox.SecretsLocked:
                shown = "(can't be read — save it again)"
            saved.append({"name": name, "hint": shown})
    return {
        "saved": saved,
        "provider": record.get("provider") if same else None,
        "check": record.get("check") if same and saved else None,
    }
