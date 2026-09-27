"""The models on a user's own computer, as model sources the router can use.

Each runtime a paired computer reports becomes one `ConnectorProvider` in its
owner's router — the same `SourceProvider` every runtime on this machine is, with a
`ConnectorAdapter` underneath that sends the typed operations over the connector
instead of HTTP. Profiles, the compatibility check, per-model settings and thinking
all work the way they do for a local runtime, because they are the same code.

What differs is what "available" means and what a failure means:

  * **Availability is presence.** A computer is up when its connection is open and
    approved — never a probe. Its model list is the last `hello` it sent.
  * **Its RAM is its own.** The profile and the compatibility check use the memory
    the computer reported, not this server's.
  * **A closed connection is a pause, not a failure.** A call that finds the
    computer gone waits `GRACE_SECONDS` for it to come back — a laptop waking up, a
    Wi-Fi drop — and then raises `ComputerDisconnected`, which the runner turns into
    a paused build that resumes by itself when the computer reconnects.

Source ids are `pc-<first 8 of the device id>-<the computer's source id>`, so a
saved choice still reads as a source while the computer is off.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import select

from app.connector import protocol as P
from app.connector.hub import ConnectorError, hub
from app.core.config import settings
from app.core.logging import get_logger
from app.router import inflight
from app.router.base import ComputerDisconnected, ProviderError, RequestCancelled
from app.router.runtimes import table
from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.provider import Source, SourceProvider, SourceState
from app.router.runtimes.types import ChatRequest, ChatResult, ModelEntry, ModelInfo

log = get_logger(__name__)

ORIGIN_CONNECTOR = "connector"
PREFIX = "pc-"
_ID = re.compile(r"^pc-[0-9a-f]{8}-[a-z0-9][a-z0-9-]{0,47}$")
#: How long a device list read from the database is trusted, when nothing connected,
#: dropped or changed in between. Any of those bumps `hub.version` and it is re-read.
_DEVICES_TTL_SECONDS = 5.0
#: Asking a computer about one model.
_INFO_TIMEOUT = 25.0
#: A generation on a laptop's CPU can take many minutes. The computer enforces its
#: own, usually shorter, timeout and says so; this is only the server's backstop.
_CHAT_TIMEOUT = 1800.0
_EMBED_TIMEOUT = 300.0


def source_id_for(device_id: str, remote_source: str) -> str:
    return f"{PREFIX}{device_id[:8]}-{remote_source}"[:56]


def looks_like_device_source(value: str) -> bool:
    return bool(_ID.match(value or ""))


def grace_seconds() -> float:
    return float(max(settings.connector_grace_seconds, 0))


@dataclass
class DeviceView:
    """What a router needs of one paired computer, read from its row."""

    id: str
    name: str
    hello: dict
    chat_model: Optional[str]
    embed_model: Optional[str]

    @property
    def ram_bytes(self) -> Optional[int]:
        ram = self.hello.get("ram_bytes")
        return int(ram) if isinstance(ram, int) and ram > 0 else None


def _info_from_report(report: P.ModelInfoReport) -> ModelInfo:
    return ModelInfo(
        name=report.name,
        context_window=report.context_window,
        context_source=report.context_source,
        parameters_total=report.parameters_total,
        parameters_active=report.parameters_active,
        parameter_label=report.parameter_label,
        quantization=report.quantization,
        kind=report.kind,
        capabilities=tuple(report.capabilities) if report.capabilities is not None else None,
        structured_output=report.structured_output or "none",
        thinking=report.thinking,
        is_local=report.is_local,
        architecture=report.architecture,
        kv_bytes_per_token=report.kv_bytes_per_token,
        kv_bytes_per_token_windowed=report.kv_bytes_per_token_windowed,
        sliding_window=report.sliding_window,
        experts_total=report.experts_total,
        experts_active=report.experts_active,
        weights_bytes=report.weights_bytes,
        defaults=dict(report.defaults),
        runtime_version=report.runtime_version,
    )


def chat_args(request: ChatRequest, remote_source: str, purpose: Optional[str]) -> dict:
    """A `ChatRequest` as the connector's `chat` arguments — sampling only.

    Machine settings are never copied: `ChatArgs` has no field for them, and the
    computer applies its own.
    """
    args = P.ChatArgs(
        source=remote_source,
        model=request.model,
        messages=[{"role": m.get("role"), "content": m.get("content") or ""} for m in request.messages],
        max_tokens=request.max_tokens,
        context_window=request.context_window,
        temperature=request.temperature,
        top_p=request.top_p,
        top_k=request.top_k,
        min_p=request.min_p,
        repeat_penalty=request.repeat_penalty,
        presence_penalty=request.presence_penalty,
        frequency_penalty=request.frequency_penalty,
        seed=request.seed,
        stop=request.stop,
        thinking=request.thinking,
        json_schema=request.json_schema,
        json_mode=request.json_mode,
        structured_output=request.structured_output,
        purpose=purpose[:160] if purpose else None,
    )
    return args.model_dump(exclude_none=True)


def _purpose() -> Optional[str]:
    build = inflight.current()
    agent = inflight.current_agent()
    if build is None and agent is None:
        return None
    who = agent or "An agent"
    name = (build or {}).get("name")
    return f"{who} for build '{name}'" if name else who


class ConnectorAdapter(RuntimeAdapter):
    """One runtime on a paired computer, reached through its connector.

    It speaks what that runtime's own adapter speaks — the same supported settings,
    the same naming rule — because the connector runs that adapter on the other end.
    """

    def __init__(self, device_id: str, device_name: str, remote_source: str, runtime: Optional[str]) -> None:
        super().__init__(f"connector://{device_id[:8]}/{remote_source}")
        self.device_id = device_id
        self.device_name = device_name
        self.remote_source = remote_source
        cls = table.adapter_for(runtime)
        self.runtime = cls.runtime
        self.sampling_supported = cls.sampling_supported
        self.thinking_supported = cls.thinking_supported
        self.schema_with_reasoning = cls.schema_with_reasoning
        # Filled on the computer, by its own adapter, from its own defaults.
        self.fills_sampling_defaults = False
        # Machine settings are the computer's to set; the server never sends one.
        self.machine_supported = frozenset()
        #: Only for its naming rule: never asked anything over the network.
        self._naming = cls("http://127.0.0.1:9")
        #: request id -> the link carrying it, so `cancel` reaches the right socket.
        self._links: dict[str, object] = {}
        self._links_lock = threading.Lock()

    @classmethod
    def fingerprint(cls, base_url, api_key=None, *, timeout=0.0):  # pragma: no cover - never probed
        return None

    def list_models(self) -> list[ModelEntry]:  # pragma: no cover - the provider reads its hello
        raise ProviderError("A connected computer's models come from what it reported.", retryable=False)

    def resolves(self, model, names) -> bool:
        return self._naming.resolves(model, names)

    # ── reaching the computer ────────────────────────────────────────────────
    def _gone(self, why: str = "", *, paused_there: bool = False) -> ComputerDisconnected:
        name = self.device_name or "Your computer"
        return ComputerDisconnected(
            why or f"{name} disconnected. Start the connector on it to continue.",
            device_id=self.device_id,
            device_name=self.device_name,
            paused_there=paused_there,
        )

    def _link(self, *, wait: bool):
        link = hub.live(self.device_id)
        if link is not None and link.approved:
            return link
        if not wait:
            return None
        link = hub.wait_for(self.device_id, grace_seconds())
        if link is None:
            raise self._gone()
        return link

    def _error(self, e: ConnectorError, request_id: Optional[str] = None) -> ProviderError:
        """What a connector's refusal means for the build that asked."""
        if e.code == P.ERR_CANCELLED or (request_id and inflight.was_cancelled(request_id)):
            return RequestCancelled()
        if e.code == P.ERR_PAUSED:
            return self._gone(
                f"{self.device_name or 'Your computer'} is paused. Run `aiteam-connect resume` on it to continue.",
                paused_there=True,
            )
        if e.code == P.ERR_LIMIT:
            return ProviderError(f"{self.device_name or 'Your computer'} refused: {e}", retryable=False)
        if e.code == P.ERR_REFUSED:
            return ProviderError(f"{self.device_name or 'Your computer'} refused: {e}", retryable=False)
        if e.code in (P.ERR_RUNTIME, P.ERR_UNREACHABLE):
            return ProviderError(f"On {self.device_name or 'your computer'}: {e}")
        return ProviderError(str(e), retryable=False)

    def _ask(self, op: str, args: dict, *, timeout: float, request_id: Optional[str] = None):
        """One model op, surviving a connection that drops and comes back in time.

        A request lost with its connection is sent again once the computer is back —
        the old one died with the socket, and the connector cancelled it there.
        """
        for attempt in range(2):
            link = self._link(wait=True)
            if request_id:
                with self._links_lock:
                    self._links[request_id] = link
                # Registered first, then checked: a Stop from here on reaches the
                # link, and one that came while this waited for it is seen now.
                if inflight.was_cancelled(request_id):
                    with self._links_lock:
                        self._links.pop(request_id, None)
                    link.forget_cancel(request_id)
                    raise RequestCancelled()
            try:
                return hub.call(link.request(op, args, timeout=timeout, request_id=request_id), timeout + 10)
            except ConnectorError as e:
                if request_id and inflight.was_cancelled(request_id):
                    raise RequestCancelled() from None
                if e.code is None and link.closed and attempt == 0:
                    log.info("The connection to device %s dropped during '%s'; waiting for it.", self.device_id, op)
                    continue
                if e.code is None and link.closed:
                    raise self._gone() from None
                raise self._error(e, request_id) from None
            finally:
                if request_id:
                    with self._links_lock:
                        self._links.pop(request_id, None)
        raise self._gone()

    # ── the typed operations ─────────────────────────────────────────────────
    def model_info(self, model: str) -> Optional[ModelInfo]:
        link = self._link(wait=False)
        if link is None:
            return None
        try:
            result = hub.call(
                link.request("model_info", {"source": self.remote_source, "model": model}, timeout=_INFO_TIMEOUT),
                _INFO_TIMEOUT + 5,
            )
            return _info_from_report(P.ModelInfoReport.model_validate(result))
        except (ConnectorError, ValidationError) as e:
            log.info("Device %s couldn't describe %s: %s", self.device_id, model, e)
            return None

    def chat(self, request: ChatRequest) -> ChatResult:
        try:
            args = chat_args(request, self.remote_source, _purpose())
        except ValidationError as e:
            raise ProviderError(f"This request can't be sent to your computer: {e.errors()[0]['msg']}",
                                retryable=False) from None
        result = self._ask("chat", args, timeout=_CHAT_TIMEOUT, request_id=request.request_id)
        try:
            report = P.ChatReport.model_validate(result)
        except ValidationError:
            raise ProviderError("Your computer's answer didn't match the protocol.", retryable=False) from None
        return ChatResult(
            text=report.text,
            prompt_tokens=report.prompt_tokens,
            completion_tokens=report.completion_tokens,
            finish_reason=report.finish_reason,
            structured_output=report.structured_output,
            structured_output_rejected=report.structured_output_rejected,
            reasoning=report.reasoning,
            unsent=tuple(report.unsent),
        )

    def embed(self, model: str, inputs: list[str], *, request_id: Optional[str] = None) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), P.MAX_EMBED_INPUTS):
            batch = inputs[start : start + P.MAX_EMBED_INPUTS]
            args = P.EmbedArgs(source=self.remote_source, model=model, inputs=batch).model_dump()
            result = self._ask("embed", args, timeout=_EMBED_TIMEOUT, request_id=request_id)
            try:
                report = P.EmbedReport.model_validate(result)
            except ValidationError:
                raise ProviderError("Your computer's embeddings didn't match the protocol.", retryable=False) from None
            if len(report.vectors) != len(batch):
                raise ProviderError("Your computer returned the wrong number of embeddings.", retryable=False)
            vectors.extend(report.vectors)
        return vectors

    def cancel(self, request_id: str) -> bool:
        with self._links_lock:
            link = self._links.get(request_id)
        if link is None:
            return False
        try:
            return bool(hub.call(link.cancel_request(request_id), 5.0))
        except ConnectorError:
            return False


