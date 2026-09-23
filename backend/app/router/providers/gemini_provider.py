"""Google Gemini provider (cloud), via `google-generativeai`."""
from __future__ import annotations
from typing import Optional

import threading
import time

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError, status_is_retryable
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)


#: Held while the SDK's process-wide key is set and a client is taken from it.
_CONFIGURE_LOCK = threading.Lock()


class GeminiProvider(LLMProvider):
    name = "gemini"
    is_local = False
    #: Published rather than probed — there is no capability endpoint to ask.
    context_tokens = settings.gemini_context_tokens

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or settings.gemini_api_key

    def set_api_key(self, key: Optional[str]) -> None:
        """Update the key at runtime (Settings UI)."""
        self.api_key = key or None

    def _model(self, **kwargs):
        """A `GenerativeModel` bound to this provider's key, and nobody else's.

        The SDK keeps its key in process-wide state (`genai.configure`), and every
        account has its own provider with its own key. Configuring and taking the
        client happen together under one lock, and the client is pinned to the
        model, so a call never runs on the key another account configured a moment
        later. Returns (model, pinned): unpinned, the caller holds the lock for the
        whole call instead.
        """
        import google.generativeai as genai

        genai.configure(api_key=self.api_key)
        gmodel = genai.GenerativeModel(**kwargs)
        try:
            from google.generativeai import client as genai_client

            gmodel._client = genai_client.get_default_generative_client()
            return gmodel, True
        except Exception:  # noqa: BLE001 - an SDK that moved this: fall back to the lock
            return gmodel, False

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
            raise ProviderError("GEMINI_API_KEY is not set.", retryable=False)

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

        gen_config: dict = {
            "max_output_tokens": options.resolve_max_tokens(
                self.profile(model).max_output_tokens
            )
        }
        if options.temperature is not None:
            gen_config["temperature"] = options.temperature
        if options.json_mode:
            gen_config["response_mime_type"] = "application/json"

        started = time.perf_counter()
        try:
            with _CONFIGURE_LOCK:
                gmodel, pinned = self._model(
                    model_name=model,
                    system_instruction=system_text or None,
                    generation_config=gen_config,
                )
                if not pinned:
                    resp = gmodel.generate_content(contents)
            if pinned:
                resp = gmodel.generate_content(contents)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                f"Gemini call failed: {e}", retryable=status_is_retryable(e)
            ) from e

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
