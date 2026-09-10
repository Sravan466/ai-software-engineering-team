"""What a model can actually do — asked of the model, never assumed.

Every number this application used to guess about the model lives here, and every
one of them is now either read from the model itself or set by the user:

    model_info["<arch>.context_length"]        the window it was trained for
    model_info["<arch>.block_count"] + heads   what one token of KV cache costs in RAM
    details.parameter_size                     how big it is, in words a person reads
    capabilities                               whether decoding can be schema-constrained

That matters because the alternative is silent. Ollama's default window is small,
and a request that exceeds it is truncated **from the head** — the system prompt,
where the required output shape is written, is the first thing discarded — and the
truncation is logged on the server where no client ever sees it. Half the pipeline
was generating against a prompt with its instructions cut off, and the two safety
gates downstream read keys that were no longer being produced.

Nothing here is a constant a user cannot move: the probe reports the ceiling, RAM
decides what fits under it, and `OLLAMA_CONTEXT_CEILING` lowers it further if they
want the memory back.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: Bytes per element in the KV cache (f16 — Ollama's default). A property of the
#: runtime's cache format, not a budget: it is what one number in the cache weighs.
_KV_BYTES_PER_ELEMENT = 2
#: K and V are both cached, so a token costs two of everything below.
_KV_TENSORS_PER_TOKEN = 2


def _int(value: object) -> Optional[int]:
    """A positive int, or None. Model metadata is occasionally a string or a list."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (list, tuple)):
        # Per-layer head counts show up as a list; the largest is what must fit.
        numbers = [n for n in (_int(v) for v in value) if n]
        return max(numbers) if numbers else None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def total_ram_bytes() -> Optional[int]:
    """Physical RAM, or None where the platform will not say."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages > 0 and page_size > 0:
        return pages * page_size
    return None


@dataclass(frozen=True)
class ModelProfile:
    """The resolved answer to "how much room does this model have, and for what?"."""

    provider: str
    model: str
    #: The window the model itself reports, or None when the provider cannot say.
    context_limit: Optional[int]
    #: The window this application will actually ask for. Never above the limit.
    context_window: int
    #: Per-call output ceiling, derived from the window and the configured ceiling.
    max_output_tokens: int

    parameter_count: Optional[int] = None
    parameter_size: Optional[str] = None
    quantization: Optional[str] = None
    capabilities: tuple[str, ...] = ()
    #: True when the provider can constrain decoding to a JSON Schema, not just JSON.
    supports_schema_format: bool = False
    #: "probe" (asked the model), "configured" (a documented provider window), or
    #: "fallback" (nobody could say — the configured fallback window is in force).
    source: str = "fallback"
    #: Set when the window sits below the model's own limit, saying what lowered it.
    clamp_reason: Optional[str] = None
    architecture: Optional[str] = None
    #: Extra notes worth showing a person (e.g. "this model is small").
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # ── budgets derived from the window ──────────────────────────────────────
    @property
    def prompt_token_budget(self) -> int:
        """Tokens the prompt may occupy, with room kept for the reply.

        The safety margin covers what the chat template adds around the messages —
        a budget that exactly fills the window is a budget that truncates.
        """
        room = self.context_window - self.max_output_tokens
        return max(int(room * _PROMPT_SAFETY_MARGIN), _MIN_PROMPT_TOKENS)

    @property
    def prompt_char_budget(self) -> int:
        """The same budget in characters, which is what truncation actually cuts."""
        return int(self.prompt_token_budget * max(settings.approx_chars_per_token, 1.0))

    @property
    def is_small(self) -> bool:
        """Small enough that thin, shapeless output is the model, not the prompt."""
        if self.parameter_count is None:
            return False
        return self.parameter_count < settings.small_model_parameter_count

    def describe(self) -> str:
        """One line for the logs: what was resolved, and where it came from."""
        limit = f"{self.context_limit:,}" if self.context_limit else "unreported"
        line = (
            f"{self.provider}:{self.model} — context {self.context_window:,} tokens "
            f"(model limit {limit}, source {self.source}), "
            f"output ≤ {self.max_output_tokens:,}, "
            f"schema-constrained decoding {'on' if self.supports_schema_format else 'off'}"
        )
        return f"{line}; {self.clamp_reason}" if self.clamp_reason else line

    def as_dict(self) -> dict:
        """Serialisable view for the API (the Settings page shows this)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "context_limit": self.context_limit,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "prompt_token_budget": self.prompt_token_budget,
            "parameter_count": self.parameter_count,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "capabilities": list(self.capabilities),
            "supports_schema_format": self.supports_schema_format,
            "source": self.source,
            "clamp_reason": self.clamp_reason,
            "is_small": self.is_small,
            "warnings": list(self.warnings),
        }


