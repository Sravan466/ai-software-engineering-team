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
    # Each entry: {"provider": ..., "model": ..., "error": ...}
    attempts: list[dict] = Field(default_factory=list)


class GenerationOptions(BaseModel):
    """Knobs passed from agents to the router. Kept minimal and provider-neutral."""

    temperature: Optional[float] = None
    max_tokens: int = 4096
    json_mode: bool = False  # ask the provider for JSON output when supported
