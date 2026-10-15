"""Provider abstraction. Every LLM backend implements `LLMProvider.generate`."""
from __future__ import annotations

import abc
import re
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
    if failure.kind not in keyerrors.BLOCKING and failure.kind not in keyerrors.RETRYABLE:
        # Not about the key or the account — a prompt too long, a parameter the model
        # refuses. Key advice would hide the cause, so the provider's own reason stays
        # (scrubbed, trimmed), and no kind is claimed.
        said = keyerrors.provider_message(error) or technical
        return ProviderError(
            f"{label} refused the request (HTTP {failure.status}): {said[:400]}",
            retryable=False,
            status=failure.status,
            provider=provider,
            technical=technical,
        )
    advice = keyerrors.advice(failure.kind, provider, status=failure.status, code=failure.code)
    # Retryable kinds only surface once the retries ran out — say that, not "retried
    # by themselves".
    message = (
        f"{advice.title}, and the call still failed after retrying. Try again in a few minutes."
        if failure.retryable else advice.sentence()
    )
    return ProviderError(
        message,
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


class PromptTooLong(ProviderError):
    """The prompt doesn't fit the window of the model about to answer it (#101).

    Raised instead of letting a runtime cut it: Ollama, by default, drops the head of
    an oversized prompt and still answers 200 — the system prompt and the shape it
    carries are what go. With `truncate: false` it says so, with the exact count, and
    so does llama.cpp; a count taken before sending says it sooner. The agent that
    built the prompt shrinks it to `fits` and sends it once more. The router fills in
    which model it was for, so a prompt that met a fallback is re-sized for that one.
    """

    def __init__(
        self,
        message: str,
        *,
        prompt_tokens: Optional[int] = None,
        context: Optional[int] = None,
        chars: int = 0,
    ) -> None:
        super().__init__(message, retryable=False, kind="too_long")
        #: The prompt's real (or exactly counted) size, when the runtime said.
        self.prompt_tokens = prompt_tokens
        #: The window it was measured against, when the runtime said.
        self.context = context
        #: Characters sent, so the real characters-per-token can be learned from it.
        self.chars = chars
        #: Set by the router: `provider:model`, and that model's profile.
        self.model: Optional[str] = None
        self.profile: Optional[ModelProfile] = None


class WindowTooSmall(ProviderError):
    """An agent's own instructions don't fit the model's window (#101).

    Nothing can be trimmed: the instructions *are* the shape it must answer in, and a
    runtime would cut them from the head. The phase stops and says so, rather than
    sending a prompt that has already lost what it is asking for.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False, kind="window")


def overflow(text: str) -> Optional[tuple[int, Optional[int]]]:
    """(prompt tokens, window) from a runtime's "the prompt doesn't fit" answer, or
    None when the answer is about something else. llama.cpp's shape, which Ollama's
    engine shares: `exceed_context_size_error`, `n_prompt_tokens`, `n_ctx` — possibly
    JSON inside a JSON string."""
    said = text or ""
    if "exceed_context_size" not in said and "n_prompt_tokens" not in said and "longer than the context" not in said:
        return None
    found = re.search(r"n_prompt_tokens\W{0,6}(\d+)", said) or re.search(r"request \((\d+) tokens\)", said)
    window = re.search(r"n_ctx\W{0,6}(\d+)", said) or re.search(r"context size \((\d+) tokens\)", said)
    if not found:
        return None
    return int(found.group(1)), int(window.group(1)) if window else None


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

    def count_tokens(self, messages: list[ChatMessage], model: str) -> Optional[int]:
        """The prompt's exact size in this model's tokens, chat template included, when
        this provider can count it (#101) — None when it can't, and the learned
        characters-per-token stands in."""
        return None

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