#: Prompts are measured in tokens we cannot count exactly, and the chat template
#: adds more around them. Nine tenths of the free room is the working budget.
_PROMPT_SAFETY_MARGIN = 0.9
#: However the arithmetic lands, an agent needs room for its own instructions.
_MIN_PROMPT_TOKENS = 512


def fallback_profile(
    provider: str,
    model: str,
    *,
    context_limit: Optional[int] = None,
    source: str = "fallback",
    local: bool = False,
) -> ModelProfile:
    """A profile for a model nobody could probe: the configured window, and no schema.

    Used for cloud providers (which publish their window rather than exposing a probe)
    and for a local model while Ollama is unreachable — the run still needs budgets.
    `local` says whether `OLLAMA_CONTEXT_CEILING` applies: it is a knob about the
    memory on *this* machine, and has no business shrinking a cloud model's window.
    """
    limit = context_limit or settings.model_context_fallback_tokens
    window = _apply_ceiling(limit) if local else limit
    return ModelProfile(
        provider=provider,
        model=model,
        context_limit=context_limit,
        context_window=window,
        max_output_tokens=_output_budget(window),
        source=source,
    )


def _apply_ceiling(window: int) -> int:
    """Lower a window to the configured ceiling, if the user set one."""
    ceiling = settings.ollama_context_ceiling
    if ceiling and ceiling > 0:
        return max(min(window, ceiling), _MIN_PROMPT_TOKENS)
    return window


