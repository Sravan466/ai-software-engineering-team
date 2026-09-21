"""Adapters for runtimes that speak the OpenAI dialect with a few extras of their own.

Each is the generic adapter plus the one answer that identifies it and whatever it
reports that the plain dialect does not — usually the context window. Where a field
below is marked unverified, it is taken from the runtime's documentation and has not
yet been seen from a running server; it is read defensively, so a runtime that
answers differently degrades to the generic adapter's "unknown", never to an error.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from app.router.runtimes.base import FINGERPRINT_TIMEOUT, LIST_TIMEOUT, get_json, http_error
from app.router.runtimes.openai_compat import OpenAICompatAdapter, _positive, api_root
from app.router.runtimes.types import (
    CONTEXT_REPORTED,
    KIND_CHAT,
    KIND_EMBEDDING,
    KIND_VISION,
    STRUCTURED_NONE,
    Hello,
    ModelEntry,
    ModelInfo,
)


def _owned_by(base_url: str, owner: str, api_key: Optional[str], timeout: float) -> bool:
    data = get_json(base_url, "/v1/models", api_key, timeout=timeout)
    entries = data.get("data") if isinstance(data, dict) else None
    return (
        isinstance(entries, list)
        and bool(entries)
        and all(isinstance(e, dict) and e.get("owned_by") == owner for e in entries)
    )


# ── LM Studio ────────────────────────────────────────────────────────────────
#: LM Studio's own REST API types each model: `llm`, `vlm` or `embeddings`.
_LMSTUDIO_KINDS = {"llm": KIND_CHAT, "vlm": KIND_VISION, "embeddings": KIND_EMBEDDING}


class LMStudioAdapter(OpenAICompatAdapter):
    """LM Studio: recognised by its own REST listing, `/api/v0/models`.

    That listing types each model and reports the longest window it can be loaded
    with (`max_context_length`) and whether it is loaded now (`state`). Chat and
    embeddings go through the OpenAI dialect like every other server.
    """

    runtime = "lmstudio"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        data = get_json(base, "/api/v0/models", api_key, timeout=timeout)
        if not (isinstance(data, dict) and isinstance(data.get("data"), list)):
            return None
        entries = data["data"]
        if entries and not all(isinstance(e, dict) and "type" in e and "state" in e for e in entries):
            return None
        return Hello(runtime=cls.runtime, base_url=base)

    def _rich(self) -> list[dict]:
        try:
            r = self._get("/api/v0/models", timeout=LIST_TIMEOUT)
            r.raise_for_status()
            data = r.json().get("data", []) or []
        except Exception as e:  # noqa: BLE001
            raise http_error(e, f"LM Studio at {self.base_url}") from e
        entries = [d for d in data if isinstance(d, dict) and d.get("id")]
        with self._lock:
            self._raw = {d["id"]: d for d in entries}
        return entries

    def list_models(self) -> list[ModelEntry]:
        return [
            ModelEntry(
                name=d["id"],
                kind=_LMSTUDIO_KINDS.get(str(d.get("type"))),
                capabilities=(str(d["type"]),) if d.get("type") else None,
                loaded=d.get("state") == "loaded" if d.get("state") else None,
            )
            for d in self._rich()
        ]

    def _loaded_window(self, model: str) -> Optional[int]:
        """The window a loaded instance was loaded with, from LM Studio's v1 listing.

        Only v1 (LM Studio 0.4 and later) reports it — `loaded_instances[].config.
        context_length` — and it is read defensively (unverified against a running
        server): any other shape reads as "not reported".
        """
        data = get_json(self.base_url, "/api/v1/models", self.api_key, timeout=FINGERPRINT_TIMEOUT)
        entries = data.get("models") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or model not in (entry.get("key"), entry.get("id")):
                continue
            for inst in entry.get("loaded_instances") or []:
                window = _positive(((inst or {}).get("config") or {}).get("context_length"))
                if window:
                    return window
        return None

    def model_info(self, model: str) -> Optional[ModelInfo]:
        raw = self.raw_entry(model)
        if not raw:
            try:
                self._rich()
            except Exception:  # noqa: BLE001
                return None
            raw = self.raw_entry(model)
        kind = _LMSTUDIO_KINDS.get(str(raw.get("type")))
        loaded = self._loaded_window(model)
        longest = _positive(raw.get("max_context_length"))
        warnings: tuple[str, ...] = ()
        if loaded:
            window, source = loaded, CONTEXT_REPORTED
        else:
            # The window it will actually run at is the one it is loaded with, which
            # this LM Studio does not report — and it is often far below the longest
            # the model supports. Budgeting for the longest is how a prompt gets cut;
            # the configured fallback is the stated assumption instead.
            window, source = None, None
            warnings = (
                "LM Studio doesn't report the window this model is loaded with"
                + (f" (it supports up to {longest:,} tokens)" if longest else "")
                + ", so the configured fallback is in force. Load it in LM Studio with at "
                "least that window, or long prompts are cut short.",
            )
        return ModelInfo(
            name=model,
            context_window=window,
            context_source=source,
            quantization=raw.get("quantization") or None,
            kind=kind,
            capabilities=(str(raw["type"]),) if raw.get("type") else None,
            structured_output=self.structured_mode(model) if kind != KIND_EMBEDDING else STRUCTURED_NONE,
            architecture=raw.get("arch") or None,
            listen_address=self.base_url,
            warnings=warnings,
        )


# ── vLLM ─────────────────────────────────────────────────────────────────────
class VLLMAdapter(OpenAICompatAdapter):
    """vLLM: `owned_by: "vllm"`, with `max_model_len` on every model entry."""

    runtime = "vllm"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        if not _owned_by(base, "vllm", api_key, timeout):
            return None
        version = get_json(base, "/version", api_key, timeout=timeout)
        return Hello(
            runtime=cls.runtime,
            base_url=base,
            version=str(version.get("version")) if isinstance(version, dict) else None,
        )


# ── SGLang ───────────────────────────────────────────────────────────────────
class SGLangAdapter(OpenAICompatAdapter):
    """SGLang: `owned_by: "sglang"` (unverified); window from `/get_model_info`."""

    runtime = "sglang"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        return Hello(runtime=cls.runtime, base_url=base) if _owned_by(base, "sglang", api_key, timeout) else None

    def model_info(self, model: str) -> Optional[ModelInfo]:
        info = super().model_info(model)
        extra = get_json(self.base_url, "/get_model_info", self.api_key, timeout=FINGERPRINT_TIMEOUT)
        window = _positive(extra.get("max_context_length")) if isinstance(extra, dict) else None
        if info is None or not window or info.context_window:
            return info
        return replace(info, context_window=window, context_source=CONTEXT_REPORTED)


# ── KoboldCpp ────────────────────────────────────────────────────────────────
class KoboldCppAdapter(OpenAICompatAdapter):
    """KoboldCpp: `/api/extra/version` names it; `/api/extra/true_max_context_length`
    is the window it was started with."""

    runtime = "koboldcpp"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        data = get_json(base, "/api/extra/version", api_key, timeout=timeout)
        if not (isinstance(data, dict) and str(data.get("result", "")).lower() == "koboldcpp"):
            return None
        return Hello(runtime=cls.runtime, base_url=base, version=str(data.get("version") or "") or None)

    def model_info(self, model: str) -> Optional[ModelInfo]:
        info = super().model_info(model)
        extra = get_json(
            self.base_url, "/api/extra/true_max_context_length", self.api_key, timeout=FINGERPRINT_TIMEOUT
        )
        window = _positive(extra.get("value")) if isinstance(extra, dict) else None
        if info is None or not window:
            return info
        return replace(info, context_window=window, context_source=CONTEXT_REPORTED)


# ── LocalAI ──────────────────────────────────────────────────────────────────
class LocalAIAdapter(OpenAICompatAdapter):
    """LocalAI: `/system` lists its backends (unverified against a running server).

    It shares llama.cpp's default port, so only this answer tells them apart — never
    the port, and never `/v1/models`, which LocalAI serves in the plain dialect.
    """

    runtime = "localai"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        system = get_json(base, "/system", api_key, timeout=timeout)
        if not (isinstance(system, dict) and "backends" in system):
            return None
        version = get_json(base, "/version", api_key, timeout=timeout)
        return Hello(
            runtime=cls.runtime,
            base_url=base,
            version=str(version.get("version")) if isinstance(version, dict) else None,
        )

