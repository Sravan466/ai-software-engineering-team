"""Every connector connected right now, and what the server may ask of each.

One `Link` per live connection, held in this process — presence is "is there a
link", never a column that can go stale. The server asks through `Link.request`,
which only ever sends an op from `protocol.OPS`, and only to an approved device:
a pending computer is told it is pending and sent nothing else.

A link is kept honest by three timers: an application `ping` every
`PING_EVERY_SECONDS`, a fresh signature every `REAUTH_EVERY_SECONDS`, and an idle
timeout — no frame at all for `IDLE_TIMEOUT_SECONDS` and it is closed as dead.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError
from starlette.websockets import WebSocket

from app.connector import protocol as P
from app.connector.pairing import verify
from app.core.logging import get_logger

log = get_logger(__name__)


class ConnectorError(RuntimeError):
    """A request the connector didn't answer, or answered with an error."""


class ProtocolViolation(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class Link:
    def __init__(self, ws: WebSocket, *, device_id: str, owner_id: str, public_key: str, approved: bool) -> None:
        self.ws = ws
        self.device_id = device_id
        self.owner_id = owner_id
        self.public_key = public_key
        self.approved = approved
        self.connected_at = datetime.now(timezone.utc)
        self.last_frame = time.monotonic()
        self.closed = False
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._reauth_nonce: Optional[str] = None
        self._reauth_deadline = 0.0

    # ── sending ──────────────────────────────────────────────────────────────
    async def send(self, message: dict) -> None:
        text = json.dumps(message, separators=(",", ":"))
        if len(text.encode("utf-8")) > P.MAX_MESSAGE_BYTES:
            raise ConnectorError("Refusing to send a message over the size limit.")
        async with self._send_lock:
            await self.ws.send_text(text)

    async def request(self, op: str, args: Optional[dict] = None, *, timeout: float = 20.0) -> Any:
        """Ask the connector for one typed operation, and wait for its answer."""
        if op not in P.OPS:
            raise ConnectorError(f"'{op}' is not an operation a connector performs.")
        if not self.approved:
            raise ConnectorError("This computer hasn't been approved yet.")
        if self.closed:
            raise ConnectorError("This computer isn't connected.")
        request_id = secrets.token_hex(8)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send({"type": "request", "id": request_id, "op": op, "args": args or {}})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as e:
            raise ConnectorError(f"Your computer didn't answer '{op}' within {int(timeout)} seconds.") from e
        finally:
            self._pending.pop(request_id, None)

    async def close(self, code: int, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.send({"type": "bye", "reason": reason[:500], "code": code})
        except Exception:  # noqa: BLE001 - the socket may already be gone
            pass
        try:
            await self.ws.close(code=code, reason=reason[:120])
        except Exception:  # noqa: BLE001
            pass
        self._fail_pending("The connection closed.")

    def _fail_pending(self, why: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectorError(why))
        self._pending.clear()

    # ── receiving ────────────────────────────────────────────────────────────
    def receive(self, text: Optional[str], data: Optional[bytes]) -> None:
        """Validate one frame and act on it. Raises `ProtocolViolation` to close."""
        self.last_frame = time.monotonic()
        if data is not None or text is None:
            raise ProtocolViolation(P.CLOSE_PROTOCOL, "Only JSON text frames are accepted.")
        if len(text.encode("utf-8")) > P.MAX_MESSAGE_BYTES:
            raise ProtocolViolation(P.CLOSE_TOO_BIG, "Message over the size limit.")
        try:
            message = P.CONNECTOR_MESSAGE.validate_python(json.loads(text))
        except (ValueError, ValidationError):
            raise ProtocolViolation(P.CLOSE_PROTOCOL, "A message didn't match the protocol.") from None
        if isinstance(message, P.ConnectorReauth):
            nonce, self._reauth_nonce = self._reauth_nonce, None
            if nonce is None or not verify(self.public_key, message.sig, P.reauth_message(self.device_id, nonce)):
                raise ProtocolViolation(P.CLOSE_FORGOTTEN, "Re-authentication failed.")
            return
        future = self._pending.get(message.id)
        if future is None or future.done():
            return  # an answer to a request that already timed out
        if message.ok:
            future.set_result(message.result)
        else:
            future.set_exception(ConnectorError(message.error or "Your computer refused the request."))

    # ── keeping it honest ────────────────────────────────────────────────────
    async def reauth(self) -> None:
        self._reauth_nonce = secrets.token_hex(16)
        self._reauth_deadline = time.monotonic() + P.REAUTH_GRACE_SECONDS
        await self.send({"type": "reauth", "nonce": self._reauth_nonce})

    def overdue(self) -> Optional[tuple[int, str]]:
        """Why this link should be closed now, if it should."""
        now = time.monotonic()
        if self._reauth_nonce is not None and now > self._reauth_deadline:
            return P.CLOSE_FORGOTTEN, "Re-authentication timed out."
        # A pending computer is sent nothing, so it has nothing to answer: its
        # liveness is the WebSocket's own ping, which the server answers for it.
        if self.approved and now - self.last_frame > P.IDLE_TIMEOUT_SECONDS:
            return 1001, "Idle timeout."
        return None


class Hub:
    def __init__(self) -> None:
        self._links: dict[str, Link] = {}

    def live(self, device_id: str) -> Optional[Link]:
        link = self._links.get(device_id)
        return link if link is not None and not link.closed else None

    def count_for(self, owner_id: str) -> int:
        return sum(1 for link in self._links.values() if link.owner_id == owner_id and not link.closed)

    async def register(self, link: Link) -> None:
        """Hold `link` as its device's connection, closing any older one it replaces."""
        old = self._links.get(link.device_id)
        self._links[link.device_id] = link
        if old is not None and old is not link:
            await old.close(1000, "Replaced by a newer connection from the same computer.")

    def unregister(self, link: Link) -> None:
        link.closed = True
        link._fail_pending("The connection closed.")
        if self._links.get(link.device_id) is link:
            del self._links[link.device_id]

    async def drop(self, device_id: str, code: int, reason: str) -> bool:
        link = self._links.get(device_id)
        if link is None:
            return False
        await link.close(code, reason)
        self.unregister(link)
        return True

    async def approve(self, device_id: str, account: str) -> Optional[Link]:
        link = self.live(device_id)
        if link is not None:
            link.approved = True
            await link.send({"type": "state", "state": P.STATE_APPROVED, "account": account})
        return link


hub = Hub()
