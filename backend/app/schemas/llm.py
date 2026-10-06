"""Provider-agnostic message and response types used across the router and agents."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResponse(BaseModel):
    """Normalised result returned by every provider."""

    text: str
    provider: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = 0
    fallback_used: bool = False
    #: Whether the model ran on hardware the user controls — decided by the provider
    #: that served it, per model, so analytics and pricing never guess from a name.
    #: None only where nothing could say (the test suite's stub).
    is_local: Optional[bool] = None
    # Each entry: {"provider": ..., "model": ..., "error": ...}
    attempts: list[dict] = Field(default_factory=list)
    #: Why an earlier model in the chain was passed over, when a provider refused its
    #: key ("OpenAI: out of credit; continued on Google Gemini (…)").
    fallback_note: Optional[str] = None
    #: Why generation stopped, normalised: `length` when the reply hit the output
    #: limit (OpenAI's `length`, Anthropic's `max_tokens`, Gemini's `MAX_TOKENS`,
    #: Ollama's `done_reason`), else whatever the runtime said. A cut reply is not a
    #: malformed one, and the agent that asked treats them differently.
    finish_reason: Optional[str] = None
    #: What the model reasoned before answering, when it did. Kept apart from `text`
    #: so no parser ever reads a thought as the deliverable, and excluded from every
    #: serialisation so it is never stored, echoed into a prompt, or acted on.
    reasoning: Optional[str] = Field(default=None, exclude=True, repr=False)


def normalise_finish(reason: object) -> Optional[str]:
    """One word for "the output limit cut this reply", whatever the provider calls it."""
    if reason is None:
        return None
    text = str(getattr(reason, "name", reason) or "").strip()
    if not text:
        return None
    if text.lower() in ("length", "max_tokens", "max_output_tokens", "2") or text.upper() == "MAX_TOKENS":
        return "length"
    return text.lower()


class GenerationOptions(BaseModel):
    """Knobs passed from agents to the router. Kept minimal and provider-neutral."""

    temperature: Optional[float] = None
    #: Ceiling on tokens this one call may generate. `None` — the normal case — means
    #: "as much as the resolved model window allows", because a literal here is a
    #: guess about a model the caller has never seen. Providers resolve it against
    #: their own profile; see `resolve_max_tokens`.
    max_tokens: Optional[int] = None
    json_mode: bool = False  # ask the provider for JSON output when supported
    #: The JSON Schema the response must match. Providers that can constrain decoding
    #: to a schema do; the rest fall back to `json_mode` and the prompt's description,
    #: with validation and a repair round catching what drifts either way.
    json_schema: Optional[dict] = None

    def resolve_max_tokens(self, budget: int) -> int:
        """This call's output ceiling: the caller's ask, never above the model's room."""
        if self.max_tokens is None or self.max_tokens <= 0:
            return budget
        return min(self.max_tokens, budget)
