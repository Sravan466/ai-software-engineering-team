"""Send a prompt that fits the model that answers it — shrunk and sent once more when it
doesn't (#101).

Every caller that builds a prompt sizes it from a profile: an agent's sections, a code
phase's write call, the debate's quoted architecture, the mockup's context. When the
model refuses it — a runtime's exact count, or the router's check before sending — the
caller's own `build` is run again with the profile `fitted` makes from the refusal, and
the result is sent once more, held to the model it was rebuilt for. A second refusal is
not retried: the same rebuild would only be refused again.
"""
from __future__ import annotations

from typing import Callable, Optional, TypeVar

from app.core.logging import get_logger
from app.router.base import PromptTooLong, StillTooLong
from app.router.model_profile import ModelProfile, fitted
from app.schemas.llm import LLMResponse

log = get_logger(__name__)

T = TypeVar("T")


def send_fitting(
    build: Callable[[ModelProfile], T],
    profile: ModelProfile,
    send: Callable[[T, Optional[str]], LLMResponse],
    *,
    who: str,
) -> tuple[LLMResponse, T, ModelProfile]:
    """(the reply, what `build` made for it, the profile it was built with)."""
    built = build(profile)
    try:
        return send(built, None), built, profile
    except PromptTooLong as e:
        smaller = fitted(e, profile)
        log.warning(
            "%s's prompt didn't fit %s (%s). Rebuilt at %s characters and sent once more.",
            who, e.model or smaller.model, e, f"{smaller.prompt_char_budget:,}",
        )
        built = build(smaller)
        try:
            return send(built, e.model), built, smaller
        except PromptTooLong as again:
            raise StillTooLong(
                f"{who}'s prompt still didn't fit {again.model or smaller.model} after it was rebuilt for it: {again}"
            ) from again
