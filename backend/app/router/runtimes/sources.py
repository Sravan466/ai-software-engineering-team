"""Every model source this backend knows about, and one provider for each.

Sources arrive three ways, and all three end up as the same thing:

  **configured** — `LOCAL_SOURCES` (and the deprecated runtime address) in `.env`.
  Whoever edits that file runs this backend, so an address on another computer is
  honoured there as written.

  **added** — typed into Settings. An address that is not loopback is refused unless
  the request explicitly confirms it, and is marked remote wherever it is shown.

  **detected** — found by probing loopback on the adapter table's default ports.
  Only a runtime that identifies itself is adopted; anything else that answers is
  listed as unknown and waits for someone to confirm it.

A source keeps its id for as long as the process runs, and a detected one stays
listed after its runtime stops — as unreachable, which is what a person looking at
Settings needs to see — rather than vanishing and taking every choice made for it
with it. Ids are the runtime's table id, with a suffix when two share one.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from app.core import secrets_store
from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import CLOUD_PROVIDERS
from app.router.runtimes import detect, table
from app.router.runtimes.openai_compat import api_root
from app.router.runtimes.provider import Source, SourceProvider

log = get_logger(__name__)

#: How long one loopback probe is trusted before the next status request re-runs
#: it. Short enough that a runtime started a moment ago appears on its own.
DETECT_TTL_SECONDS = 30.0

ORIGIN_DETECTED = "detected"
ORIGIN_CONFIGURED = "configured"
ORIGIN_ADDED = "added"


class SourceError(ValueError):
    """A source that cannot be added, or removed, as asked. The message is for a person."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def normalise_url(raw: str) -> str:
    """A base URL, checked: http(s), a host, no credentials, no path beyond `/v1`."""
    url = (raw or "").strip()
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SourceError(f"'{raw}' isn't an http(s) address with a host.")
    if parsed.username or parsed.password:
        raise SourceError("Leave credentials out of the address — use the API key field.")
    if parsed.path.rstrip("/") not in ("", "/v1"):
        raise SourceError("Give the server's address, without a path (a trailing /v1 is fine).")
    if parsed.query or parsed.fragment:
        raise SourceError("Give the server's address without a query or fragment.")
    return api_root(f"{parsed.scheme}://{parsed.netloc}")


class SourceRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, SourceProvider] = {}
        self._unknown: list[dict] = []
        self._tried: list[str] = []
        #: When loopback was last probed; None until it has been. Not 0.0: the
        #: monotonic clock can start near zero, and "probed at 0" would read as fresh.
        self._detected_at: Optional[float] = None
        self._loaded = False
        self._lock = threading.RLock()
        #: Serialises detection itself, so concurrent status requests share one probe.
        self._detect_lock = threading.Lock()

    # ── what is known ────────────────────────────────────────────────────────
    def ensure(self, *, max_age: float = DETECT_TTL_SECONDS) -> None:
        """Load the configured and added sources once, and re-probe loopback when stale."""
        with self._lock:
            if not self._loaded:
                self._load()
                self._loaded = True
        if self._detected_at is None or time.monotonic() - self._detected_at >= max_age:
            self.rescan()

    def providers(self) -> list[SourceProvider]:
        """Every source, configured first, then added, then detected."""
        order = {ORIGIN_CONFIGURED: 0, ORIGIN_ADDED: 1, ORIGIN_DETECTED: 2}
        with self._lock:
            return sorted(self._providers.values(), key=lambda p: order.get(p.source.origin, 3))

    def get(self, source_id: str) -> Optional[SourceProvider]:
        with self._lock:
            return self._providers.get(source_id)

    def ids(self) -> set[str]:
        with self._lock:
            return set(self._providers)

    @property
    def unknown(self) -> list[dict]:
        with self._lock:
            return list(self._unknown)

    @property
    def tried(self) -> list[str]:
        with self._lock:
            return list(self._tried)

    # ── loading ──────────────────────────────────────────────────────────────
    def _load(self) -> None:
        for entry in settings.configured_sources:
            try:
                url = normalise_url(str(entry.get("base_url")))
            except SourceError as e:
                log.warning("Ignoring the configured source %r: %s", entry.get("base_url"), e)
                continue
            if any(detect.same_address(url, p.source.base_url) for p in self._providers.values()):
                continue
            hint = entry.get("runtime") or (
                table.LEGACY_BARE_RUNTIME if entry.get("runtime_hint") == "legacy" else None
            )
            label = str(entry.get("label") or "").strip()
            runtime = hint if hint in table.BY_ID else None
            if runtime is None:
                hello = detect.identify(url, entry.get("api_key"))
                runtime = hello.runtime if hello else None
            source_id = self._free_id(_slug(label) if label else (runtime or f"source-{urlparse(url).port or 80}"), url)
            same = entry.get("same_machine")
            self._register(
                Source(
                    id=source_id,
                    label=label or (table.spec_for(runtime).label if runtime else url),
                    base_url=url,
                    runtime=runtime,
                    origin=ORIGIN_CONFIGURED,
                    api_key=entry.get("api_key") or None,
                    same_machine_override=same if isinstance(same, bool) else None,
                )
            )
        for entry in secrets_store.get_sources():
            if entry["id"] in self._providers:
                continue
            self._register(
                Source(
                    id=entry["id"],
                    label=entry.get("label") or entry["id"],
                    base_url=entry["base_url"],
                    runtime=entry.get("runtime") or table.GENERIC,
                    origin=ORIGIN_ADDED,
                    api_key=entry.get("api_key") or None,
                )
            )

    def _register(self, source: Source) -> SourceProvider:
        adapter = table.adapter_for(source.runtime)(source.base_url, source.api_key)
        provider = SourceProvider(source, adapter)
        self._providers[source.id] = provider
        return provider

    def _free_id(self, wanted: str, url: str) -> str:
        """`wanted`, or `wanted-<port>`, or `wanted-2`… — whichever is not taken.

        A cloud provider's name is never a source id, so `anthropic:…` cannot be read
        two ways.
        """
        base = wanted or "source"
        taken = set(self._providers) | set(CLOUD_PROVIDERS)
        if base not in taken:
            return base
        port = urlparse(url).port
        if port and f"{base}-{port}" not in taken:
            return f"{base}-{port}"
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    # ── detection ────────────────────────────────────────────────────────────
    def rescan(self) -> None:
        """Probe loopback again, adopting runtimes that identify themselves."""
        if not self._detect_lock.acquire(blocking=False):
            # Someone else is probing right now; their answer is as fresh as ours.
            with self._detect_lock:
                return
        try:
            found = detect.detect()
            # A source someone named that nobody could identify when it was loaded —
            # its runtime was still starting, say — is asked again, wherever it is.
            with self._lock:
                pending = [p for p in self._providers.values() if p.source.runtime is None]
            for provider in pending:
                hello = detect.identify(provider.source.base_url, provider.source.api_key)
                if hello is not None:
                    with self._lock:
                        self._identified(provider, hello)
            with self._lock:
                for hello in found.found:
                    self._adopt(hello)
                self._unknown = [
                    u
                    for u in found.unknown
                    if not any(detect.same_address(u["base_url"], p.source.base_url) for p in self._providers.values())
                ]
                self._tried = found.tried
                self._detected_at = time.monotonic()
        finally:
            self._detect_lock.release()

    def _adopt(self, hello) -> None:
        for provider in self._providers.values():
            if not detect.same_address(hello.base_url, provider.source.base_url):
                continue
            source = provider.source
            if source.runtime == hello.runtime:
                source.version = hello.version or source.version
                return
            if source.origin != ORIGIN_DETECTED and source.runtime is not None:
                return  # someone said what this is; an answer does not overrule them
            if source.origin != ORIGIN_DETECTED:
                # A configured source nobody could identify at start now answers.
                self._identified(provider, hello)
                return
            # A different runtime now answers on a port a detected one used to.
            self._providers.pop(source.id, None)
            break
        spec = table.spec_for(hello.runtime)
        source = Source(
            id=self._free_id(spec.id, hello.base_url),
            label=spec.label,
            base_url=hello.base_url,
            runtime=hello.runtime,
            origin=ORIGIN_DETECTED,
            version=hello.version,
        )
        self._register(source)
        log.info("Found %s at %s (source '%s').", spec.label, hello.base_url, source.id)

    @staticmethod
    def _identified(provider: SourceProvider, hello) -> None:
        source = provider.source
        source.runtime = hello.runtime
        source.version = hello.version
        if source.label == source.base_url:
            source.label = table.spec_for(hello.runtime).label
        provider.adapter = table.adapter_for(hello.runtime)(source.base_url, source.api_key)
        provider.invalidate()

    # ── changes from Settings ────────────────────────────────────────────────
    def add(
        self,
        base_url: str,
        *,
        label: Optional[str] = None,
        api_key: Optional[str] = None,
        confirm_remote: bool = False,
    ) -> SourceProvider:
        """Add a source typed into Settings — after it has answered.

        A source that does not answer is not added: the fingerprint is what picks
        its adapter, and a guess made now would be wrong the moment it came up. An
        address that is not loopback is refused without `confirm_remote`, because
        prompts sent there leave this machine.
        """
        url = normalise_url(base_url)
        if not detect.is_loopback(url) and not confirm_remote:
            raise SourceError(
                f"{url} is on another computer. Every prompt a build sends it leaves "
                "this machine — confirm that this is intended to add it."
            )
        with self._lock:
            self.ensure()
            for provider in self._providers.values():
                if detect.same_address(url, provider.source.base_url):
                    raise SourceError(f"{url} is already listed as '{provider.source.label}'.")
        hello = detect.identify(url, api_key)
        if hello is None:
            from app.router.runtimes.openai_compat import OpenAICompatAdapter

            hello = OpenAICompatAdapter.fingerprint(url, api_key)
        if hello is None:
            raise SourceError(
                f"Nothing at {url} answered as a model runtime. Check it is running and "
                "that the address and key are right."
            )
        spec = table.spec_for(hello.runtime)
        with self._lock:
            source = Source(
                id=self._free_id(_slug(label) if label else spec.id, url),
                label=(label or "").strip() or spec.label,
                base_url=url,
                runtime=hello.runtime,
                origin=ORIGIN_ADDED,
                api_key=api_key or None,
                version=hello.version,
            )
            provider = self._register(source)
            self._unknown = [u for u in self._unknown if not detect.same_address(u["base_url"], url)]
            self._save()
        return provider

    def remove(self, source_id: str) -> None:
        with self._lock:
            provider = self._providers.get(source_id)
            if provider is None:
                raise SourceError(f"No source is called '{source_id}'.")
            if provider.source.origin == ORIGIN_CONFIGURED:
                raise SourceError(
                    f"'{provider.source.label}' comes from this backend's configuration; "
                    "remove it there."
                )
            if provider.source.origin == ORIGIN_DETECTED:
                raise SourceError(
                    f"'{provider.source.label}' was found running on this machine; stop "
                    "it there and it will show as not answering."
                )
            self._providers.pop(source_id, None)
            self._save()

    def set_key(self, source_id: str, api_key: Optional[str]) -> SourceProvider:
        """Set ("…"), clear ("") or keep (None) a source's API key."""
        with self._lock:
            provider = self._providers.get(source_id)
            if provider is None:
                raise SourceError(f"No source is called '{source_id}'.")
            if provider.source.origin == ORIGIN_CONFIGURED:
                raise SourceError("This source's key comes from the backend's configuration.")
            if api_key is None:
                return provider
            provider.source.api_key = api_key or None
            provider.adapter.api_key = api_key or None
            provider.invalidate()
            if provider.source.origin == ORIGIN_DETECTED:
                # A key makes a detected source something the user configured.
                provider.source.origin = ORIGIN_ADDED
            self._save()
            return provider

    def _save(self) -> None:
        secrets_store.save_sources(
            [
                {
                    "id": p.source.id,
                    "label": p.source.label,
                    "base_url": p.source.base_url,
                    "runtime": p.source.runtime,
                    **({"api_key": p.source.api_key} if p.source.api_key else {}),
                }
                for p in self._providers.values()
                if p.source.origin == ORIGIN_ADDED
            ]
        )
