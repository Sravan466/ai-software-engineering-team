"""What the connector program talks to: pairing, and its one long-lived socket.

These are the only routes a computer reaches without a browser session. Pairing is
open because the connector has nothing to sign in with yet — it is guarded by the
code, which only a signed-in account can make, and by rate limits. The socket is
authenticated by a signature in its upgrade request's `Authorization` header
(never the query string), made with a key the server has never seen.

`Origin` is ignored on purpose (RFC 6455 §10.1): the connector is not a browser,
and a browser that forged one would still need the device's private key.
"""
from __future__ import annotations

import asyncio

import time
from datetime import datetime, timezone
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.connector import pairing, protocol as P
from app.connector.hub import ConnectorError, Link, ProtocolViolation, hub
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import Device, User

log = get_logger(__name__)

router = APIRouter(prefix="/api/connector", tags=["connector"])


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class CodeBody(BaseModel):
    code: str = Field(max_length=32)


class ClaimBody(CodeBody):
    public_key: str = Field(max_length=64)
    name: str = Field(default="My computer", max_length=120)
    os: str = Field(default="", max_length=64)
    connector_version: str = Field(default="", max_length=32)


@router.post("/pair/lookup")
def pair_lookup(body: CodeBody, request: Request, db: Session = Depends(get_db)) -> dict:
    """Whose code this is — so the connector can ask "connect to <account>?" first."""
    try:
        return pairing.lookup(db, body.code, _client(request))
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None


@router.post("/pair/claim", status_code=201)
def pair_claim(body: ClaimBody, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        device, user = pairing.claim(
            db,
            body.code,
            _client(request),
            public_key=body.public_key,
            name=body.name,
            os=body.os,
            connector_version=body.connector_version,
        )
    except pairing.PairingError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    log.info("A computer claimed a pairing code and is waiting for approval (device %s).", device.id)
    return {"device_id": device.id, "account": pairing.account_label(user), "state": device.status}


# ── the socket ───────────────────────────────────────────────────────────────
def _authenticate(header: Optional[str], host: str) -> Optional[dict]:
    with SessionLocal() as db:
        device = pairing.verify_handshake(db, header, host)
        if device is None:
            return None
        user = db.get(User, device.owner_id)
        return {
            "id": device.id,
            "owner_id": device.owner_id,
            "public_key": device.public_key,
            "approved": device.status == P.STATE_APPROVED,
            "account": pairing.account_label(user) if user else "",
        }


def _touch(device_id: str) -> None:
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device is not None:
            device.last_seen_at = datetime.now(timezone.utc)
            db.commit()


def _store_hello(device_id: str, report: P.HelloReport) -> Optional[dict]:
    data = report.model_dump()
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        if device is None:
            return None
        device.hello = data
        device.os = report.os[:64]
        device.connector_version = report.connector_version[:32]
        device.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    return data


async def refresh_hello(link: Link) -> dict:
    """Ask the computer what it has now, check the answer, and keep it."""
    result = await link.request("hello", timeout=30.0)
    try:
        report = P.HelloReport.model_validate(result)
    except ValidationError:
        await link.close(P.CLOSE_PROTOCOL, "A hello didn't match the protocol.")
        raise ConnectorError("Your computer sent a description this server couldn't read.") from None
    if report.device_id != link.device_id:
        await link.close(P.CLOSE_PROTOCOL, "A hello named another device.")
        raise ConnectorError("Your computer answered as a different device.")
    stored = await anyio.to_thread.run_sync(_store_hello, link.device_id, report)
    if stored is None:
        raise ConnectorError("This computer is no longer paired.")
    return stored


async def _refresh_quietly(link: Link) -> None:
    try:
        await refresh_hello(link)
    except ConnectorError as e:
        log.info("Couldn't read what device %s has: %s", link.device_id, e)


async def _ping(link: Link) -> None:
    try:
        await link.request("ping", timeout=20.0)
        await anyio.to_thread.run_sync(_touch, link.device_id)
    except ConnectorError:
        pass  # the idle timeout decides; one slow answer isn't a death


async def _keep(link: Link) -> None:
    """Heartbeat, re-authentication and the idle timeout, for one link."""
    last_ping = last_reauth = time.monotonic()
    while not link.closed:
        await asyncio.sleep(2.0)
        why = link.overdue()
        if why is not None:
            await link.close(*why)
            return
        now = time.monotonic()
        if link.approved and now - last_ping >= P.PING_EVERY_SECONDS:
            last_ping = now
            asyncio.create_task(_ping(link))
        if now - last_reauth >= P.REAUTH_EVERY_SECONDS:
            last_reauth = now
            await link.reauth()


@router.websocket("/ws")
async def connector_socket(ws: WebSocket) -> None:
    who = await anyio.to_thread.run_sync(
        _authenticate, ws.headers.get("authorization"), ws.headers.get("host", "")
    )
    if who is None:
        # Closed before accepting: the upgrade is answered with a plain 403, and
        # nothing about why is said to whoever sent it.
        await ws.close(code=P.CLOSE_FORGOTTEN)
        return
    await ws.accept()
    link = Link(
        ws,
        device_id=who["id"],
        owner_id=who["owner_id"],
        public_key=who["public_key"],
        approved=who["approved"],
    )
    if P.outdated(ws.headers.get("x-connector-version")):
        await link.close(
            P.CLOSE_OUTDATED,
            f"This connector is out of date. Install {P.PACKAGE}=={P.CONNECTOR_VERSION} and run it again.",
        )
        return
    if hub.live(link.device_id) is None and hub.count_for(link.owner_id) >= P.MAX_LIVE_PER_ACCOUNT:
        await link.close(
            P.CLOSE_LIMIT,
            f"{P.MAX_LIVE_PER_ACCOUNT} computers are already connected to this account. Disconnect one first.",
        )
        return

    await hub.register(link)
    keeper = asyncio.create_task(_keep(link))
    try:
        await link.send(
            {
                "type": "state",
                "state": P.STATE_APPROVED if link.approved else P.STATE_PENDING,
                "account": who["account"][:320],
            }
        )
        if link.approved:
            asyncio.create_task(_refresh_quietly(link))
        while not link.closed:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                link.receive(message.get("text"), message.get("bytes"))
            except ProtocolViolation as v:
                # Never the frame itself: it could hold anything.
                log.warning("Closed device %s's connection: %s", link.device_id, v.reason)
                await link.close(v.code, v.reason)
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        keeper.cancel()
        hub.unregister(link)
        await anyio.to_thread.run_sync(_touch, link.device_id)


