"""The Connectors tab: connect a service once, and every build that needs it uses it (#59).

    GET    /api/connectors                the catalog, with each connector's status
    GET    /api/connectors/{id}           one connector, plus the builds that use it
    PUT    /api/connectors/{id}           parse, test, and save when it is usable
    POST   /api/connectors/{id}/check     the same; with no values, re-test what's saved
    DELETE /api/connectors/{id}           disconnect — the builds using it turn to "later"
    POST   /api/connectors/preview        which connectors an idea would use, and why

Named "connectors" on screen and in these URLs; in code the feature is `integrations`,
because `app/connector/` and `routes/connector.py` already belong to the program a
person runs on their own computer (#46).

A value goes in and never comes back out: every response carries names and hints
(`…last4`) and nothing else. Writes are allowed only from an address this backend is
served at, and the check route is rate limited — it is otherwise a free way to try
stolen keys against a provider.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.api.routes.settings import _trusted_host
from app.build import integrations
from app.core import connectors_store, project_secrets, secretbox
from app.core.logging import get_logger
from app.db.base import get_db
from app.db.models import Project, User
from app.orchestration import connectors
from app.router.keycheck import RateLimiter

log = get_logger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

#: Same budget as cloud key checks.
limiter = RateLimiter()


class ConnectValues(BaseModel):
    #: NAME -> value, as pasted. Names left out keep what is saved, so changing the
    #: model or adding a webhook secret doesn't mean pasting the key again.
    values: dict[str, str] = Field(default_factory=dict)
    #: A live key (real charges, real email) is saved only when this says so.
    confirm_live: bool = False


class PreviewRequest(BaseModel):
    idea: str = ""
    use: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)


class DefaultRequest(BaseModel):
    capability: str
    id: Optional[str] = None


def guard_write(request: Request) -> None:
    if not _trusted_host(request):
        raise HTTPException(
            403,
            "Connectors can only be changed from an address this backend is served at "
            "(localhost, or BACKEND_PUBLIC_URL).",
        )


def require(iid: str) -> integrations.Integration:
    found = integrations.get(iid)
    if found is None:
        raise HTTPException(404, f"There's no connector called '{iid}'.")
    if not found.connectable:
        raise HTTPException(409, f"{found.label} is coming soon — it can't be connected yet.")
    return found


def _store(user: User) -> connectors_store.ConnectorsStore:
    return connectors_store.for_user(user.id)


def used_by(db: Session, user_id: str, iid: str) -> list[dict]:
    """The builds that use this connector through the account's connection.

    A build with its own key for it doesn't depend on the account's, so it isn't listed.
    """
    rows = db.execute(
        select(Project).where(Project.owner_id == user_id).order_by(Project.created_at.desc())
    ).scalars()
    out = []
    for p in rows:
        if iid not in connectors.used(p):
            continue
        if project_secrets.integrations_load(p.owner_id, p.id).get(iid):
            continue
        out.append({"id": p.id, "name": p.name or (p.idea or "")[:60], "status": p.status})
    return out


def _entry(found: integrations.Integration, store: connectors_store.ConnectorsStore) -> dict:
    out = found.as_dict()
    if found.connectable:
        out.update(store.public(found.id))
    else:
        out["connected"] = False
    return out


@router.get("")
def catalog(user: User = Depends(current_user)) -> dict:
    store = _store(user)
    try:
        store.readable()
        entries = [_entry(i, store) for i in integrations.catalog()]
    except connectors_store.StoreUnreadable as e:
        raise HTTPException(503, str(e))
    return {
        "categories": [
            {"id": c, "label": label, "count": sum(1 for e in entries if e["category"] == c)}
            for c, label in integrations.CATEGORIES
        ],
        "connectors": entries,
        "connected": sum(1 for e in entries if e.get("connected")),
        "defaults": store.defaults(),
    }


@router.get("/{iid}")
def one(iid: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    found = integrations.get(iid)
    if found is None:
        raise HTTPException(404, f"There's no connector called '{iid}'.")
    try:
        _store(user).readable()
    except connectors_store.StoreUnreadable as e:
        raise HTTPException(503, str(e))
    out = _entry(found, _store(user))
    out["used_by"] = used_by(db, user.id, iid) if found.connectable else []
    return out


def save_checked(
    found: integrations.Integration,
    given: dict[str, str],
    saved: dict[str, str],
    confirm_live: bool,
    who: str,
):
    """Parse → live-key confirmation → rate limit → check. Shared with the build card.

    Returns (parsed, checked) or a response dict when it stops before saving.
    """
    merged = {**saved, **{k: v for k, v in given.items()}}
    # An empty string clears an optional value; it doesn't keep the saved one.
    merged = {k: v for k, v in merged.items() if str(v or "").strip()}
    parsed = integrations.parse(found, merged)
    if parsed.problems:
        return {
            "ok": False,
            "status": "invalid",
            "problems": [p.as_dict() for p in parsed.problems],
        }
    if integrations.is_live(found, parsed.values) and not confirm_live:
        # Nothing touched the network, and nothing was saved.
        raise HTTPException(
            409,
            {
                "status": "needs_live_confirm",
                "message": f"This is a live {found.label} key — real charges and real emails. Use it anyway?",
            },
        )
    if not limiter.allow(who):
        raise HTTPException(429, "Too many key tests in a short time. Wait a few minutes and try again.")
    with project_secrets.held(integrations.secret_parts(found, parsed.values)):
        checked = integrations.check(found, parsed.values, parsed.mode)
    return parsed, checked


def _connect(found: integrations.Integration, body: ConnectValues, user: User, db: Session) -> dict:
    store = _store(user)
    try:
        saved = store.values(found.id) if store.entry(found.id) else {}
    except secretbox.SecretsLocked:
        saved = {}
    outcome = save_checked(found, body.values, saved, body.confirm_live, user.id)
    if isinstance(outcome, dict):
        return {**outcome, "connector": one(found.id, user, db)}
    parsed, checked = outcome
    if checked.result.status == integrations.FAILED:
        # A key that failed its test isn't saved over one that might work.
        log.info("Connector test failed for %s: %s", found.id, checked.result.reason)
        return {"ok": False, "status": "failed", "check": checked.as_dict(), "connector": one(found.id, user, db)}
    try:
        store.save(found.id, parsed.values, checked, parsed.mode)
    except connectors_store.StoreUnreadable as e:
        raise HTTPException(503, str(e))
    except secretbox.SecretsLocked as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "status": checked.result.status, "check": checked.as_dict(), "connector": one(found.id, user, db)}


@router.put("/{iid}")
def connect(
    iid: str,
    body: ConnectValues,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    guard_write(request)
    return _connect(require(iid), body, user, db)


@router.post("/{iid}/check")
def check(
    iid: str,
    request: Request,
    body: Optional[ConnectValues] = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """With values: the same as PUT. Without: test what is saved, again."""
    guard_write(request)
    found = require(iid)
    if body is not None and body.values:
        return _connect(found, body, user, db)
    store = _store(user)
    if not store.entry(iid).get("values"):
        raise HTTPException(400, f"{found.label} isn't connected, so there's nothing to re-test.")
    try:
        values = store.values(iid)
    except secretbox.SecretsLocked as e:
        raise HTTPException(503, str(e))
    if not limiter.allow(user.id):
        raise HTTPException(429, "Too many key tests in a short time. Wait a few minutes and try again.")
    checked = integrations.check(found, values, store.entry(iid).get("mode"))
    store.record_check(iid, checked, tested=values)
    return {
        "ok": checked.result.status != integrations.FAILED,
        "status": checked.result.status,
        "check": checked.as_dict(),
        "connector": one(iid, user, db),
    }


@router.delete("/{iid}")
def disconnect(
    iid: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Disconnect. Every build that used the account's connection turns to "later"."""
    guard_write(request)
    found = require(iid)
    affected = used_by(db, user.id, iid)
    try:
        removed = _store(user).remove(iid)
    except connectors_store.StoreUnreadable as e:
        raise HTTPException(503, str(e))
    if affected:
        for row in db.execute(select(Project).where(Project.id.in_([a["id"] for a in affected]))).scalars():
            connectors.mark_later(row, [iid])
        db.commit()
    return {"removed": removed, "affected": affected, "connector": one(found.id, user, db)}


