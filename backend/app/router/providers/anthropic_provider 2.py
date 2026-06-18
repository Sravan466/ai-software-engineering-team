"""Anthropic Claude provider (cloud).

Uses the official `anthropic` SDK and defaults to `claude-opus-4-8`. Per current API
guidance for the Opus 4.x family: adaptive thinking is enabled for these reasoning-heavy
agent tasks, and sampling params (temperature/top_p/top_k) are NOT sent (they 400 on 4.7+).
"""
from __future__ import annotations
from typing import Optional

import time

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)

# Models that use the adaptive-thinking / no-sampling-params request surface.
_ADAPTIVE_THINKING_PREFIXES = ("claude-opus-4-8", "claude-opus-4-7", "claude-fable-5")


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    is_local = False

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # imported lazily so the package is optional at runtime

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        if not self.available():
            raise ProviderError("ANTHROPIC_API_KEY is not set.")

        # Anthropic takes `system` separately from the user/assistant turns.
        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        convo = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        if not convo:  # the API requires at least one user turn
            convo = [{"role": "user", "content": system_text or "Proceed."}]

        kwargs: dict = {
            "model": model,
            "max_tokens": options.max_tokens,
            "messages": convo,
        }
        if system_text:
            kwargs["system"] = system_text

        if model.startswith(_ADAPTIVE_THINKING_PREFIXES):
            # Adaptive thinking only; do not send temperature on this model family.
            kwargs["thinking"] = {"type": "adaptive"}
        elif options.temperature is not None:
            kwargs["temperature"] = options.temperature

        started = time.perf_counter()
        try:
            client = self._get_client()
            resp = client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - normalise SDK/network errors
            raise ProviderError(f"Anthropic call failed: {e}") from e

        latency = int((time.perf_counter() - started) * 1000)
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        usage = Usage(
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
        )
        return LLMResponse(
            text=text, provider=self.name, model=model, usage=usage, latency_ms=latency
        )
