"""OpenAI GPT provider (cloud), via the official `openai` SDK."""
from __future__ import annotations
from typing import Optional

import time

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError, cloud_error
from app.router.providers.cloud_key import CloudKey
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage, normalise_finish

log = get_logger(__name__)


class OpenAIProvider(CloudKey, LLMProvider):
    name = "openai"
    is_local = False
    #: Published rather than probed — there is no capability endpoint to ask.
    context_tokens = settings.openai_context_tokens

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._init_key(api_key or settings.openai_api_key)
        self._client = None
        #: Models that refused a temperature (#101): reasoning models take only their
        #: default. Learned from the refusal, never guessed from a name.
        self._fixed_temperature: set[str] = set()

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            # Tenacity (`ModelRouter._generate`) is the one retry layer: the SDK's own
            # would retry a no-credit 429 as if it were a rate limit.
            self._client = OpenAI(api_key=self.secret(), max_retries=0)
        return self._client

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
        if options.temperature is not None and model not in self._fixed_temperature:
            kwargs["temperature"] = options.temperature
        if options.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            try:
                resp = self._get_client().chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                if "temperature" not in kwargs or not _refuses_temperature(e):
                    raise
                # A model that takes only its default temperature: once more without
                # one, and never send it one again.
                log.info("%s takes no temperature; asking again without one.", model)
                self._fixed_temperature.add(model)
                kwargs.pop("temperature")
                resp = self._get_client().chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise cloud_error(self.name, "OpenAI", e) from e

        latency = int((time.perf_counter() - started) * 1000)
        text = resp.choices[0].message.content or ""
        u = resp.usage
        usage = Usage(
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
        )
        return LLMResponse(
            text=text,
            provider=self.name,
            model=model,
            usage=usage,
            latency_ms=latency,
            finish_reason=normalise_finish(resp.choices[0].finish_reason),
        )


def _refuses_temperature(error: Exception) -> bool:
    """OpenAI's 400 for a temperature the model doesn't take ("Unsupported value:
    'temperature' does not support 0.2 with this model")."""
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    text = str(error).lower()
    return status == 400 and "temperature" in text and ("unsupported" in text or "not support" in text)
