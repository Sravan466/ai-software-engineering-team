"""Provider abstraction. Every LLM backend implements `LLMProvider.generate`."""
from __future__ import annotations

import abc
from typing import Optional

from app.router.model_profile import ModelProfile, fallback_profile
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse


class ProviderError(RuntimeError):
    """Raised when a provider call fails (network, quota, missing key, bad model)."""


class LLMProvider(abc.ABC):
    """Common interface for cloud and local model backends."""

    #: short stable identifier, e.g. "ollama", "anthropic"
    name: str = "base"
    #: True for local backends (Ollama) — used by Local-Only / Auto routing.
    is_local: bool = False
    #: Context window this provider publishes for its models, when it publishes one.
    #: Local backends override `profile()` and probe the model instead.
    context_tokens: Optional[int] = None

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this provider is usable right now (key present / server reachable)."""

    @abc.abstractmethod
    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        """Run one completion and return a normalised response. Raise ProviderError on failure."""

    def profile(self, model: str) -> ModelProfile:
        """How much room `model` has, and what it can be asked to do.

        Callers size their prompts from this, so it must always answer — a provider
        that cannot say returns the configured fallback window rather than nothing,
        and the run proceeds on a stated assumption instead of an unstated one.
        """
        return fallback_profile(
            self.name,
            model,
            context_limit=self.context_tokens,
            source="configured" if self.context_tokens else "fallback",
        )
