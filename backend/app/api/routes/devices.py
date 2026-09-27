"""The website's half of the connector: pair a computer, approve it, see what it has.

Everything here is the signed-in account's own. Another account's device answers
exactly as one that doesn't exist — a 404 — like every other owned thing.

Nothing here can tell a computer where to connect or what to run: the only
requests that reach one are the typed operations in `app.connector.protocol`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.api.routes.connector import refresh_hello
from app.connector import pairing, protocol as P
from app.connector.hub import ConnectorError, hub
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import Device, PairingCode, User, _aware
from app.router.runtimes import table

log = get_logger(__name__)

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.isoformat() if value else None


def _server_url() -> str:
    return settings.backend_public_url.rstrip("/")


def connector_info() -> dict:
    """The pinned install command, per OS, and what this server expects of it."""
    pin = f"{P.PACKAGE}=={P.CONNECTOR_VERSION}"
    server = _server_url()
    unix = f"pipx run --spec {pin} aiteam-connect --server {server}"
    return {
        "package": P.PACKAGE,
        "version": P.CONNECTOR_VERSION,
        "min_version": P.MIN_CONNECTOR_VERSION,
        "server": server,
        "commands": {
            "macos": unix,
            "linux": unix,
            "windows": f"py -m pipx run --spec {pin} aiteam-connect --server {server}",
        },
        # For an install run from this repository, before the package is on PyPI.
        "source_command": f"cd backend && .venv/bin/python -m aiteam_connect --server {server}",
        "verify": (
            f"pip download --no-deps {pin} && pipx run pypi-attestations verify pypi "
            "--repository https://github.com/Sravan466/ai-software-engineering-team "
            f"pypi:aiteam_connect-{P.CONNECTOR_VERSION}-py3-none-any.whl"
        ),
        "ops": list(P.OPS),
        "refused": {k: list(v) for k, v in P.REFUSED.items()},
    }


def _device(d: Device, request_ip: Optional[str] = None) -> dict:
    link = hub.live(d.id)
    hello = d.hello or {}
    return {
        "id": d.id,
        "name": d.name,
        "status": d.status,
        "os": d.os,
        "connector_version": d.connector_version,
        "outdated": P.outdated(d.connector_version) if d.connector_version else False,
        "paired_from": d.paired_from,
        # "Approximate location" is the network the pairing came from. Said plainly
        # as "the same network as this browser" when it is, which is what a person
        # approving it can actually judge.
        # None when this server can't tell (behind a proxy): the card then says so
        # rather than vouching for a network it never saw.
        "same_network": (d.paired_from == request_ip) if request_ip else None,
        "created_at": _iso(d.created_at),
        "approved_at": _iso(d.approved_at),
        "last_seen_at": _iso(d.last_seen_at),
        "online": link is not None,
        "connected_since": _iso(link.connected_at) if link else None,
        "hello": hello or None,
        "chat_model": d.chat_model,
        "embed_model": d.embed_model,
        "advice": pairing.size_advice(hello.get("ram_bytes")),
    }


def _mine(db: Session, user: User, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None or device.owner_id != user.id:
        raise HTTPException(status_code=404, detail="That computer isn't paired to your account.")
    return device


#: Headers a reverse proxy adds. Behind one, `request.client` is the proxy, so every
#: browser and every computer would look like "the same network".
_PROXIED = ("x-forwarded-for", "x-real-ip", "forwarded")


def _ip(request: Request) -> Optional[str]:
    """The browser's address, or None when a proxy hides it."""
    if any(request.headers.get(h) for h in _PROXIED):
        return None
    return request.client.host if request.client else None


# ── reading ─────────────────────────────────────────────────────────────────
@router.get("/setup")
def setup_guide() -> dict:
    """Everything the Setup tab shows that isn't about one computer."""
    return {"runtimes": table.setup_cards(), "connector": connector_info()}


@router.get("")
def list_devices(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(Device).where(Device.owner_id == user.id).order_by(Device.created_at.desc())
    ).scalars().all()
    return {"devices": [_device(d, _ip(request)) for d in rows]}


