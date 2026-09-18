"""OpenAI GPT provider (cloud), via the official `openai` SDK."""
from __future__ import annotations
from typing import Optional

import time

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"
    is_local = False
    #: Published rather than probed — there is no capability endpoint to ask.
    context_tokens = settings.openai_context_tokens

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or settings.openai_api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def set_api_key(self, key: Optional[str]) -> None:
        """Update the key at runtime (Settings UI) and drop the cached client."""
        self.api_key = key or None
        self._client = None

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        if not self.available():
            # A key that is absent now will be absent on the retry too; asking
            # three times only delays the sentence that says to add one.
            raise ProviderError("OPENAI_API_KEY is not set.", retryable=False)

        kwargs: dict = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": options.resolve_max_tokens(self.profile(model).max_output_tokens),
        }
        if options.temperature is not None:
            kwargs["temperature"] = options.temperature
        if options.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            resp = self._get_client().chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"OpenAI call failed: {e}") from e

        latency = int((time.perf_counter() - started) * 1000)
        text = resp.choices[0].message.content or ""
        u = resp.usage
        usage = Usage(
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
        )
        return LLMResponse(
            text=text, provider=self.name, model=model, usage=usage, latency_ms=latency
        )
