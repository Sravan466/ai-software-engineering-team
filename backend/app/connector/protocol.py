"""The connector protocol, shared by the server and the connector program.

Both ends import this file, so a message one side sends is checked against the
same schema the other side reads it with. It depends on pydantic and the standard
library only: it ships inside the connector package.

**Shape.** JSON text frames, each at most `MAX_MESSAGE_BYTES`. The server asks and
the connector answers:

  server → connector   `state`   pending | approved
                       `request` {id, op, args}: one of `OPS`, and nothing else
                       `reauth`  {nonce}: sign this to keep the connection
                       `bye`     {reason, code}: the server is closing, and why
  connector → server   `response` {id, ok, result | error}
                       `reauth`   {sig}

Every model is `extra="forbid"`: a field either side doesn't know is a protocol
error, and the connection closes (RFC 6455 §10.7) rather than guessing.

**Authentication.** The connector made an Ed25519 keypair when it paired, and the
server stored only the public half. The upgrade request carries a signature over
the device id, a timestamp, a fresh nonce and the host it is addressed to — in a
header, never the query string — and every `REAUTH_EVERY_SECONDS` the server sends
a new nonce that must be signed again on the open connection.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL = "aiteam-connect/1"
PACKAGE = "aiteam-connect"
#: The version this server shows in its pinned install command.
CONNECTOR_VERSION = "0.1.0"
#: A connector older than this is told it is out of date and sent nothing else.
MIN_CONNECTOR_VERSION = "0.1.0"

WS_PATH = "/api/connector/ws"
AUTH_SCHEME = "AiteamDevice"

#: RFC 8628 §6.1: 20 consonants, no vowels (no words), no 0/O or 1/I to confuse.
#: 8 characters is 20^8 ≈ 2^34.5.
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 600
#: Lookups and claims one code allows before it is burned.
CODE_MAX_ATTEMPTS = 5

#: Both directions. Python `websockets` defaults to 1 MiB and uvicorn to 16 MiB; a
#: model list is a few kilobytes, so anything near this is not a model list.
MAX_MESSAGE_BYTES = 256 * 1024
#: A signed handshake is good for this long either side of the server's clock.
HANDSHAKE_SKEW_SECONDS = 60
REAUTH_EVERY_SECONDS = 600
REAUTH_GRACE_SECONDS = 30
PING_EVERY_SECONDS = 30
#: No frame at all for this long, and the connection is closed as dead.
IDLE_TIMEOUT_SECONDS = 90
MAX_LIVE_PER_ACCOUNT = 5

#: Close codes the connector acts on. A connector told 4000, 4401, 4426 or 4429 stops
#: instead of reconnecting: someone decided it should, or reconnecting can't help.
CLOSE_DISCONNECTED = 4000  # "Disconnect" on the website; the pairing is kept
CLOSE_FORGOTTEN = 4401  # forgotten on the website: the credential is dead, delete it
#: Re-authentication failed or timed out. Not "forgotten": a connector busy scanning,
#: or a laptop waking from sleep, misses the grace period — it reconnects, signing a
#: fresh handshake, and keeps its key.
CLOSE_REAUTH = 4408
CLOSE_OUTDATED = 4426  # this connector is older than MIN_CONNECTOR_VERSION
CLOSE_LIMIT = 4429  # too many computers connected on this account at once
CLOSE_PROTOCOL = 1008  # a message that doesn't match its schema
CLOSE_TOO_BIG = 1009

#: The only operations a connector performs. Each maps to one typed adapter
#: operation; there is no way to name a URL, path, header or method.
OPS = ("hello", "list_models", "model_info", "ping")

#: What the server may never ask for, whatever it sends. The connector refuses every
#: op not in `OPS` anyway; these are named so a refusal can say what was attempted,
#: and so the list in the issue's threat model is written down in code.
REFUSED = {
    "ollama": ("pull", "push", "create", "copy", "delete", "blobs", "/api/me", "/api/signout",
               "/api/user/keys", "/api/experimental/*"),
    "llamacpp": ("model add", "model delete", "model load", "model unload"),
    "tgw": ("model loading",),
    "vllm": ("/pause", "/update_weights"),
    "*": ("tools", "shell commands", "file writes", "model downloads", "raw URLs or paths"),
}

STATE_PENDING = "pending"
STATE_APPROVED = "approved"

_SAFE_ID = r"^[A-Za-z0-9._:@/+-]{1,200}$"


# ── pairing codes ────────────────────────────────────────────────────────────
def normalise_code(raw: str) -> str:
    """What a person typed, as the canonical code: upper case, separators dropped."""
    return re.sub(r"[^A-Z]", "", (raw or "").upper())


def valid_code(code: str) -> bool:
    return len(code) == CODE_LENGTH and all(c in CODE_ALPHABET for c in code)


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


# ── signing ─────────────────────────────────────────────────────────────────
def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def handshake_message(device_id: str, ts: int, nonce: str, host: str) -> bytes:
    """What the connector signs to open a connection. Bound to the host it is
    addressed to, so a signature captured for one server opens nothing on another."""
    return f"{PROTOCOL}\nconnect\n{device_id}\n{ts}\n{nonce}\n{host.lower()}".encode("utf-8")


def reauth_message(device_id: str, nonce: str) -> bytes:
    return f"{PROTOCOL}\nreauth\n{device_id}\n{nonce}".encode("utf-8")


_AUTH = re.compile(r'(\w+)="?([^",\s]+)"?')


def auth_header(device_id: str, ts: int, nonce: str, sig: str) -> str:
    return f'{AUTH_SCHEME} id="{device_id}", ts="{ts}", nonce="{nonce}", sig="{sig}"'


def parse_auth_header(value: Optional[str]) -> Optional[dict]:
    """`{id, ts, nonce, sig}` from an `Authorization` header, or None if malformed."""
    if not value or not value.startswith(AUTH_SCHEME + " ") or len(value) > 1024:
        return None
    fields = dict(_AUTH.findall(value[len(AUTH_SCHEME) + 1 :]))
    try:
        out = {
            "id": fields["id"],
            "ts": int(fields["ts"]),
            "nonce": fields["nonce"],
            "sig": fields["sig"],
        }
    except (KeyError, ValueError):
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", out["id"]) or not re.fullmatch(r"[0-9a-f]{32}", out["nonce"]):
        return None
    return out


def version_tuple(version: Optional[str]) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version or "")[:3]
    return tuple(int(p) for p in parts) or (0,)


def outdated(version: Optional[str]) -> bool:
    return version_tuple(version) < version_tuple(MIN_CONNECTOR_VERSION)


# ── messages ────────────────────────────────────────────────────────────────
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelInfoArgs(_Strict):
    """A source the connector itself reported, and a model it listed there. Never
    an address: the connector maps the id to a URL it was configured with."""

    source: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    model: str = Field(pattern=_SAFE_ID)


class NoArgs(_Strict):
    pass


class StateMessage(_Strict):
    type: Literal["state"]
    state: Literal["pending", "approved"]
    account: Optional[str] = Field(default=None, max_length=320)


class RequestMessage(_Strict):
    type: Literal["request"]
    id: str = Field(pattern=r"^[0-9a-f]{8,32}$")
    op: str = Field(max_length=64)
    args: dict = Field(default_factory=dict)


class ServerReauth(_Strict):
    type: Literal["reauth"]
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")


class ByeMessage(_Strict):
    type: Literal["bye"]
    reason: str = Field(max_length=500)
    code: int


ServerMessage = Union[StateMessage, RequestMessage, ServerReauth, ByeMessage]
SERVER_MESSAGE = TypeAdapter(ServerMessage)


class ResponseMessage(_Strict):
    type: Literal["response"]
    id: str = Field(pattern=r"^[0-9a-f]{8,32}$")
    ok: bool
    result: Optional[Any] = None
    error: Optional[str] = Field(default=None, max_length=2000)


class ConnectorReauth(_Strict):
    type: Literal["reauth"]
    sig: str = Field(max_length=200)


ConnectorMessage = Union[ResponseMessage, ConnectorReauth]
CONNECTOR_MESSAGE = TypeAdapter(ConnectorMessage)


# ── what `hello` and `list_models` answer ─────────────────────────────────────
class ModelReport(_Strict):
    name: str = Field(max_length=300)
    kind: Optional[str] = Field(default=None, max_length=32)
    capabilities: Optional[list[str]] = Field(default=None, max_length=32)
    is_local: bool = True
    size_bytes: Optional[int] = Field(default=None, ge=0)
    loaded: Optional[bool] = None


class SourceReport(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    runtime: str = Field(max_length=48)
    label: str = Field(max_length=120)
    #: Where the connector reaches it. Loopback unless the user set another address
    #: on that computer and confirmed it there.
    base_url: str = Field(max_length=300)
    remote: bool = False
    version: Optional[str] = Field(default=None, max_length=64)
    reachable: bool = True
    error: Optional[str] = Field(default=None, max_length=500)
    models: list[ModelReport] = Field(default_factory=list, max_length=500)


class UnknownPort(_Strict):
    base_url: str = Field(max_length=300)
    openai: bool = False
    note: str = Field(max_length=300)


class HelloReport(_Strict):
    device_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    connector_version: str = Field(max_length=32)
    protocol: str = Field(max_length=32)
    os: str = Field(max_length=64)
    os_version: Optional[str] = Field(default=None, max_length=120)
    arch: Optional[str] = Field(default=None, max_length=32)
    ram_bytes: Optional[int] = Field(default=None, ge=0)
    hostname: Optional[str] = Field(default=None, max_length=120)
    sources: list[SourceReport] = Field(default_factory=list, max_length=32)
    unknown: list[UnknownPort] = Field(default_factory=list, max_length=32)
    tried: list[str] = Field(default_factory=list, max_length=64)
    #: Which operations this connector answers. Always a subset of `OPS`.
    capabilities: list[str] = Field(default_factory=list, max_length=16)


class ModelInfoReport(_Strict):
    """The parts of `ModelInfo` the website shows, and nothing it doesn't."""

    name: str = Field(max_length=300)
    context_window: Optional[int] = Field(default=None, ge=0)
    parameter_label: Optional[str] = Field(default=None, max_length=32)
    quantization: Optional[str] = Field(default=None, max_length=32)
    kind: Optional[str] = Field(default=None, max_length=32)
    structured_output: Optional[str] = Field(default=None, max_length=16)
    thinking: Optional[str] = Field(default=None, max_length=16)
    is_local: bool = True
    weights_bytes: Optional[int] = Field(default=None, ge=0)
