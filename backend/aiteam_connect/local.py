"""The models on this computer, and the only questions the server may ask about them.

Sources are the runtimes detection finds on loopback, using the server's own
Phase 1 adapter table and fingerprints, plus any address you added with
`aiteam-connect add-source` — confirmed on this computer when it isn't loopback.
The website can never add one: it has no message that carries an address.

`Agent.handle` answers `hello`, `list_models`, `model_info` and `ping`, and refuses
everything else. A refusal is written to `connector.log` on this computer, with
what was asked, so an attempt is never silent.
"""
from __future__ import annotations

import logging
import platform
import socket
import time
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import ValidationError

from app.connector import protocol as P
from app.router.model_profile import total_ram_bytes
from app.router.runtimes import detect, table
from app.router.runtimes.base import RuntimeAdapter
from aiteam_connect import store

refusals = logging.getLogger("aiteam_connect.refused")


def _log_refusals() -> None:
    """Send refusals to `connector.log` in the connector's home (once per home)."""
    path = str(store.home() / "connector.log")
    for handler in list(refusals.handlers):
        if getattr(handler, "baseFilename", None) == path:
            return
        refusals.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    refusals.addHandler(handler)
    refusals.setLevel(logging.INFO)
    refusals.propagate = False


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
    """A request this connector will not perform."""


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

    def __init__(self, device_id: str, *, skip_ports: tuple[int, ...] = ()) -> None:
        self.device_id = device_id
        self.skip_ports = skip_ports
        self._sources: dict[str, Source] = {}
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
        return P.ModelInfoReport(
            name=info.name[:300],
            context_window=info.context_window,
            parameter_label=(info.parameter_label or None) and info.parameter_label[:32],
            quantization=(info.quantization or None) and info.quantization[:32],
            kind=info.kind,
            structured_output=info.structured_output,
            thinking=info.thinking,
            is_local=info.is_local,
            weights_bytes=info.weights_bytes,
        ).model_dump()

    def handle(self, op: str, args: dict) -> Any:
        """Perform one of `protocol.OPS`, or raise `Refused` and log the attempt here."""
        if op not in P.OPS:
            refusals.info("refused op %r (not one of %s) args=%s", op[:64], ",".join(P.OPS), sorted(args)[:16])
            raise Refused(f"'{op[:64]}' is not something this connector does.")
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