# ── pairing ─────────────────────────────────────────────────────────────────
@router.post("/pairing", status_code=201)
def start_pairing(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row, code = pairing.create_code(db, user)
    return {
        "id": row.id,
        "code": P.display_code(code),
        "expires_at": _iso(row.expires_at),
        "ttl_seconds": P.CODE_TTL_SECONDS,
        "account": pairing.account_label(user),
        "connector": connector_info(),
    }


@router.get("/pairing/{pairing_id}")
def pairing_state(
    pairing_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    row = db.get(PairingCode, pairing_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="That pairing code doesn't exist.")
    device = db.get(Device, row.device_id) if row.device_id else None
    expires = _aware(row.expires_at)
    if row.used_at is not None:
        state = "claimed" if device is not None else "used"
    elif expires is None or expires <= datetime.now(timezone.utc) or row.attempts >= P.CODE_MAX_ATTEMPTS:
        state = "expired"
    else:
        state = "waiting"
    return {
        "id": row.id,
        "state": state,
        "expires_at": _iso(row.expires_at),
        "device": _device(device, _ip(request)) if device is not None else None,
    }


@router.delete("/pairing/{pairing_id}")
def cancel_pairing(pairing_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(PairingCode, pairing_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="That pairing code doesn't exist.")
    if row.used_at is None:
        row.expires_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True}


# ── one computer ────────────────────────────────────────────────────────────
# The routes that talk to a live connection are async, so their database work runs
# in a thread: a SQLite write lock held by a build would otherwise stall the event
# loop, and every connector socket with it.
def _approve_row(user_id: str, device_id: str) -> str:
    """Mark the device approved; returns the account label for the connector."""
    with SessionLocal() as db:
        user = db.get(User, user_id)
        device = _mine(db, user, device_id)
        if device.status != P.STATE_APPROVED:
            device.status = P.STATE_APPROVED
            device.approved_at = datetime.now(timezone.utc)
            db.commit()
            log.info("Device %s was approved by its account.", device.id)
        return pairing.account_label(user)


def _read(user_id: str, device_id: str, ip: Optional[str]) -> dict:
    with SessionLocal() as db:
        return _device(_mine(db, db.get(User, user_id), device_id), ip)


def _check_mine(user_id: str, device_id: str) -> dict:
    """404 unless it's this account's; returns what the route needs of it."""
    with SessionLocal() as db:
        device = _mine(db, db.get(User, user_id), device_id)
        return {"id": device.id, "hello": device.hello}


def _check_device(user_id: str, device_id: str) -> dict:
    with SessionLocal() as db:
        device = _mine(db, db.get(User, user_id), device_id)
        return {"id": device.id, "hello": device.hello, "chat_model": device.chat_model}


def _delete(user_id: str, device_id: str) -> None:
    with SessionLocal() as db:
        db.delete(_mine(db, db.get(User, user_id), device_id))
        db.commit()


@router.post("/{device_id}/approve")
async def approve(device_id: str, request: Request, user: User = Depends(current_user)) -> dict:
    account = await anyio.to_thread.run_sync(_approve_row, user.id, device_id)
    link = await hub.approve(device_id, account)
    if link is not None:
        try:
            await refresh_hello(link)
        except ConnectorError as e:
            log.info("Approved device %s, but it didn't describe itself yet: %s", device_id, e)
        if not link.closed:
            hub.ready(link)
    return await anyio.to_thread.run_sync(_read, user.id, device_id, _ip(request))


@router.post("/{device_id}/refresh")
async def refresh(device_id: str, request: Request, user: User = Depends(current_user)) -> dict:
    await anyio.to_thread.run_sync(_check_mine, user.id, device_id)
    link = hub.live(device_id)
    if link is None:
        raise HTTPException(status_code=409, detail="This computer isn't connected right now.")
    try:
        await refresh_hello(link)
    except ConnectorError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None
    return await anyio.to_thread.run_sync(_read, user.id, device_id, _ip(request))


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    #: `source:model`, or "" to clear.
    chat_model: Optional[str] = Field(default=None, max_length=300)
    embed_model: Optional[str] = Field(default=None, max_length=300)


def _reported(device: Device, spec: str) -> Optional[dict]:
    """The model `spec` names, if this computer reported it."""
    return _reported_in(device.hello, spec)


def _reported_in(hello: Optional[dict], spec: str) -> Optional[dict]:
    source_id, sep, name = spec.partition(":")
    if not sep:
        return None
    for source in (hello or {}).get("sources") or []:
        if source.get("id") == source_id:
            for model in source.get("models") or []:
                if model.get("name") == name:
                    return model
    return None


@router.patch("/{device_id}")
def update_device(
    device_id: str,
    body: DeviceUpdate,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = _mine(db, user, device_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Give this computer a name.")
        device.name = name
    for field, want_embedding in (("chat_model", False), ("embed_model", True)):
        spec = getattr(body, field)
        if spec is None:
            continue
        if spec == "":
            setattr(device, field, None)
            continue
        model = _reported(device, spec)
        if model is None:
            raise HTTPException(status_code=422, detail=f"This computer didn't report a model called {spec}.")
        if not model.get("is_local", True):
            raise HTTPException(status_code=422, detail=f"{spec} runs on a hosted service, not on this computer.")
        is_embedding = model.get("kind") == "embedding"
        if want_embedding and model.get("kind") not in (None, "embedding"):
            raise HTTPException(status_code=422, detail=f"{spec} isn't an embedding model.")
        if not want_embedding and is_embedding:
            raise HTTPException(status_code=422, detail=f"{spec} only makes embeddings; it can't write.")
        setattr(device, field, spec)
    db.commit()
    hub.touch()
    return _device(device, _ip(request))


@router.get("/{device_id}/model")
async def model_details(device_id: str, spec: str, user: User = Depends(current_user)) -> dict:
    """What the computer can say about one of its models: window, structured output."""
    row = await anyio.to_thread.run_sync(_check_mine, user.id, device_id)
    if _reported_in(row["hello"], spec) is None:
        raise HTTPException(status_code=404, detail=f"This computer didn't report a model called {spec}.")
    link = hub.live(device_id)
    if link is None or not link.approved:
        raise HTTPException(status_code=409, detail="This computer isn't connected right now.")
    source, _, model = spec.partition(":")
    try:
        result = await link.request("model_info", {"source": source, "model": model}, timeout=20.0)
        return P.ModelInfoReport.model_validate(result).model_dump()
    except ValidationError:
        raise HTTPException(status_code=502, detail="Your computer's answer couldn't be read.") from None
    except ConnectorError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None


#: What "Send a test prompt" asks. Short, so the timing is the round trip and the
#: model's first tokens — not a long generation.
_TEST_PROMPT = "Reply with one short sentence confirming you can read this."


class TestBody(BaseModel):
    #: `source:model` on this computer; its chosen chat model when not given.
    spec: Optional[str] = Field(default=None, max_length=300)


@router.post("/{device_id}/test")
async def test_prompt(device_id: str, body: Optional[TestBody] = None, user: User = Depends(current_user)) -> dict:
    """A real round trip: this server → the connector → the runtime, and back.

    Sent through the same `chat` operation a build uses, so it passes (or fails)
    for the same reasons — the computer's limits, a model it doesn't have, a
    runtime that isn't running. The prompt and answer aren't logged.
    """
    row = await anyio.to_thread.run_sync(_check_device, user.id, device_id)
    spec = (body.spec if body else None) or row["chat_model"]
    if not spec:
        raise HTTPException(status_code=422, detail="Choose a model on this computer first.")
    model = _reported_in(row["hello"], spec)
    if model is None:
        raise HTTPException(status_code=404, detail=f"This computer didn't report a model called {spec}.")
    if model.get("kind") == "embedding":
        raise HTTPException(status_code=422, detail=f"{spec} only makes embeddings; pick a model that writes.")
    link = hub.live(device_id)
    if link is None or not link.approved:
        raise HTTPException(status_code=409, detail="This computer isn't connected right now.")
    source, _, name = spec.partition(":")
    args = P.ChatArgs(
        source=source,
        model=name,
        messages=[P.ChatTurn(role="user", content=_TEST_PROMPT)],
        max_tokens=256,
        context_window=4096,
        purpose="a test prompt from the Setup tab",
    ).model_dump(exclude_none=True)
    started = time.perf_counter()
    try:
        result = await link.request("chat", args, timeout=180.0)
        report = P.ChatReport.model_validate(result)
    except ValidationError:
        raise HTTPException(status_code=502, detail="Your computer's answer couldn't be read.") from None
    except ConnectorError as e:
        return {
            "ok": False,
            "code": e.code,
            "error": str(e),
            "seconds": round(time.perf_counter() - started, 2),
            "spec": spec,
        }
    return {
        "ok": True,
        "spec": spec,
        "seconds": round(time.perf_counter() - started, 2),
        "answer": report.text.strip()[:400],
        "thought": bool(report.reasoning),
        "prompt_tokens": report.prompt_tokens,
        "completion_tokens": report.completion_tokens,
        "finish_reason": report.finish_reason,
    }


@router.post("/{device_id}/disconnect")
async def disconnect(device_id: str, user: User = Depends(current_user)) -> dict:
    """Stop the connector now. The pairing is kept: running it again reconnects."""
    await anyio.to_thread.run_sync(_check_mine, user.id, device_id)
    dropped = await hub.drop(
        device_id, P.CLOSE_DISCONNECTED, "Disconnected from the website. Run the connector again to reconnect."
    )
    return {"ok": True, "was_connected": dropped}


@router.delete("/{device_id}")
async def forget(device_id: str, user: User = Depends(current_user)) -> dict:
    """Revoke this computer: its key is deleted and any live connection closed now."""
    await anyio.to_thread.run_sync(_delete, user.id, device_id)
    await hub.drop(device_id, P.CLOSE_FORGOTTEN, "This computer was removed from the account.")
    hub.touch()
    log.info("Device %s was forgotten by its account.", device_id)
    return {"ok": True}
