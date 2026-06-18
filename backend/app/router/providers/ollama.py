"""Local model provider via Ollama's HTTP API (the default, zero-cost backend)."""
from __future__ import annotations
from typing import Optional

import time

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)


class OllamaProvider(LLMProvider):
    name = "ollama"
    is_local = True

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:  # noqa: BLE001 - server simply not running
            return False

    def list_models(self) -> list:
        """Names of models currently pulled on the Ollama host (empty if unreachable)."""
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
        except Exception:  # noqa: BLE001 - server not running / unexpected payload
            return []

    def has_model(self, model: str) -> bool:
        """True if `model` (exact tag, or same base when no tag given) is pulled."""
        models = self.list_models()
        if model in models:
            return True
        base = model.split(":", 1)[0]
        return any(m.split(":", 1)[0] == base for m in models)

    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        payload: dict = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {},
        }
        if options.temperature is not None:
            payload["options"]["temperature"] = options.temperature
        if options.json_mode:
            payload["format"] = "json"

        started = time.perf_counter()
        try:
            # Local generation can be slow on CPU; give it room.
            r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=600.0)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            raise ProviderError(
                f"Ollama returned {e.response.status_code}: {e.response.text[:200]}. "
                f"Is the model '{model}' pulled? Try `ollama pull {model}`."
            ) from e
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Ollama call failed: {e}") from e

        latency = int((time.perf_counter() - started) * 1000)
        text = (data.get("message") or {}).get("content", "")
        usage = Usage(
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        )
        return LLMResponse(
            text=text, provider=self.name, model=model, usage=usage, latency_ms=latency
        )
