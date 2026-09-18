"""Provider abstraction. Every LLM backend implements `LLMProvider.generate`."""
from __future__ import annotations

import abc
from typing import Optional

from app.router.model_profile import ModelProfile, fallback_profile
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse


class ProviderError(RuntimeError):
    """Raised when a provider call fails (network, quota, missing key, bad model).

    `retryable` separates a hiccup from a verdict. A dropped socket, or a 503 from a
    runtime still loading weights, succeeds on the next attempt; a missing API key, a
    400, or a model that was never pulled fails identically however many times it is
    asked, and retrying those only delays the one message that says what to do about
    it. Anything that does not say otherwise is treated as transient, because that is
    the failure worth surviving.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


#: Status codes worth asking again for. Everything else in the 4xx range is a
#: statement about the request — a bad key, a malformed body, a model that does not
#: exist — and says the same thing on the third attempt as on the first.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def status_is_retryable(error: Exception) -> bool:
    """Whether a provider SDK's exception carries an HTTP status worth retrying.

    Read from `status_code`, or from the response it wraps — and deliberately *not*
    from `code`. Plenty of exceptions carry a `code` that is not an HTTP status at
    all: `OSError` puts an errno there (111 is a refused connection), and Google's
    client puts a gRPC status there (14 is UNAVAILABLE). Both of those are the
    transient failures retrying exists for, and reading them as HTTP would find them
    absent from this set and report them as permanent — turning the fix off for
    exactly the case it was written for.

    An exception with no HTTP status is transient by default, which is the dropped
    socket. The cost of being wrong that way is a few seconds; the cost of being
    wrong the other way is a run that fails on a hiccup.
    """
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
    try:
        return int(status) in RETRYABLE_STATUS
    except (TypeError, ValueError):
        return True


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
