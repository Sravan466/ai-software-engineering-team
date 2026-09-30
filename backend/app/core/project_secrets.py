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
_LOCK = threading.Lock()


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
        previous = load(owner_id, project_id)
        target = _path(owner_id, project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_private(target, json.dumps(record, indent=2))
    _forget(previous)
    for value in values.values():
        scrub.register(value)
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


def _forget(record: dict) -> None:
    for sealed in (record.get("values") or {}).values():
        try:
            scrub.forget(secretbox.decrypt(sealed))
        except secretbox.SecretsLocked:
            pass


def remove(owner_id: str, project_id: str) -> bool:
    """Delete the saved credentials. True if there were any."""
    with _LOCK:
        previous = load(owner_id, project_id)
        path = _path(owner_id, project_id)
        existed = path.exists()
        if existed:
            path.unlink()
    _forget(previous)
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
    """The saved values, decrypted — for the opt-in download only."""
    out: dict[str, str] = {}
    for name, sealed in (load(owner_id, project_id).get("values") or {}).items():
        value = secretbox.decrypt(sealed)
        if value:
            scrub.register(value)
            out[name] = value
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
