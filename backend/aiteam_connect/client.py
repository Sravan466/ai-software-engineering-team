"""Pairing with the server, and the one outbound connection that follows.

The connector never listens: it dials out over `wss://` (plain `ws://` only to a
server on this same computer, for development) and keeps that socket open, so the
model is never exposed and a home router has nothing to forward.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import connect

from app.connector import protocol as P
from app.router.runtimes import detect
from aiteam_connect import store
from aiteam_connect.limits import LimitRefused
from aiteam_connect.local import Agent, Refused, computer_name, os_name

#: Operations answered on the receiving thread: quick, and the ones that must get
#: through while a model call is still generating.
_INLINE = ("ping", "cancel")
#: Threads for everything else. The limits decide how many model calls actually run.
_WORKERS = 16

#: Close codes that mean "stop", not "reconnect".
STOP = {P.CLOSE_DISCONNECTED, P.CLOSE_FORGOTTEN, P.CLOSE_OUTDATED, P.CLOSE_LIMIT}


class ConnectError(RuntimeError):
    """Something a person has to act on. The message says what."""


@dataclass(frozen=True)
class Server:
    origin: str  # https://example.com — the key credentials are stored under
    host: str  # what the Host header carries, and what every handshake is bound to
    ws_url: str
    port: int
    local: bool


def server_for(url: str) -> Server:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConnectError("Give the server as https://your-server (the website shows the exact command).")
    local = detect.is_loopback(url)
    if parsed.scheme == "http" and not local:
        raise ConnectError("Only https:// servers are allowed — a plain http:// connection could be read in transit.")
    default = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default
    hostname = parsed.hostname.lower()
    bracketed = f"[{hostname}]" if ":" in hostname else hostname
    host = bracketed if port == default else f"{bracketed}:{port}"
    origin = f"{parsed.scheme}://{host}"
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return Server(origin=origin, host=host, ws_url=f"{ws_scheme}://{host}{P.WS_PATH}", port=port, local=local)


# ── pairing ─────────────────────────────────────────────────────────────────
def _post(server: Server, path: str, body: dict) -> dict:
    try:
        r = httpx.post(f"{server.origin}{path}", json=body, timeout=15.0)
    except httpx.HTTPError as e:
        raise ConnectError(f"Couldn't reach {server.origin}: {e}") from None
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        raise ConnectError(detail if isinstance(detail, str) else f"The server answered {r.status_code}.")
    return r.json()


def pair(server: Server, ask: Callable[[str], str], say: Callable[[str], None]) -> dict:
    """Pair this computer: code → confirm → keypair → claim. Returns the saved entry."""
    code = P.normalise_code(ask("Pairing code shown on the website: "))
    if not P.valid_code(code):
        raise ConnectError("That isn't a pairing code. It looks like BCDF-GHJK.")
    who = _post(server, "/api/connector/pair/lookup", {"code": code})
    account = str(who.get("account") or "an account")
    name = computer_name()
    answer = ask(f"This connects {name} to {account} on {server.host}. Continue? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise ConnectError("Not connected. Nothing was changed.")
    key = store.new_key()
    claimed = _post(
        server,
        "/api/connector/pair/claim",
        {
            "code": code,
            "public_key": store.public_text(key),
            "name": name,
            "os": os_name(),
            "connector_version": P.CONNECTOR_VERSION,
        },
    )
    device_id = str(claimed["device_id"])
    where = store.put_secret(store.key_name(server.origin), store.key_to_text(key))
    state = store.load_state()
    state.setdefault("servers", {})[server.origin] = {
        "device_id": device_id,
        "account": claimed.get("account") or account,
        "key_store": where,
    }
    store.save_state(state)
    say(f"Paired as {name}. Now approve it on the website — it's waiting for you there.")
    return state["servers"][server.origin]


def credential(server: Server) -> Optional[tuple[str, object, dict]]:
    entry = (store.load_state().get("servers") or {}).get(server.origin)
    if not entry:
        return None
    text = store.get_secret(store.key_name(server.origin))
    if not text:
        return None
    return entry["device_id"], store.key_from_text(text), entry


def forget(server: Server) -> bool:
    state = store.load_state()
    entry = (state.get("servers") or {}).pop(server.origin, None)
    store.delete_secret(store.key_name(server.origin))
    store.save_state(state)
    return entry is not None


# ── the connection ──────────────────────────────────────────────────────────
def _headers(server: Server, device_id: str, key) -> dict:
    ts = int(time.time())
    nonce = secrets.token_hex(16)
    sig = store.sign(key, P.handshake_message(device_id, ts, nonce, server.host))
    return {
        "Authorization": P.auth_header(device_id, ts, nonce, sig),
        "X-Connector-Version": P.CONNECTOR_VERSION,
    }


_send_lock = threading.Lock()


def _send(ws, message: dict) -> None:
    text = json.dumps(message, separators=(",", ":"), default=str)
    if len(text.encode("utf-8")) > P.MAX_MESSAGE_BYTES:
        text = json.dumps({"type": "response", "id": message.get("id"), "ok": False,
                           "error": "The answer was over the size limit.", "code": P.ERR_LIMIT})
    # Answers come from worker threads; one frame at a time on the socket.
    with _send_lock:
        ws.send(text)


def _answer(ws, agent: Agent, message) -> None:
    """Perform one request and send its answer. Never raises: a failure is an answer."""
    try:
        result = agent.handle(message.op, message.args, message.id)
        reply = {"type": "response", "id": message.id, "ok": True, "result": result}
    except (Refused, LimitRefused) as e:
        reply = {"type": "response", "id": message.id, "ok": False, "error": str(e)[:2000], "code": e.code}
    except Exception as e:  # noqa: BLE001 - a runtime error is an answer, not a crash
        reply = {"type": "response", "id": message.id, "ok": False, "error": str(e)[:500], "code": P.ERR_RUNTIME}
    try:
        _send(ws, reply)
    except ConnectionClosed:
        pass  # the connection went while this was generating; the server resends it


def session(server: Server, device_id: str, key, agent: Agent, say: Callable[[str], None]) -> int:
    """One connection, until it closes. Returns the close code."""
    with connect(
        server.ws_url,
        additional_headers=_headers(server, device_id, key),
        max_size=P.MAX_MESSAGE_BYTES,
        open_timeout=15,
        user_agent_header=f"{P.PACKAGE}/{P.CONNECTOR_VERSION}",
    ) as ws:
        approved = waiting = False
        # Two pools, so model calls waiting for a slot never hold up `hello` or
        # `model_info` behind them.
        pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="aiteam-connect-model")
        info_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aiteam-connect-info")
        try:
            while True:
                # The server pings an approved computer every PING_EVERY_SECONDS, so
                # silence this long means the line is dead: close, and reconnect.
                # (A pending computer is sent nothing; it simply reconnects, which
                # re-announces it as pending.)
                try:
                    frame = ws.recv(timeout=P.IDLE_TIMEOUT_SECONDS)
                except TimeoutError:
                    ws.close(1001, "no frames from the server")
                    return 1001
                if not isinstance(frame, str):
                    ws.close(P.CLOSE_PROTOCOL, "text frames only")
                    return P.CLOSE_PROTOCOL
                try:
                    message = P.SERVER_MESSAGE.validate_python(json.loads(frame))
                except (ValueError, ValidationError):
                    ws.close(P.CLOSE_PROTOCOL, "message didn't match the protocol")
                    return P.CLOSE_PROTOCOL
                if isinstance(message, P.StateMessage):
                    if message.state == P.STATE_APPROVED and not approved:
                        approved = True
                        say(f"✅ Connected to {message.account or 'your account'} on {server.host}. "
                            "Leave this running; press Ctrl+C to stop.")
                    elif message.state == P.STATE_PENDING and not waiting:
                        waiting = True
                        say("Waiting for you to approve this computer on the website…")
                elif isinstance(message, P.ServerReauth):
                    _send(ws, {"type": "reauth", "sig": store.sign(key, P.reauth_message(device_id, message.nonce))})
                elif isinstance(message, P.ByeMessage):
                    say(message.reason)
                elif isinstance(message, P.RequestMessage):
                    if not approved:
                        _send(ws, {"type": "response", "id": message.id, "ok": False,
                                   "error": "This computer hasn't been approved yet.", "code": P.ERR_REFUSED})
                        continue
                    if message.op in _INLINE:
                        _answer(ws, agent, message)
                    elif message.op in P.MODEL_OPS:
                        pool.submit(_answer, ws, agent, message)
                    else:
                        info_pool.submit(_answer, ws, agent, message)
        except ConnectionClosed as closed:
            # The code the server closed with — 4000 and 4401 mean "stop".
            return closed.rcvd.code if closed.rcvd is not None else 1006
        finally:
            # Nobody is left to read these answers: stop the runtime generating them.
            if agent.cancel_all():
                say("Connection lost — stopped what this computer was generating.")
            # Queued work goes too: its answer has no connection to go back on,
            # and the server sends it again once reconnected.
            pool.shutdown(wait=False, cancel_futures=True)
            info_pool.shutdown(wait=False, cancel_futures=True)
    return 1006


def run(server: Server, say: Callable[[str], None]) -> int:
    """Stay connected until the website says stop. Returns a process exit code."""
    found = credential(server)
    if found is None:
        raise ConnectError("This computer isn't paired with that server yet.")
    device_id, key, entry = found
    if entry.get("key_store") == "file":
        say("⚠ No OS keychain was available, so this computer's key is in a file only you can read "
            f"({store.home()}/keys). Any program running as you could still read it.")
    agent = Agent(device_id, skip_ports=(server.port,) if server.local else (), say=say)
    delay, refused = 1.0, 0
    while True:
        try:
            code = session(server, device_id, key, agent, say)
            delay, refused = 1.0, 0
        except InvalidStatus as e:
            refused += 1
            status = e.response.status_code
            if status in (401, 403) and refused >= 3:
                raise ConnectError(
                    "The server doesn't recognise this computer. It may have been forgotten on the website, "
                    "or this computer's clock is off by more than a minute. Run `aiteam-connect forget` "
                    "and pair again."
                ) from None
            code = 1006
        except (OSError, TimeoutError, EOFError, InvalidHandshake) as e:
            # EOFError: the server (or a proxy in front of it) closed mid-handshake,
            # as during a restart. Not Ctrl-D — so retried, never treated as "stop".
            say(f"Couldn't reach {server.host} ({str(e) or type(e).__name__}). Retrying…")
            code = 1006
        if code == P.CLOSE_FORGOTTEN:
            forget(server)
            say("This computer was removed from your account, so its key was deleted here too.")
            return 1
        if code in STOP:
            return 0 if code == P.CLOSE_DISCONNECTED else 1
        time.sleep(delay)
        delay = min(delay * 2, 60.0)