def _output_budget(window: int) -> int:
    """How many tokens a single reply may use.

    Half the window at most: an agent that is allowed to generate more than it was
    allowed to read is the truncation bug with the numbers reversed.
    """
    return max(1, min(settings.max_output_tokens, window // 2))


def resolve_window(
    *,
    context_limit: Optional[int],
    kv_bytes_per_token: Optional[int],
    weight_bytes: Optional[int],
    ram_bytes: Optional[int],
) -> tuple[int, Optional[str]]:
    """`min(model limit, what RAM holds, configured ceiling)` — and why it landed there.

    Asking for more than the model was trained for either errors or wastes memory,
    and asking for more KV cache than the machine has swaps or gets killed. Both are
    ceilings on the same number, so the smallest wins and the reason is kept for the
    log line and the Settings page.
    """
    limit = context_limit or settings.model_context_fallback_tokens
    window = limit
    reason: Optional[str] = None

    ram_tokens = _tokens_that_fit_in_ram(kv_bytes_per_token, weight_bytes, ram_bytes)
    if ram_tokens is not None and ram_tokens < window:
        gib = (ram_bytes or 0) / 2**30
        window = ram_tokens
        reason = (
            f"clamped to {window:,} tokens by RAM — {gib:.0f} GiB total, "
            f"{settings.ollama_ram_fraction:.0%} of it available to the KV cache"
        )

    ceilinged = _apply_ceiling(window)
    if ceilinged < window:
        window = ceilinged
        reason = f"clamped to the configured OLLAMA_CONTEXT_CEILING of {window:,} tokens"

    # A floor, so a machine under memory pressure degrades instead of collapsing to
    # a window no prompt fits in. Never above what the model itself supports.
    floor = min(limit, settings.model_context_fallback_tokens)
    if window < floor:
        window = floor
        reason = (
            f"held at the {floor:,}-token floor — less than that and no agent prompt fits"
        )
    return window, reason


def _tokens_that_fit_in_ram(
    kv_bytes_per_token: Optional[int],
    weight_bytes: Optional[int],
    ram_bytes: Optional[int],
) -> Optional[int]:
    """How many tokens of KV cache the machine can hold after the weights load."""
    if not kv_bytes_per_token or not ram_bytes:
        return None
    spare = ram_bytes * settings.ollama_ram_fraction - (weight_bytes or 0)
    if spare <= 0:
        return 0
    return int(spare // kv_bytes_per_token)


def kv_bytes_per_token(model_info: dict, arch: str) -> Optional[int]:
    """Bytes of KV cache one token costs, from the model's own architecture.

    `2 (K and V) × layers × kv-heads × head-dim × 2 bytes (f16)`. Every term comes
    from `model_info`; if any is missing the RAM clamp is simply not applied rather
    than being invented.
    """
    layers = _int(model_info.get(f"{arch}.block_count"))
    kv_heads = _int(model_info.get(f"{arch}.attention.head_count_kv"))
    embedding = _int(model_info.get(f"{arch}.embedding_length"))
    heads = _int(model_info.get(f"{arch}.attention.head_count"))
    if not (layers and kv_heads and embedding and heads):
        return None
    head_dim = embedding // heads
    if head_dim <= 0:
        return None
    return _KV_TENSORS_PER_TOKEN * layers * kv_heads * head_dim * _KV_BYTES_PER_ELEMENT


def parameter_count_from(model_info: dict, details: dict) -> Optional[int]:
    """The parameter count, from the exact number or from the "7.6B" label."""
    exact = _int(model_info.get("general.parameter_count"))
    if exact:
        return exact
    label = str(details.get("parameter_size") or "")
    match = re.match(r"\s*([\d.]+)\s*([KMB])", label, re.IGNORECASE)
    if not match:
        return None
    scale = {"k": 10**3, "m": 10**6, "b": 10**9}[match.group(2).lower()]
    try:
        return int(float(match.group(1)) * scale)
    except ValueError:
        return None


def build_profile(
    *,
    provider: str,
    model: str,
    show: dict,
    weight_bytes: Optional[int],
    supports_schema_format: bool,
    ram_bytes: Optional[int] = None,
) -> ModelProfile:
    """Turn one `/api/show` payload into the profile the rest of the app runs on."""
    model_info = show.get("model_info") or {}
    details = show.get("details") or {}
    arch = str(model_info.get("general.architecture") or details.get("family") or "").strip()

    context_limit = _int(model_info.get(f"{arch}.context_length")) if arch else None
    if context_limit is None:
        # Some architectures spell it differently; take the only context length there is.
        lengths = [
            _int(v) for k, v in model_info.items() if k.endswith(".context_length")
        ]
        found = [n for n in lengths if n]
        context_limit = max(found) if found else None

    window, clamp_reason = resolve_window(
        context_limit=context_limit,
        kv_bytes_per_token=kv_bytes_per_token(model_info, arch) if arch else None,
        weight_bytes=weight_bytes,
        ram_bytes=ram_bytes if ram_bytes is not None else total_ram_bytes(),
    )

    parameters = parameter_count_from(model_info, details)
    warnings: list[str] = []
    if parameters and parameters < settings.small_model_parameter_count:
        warnings.append(
            f"{details.get('parameter_size') or f'{parameters/1e9:.1f}B'} parameters is a "
            "small model. It will follow the required output shape, but the content inside "
            "it will be thin — a larger model is worth the download for real builds."
        )
    if context_limit is None:
        warnings.append(
            "This model does not report a context length, so the configured fallback "
            f"window of {settings.model_context_fallback_tokens:,} tokens is in force."
        )

    return ModelProfile(
        provider=provider,
        model=model,
        context_limit=context_limit,
        context_window=window,
        max_output_tokens=_output_budget(window),
        parameter_count=parameters,
        parameter_size=details.get("parameter_size") or None,
        quantization=details.get("quantization_level") or None,
        capabilities=tuple(str(c) for c in (show.get("capabilities") or [])),
        supports_schema_format=supports_schema_format,
        source="probe" if context_limit else "fallback",
        clamp_reason=clamp_reason,
        architecture=arch or None,
        warnings=tuple(warnings),
    )


class ProfileCache:
    """One probe per model, not one per agent call.

    A pipeline run is eight agents plus a debate against the same model; the probe is
    a network round trip that returns the same answer every time. Failures are *not*
    cached — a model pulled after Ollama came up should be picked up on the next call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], ModelProfile] = {}

    def get(self, key: tuple[str, str]) -> Optional[ModelProfile]:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: tuple[str, str], profile: ModelProfile) -> ModelProfile:
        with self._lock:
            self._entries[key] = profile
        log.info("Resolved model profile: %s", profile.describe())
        return profile

    def forget(self, key: Optional[tuple[str, str]] = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)
