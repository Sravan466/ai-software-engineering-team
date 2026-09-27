"""The models on this computer, and the only questions the server may ask about them.

Sources are the runtimes detection finds on loopback, using the server's own
Phase 1 adapter table and fingerprints, plus any address you added with
`aiteam-connect add-source` — confirmed on this computer when it isn't loopback.
The website can never add one: it has no message that carries an address.

`Agent.handle` answers `hello`, `list_models`, `model_info`, `ping`, `chat`, `embed`
and `cancel`, and refuses everything else. A refusal is written to `connector.log`
on this computer, with what was asked, so an attempt is never silent.

A model call runs under this computer's limits (`limits.py`), with its machine
settings (GPU layers, threads, keep-alive) taken from here only. Each one is written
to `activity.log` — time, operation, model, token counts — and never its contents.
The answer goes back to the server as data: nothing here runs a tool or a command,
or writes a file, whatever the model says.
"""
from __future__ import annotations

import logging
import platform
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from pydantic import ValidationError

from app.connector import protocol as P
from app.router.model_profile import total_ram_bytes
from app.router.runtimes import detect, table
from app.router.base import ProviderError
from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.types import ChatRequest
from aiteam_connect import limits as L
from aiteam_connect import store

refusals = logging.getLogger("aiteam_connect.refused")
activity = logging.getLogger("aiteam_connect.activity")


def _log_to(logger: logging.Logger, filename: str) -> None:
    """Send `logger` to `filename` in the connector's home (once per home)."""
    path = str(store.home() / filename)
    for handler in list(logger.handlers):
        if getattr(handler, "baseFilename", None) == path:
            return
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _log_refusals() -> None:
    _log_to(refusals, "connector.log")
    _log_to(activity, "activity.log")


