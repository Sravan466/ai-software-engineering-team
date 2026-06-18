"""Provider abstraction. Every LLM backend implements `LLMProvider.generate`."""
from __future__ import annotations

import abc

from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse


class ProviderError(RuntimeError):
    """Raised when a provider call fails (network, quota, missing key, bad model)."""


class LLMProvider(abc.ABC):
    """Common interface for cloud and local model backends."""

    #: short stable identifier, e.g. "ollama", "anthropic"
    name: str = "base"
    #: True for local backends (Ollama) — used by Local-Only / Auto routing.
    is_local: bool = False

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