class ConnectorProvider(SourceProvider):
    """One runtime on a paired computer, as a model source in its owner's router."""

    is_local = True

    def __init__(self, device: DeviceView, report: dict, tuning=None) -> None:
        remote_source = str(report.get("id"))
        runtime = report.get("runtime") if report.get("runtime") in table.BY_ID else table.GENERIC
        source = Source(
            id=source_id_for(device.id, remote_source),
            label=f"{report.get('label') or runtime} on {device.name}",
            base_url=f"connector://{device.id[:8]}/{remote_source}",
            runtime=runtime,
            origin=ORIGIN_CONNECTOR,
            version=report.get("version"),
            same_machine_override=False,
        )
        super().__init__(source, ConnectorAdapter(device.id, device.name, remote_source, runtime), tuning)
        self.device_id = device.id
        self.remote_source = remote_source
        self.update(device, report)

    def update(self, device: DeviceView, report: dict) -> None:
        self.device_name = device.name
        self.adapter.device_name = device.name
        self.source.label = f"{report.get('label') or self.source.runtime} on {device.name}"
        self._ram = device.ram_bytes
        self._reported_reachable = bool(report.get("reachable", True))
        self._reported_error = report.get("error")
        # What hygiene is judged on: the version the runtime told that computer, and
        # whether it answers on that computer's network address as well as loopback.
        self.source.version = report.get("version")
        exposed = report.get("exposed_on")
        self.source.exposed = [str(a) for a in exposed] if isinstance(exposed, list) else None
        entries = []
        for m in report.get("models") or []:
            caps = m.get("capabilities")
            entries.append(
                ModelEntry(
                    name=str(m.get("name")),
                    kind=m.get("kind"),
                    capabilities=tuple(caps) if isinstance(caps, list) else None,
                    is_local=bool(m.get("is_local", True)),
                    size_bytes=m.get("size_bytes"),
                    loaded=m.get("loaded"),
                )
            )
        self._models = entries

    # ── presence, not probes ─────────────────────────────────────────────────
    def connected(self) -> bool:
        link = hub.live(self.device_id)
        return link is not None and link.approved

    def in_grace(self) -> bool:
        """Dropped a moment ago: a call waits for it rather than giving up on it."""
        dropped = hub.dropped_at(self.device_id)
        return dropped is not None and time.monotonic() - dropped < grace_seconds()

    def state(self, max_age: float = 0.0) -> SourceState:
        name = self.device_name or "Your computer"
        if not self.connected() and not self.in_grace():
            return SourceState(
                reachable=False,
                models=list(self._models),
                error=f"{name} isn't connected. Start the connector on it.",
                checked_at=time.monotonic(),
            )
        if not self._reported_reachable:
            return SourceState(
                reachable=False,
                models=list(self._models),
                error=f"{self.source.label} isn't answering on that computer: {self._reported_error or 'no answer'}",
                checked_at=time.monotonic(),
            )
        return SourceState(reachable=True, models=list(self._models), checked_at=time.monotonic())

    def describe(self) -> dict[str, ModelEntry]:
        return {m.name: m for m in self._models}

    def down_reason(self) -> str:
        return self.state().error or f"{self.device_name} isn't connected."

    def gone(self) -> bool:
        """Not connected, and past the grace a dropped connection gets — what pauses a
        build. A computer that is connected but whose runtime is down is not gone:
        no reconnect is coming to resume a build paused for that."""
        return not self.connected() and not self.in_grace()

    def unavailable_error(self) -> ProviderError:
        if self.gone():
            return ComputerDisconnected(self.down_reason(), device_id=self.device_id, device_name=self.device_name)
        return ProviderError(self.down_reason(), retryable=False)

    def ram_bytes(self) -> Optional[int]:
        return self._ram

    def remote_host(self) -> bool:
        # The computer said how much memory it has, so the check can use it.
        return self._ram is None


