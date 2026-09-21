"""Model pricing + a tiny capability hint table.

Prices are USD per 1M tokens (input, output). Update freely — this is a convenience
table, not a source of truth, which is exactly why a model missing from it must not
be reported as free. It used to be: `price_for` returned `(0.0, 0.0)` for anything
unlisted, so pointing a role at a cloud model this table has never heard of produced
a dashboard reading **$0.00** and a cost cap that could never trip. A price nobody
knows is not a price of zero, and the difference is the whole bill.

Local models genuinely are free — they run on hardware the user controls — so those
are priced at zero because the call says it ran locally, never by absence from a table
and never by the name of the runtime that served it.
"""
from __future__ import annotations

from typing import Optional

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

#: Providers whose calls cost nothing whatever they report. Only the test suite's
#: stub, so it reports a priced zero rather than an unpriced one.
FREE_PROVIDERS = frozenset({"mock"})


def price_for(
    model: str, provider: Optional[str] = None, is_local: Optional[bool] = None
) -> Optional[tuple[float, float]]:
    """(input, output) USD per 1M tokens — or **None** when nobody knows.

    `is_local` is what makes "free" sayable: a model on the user's own hardware costs
    nothing whatever it is, including one added five minutes ago that no table lists.
    A model a local runtime sends to a hosted service to run is *not* local, and is
    priced like anything else — which, for a model this table does not list, is
    unknown. Everything that is not local has to be in `PRICING` to be priced at all.
    """
    if is_local or (provider and provider.lower() in FREE_PROVIDERS):
        return (0.0, 0.0)
    return PRICING.get(model)


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    provider: Optional[str] = None,
    is_local: Optional[bool] = None,
) -> Optional[float]:
    """USD for one call, or None when the model has no known price.

    Callers must keep None distinct from 0.0 all the way to the screen. Folding the
    two together is the bug this signature exists to make hard to write by accident.
    """
    rates = price_for(model, provider, is_local)
    if rates is None:
        return None
    in_rate, out_rate = rates
    return (prompt_tokens / 1_000_000) * in_rate + (completion_tokens / 1_000_000) * out_rate
