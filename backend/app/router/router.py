"""Hybrid LLM router.

Resolves which (provider, model) to use for a request based on the selection mode, then
runs through a fallback chain so one failing backend never stalls the pipeline. The local
Ollama model is always the chain's safety net.

Selection modes
---------------
- local_only : only the local Ollama model is ever used.
- manual     : use the caller's preferred `provider:model`, fall back to the chain.
- auto       : pick by a rough complexity hint, cost, and which providers are configured,
               preferring local for cheap/simple work.
"""
from __future__ import annotations
from typing import Optional

from app.core.config import settings
from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.router.providers.anthropic_provider import AnthropicProvider
from app.router.providers.gemini_provider import GeminiProvider
from app.router.providers.ollama import OllamaProvider
from app.router.providers.openai_provider import OpenAIProvider
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)


class ModelRouter:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {
            "ollama": OllamaProvider(),
            "anthropic": AnthropicProvider(),
            "openai": OpenAIProvider(),
            "gemini": GeminiProvider(),
        }
        self._default_model: dict[str, str] = {
            "ollama": settings.ollama_default_model,
            "anthropic": settings.anthropic_default_model,
            "openai": settings.openai_default_model,
            "gemini": settings.gemini_default_model,
        }
        # Apply any keys/models saved at runtime via the Settings UI (overrides .env).
        self._load_persisted()

    CLOUD_PROVIDERS = ("anthropic", "openai", "gemini")

    # ── public API ────────────────────────────────────────────────────────────
    def provider(self, name: str) -> Optional[LLMProvider]:
        return self._providers.get(name)

    # ── runtime provider configuration (Settings UI) ───────────────────────────
    def _apply(
        self, provider: str, api_key: Optional[str], default_model: Optional[str]
    ) -> None:
        """Update one provider's key/model in memory + the shared settings object."""
        prov = self._providers.get(provider)
        if prov is None:
            return
        if api_key is not None and hasattr(prov, "set_api_key"):
            prov.set_api_key(api_key)
            # Reflect into settings so Auto-mode's `configured_cloud_providers()` sees it.
            setattr(settings, f"{provider}_api_key", api_key or None)
        if default_model:
            self._default_model[provider] = default_model
            setattr(settings, f"{provider}_default_model", default_model)

    def _load_persisted(self) -> None:
        from app.core import secrets_store

        for pname, entry in secrets_store.get_all().items():
            if pname in self._providers:
                self._apply(pname, entry.get("api_key"), entry.get("default_model"))

    def set_provider_key(
        self,
        provider: str,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
    ) -> None:
        """Set/clear a cloud provider's API key at runtime and persist it.

        api_key: None = leave unchanged, "" = clear, "sk-..." = set.
        """
        if provider not in self.CLOUD_PROVIDERS:
            raise ValueError(
                f"Unknown cloud provider '{provider}'. Expected one of {self.CLOUD_PROVIDERS}."
            )
        self._apply(provider, api_key, default_model)
        from app.core import secrets_store

        secrets_store.set_provider(provider, api_key, default_model)

    def provider_settings(self) -> dict:
        """Per-cloud-provider config for the Settings UI (never exposes the raw key)."""
        out: dict = {}
        for name in self.CLOUD_PROVIDERS:
            prov = self._providers[name]
            key = getattr(prov, "api_key", None)
            out[name] = {
                "configured": bool(key),
                "available": prov.available(),
                "key_hint": ("…" + key[-4:]) if key and len(key) >= 4 else ("set" if key else None),
                "default_model": self._default_model.get(name),
            }
        return out

    def local_status(self) -> dict:
        """Ollama reachability + which models are pulled (for the local-model panel)."""
        prov = self._providers["ollama"]
        default = self._default_model["ollama"]
        models = prov.list_models() if hasattr(prov, "list_models") else []
        return {
            "base_url": getattr(prov, "base_url", settings.ollama_base_url),
            "reachable": prov.available(),
            "models": models,
            "default_model": default,
            "has_default": (
                prov.has_model(default) if hasattr(prov, "has_model") else (default in models)
            ),
        }

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        mode: RoutingMode = RoutingMode.LOCAL_ONLY,
        preferred_model: Optional[str] = None,
        options: Optional[GenerationOptions] = None,
        complexity: str = "medium",  # "low" | "medium" | "high"
    ) -> LLMResponse:
        options = options or GenerationOptions()
        chain = self._resolve_chain(mode, preferred_model, complexity)
        if not chain:
            raise ProviderError("No usable model could be resolved for this request.")

        attempts: list[dict] = []
        for idx, (pname, model) in enumerate(chain):
            prov = self._providers.get(pname)
            if prov is None or not prov.available():
                attempts.append(
                    {"provider": pname, "model": model, "error": "unavailable"}
                )
                continue
            try:
                resp = prov.generate(messages, model, options)
                resp.fallback_used = idx > 0
                resp.attempts = attempts
                return resp
            except ProviderError as e:
                log.warning("Provider %s/%s failed: %s", pname, model, e)
                attempts.append({"provider": pname, "model": model, "error": str(e)})

        raise ProviderError(
            "All providers in the routing chain failed. Attempts: "
            + "; ".join(f"{a['provider']}:{a['model']} -> {a['error']}" for a in attempts)
        )

    def status(self) -> dict:
        """Snapshot of provider availability + the configured defaults (for the UI)."""
        return {
            "default_mode": settings.default_routing_mode,
            "providers": {
                name: {
                    "available": prov.available(),
                    "is_local": prov.is_local,
                    "default_model": self._default_model.get(name),
                }
                for name, prov in self._providers.items()
            },
            "fallback_chain": [f"{p}:{m}" for p, m in settings.fallback_pairs],
        }

    # ── chain resolution ──────────────────────────────────────────────────────
    def _resolve_chain(
        self, mode: RoutingMode, preferred_model: Optional[str], complexity: str
    ) -> list[tuple[str, str]]:
        if mode == RoutingMode.LOCAL_ONLY:
            return [("ollama", self._default_model["ollama"])]

        chain: list[tuple[str, str]] = []

        if mode == RoutingMode.MANUAL and preferred_model:
            chain.append(self._parse_pair(preferred_model))
        elif mode == RoutingMode.AUTO:
            chain.append(self._auto_pick(complexity))

        # Append the configured fallback chain, de-duplicating while preserving order.
        for pair in settings.fallback_pairs:
            if pair not in chain:
                chain.append(pair)

        # Guarantee the local model is the final safety net.
        local = ("ollama", self._default_model["ollama"])
        if local not in chain:
            chain.append(local)
        return chain

    def _auto_pick(self, complexity: str) -> tuple[str, str]:
        """Heuristic primary choice for Auto mode.

        - High complexity + a cloud key available -> strongest configured cloud model.
        - Otherwise prefer the free local model when Ollama is reachable.
        """
        cloud = settings.configured_cloud_providers()
        local_up = self._providers["ollama"].available()

        if complexity == "high" and cloud:
            # Preference order by reasoning strength.
            for pname in ("anthropic", "openai", "gemini"):
                if pname in cloud:
                    return (pname, self._default_model[pname])

        if local_up:
            return ("ollama", self._default_model["ollama"])

        if cloud:
            pname = cloud[0]
            return (pname, self._default_model[pname])

        # Nothing configured and Ollama down — still return local so the error is clear.
        return ("ollama", self._default_model["ollama"])

    @staticmethod
    def _parse_pair(spec: str) -> tuple[str, str]:
        """'anthropic:claude-opus-4-8' -> ('anthropic', 'claude-opus-4-8')."""
        if ":" not in spec:
            # Bare model name — assume Ollama local.
            return ("ollama", spec)
        provider, model = spec.split(":", 1)
        return (provider.strip(), model.strip())


# Shared singleton used across the app.
router = ModelRouter()
