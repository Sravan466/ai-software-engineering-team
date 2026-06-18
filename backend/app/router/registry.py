"""Model pricing + a tiny capability hint table.

Prices are USD per 1M tokens (input, output). Local models are free. Used by the
analytics tracker to estimate cost and by Auto routing as a rough complexity hint.
Update freely — this is a convenience table, not a source of truth.
"""
from __future__ import annotations

# (input_per_mtok, output_per_mtok)
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # OpenAI (illustrative)
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    # Google Gemini (illustrative)
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
}


def price_for(model: str) -> tuple[float, float]:
    """Return (input, output) USD per 1M tokens. Local/unknown models cost 0."""
    return PRICING.get(model, (0.0, 0.0))


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_rate, out_rate = price_for(model)
    return (prompt_tokens / 1_000_000) * in_rate + (completion_tokens / 1_000_000) * out_rate
