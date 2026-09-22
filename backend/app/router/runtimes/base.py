"""What every runtime adapter is: one address, spoken to in one API dialect.

An adapter turns the typed operations in `types.py` into one runtime's HTTP calls
and back. It knows its runtime's field names and quirks and nothing else — not
which roles use it, not whether a build may start, not what a model costs. Those
belong to the router, which only ever holds adapters by their typed operations.

Adapters are identified by what an address *answers*, never by its port: the same
port is the default for several runtimes, and a runtime can be moved to any port.
"""
from __future__ import annotations

import abc
import threading
from typing import Iterable, Iterator, Optional

import httpx

from app.core.logging import get_logger
from app.router.base import RETRYABLE_STATUS, ProviderError
from app.router.runtimes.types import (
    MACHINE_KEYS,
    SAMPLING_KEYS,
    ChatRequest,
    ChatResult,
    Hello,
    ModelEntry,
    ModelInfo,
)

log = get_logger(__name__)

#: A fingerprint probe asks a server that is already known to accept connections
#: for one small JSON answer. A runtime that takes longer than this to say who it
#: is will not be mistaken for one that is not there — it is asked again later.
FINGERPRINT_TIMEOUT = 1.5
#: Listing models is the reachability check, so it is short: a Settings page that
#: waits on a runtime that is not there should wait once, briefly.
LIST_TIMEOUT = 3.0
#: Describing one model. Some runtimes read the model file to answer.
INFO_TIMEOUT = 10.0
#: One completion. Local generation on a CPU can take minutes.
CHAT_TIMEOUT = 600.0
EMBED_TIMEOUT = 120.0


def http_error(error: Exception, what: str, *, advice: str = "") -> ProviderError:
    """A failed call as a `ProviderError`, retryable only when retrying can help."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        body = (error.response.text or "")[:200]
        return ProviderError(
            f"{what} returned {status}: {body}{advice}",
            retryable=status in RETRYABLE_STATUS,
        )
    # A timeout, a refused connection, a half-closed socket: a local runtime does all
    # three while it swaps a model into memory, and those are what retrying is for.
    # Only a connection that never opened says the runtime is not there at all.
    return ProviderError(
        f"{what} failed: {error}",
        unreachable=isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)),
    )


class RuntimeAdapter(abc.ABC):
    """One runtime at one address, answering the typed operations."""

    #: The runtime's id in the adapter table.
    runtime: str = "base"
    #: Whether this adapter can ask its runtime to download a model.
    can_download: bool = False
    #: Which of `SAMPLING_KEYS` and `MACHINE_KEYS` this adapter can put on the wire.
    #: A setting outside these is shown as unsupported and reported on the call,
    #: never dropped without a word.
    sampling_supported: frozenset[str] = frozenset()
    machine_supported: frozenset[str] = frozenset()
    #: Which thinking settings (`THINKING_SETTINGS`) the runtime can be told.
    thinking_supported: tuple[str, ...] = ()
    #: Whether a schema-constrained reply still lets the model reason first. Where
    #: it does not, decoding is held to the schema from the first token, so a
    #: request that thinks drops the constraint and relies on validation instead.
    schema_with_reasoning: bool = False

    def unsent(self, request: ChatRequest) -> tuple[str, ...]:
        """The settings `request` carries that this adapter cannot send."""
        out = [
            key
            for key in (*SAMPLING_KEYS, *MACHINE_KEYS)
            if key != "thinking"
            and getattr(request, key, None) is not None
            and key not in self.sampling_supported
            and key not in self.machine_supported
        ]
        if request.thinking is not None and request.thinking not in self.thinking_supported:
            out.append("thinking")
        return tuple(out)

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self._inflight: dict[str, httpx.Client] = {}
        self._inflight_lock = threading.Lock()

    # ── identification ───────────────────────────────────────────────────────
    @classmethod
    @abc.abstractmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        """Who answers at `base_url`, if it is this runtime; None if it is not.

        Must be cheap and must never raise: detection asks every adapter in turn,
        and one runtime's odd reply must not stop the next one being asked.
        """

    # ── the typed operations ─────────────────────────────────────────────────
    @abc.abstractmethod
    def list_models(self) -> list[ModelEntry]:
        """Every model this source serves. Raises `ProviderError` when unreachable."""

    @abc.abstractmethod
    def model_info(self, model: str) -> Optional[ModelInfo]:
        """What the runtime can say about `model`, or None when it cannot say."""

    @abc.abstractmethod
    def chat(self, request: ChatRequest) -> ChatResult:
        """One completion. Raises `ProviderError` on failure."""

    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        """One vector per input. Raises `ProviderError` on failure."""
        raise ProviderError(
            f"This runtime does not serve embeddings through its API ({self.base_url}).",
            retryable=False,
        )

    def cancel(self, request_id: str) -> bool:
        """Stop a request that is still generating. True if one was stopped.

        Closing the connection is the one cancellation every HTTP runtime honours:
        each stops generating for a client that has gone.
        """
        with self._inflight_lock:
            client = self._inflight.pop(request_id, None)
        if client is None:
            return False
        client.close()
        return True

    # ── optional: downloads ──────────────────────────────────────────────────
    def pull(self, model: str) -> Iterator[dict]:
        """Download `model`, yielding `{status, total?, completed?}` as it goes."""
        raise ProviderError("This runtime does not download models through its API.", retryable=False)

    # ── naming, and the caches behind it ────────────────────────────────────
    def resolves(self, model: str, names: Iterable[str]) -> bool:
        """Whether `model` names one of `names` by this runtime's naming rule."""
        return model in set(names)

    def describe(self, entries: list[ModelEntry]) -> dict[str, ModelEntry]:
        """`{name: entry}`, with whatever the list left unsaid filled in where cheap.

        The list is usually enough. Adapters whose runtime lists models without
        saying what they are for override this to ask, side by side.
        """
        return {e.name: e for e in entries}

    def known_kind(self, model: str) -> Optional[ModelEntry]:
        """What is already remembered about `model` — never a network call."""
        return None

    def forget(self, model: Optional[str] = None) -> None:
        """Drop anything remembered about `model` (all models when None)."""

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _get(self, path: str, *, timeout: float) -> httpx.Response:
        return httpx.get(f"{self.base_url}{path}", headers=self.headers(), timeout=timeout)

    def _post(self, path: str, body: dict, *, timeout: float) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}{path}", json=body, headers=self.headers(), timeout=timeout
        )

    def _post_cancellable(
        self, path: str, body: dict, *, timeout: float, request_id: Optional[str]
    ) -> httpx.Response:
        """A POST that `cancel(request_id)` can cut short by closing its connection."""
        if not request_id:
            return self._post(path, body, timeout=timeout)
        client = httpx.Client(headers=self.headers(), timeout=timeout)
        with self._inflight_lock:
            self._inflight[request_id] = client
        try:
            return client.post(f"{self.base_url}{path}", json=body)
        finally:
            with self._inflight_lock:
                self._inflight.pop(request_id, None)
            client.close()


def get_json(
    base_url: str, path: str, api_key: Optional[str], *, timeout: float
) -> Optional[object]:
    """GET one JSON document, or None for anything else — for fingerprinting only."""
    try:
        r = httpx.get(
            f"{base_url.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - not this runtime, or not answering
        return None