class DeviceSources:
    """One account's paired computers, as model sources — read from the database,
    kept while nothing about who is connected has changed."""

    def __init__(self, user_id: Optional[str], tuning=None) -> None:
        self.user_id = user_id
        self._tuning = tuning
        self._lock = threading.Lock()
        self._providers: dict[str, ConnectorProvider] = {}
        self._devices: list[DeviceView] = []
        self._read_at: Optional[float] = None
        self._version = -1

    def _read(self) -> list[DeviceView]:
        from app.db.base import SessionLocal
        from app.db.models import Device

        with SessionLocal() as db:
            rows = db.execute(
                select(Device)
                .where(Device.owner_id == self.user_id, Device.status == P.STATE_APPROVED)
                .order_by(Device.created_at)
            ).scalars().all()
            return [
                DeviceView(id=d.id, name=d.name, hello=dict(d.hello or {}), chat_model=d.chat_model,
                           embed_model=d.embed_model)
                for d in rows
            ]

    def refresh(self, *, force: bool = False) -> None:
        if not self.user_id:
            return
        now = time.monotonic()
        with self._lock:
            fresh = (
                self._read_at is not None
                and now - self._read_at < _DEVICES_TTL_SECONDS
                and self._version == hub.version
            )
            if fresh and not force:
                return
            version = hub.version
            try:
                devices = self._read()
            except Exception as e:  # noqa: BLE001 - a busy database mustn't take routing down
                log.warning("Couldn't read the paired computers: %s", e)
                return
            providers: dict[str, ConnectorProvider] = {}
            for device in devices:
                for report in device.hello.get("sources") or []:
                    if not isinstance(report, dict) or not report.get("id"):
                        continue
                    sid = source_id_for(device.id, str(report["id"]))
                    existing = self._providers.get(sid)
                    if existing is not None:
                        existing.update(device, report)
                        providers[sid] = existing
                    else:
                        providers[sid] = ConnectorProvider(device, report, self._tuning)
            self._providers = providers
            self._devices = devices
            self._read_at = now
            self._version = version

    def invalidate(self) -> None:
        with self._lock:
            self._read_at = None

    def providers(self) -> list[ConnectorProvider]:
        self.refresh()
        with self._lock:
            return list(self._providers.values())

    def get(self, source_id: str) -> Optional[ConnectorProvider]:
        if not looks_like_device_source(source_id):
            return None
        self.refresh()
        with self._lock:
            return self._providers.get(source_id)

    def devices(self) -> list[DeviceView]:
        self.refresh()
        with self._lock:
            return list(self._devices)

    def _choice(self, field: str) -> Optional[tuple[str, str]]:
        """The model chosen on the account's computers for `field` — a connected
        computer's first, so an old laptop left off doesn't hold the build."""
        choices = []
        for device in self.devices():
            spec = getattr(device, field)
            if not spec:
                continue
            remote_source, sep, model = spec.partition(":")
            if not sep or not model:
                continue
            link = hub.live(device.id)
            choices.append((link is None, (source_id_for(device.id, remote_source), model)))
        choices.sort(key=lambda c: c[0])
        return choices[0][1] if choices else None

    def chat_choice(self) -> Optional[tuple[str, str]]:
        return self._choice("chat_model")

    def embed_choice(self) -> Optional[tuple[str, str]]:
        return self._choice("embed_model")
