"""Where a build's app connectors stand, read live (#59).

A connector a build uses is answered in one of three ways, in order of precedence:

  **project** — keys saved for this build only (at its question, without saving them
  to the account, or as "use a different key for this build"). Always wins.

  **account** — connected once in the Connectors tab, and linked by reference. Never
  copied in, so a key rotated there reaches every build at once, and disconnecting it
  there turns every build that used it to "later".

  **later** — the person said they'll add it later (`project.integrations_status`).

Anything else is unanswered, and an unanswered connector is what raises the build's
connectors question. Nothing here holds a value longer than the call that needs it.
"""
from __future__ import annotations

from typing import Optional

from app.build import dbconnect, integrations
from app.core import connectors_store, project_secrets, secretbox
from app.core.logging import get_logger
from app.db.models import Project
from app.orchestration.charter import Charter

log = get_logger(__name__)

LATER = "later"
PROJECT = "project"
ACCOUNT = "account"


def used(project: Project) -> tuple[str, ...]:
    """The connectors the build's charter says it uses."""
    charter = Charter.from_dict(project.charter)
    return charter.integrations if charter else ()


def _account(project: Project) -> connectors_store.ConnectorsStore:
    return connectors_store.for_user(project.owner_id or "")


def status_of(project: Project, iid: str) -> tuple[Optional[str], Optional[str]]:
    """(status, source): status is `connected`, `unchecked`, `later` or None."""
    if project.owner_id:
        mine = project_secrets.integrations_load(project.owner_id, project.id).get(iid)
        if mine and mine.get("values"):
            return (mine.get("check") or {}).get("status") or dbconnect.UNCHECKED, PROJECT
        try:
            entry = _account(project).entry(iid)
        except (ValueError, OSError):
            entry = {}
        if entry.get("values"):
            return (entry.get("check") or {}).get("status") or dbconnect.UNCHECKED, ACCOUNT
    answered = (project.integrations_status or {}).get(iid)
    return (LATER, None) if answered == LATER else (None, None)


def unanswered(project: Project) -> list[str]:
    return [iid for iid in used(project) if status_of(project, iid)[0] is None]


def not_connected(project: Project) -> list[str]:
    """The connectors still to connect: put off till later, or never answered."""
    return [iid for iid in used(project) if status_of(project, iid)[0] in (None, LATER)]


def values_for(project: Project, *, client_only: bool = False) -> dict[str, str]:
    """Every saved connector value this build uses — for the opt-in download and a
    deploy's public env only. Raises `secretbox.SecretsLocked` like `reveal`."""
    out: dict[str, str] = {}
    if not project.owner_id:
        return out
    for iid in used(project):
        found = integrations.get(iid)
        if found is None:
            continue
        _, source = status_of(project, iid)
        if source == PROJECT:
            values = project_secrets.integration_values(project.owner_id, project.id, iid)
        elif source == ACCOUNT:
            values = _account(project).values(iid)
        else:
            continue
        for name, value in values.items():
            var = found.var(name)
            if var is None or (client_only and var.side != "client"):
                continue
            out[name] = value
    return out


def saved_names(project: Project) -> tuple[str, ...]:
    """The names (never the values) of connector variables saved for this build."""
    names: list[str] = []
    try:
        for name in values_for(project):
            names.append(name)
    except secretbox.SecretsLocked:
        pass
    return tuple(names)


def mark_later(project: Project, ids: list[str]) -> None:
    status = dict(project.integrations_status or {})
    for iid in ids:
        status[iid] = LATER
    project.integrations_status = status


def forget(project: Project, ids: list[str]) -> None:
    """A connector the build no longer uses: its answer and its own keys go."""
    status = dict(project.integrations_status or {})
    for iid in ids:
        status.pop(iid, None)
        if project.owner_id:
            try:
                project_secrets.remove_integration(project.owner_id, project.id, iid)
            except ValueError:
                pass
    project.integrations_status = status or None
