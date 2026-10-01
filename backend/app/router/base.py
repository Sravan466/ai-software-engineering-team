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

    `unreachable` says nothing answered at all — a refused or timed-out connection —
    which is what lets a source be marked down at once, so the next link in the chain
    does not wait on it too.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        unreachable: bool = False,
        status: Optional[int] = None,
        kind: Optional[str] = None,
        provider: Optional[str] = None,
        technical: Optional[str] = None,
    ) -> None:
        # Scrubbed here, once: this text goes on to the log, the attempts list, the
        # project's `last_error` and the page, and a provider may have quoted the key.
        from app.core.scrub import scrub

        super().__init__(scrub(message))
        self.retryable = retryable
        self.unreachable = unreachable
        #: The HTTP status the provider answered with, when there was one.
        self.status = status
        #: What a cloud provider's refusal meant (`app.core.keyerrors`): `no_credit`,
        #: `expired`, … — None for anything that isn't a provider's answer.
        self.kind = kind
        #: Which provider said so — the cloud one, for its links and its words.
        self.provider = provider
        #: The provider's own text, scrubbed: for the server log and "Technical
        #: details", never the headline.
        self.technical = scrub(technical) if technical else None


def cloud_error(provider: str, label: str, error: Exception) -> ProviderError:
    """An SDK's exception, as the sentence a person can act on.

    Classified once (`app.core.keyerrors`): a 429 that means "no credit" is not
    retried, a rate limit is. The SDK's own text — raw JSON that may quote the key —
    goes to the server log, scrubbed, and never becomes the message.
    """
    from app.core import keyerrors
    from app.core.logging import get_logger
    from app.core.scrub import scrub

    failure = keyerrors.from_exception(provider, error)
    technical = f"{label} call failed: {error}"
    if failure is None:
        # No HTTP answer: a dropped connection. Its text is a socket error, not JSON.
        return ProviderError(technical, retryable=status_is_retryable(error), status=None, provider=provider)
    get_logger(__name__).warning("%s", scrub(technical))
    advice = keyerrors.advice(failure.kind, provider, status=failure.status, code=failure.code)
    return ProviderError(
        advice.sentence(),
        retryable=failure.retryable,
        status=failure.status,
        kind=failure.kind,
        provider=provider,
        technical=technical,
    )


class RequestCancelled(ProviderError):
    """The call was stopped on purpose — Stop was pressed. Never retried, never
    handed to the next link in the chain."""

    def __init__(self, message: str = "Stopped.") -> None:
        super().__init__(message, retryable=False)


class ComputerDisconnected(ProviderError):
    """The user's own computer, which runs this model, isn't connected — or is paused.

    Not a failure of the build: nothing it did was wrong, and it can carry on the
    moment the computer is back. The runner pauses on this instead of failing, and
    the build resumes by itself when that computer reconnects.
    """

    def __init__(
        self, message: str, *, device_id: str, device_name: str = "", paused_there: bool = False
    ) -> None:
        super().__init__(message, retryable=False, unreachable=True)
        self.device_id = device_id
        self.device_name = device_name
        #: Paused on the computer itself (`aiteam-connect pause`), not disconnected.
        self.paused_there = paused_there


#: The providers that are services rather than model sources. Their names are never
#: a source id, so `anthropic:…` cannot be read two ways.
CLOUD_PROVIDERS = ("anthropic", "openai", "gemini")


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
    status = status_of(error)
    return True if status is None else status in RETRYABLE_STATUS


def status_of(error: Exception) -> Optional[int]:
    """The HTTP status an SDK exception carries, or None. (Not `code` — see above.)"""
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


class LLMProvider(abc.ABC):
    """Common interface for cloud and local model backends."""

    #: short stable identifier: a cloud provider's name, or a model source's id
    name: str = "base"
    #: True for model sources that run models on hardware the user controls — used
    #: by Local-Only / Auto routing. Per model, `is_local_model` has the last word.
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

    def is_local_model(self, model: str) -> bool:
        """Whether `model` runs on the user's own hardware when this provider serves it.

        A local runtime can list a model it sends elsewhere to run; its source says
        so per model. Everything else answers for all its models at once.
        """
        return self.is_local

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
