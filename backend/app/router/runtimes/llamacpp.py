"""The llama.cpp adapter: `llama-server`, and servers built on it.

Recognised by `/v1/models` listing its models as `owned_by: "llamacpp"`, and enriched
from two places the plain dialect does not have. Each model entry carries `meta`
(`n_ctx` — the window the server was *started* with — `n_ctx_train`, `n_params`,
`ftype`), and `/props` repeats the running window per request slot. The window a
prompt is budgeted for is the loaded one, never the trained one: the server cannot
take a longer prompt than it was started with, however long the model could read.

What a model is for is asked directly and cheaply. A server started with
`--embeddings` serves embeddings only; one started without answers `/v1/embeddings`
with an immediate 501. One probe per model, remembered, settles which it is — the
model list itself claims `completion` for both.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import httpx

from app.core.logging import get_logger
from app.router.runtimes.base import FINGERPRINT_TIMEOUT, get_json
from app.router.runtimes.openai_compat import OpenAICompatAdapter, _positive, api_root
from app.router.runtimes.types import (
    CONTEXT_REPORTED,
    KIND_CHAT,
    KIND_EMBEDDING,
    KIND_VISION,
    STRUCTURED_NONE,
    STRUCTURED_SCHEMA,
    Hello,
    ModelEntry,
    ModelInfo,
)

log = get_logger(__name__)

_OWNER = "llamacpp"
#: How long a kind probe's answer is kept. A server's mode is fixed at start, so
#: this only bounds how long a restarted server on the same port is misdescribed.
_KIND_TTL_SECONDS = 300.0
#: How long a probe that got no answer is left alone. Without it, a busy server
#: cost every caller that asked about its models a timeout of its own.
_FAILED_KIND_TTL_SECONDS = 30.0
_KIND_PROBE_TIMEOUT = 5.0
#: "Asked, and got no answer" — unknown, but not worth asking again just yet.
_UNANSWERED = "?"


def _label(n_params: Optional[int]) -> Optional[str]:
    if not n_params:
        return None
    return f"{n_params / 1e9:.1f}B" if n_params >= 1e9 else f"{n_params / 1e6:.0f}M"


class LlamaCppAdapter(OpenAICompatAdapter):
    runtime = "llamacpp"

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        super().__init__(base_url, api_key)
        self._kinds: dict[str, tuple[str, float]] = {}
        self._kind_lock = threading.Lock()

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        data = get_json(base, "/v1/models", api_key, timeout=timeout)
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            return None
        if not all(isinstance(e, dict) and e.get("owned_by") == _OWNER for e in entries):
            return None
        props = get_json(base, "/props", api_key, timeout=timeout)
        version = props.get("build_info") if isinstance(props, dict) else None
        return Hello(runtime=cls.runtime, base_url=base, version=str(version) if version else None)

    # ── what each model is for ───────────────────────────────────────────────
    def _props(self) -> dict:
        props = get_json(self.base_url, "/props", self.api_key, timeout=FINGERPRINT_TIMEOUT)
        return props if isinstance(props, dict) else {}

    def _loaded(self, raw: dict) -> bool:
        """Whether asking about this model would load it. Router mode lists unloaded ones."""
        status = raw.get("status")
        if isinstance(status, dict) and status.get("value"):
            return status.get("value") == "loaded"
        return True

    def kind(self, model: str, raw: Optional[dict] = None) -> Optional[str]:
        with self._kind_lock:
            hit = self._kinds.get(model)
        if hit and hit[1] > time.monotonic():
            return None if hit[0] == _UNANSWERED else hit[0]
        raw = raw if raw is not None else self.raw_entry(model)
        if raw and not self._loaded(raw):
            return None  # asking would load it; unknown is the honest answer
        try:
            r = httpx.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": model, "input": ["."]},
                headers=self.headers(),
                timeout=_KIND_PROBE_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - busy or gone; asked again after a pause
            r = None
        if r is not None and r.status_code == 200:
            found = KIND_EMBEDDING
        elif r is not None and (r.status_code == 501 or "not_supported" in (r.text or "")):
            vision = (self._props().get("modalities") or {}).get("vision")
            found = KIND_VISION if vision else KIND_CHAT
        else:
            with self._kind_lock:
                self._kinds[model] = (_UNANSWERED, time.monotonic() + _FAILED_KIND_TTL_SECONDS)
            return None
        with self._kind_lock:
            self._kinds[model] = (found, time.monotonic() + _KIND_TTL_SECONDS)
        return found

    def list_models(self) -> list[ModelEntry]:
        return [
            ModelEntry(
                name=raw["id"],
                kind=self._known(raw["id"]),
                capabilities=self._words(self._known(raw["id"])),
                size_bytes=_positive((raw.get("meta") or {}).get("size")),
                loaded=self._loaded(raw),
            )
            for raw in self._models_payload()
        ]

    def describe(self, entries: list[ModelEntry]) -> dict[str, ModelEntry]:
        out: dict[str, ModelEntry] = {}
        for entry in entries:
            kind = self.kind(entry.name)
            out[entry.name] = ModelEntry(
                name=entry.name,
                kind=kind,
                capabilities=self._words(kind),
                size_bytes=entry.size_bytes,
                loaded=entry.loaded,
            )
        return out

    def known_kind(self, model: str) -> Optional[ModelEntry]:
        kind = self._known(model)
        return ModelEntry(name=model, kind=kind, capabilities=self._words(kind)) if kind else None

    def _known(self, model: str) -> Optional[str]:
        with self._kind_lock:
            hit = self._kinds.get(model)
        if not hit or hit[1] <= time.monotonic() or hit[0] == _UNANSWERED:
            return None
        return hit[0]

    @staticmethod
    def _words(kind: Optional[str]) -> Optional[tuple[str, ...]]:
        """What the server showed, in the words it answers with — quoted, not guessed."""
        if kind == KIND_EMBEDDING:
            return ("embeddings",)
        if kind in (KIND_CHAT, KIND_VISION):
            return ("completion", "vision") if kind == KIND_VISION else ("completion",)
        return None

    def forget(self, model: Optional[str] = None) -> None:
        with self._kind_lock:
            if model:
                self._kinds.pop(model, None)
            else:
                self._kinds.clear()

    # ── model_info ───────────────────────────────────────────────────────────
    def model_info(self, model: str) -> Optional[ModelInfo]:
        raw = self.raw_entry(model)
        if not raw:
            return None
        meta = raw.get("meta") or {}
        props = self._props() if self._loaded(raw) else {}
        # The per-request window the server runs: `/props` says it per slot, and the
        # model entry repeats it. The trained window is the model's, not the server's.
        running = _positive((props.get("default_generation_settings") or {}).get("n_ctx"))
        window = running or _positive(meta.get("n_ctx"))
        trained = _positive(meta.get("n_ctx_train"))
        warnings: tuple[str, ...] = ()
        if window and trained and window < trained:
            warnings = (
                f"This server was started with a {window:,}-token window; the model can read "
                f"{trained:,}. Restart it with a larger `-c` to give agents more room.",
            )
        caps = props.get("chat_template_caps") or {}
        kind = self.kind(model, raw)
        n_params = _positive(meta.get("n_params"))
        return ModelInfo(
            name=model,
            context_window=window,
            context_source=CONTEXT_REPORTED if window else None,
            parameters_total=n_params,
            parameter_label=_label(n_params),
            quantization=meta.get("ftype") or None,
            kind=kind,
            capabilities=self._words(kind),
            structured_output=self.structured_mode(model) if kind != KIND_EMBEDDING else STRUCTURED_NONE,
            thinking=(
                "toggle"
                if caps.get("supports_reasoning_effort") or caps.get("supports_preserve_reasoning")
                else None
            ),
            runtime_version=str(props.get("build_info")) if props.get("build_info") else None,
            listen_address=self.base_url,
            warnings=warnings,
        )

    # `response_format: json_schema` is compiled to a grammar by the server itself,
    # so the generic ladder's first rung is the right one here too.