@router.put("/defaults/capability")
def set_default(body: DefaultRequest, request: Request, user: User = Depends(current_user)) -> dict:
    """Which connected connector a build uses for a capability when several could."""
    guard_write(request)
    if body.id is not None:
        found = require(body.id)
        if found.capability != body.capability:
            raise HTTPException(422, f"{found.label} isn't a {body.capability} connector.")
    _store(user).set_default(body.capability, body.id)
    return {"defaults": _store(user).defaults()}


@router.post("/preview")
def preview(body: PreviewRequest, user: User = Depends(current_user)) -> dict:
    """Which connectors this idea would use, why, and whether each is connected.

    Read from the idea alone: Atlas's design can add one later (it might choose to
    send confirmation emails), and the build picks that up when it freezes its stack.
    """
    store = _store(user)
    connected = store.connected_ids()
    matches = integrations.relevant(
        body.idea,
        (),
        connected,
        use=body.use,
        skip=body.skip,
        defaults=store.defaults(),
        recent=store.recent(),
    )
    picked = {m.iid for m in matches}
    out = []
    for m in matches:
        found = integrations.get(m.iid)
        info = store.public(m.iid)
        out.append(
            {
                **m.as_dict(),
                "label": found.label,
                "capability": found.capability,
                "capability_label": integrations.CAPABILITY_LABELS.get(found.capability, found.capability),
                "connected": bool(info.get("connected")),
                "mode": info.get("mode"),
            }
        )
    # Switched off by the person, so the chip can show it as off rather than vanish.
    skipped = [
        {"id": i, "label": integrations.get(i).label}
        for i in body.skip
        if integrations.connectable(i) and i not in picked
    ]
    # What else could be added by hand: connected, and not already picked.
    addable = [
        {"id": i, "label": integrations.get(i).label}
        for i in connected
        if i not in picked and i not in body.skip
    ]
    return {"idea": body.idea, "connectors": out, "skipped": skipped, "addable": addable}
