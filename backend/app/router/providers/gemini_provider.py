"""Google Gemini provider (cloud), via `google-generativeai`."""
from __future__ import annotations
from typing import Optional

import time

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)


class GeminiProvider(LLMProvider):
    name = "gemini"
    is_local = False

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or settings.gemini_api_key
        self._configured = False

    def _configure(self):
        if not self._configured:
            import google.generativeai as genai

            genai.configure(api_key=self.api_key)
            self._configured = True

    def set_api_key(self, key: Optional[str]) -> None:
        """Update the key at runtime (Settings UI) and force re-configuration."""
        self.api_key = key or None
        self._configured = False

    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        if not self.available():
            raise ProviderError("GEMINI_API_KEY is not set.")

        import google.generativeai as genai

        self._configure()

        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        # Gemini uses roles "user" / "model".
        contents = []
        for m in messages:
            if m.role == "system":
                continue
            role = "model" if m.role == "assistant" else "user"
            contents.append({"role": role, "parts": [m.content]})
        if not contents:
            contents = [{"role": "user", "parts": [system_text or "Proceed."]}]

        gen_config: dict = {"max_output_tokens": options.max_tokens}
        if options.temperature is not None:
            gen_config["temperature"] = options.temperature
        if options.json_mode:
            gen_config["response_mime_type"] = "application/json"

        started = time.perf_counter()
        try:
            gmodel = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_text or None,
                generation_config=gen_config,
            )
            resp = gmodel.generate_content(contents)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Gemini call failed: {e}") from e

        latency = int((time.perf_counter() - started) * 1000)
        text = getattr(resp, "text", "") or ""
        meta = getattr(resp, "usage_metadata", None)
        usage = Usage(
            prompt_tokens=getattr(meta, "prompt_token_count", 0) if meta else 0,
            completion_tokens=getattr(meta, "candidates_token_count", 0) if meta else 0,
            total_tokens=getattr(meta, "total_token_count", 0) if meta else 0,
        )
        return LLMResponse(
            text=text, provider=self.name, model=model, usage=usage, latency_ms=latency
        )