def os_name() -> str:
    return {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(platform.system(), platform.system())


def os_version() -> str:
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    return f"{platform.system()} {platform.release()}".strip()


def computer_name() -> str:
    name = socket.gethostname().split(".")[0]
    return (name or "My computer")[:120]


class Refused(Exception):
    """A request this connector will not perform. `code` says why, for the server."""

    code = P.ERR_REFUSED


class Paused(Refused):
    code = P.ERR_PAUSED


class Cancelled(Refused):
    code = P.ERR_CANCELLED


class RuntimeFailed(Refused):
    code = P.ERR_RUNTIME


class Unreachable(Refused):
    code = P.ERR_UNREACHABLE


class Source:
    def __init__(self, source_id: str, runtime: str, base_url: str, adapter: RuntimeAdapter, *,
                 remote: bool, version: Optional[str]) -> None:
        self.id = source_id
        self.runtime = runtime
        self.base_url = base_url
        self.adapter = adapter
        self.remote = remote
        self.version = version
        self.models: list[str] = []


class Agent:
    """Answers the server's typed operations for this computer."""

    def __init__(
        self, device_id: str, *, skip_ports: tuple[int, ...] = (), say: Optional[Callable[[str], None]] = None
    ) -> None:
        self.device_id = device_id
        self.skip_ports = skip_ports
        self._sources: dict[str, Source] = {}
        self._say = say or (lambda _text: None)
        self.gate = L.Gate()
        #: request id -> (the adapter running it, set when it's been stopped)
        self._running: dict[str, tuple[RuntimeAdapter, threading.Event]] = {}
        self._running_lock = threading.Lock()
        #: (source, model) -> (the window the model reports, when asked), for the clamp.
        self._windows: dict[tuple[str, str], tuple[Optional[int], float]] = {}
        _log_refusals()

    # ── finding the sources ─────────────────────────────────────────────────
    def _configured(self) -> list[dict]:
        out = []
        for entry in store.load_state().get("sources") or []:
            url = str(entry.get("url") or "")
            if not url:
                continue
            if not detect.is_loopback(url) and not entry.get("confirmed_remote"):
                refusals.info("skipped source %s: not loopback and never confirmed on this computer", url)
                continue
            out.append(entry)
        return out

    def _id_for(self, runtime: str, base_url: str) -> str:
        if runtime not in {s.runtime for s in self._sources.values()}:
            return runtime
        port = urlparse(base_url).port or 0
        return f"{runtime}-{port}"[:48]

    def scan(self) -> tuple[list[dict], list[dict], list[str]]:
        """(sources, unknown ports, addresses tried), and remember the sources."""
        self._sources = {}
        found = detect.detect(skip_ports=self.skip_ports)
        hellos = list(found.found)
        for entry in self._configured():
            url = entry["url"].rstrip("/")
            if any(detect.same_address(url, h.base_url) for h in hellos):
                continue
            api_key = store.get_secret(store.source_key_name(url))
            hello = detect.identify(url, api_key, prefer=entry.get("runtime"))
            runtime = hello.runtime if hello else table.GENERIC
            adapter = table.adapter_for(runtime)(url, api_key)
            source = Source(self._id_for(runtime, url), runtime, url, adapter,
                            remote=not detect.is_loopback(url), version=hello.version if hello else None)
            self._sources[source.id] = source
        for hello in hellos:
            adapter = table.adapter_for(hello.runtime)(hello.base_url)
            source = Source(self._id_for(hello.runtime, hello.base_url), hello.runtime, hello.base_url,
                            adapter, remote=False, version=hello.version)
            self._sources[source.id] = source

        reports = []
        for source in self._sources.values():
            report: dict[str, Any] = {
                "id": source.id,
                "runtime": source.runtime,
                "label": table.spec_for(source.runtime).label,
                "base_url": source.base_url,
                "remote": source.remote,
                "version": source.version,
                "reachable": True,
                "error": None,
                "models": [],
            }
            try:
                entries = source.adapter.list_models()
                source.models = [e.name for e in entries]
                report["models"] = [
                    {
                        "name": e.name[:300],
                        "kind": e.kind,
                        "capabilities": list(e.capabilities)[:32] if e.capabilities is not None else None,
                        "is_local": e.is_local,
                        "size_bytes": e.size_bytes,
                        "loaded": e.loaded,
                    }
                    for e in entries[:500]
                ]
            except Exception as e:  # noqa: BLE001 - reported, not raised: the others still count
                report["reachable"] = False
                report["error"] = str(e)[:500]
            reports.append(report)
        return reports, found.unknown[:32], found.tried[:64]

    # ── the operations ──────────────────────────────────────────────────────
    def hello(self) -> dict:
        sources, unknown, tried = self.scan()
        return P.HelloReport(
            device_id=self.device_id,
            connector_version=P.CONNECTOR_VERSION,
            protocol=P.PROTOCOL,
            os=os_name(),
            os_version=os_version(),
            arch=platform.machine()[:32] or None,
            ram_bytes=total_ram_bytes(),
            hostname=computer_name(),
            sources=sources,
            unknown=unknown,
            tried=tried,
            capabilities=list(P.OPS),
            limits=L.read(store.load_state()),
            paused=bool(store.load_state().get("paused")),
        ).model_dump()

    def model_info(self, args: dict) -> dict:
        try:
            wanted = P.ModelInfoArgs.model_validate(args)
        except ValidationError:
            raise Refused("model_info takes a source id and a model name, and nothing else.") from None
        source = self._sources.get(wanted.source)
        if source is None:
            raise Refused(f"No source called '{wanted.source}' on this computer.")
        if wanted.model not in source.models:
            raise Refused(f"'{wanted.model}' isn't a model {source.id} listed.")
        info = source.adapter.model_info(wanted.model)
        if info is None:
            return P.ModelInfoReport(name=wanted.model).model_dump()
        # Reported as the window this computer will actually run it at, so the
        # server budgets its prompts to fit — not to a window it would truncate.
        cap = L.read(store.load_state())["max_context_tokens"]
        window = min(info.context_window, cap) if info.context_window else None
        defaults = {
            k: v for k, v in (info.defaults or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float, str, list, type(None)))
        }
        return P.ModelInfoReport(
            name=info.name[:300],
            context_window=window,
            context_source=(info.context_source or None) and info.context_source[:16],
            parameters_total=info.parameters_total,
            parameters_active=info.parameters_active,
            parameter_label=(info.parameter_label or None) and info.parameter_label[:32],
            quantization=(info.quantization or None) and info.quantization[:32],
            kind=info.kind,
            capabilities=[c[:64] for c in info.capabilities][:32] if info.capabilities is not None else None,
            structured_output=info.structured_output,
            thinking=info.thinking,
            is_local=info.is_local,
            architecture=(info.architecture or None) and info.architecture[:64],
            kv_bytes_per_token=info.kv_bytes_per_token,
            kv_bytes_per_token_windowed=info.kv_bytes_per_token_windowed or 0,
            sliding_window=info.sliding_window,
            experts_total=info.experts_total,
            experts_active=info.experts_active,
            weights_bytes=info.weights_bytes,
            defaults=dict(list(defaults.items())[:16]),
            runtime_version=(info.runtime_version or None) and info.runtime_version[:64],
        ).model_dump()

    # ── model calls ─────────────────────────────────────────────────────────
    def _source_for(self, source_id: str, model: str) -> Source:
        if not self._sources:
            self.scan()
        source = self._sources.get(source_id)
        if source is None:
            raise Refused(f"No source called '{source_id}' on this computer.")
        if model not in source.models and not source.adapter.resolves(model, source.models):
            # A model pulled since the last scan is found by looking again.
            self.scan()
            source = self._sources.get(source_id)
            if source is None or not source.adapter.resolves(model, source.models):
                raise Refused(f"'{model}' isn't a model {source_id} lists on this computer.")
        return source

    def _check_paused(self) -> None:
        if store.load_state().get("paused"):
            raise Paused("Model calls are paused on this computer. Run `aiteam-connect resume` there.")

    def _say_safely(self, text: str) -> None:
        """Tell the terminal — but a console that can't print this must never stop a call."""
        try:
            self._say(text)
        except Exception:  # noqa: BLE001 - an encoding error, a closed stdout
            pass

    @contextmanager
    def _slot(self, request_id: str, adapter: RuntimeAdapter, limits: dict, size: int):
        """Register the call so `cancel` can reach it, then wait for a free slot.

        Registered *before* waiting, so a cancel that arrives while it is queued —
        or before the runtime has been asked anything — is kept, not lost. The slot
        is released on every way out, and only if it was taken.
        """
        stopped = threading.Event()
        with self._running_lock:
            self._running[request_id] = (adapter, stopped)
        try:
            try:
                self.gate.admit(limits, size, stopped)
            except L.LimitRefused:
                if stopped.is_set():
                    raise Cancelled("Stopped.") from None
                raise
            try:
                yield stopped
            finally:
                self.gate.release()
        finally:
            with self._running_lock:
                self._running.pop(request_id, None)

    def _run(self, request_id: str, adapter: RuntimeAdapter, stopped: threading.Event, timeout: int, fn):
        """Run one model call, stoppable by `cancel` and by the local timeout."""
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            adapter.cancel(request_id)

        if stopped.is_set():
            raise Cancelled("Stopped.")
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            result = fn()
        except ProviderError as e:
            if stopped.is_set():
                raise Cancelled("Stopped.") from None
            if timed_out.is_set():
                raise L.LimitRefused(
                    f"The call ran past this computer's limit of {timeout} seconds and was stopped "
                    "(aiteam-connect limits --timeout-seconds)."
                ) from None
            if e.unreachable:
                raise Unreachable(str(e)[:500]) from None
            raise RuntimeFailed(str(e)[:500]) from None
        finally:
            timer.cancel()
        if stopped.is_set():
            # Cancelled just as it finished: whoever asked has stopped listening.
            raise Cancelled("Stopped.")
        return result

    def _context_for(self, source: Source, model: str, wanted: int, limits: dict) -> int:
        """The window this call runs at: what the server asked, held to what this
        computer allows and to what the model itself reports.

        The window sizes the runtime's KV cache — memory on this computer — so it
        is a machine setting in effect, and the server never has the last word.
        """
        window = min(int(wanted), limits["max_context_tokens"])
        key = (source.id, model)
        known = self._windows.get(key)
        if known is None or time.monotonic() - known[1] > 300:
            try:
                info = source.adapter.model_info(model)
            except Exception:  # noqa: BLE001 - a model that can't be described keeps the local cap
                info = None
            known = ((info.context_window if info is not None else None) or None, time.monotonic())
            self._windows[key] = known
        if known[0]:
            window = min(window, int(known[0]))
        return max(window, 256)

    def chat(self, args: dict, request_id: str) -> dict:
        try:
            wanted = P.ChatArgs.model_validate(args)
        except ValidationError as e:
            fields = sorted({str(err["loc"][0]) for err in e.errors() if err.get("loc")})[:8]
            refusals.info("refused chat: arguments off the schema (%s)", ",".join(fields))
            raise Refused(
                "This computer refused a chat request that didn't match its schema"
                + (f" ({', '.join(fields)})" if fields else "")
                + ". Machine settings such as GPU layers are set here, never by the server."
            ) from None
        self._check_paused()
        source = self._source_for(wanted.source, wanted.model)
        state = store.load_state()
        limits = L.read(state)
        prompt_chars = sum(len(m.content) for m in wanted.messages)
        started = time.monotonic()
        try:
            with self._slot(request_id, source.adapter, limits, prompt_chars) as stopped:
                self._say_safely(f"Answering {wanted.purpose or 'a build'} with {wanted.model}...")
                request = ChatRequest(
                    model=wanted.model,
                    messages=[m.model_dump() for m in wanted.messages],
                    max_tokens=L.clamp_tokens(wanted.max_tokens, limits),
                    context_window=self._context_for(source, wanted.model, wanted.context_window, limits),
                    temperature=wanted.temperature,
                    top_p=wanted.top_p,
                    top_k=wanted.top_k,
                    min_p=wanted.min_p,
                    repeat_penalty=wanted.repeat_penalty,
                    presence_penalty=wanted.presence_penalty,
                    frequency_penalty=wanted.frequency_penalty,
                    seed=wanted.seed,
                    stop=wanted.stop,
                    thinking=wanted.thinking,
                    json_schema=wanted.json_schema,
                    json_mode=wanted.json_mode,
                    structured_output=wanted.structured_output,
                    request_id=request_id,
                    # This computer's own machine settings — never the server's.
                    **(L.machine(state) if not source.remote else {}),
                )
                result = self._run(request_id, source.adapter, stopped, limits["timeout_seconds"],
                                   lambda: source.adapter.chat(request))
        except (Refused, L.LimitRefused) as e:
            activity.info("chat model=%s outcome=%s", wanted.model, e.code)
            self._say_safely(f"  stopped ({e.code}).")
            raise
        seconds = time.monotonic() - started
        activity.info(
            "chat model=%s prompt_tokens=%d completion_tokens=%d seconds=%.1f finish=%s",
            wanted.model, result.prompt_tokens, result.completion_tokens, seconds, result.finish_reason,
        )
        self._say_safely(
            f"  done in {seconds:.1f} s - {result.prompt_tokens:,} in / {result.completion_tokens:,} out."
        )
        return P.ChatReport(
            text=result.text,
            reasoning=result.reasoning,
            prompt_tokens=max(int(result.prompt_tokens or 0), 0),
            completion_tokens=max(int(result.completion_tokens or 0), 0),
            finish_reason=(result.finish_reason or None) and str(result.finish_reason)[:32],
            structured_output=result.structured_output
            if result.structured_output in ("schema", "grammar", "json", "none") else "none",
            structured_output_rejected=bool(result.structured_output_rejected),
            unsent=[str(u)[:32] for u in result.unsent][:16],
        ).model_dump()

    def embed(self, args: dict, request_id: str) -> dict:
        try:
            wanted = P.EmbedArgs.model_validate(args)
        except ValidationError:
            refusals.info("refused embed: arguments off the schema")
            raise Refused("embed takes a source, a model and a list of texts, and nothing else.") from None
        self._check_paused()
        source = self._source_for(wanted.source, wanted.model)
        limits = L.read(store.load_state())
        started = time.monotonic()
        try:
            with self._slot(request_id, source.adapter, limits, sum(len(t) for t in wanted.inputs)) as stopped:
                vectors = self._run(
                    request_id, source.adapter, stopped, limits["timeout_seconds"],
                    lambda: source.adapter.embed(wanted.model, list(wanted.inputs), request_id=request_id),
                )
        except (Refused, L.LimitRefused) as e:
            activity.info("embed model=%s outcome=%s", wanted.model, e.code)
            raise
        activity.info("embed model=%s inputs=%d seconds=%.1f", wanted.model, len(wanted.inputs),
                      time.monotonic() - started)
        return P.EmbedReport(vectors=vectors).model_dump()

    def cancel(self, args: dict) -> dict:
        try:
            wanted = P.CancelArgs.model_validate(args)
        except ValidationError:
            raise Refused("cancel takes the id of a request, and nothing else.") from None
        with self._running_lock:
            running = self._running.get(wanted.id)
        if running is None:
            return {"cancelled": False}
        adapter, stopped = running
        stopped.set()
        adapter.cancel(wanted.id)
        activity.info("cancel request=%s", wanted.id)
        self._say("  stopped by the website.")
        return {"cancelled": True}

    def cancel_all(self) -> int:
        """Stop everything in flight — the connection it would answer on is gone."""
        with self._running_lock:
            running = list(self._running.items())
        for request_id, (adapter, stopped) in running:
            stopped.set()
            try:
                adapter.cancel(request_id)
            except Exception:  # noqa: BLE001
                pass
        return len(running)

    def handle(self, op: str, args: dict, request_id: Optional[str] = None) -> Any:
        """Perform one of `protocol.OPS`, or raise `Refused` and log the attempt here."""
        if op not in P.OPS:
            refusals.info("refused op %r (not one of %s) args=%s", op[:64], ",".join(P.OPS), sorted(args)[:16])
            raise Refused(f"'{op[:64]}' is not something this connector does.")
        if op == "chat":
            return self.chat(args, request_id or "0" * 16)
        if op == "embed":
            return self.embed(args, request_id or "0" * 16)
        if op == "cancel":
            return self.cancel(args)
        if op == "model_info":
            try:
                return self.model_info(args)
            except Refused as e:
                refusals.info("refused model_info: %s args=%s", e, sorted(args)[:16])
                raise
        if args:
            refusals.info("refused %s with unexpected arguments %s", op, sorted(args)[:16])
            raise Refused(f"'{op}' takes no arguments.")
        if op == "ping":
            return {"t": int(time.time())}
        if op == "list_models":
            sources, _, _ = self.scan()
            return {"sources": sources}
        return self.hello()
