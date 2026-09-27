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
import concurrent.futures
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import ValidationError
from starlette.websockets import WebSocket

from app.connector import protocol as P
from app.connector.pairing import verify
from app.core.logging import get_logger

log = get_logger(__name__)


class ConnectorError(RuntimeError):
    """A request the connector didn't answer, or answered with an error.

    `code` is the connector's reason (`protocol.ERROR_CODES`) when it refused; None
    when the connection itself failed — closed, timed out, never there.
    """

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


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
        #: Background work for this link (pings, a hello refresh). Held so it isn't
        #: garbage-collected mid-flight, and cancelled when the link closes.
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel_tasks(self) -> None:
        # Never the task doing the cancelling: a background refresh that closes the
        # link would otherwise cancel itself mid-close, before the bye is sent.
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()

    # ── sending ──────────────────────────────────────────────────────────────
    async def send(self, message: dict) -> None:
        text = json.dumps(message, separators=(",", ":"))
        if len(text.encode("utf-8")) > P.MAX_MESSAGE_BYTES:
            raise ConnectorError("Refusing to send a message over the size limit.")
        async with self._send_lock:
            if self.closed and message.get("type") != "bye":
                raise ConnectorError("This computer isn't connected.")
            try:
                await self.ws.send_text(text)
            except Exception as e:  # noqa: BLE001 - a socket that died under us
                self.closed = True
                raise ConnectorError("The connection to your computer closed.") from e

    async def request(
        self,
        op: str,
        args: Optional[dict] = None,
        *,
        timeout: float = 20.0,
        request_id: Optional[str] = None,
    ) -> Any:
        """Ask the connector for one typed operation, and wait for its answer.

        `request_id` names the request for `cancel_request`; one is made when not given.
        """
        if op not in P.OPS:
            raise ConnectorError(f"'{op}' is not an operation a connector performs.")
        if not self.approved:
            raise ConnectorError("This computer hasn't been approved yet.")
        if self.closed:
            raise ConnectorError("This computer isn't connected.")
        request_id = request_id or secrets.token_hex(8)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send({"type": "request", "id": request_id, "op": op, "args": args or {}})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as e:
            if op in P.MODEL_OPS:
                # Don't leave the computer generating an answer nobody will read.
                self.spawn(self._send_cancel(request_id))
            raise ConnectorError(f"Your computer didn't answer '{op}' within {int(timeout)} seconds.") from e
        finally:
            self._pending.pop(request_id, None)

    async def cancel_request(self, request_id: str) -> bool:
        """Stop a request in flight: fail it here now, and tell the computer to stop.

        The local answer is immediate — Stop must not wait on a model that is still
        generating. The `cancel` is sent after, and the connector passes it on to the
        runtime, which stops generating.
        """
        future = self._pending.get(request_id)
        if future is None:
            return False
        if not future.done():
            future.set_exception(ConnectorError("Stopped.", code=P.ERR_CANCELLED))
        await self._send_cancel(request_id)
        return True

    async def _send_cancel(self, request_id: str) -> None:
        if self.closed:
            return
        try:
            await self.send(
                {"type": "request", "id": secrets.token_hex(8), "op": "cancel", "args": {"id": request_id}}
            )
        except ConnectorError:
            pass  # the connection is gone, and the connector cancels everything it had

    async def close(self, code: int, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.cancel_tasks()
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
                raise ProtocolViolation(P.CLOSE_REAUTH, "Re-authentication failed.")
            return
        future = self._pending.get(message.id)
        if future is None or future.done():
            return  # an answer to a request that already timed out
        if message.ok:
            future.set_result(message.result)
        else:
            code = message.code if message.code in P.ERROR_CODES else None
            future.set_exception(ConnectorError(message.error or "Your computer refused the request.", code))

    # ── keeping it honest ────────────────────────────────────────────────────
    async def reauth(self) -> None:
        self._reauth_nonce = secrets.token_hex(16)
        self._reauth_deadline = time.monotonic() + P.REAUTH_GRACE_SECONDS
        await self.send({"type": "reauth", "nonce": self._reauth_nonce})

    def overdue(self) -> Optional[tuple[int, str]]:
        """Why this link should be closed now, if it should."""
        now = time.monotonic()
        if self._reauth_nonce is not None and now > self._reauth_deadline:
            return P.CLOSE_REAUTH, "Re-authentication timed out."
        # A pending computer is sent nothing, so it has nothing to answer: its
        # liveness is the WebSocket's own ping, which the server answers for it.
        if self.approved and now - self.last_frame > P.IDLE_TIMEOUT_SECONDS:
            return 1001, "Idle timeout."
        return None


class Hub:
    def __init__(self) -> None:
        self._links: dict[str, Link] = {}
        #: The event loop the links live on. Model calls come from worker threads
        #: (a build runs in one) and reach a link through it.
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        #: When each device's last connection closed, by the monotonic clock.
        self._dropped_at: dict[str, float] = {}
        #: Woken whenever any computer connects, is approved or drops, so a thread
        #: waiting for one to come back doesn't poll.
        self._changed = threading.Condition()
        #: Called with (owner_id, device_id) when an approved computer is ready to
        #: take requests — how a build paused for it picks itself back up.
        self.on_ready: list[Callable[[str, str], None]] = []
        #: Bumped on every change, so a cache of who is connected knows it is stale.
        self.version = 0

    def live(self, device_id: str) -> Optional[Link]:
        link = self._links.get(device_id)
        return link if link is not None and not link.closed else None

    def dropped_at(self, device_id: str) -> Optional[float]:
        """When this device's last connection closed, if it closed in this process."""
        return self._dropped_at.get(device_id)

    def touch(self) -> None:
        """Something about a device changed (its models, its chosen model): anything
        cached about who is connected with what is stale."""
        self._changed_now()

    def _changed_now(self) -> None:
        with self._changed:
            self.version += 1
            self._changed.notify_all()

    def wait_for(self, device_id: str, timeout: float) -> Optional[Link]:
        """Block a worker thread until the device is connected and approved again.

        Never call this on the event loop: it is the loop that reconnects it.
        """
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                link = self.live(device_id)
                if link is not None and link.approved:
                    return link
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._changed.wait(min(left, 1.0))

    def ready(self, link: Link) -> None:
        """An approved computer can take requests: wake waiters, resume its builds."""
        self._changed_now()
        for callback in list(self.on_ready):
            try:
                callback(link.owner_id, link.device_id)
            except Exception as e:  # noqa: BLE001 - one listener must not stop the rest
                log.warning("A connect listener failed for device %s: %s", link.device_id, e)

    def call(self, coro, timeout: float) -> Any:
        """Run `coro` on the links' loop from a worker thread, and wait for it."""
        loop = self.loop
        if loop is None or loop.is_closed():
            coro.close()
            raise ConnectorError("This computer isn't connected.")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            coro.close()
            raise ConnectorError("A model call reached the connector from its own event loop.")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            raise ConnectorError("Your computer didn't answer in time.") from e

    def count_for(self, owner_id: str) -> int:
        return sum(1 for link in self._links.values() if link.owner_id == owner_id and not link.closed)

    async def register(self, link: Link) -> None:
        """Hold `link` as its device's connection, closing any older one it replaces."""
        self.loop = asyncio.get_running_loop()
        old = self._links.get(link.device_id)
        self._links[link.device_id] = link
        if old is not None and old is not link:
            await old.close(1000, "Replaced by a newer connection from the same computer.")
        self._changed_now()

    def unregister(self, link: Link) -> None:
        link.closed = True
        link.cancel_tasks()
        link._fail_pending("The connection closed.")
        if self._links.get(link.device_id) is link:
            del self._links[link.device_id]
            self._dropped_at[link.device_id] = time.monotonic()
        self._changed_now()

    async def drop(self, device_id: str, code: int, reason: str) -> bool:
        link = self._links.get(device_id)
        if link is None:
            return False
        await link.close(code, reason)
        self.unregister(link)
        return True

    async def approve(self, device_id: str, account: str) -> Optional[Link]:
        """Tell a waiting computer it was approved. None if it isn't connected."""
        link = self.live(device_id)
        if link is None or link.approved:
            return link
        # A pending computer is sent nothing, so its idle clock has been running
        # since it connected. Restart it, or one that waited a while would be closed
        # as idle the moment it's approved.
        link.last_frame = time.monotonic()
        link.approved = True
        try:
            await link.send({"type": "state", "state": P.STATE_APPROVED, "account": account})
        except ConnectorError:
            self.unregister(link)
            return None
        self._changed_now()
        return link


hub = Hub()
