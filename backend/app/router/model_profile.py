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
        # Floored so a budget is never absurdly small — but never above the room
        # that exists. Raising it past `room` is how a 512-token ceiling ended up
        # with a 512-token prompt budget *plus* 256 tokens of output against a
        # 512-token window: a 50% overrun, guaranteeing the head truncation this
        # whole module exists to prevent.
        budget = max(min(int(room * _PROMPT_SAFETY_MARGIN), room), min(_MIN_PROMPT_TOKENS, room))
        # A window is permission, not an instruction to fill it. Without this, a
        # 200k-token cloud model inlines every prior phase in full into every later
        # one — which is what the window allows and about forty times what the old
        # literals cost per call.
        ceiling = settings.max_prompt_tokens
        return min(budget, ceiling) if ceiling and ceiling > 0 else budget

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
#: Below this a window cannot hold an agent's system prompt — which carries the whole
#: required-output shape — plus a useful reply. Measured, not picked: at 2,048 the
#: shape sketch alone overruns the budget for every agent in the pipeline, and the
#: prompt gets truncated from the head exactly as it did before any of this.
_MIN_WORKABLE_TOKENS = 4096


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
    """Lower a window to the configured ceiling, if the user set one.

    Lower, and only lower. The `max(..., _MIN_PROMPT_TOKENS)` that used to be here
    quietly doubled a ceiling of 256 — contradicting, on this path, the rule the
    resolver enforces on the other: a cap someone typed is a fact about what they
    want, not an estimate to be corrected.
    """
    ceiling = settings.ollama_context_ceiling
    if ceiling and ceiling > 0:
        return min(window, ceiling)
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
    ram_bytes: Optional[int],
) -> tuple[int, Optional[str]]:
    """`min(model limit, what RAM holds, configured ceiling)` — and why it landed there.

    Asking for more than the model was trained for either errors or wastes memory,
    and asking for more KV cache than the machine has swaps or gets killed. Both are
    ceilings on the same number, so the smallest wins, and the one that actually bound
    it is reported for the log line and the Settings page.

    The model's limit and a ceiling the user set are facts, and neither is overridden:
    a floor that raised the window back up would hand someone who capped it at 2,048 a
    8,192-token window instead. The RAM figure is an *estimate* — it assumes an f16
    cache and cannot see GPU or unified-memory offload — so that one alone is floored
    at a window anything can run in, and says so rather than quietly inflating.
    """
    limit = context_limit or settings.model_context_fallback_tokens
    candidates: list[tuple[int, Optional[str]]] = [(limit, None)]

    ram_tokens = _tokens_that_fit_in_ram(kv_bytes_per_token, ram_bytes)
    if ram_tokens is not None:
        gib = (ram_bytes or 0) / 2**30
        note = (
            f"clamped to {{window:,}} tokens by RAM — {gib:.0f} GiB total, "
            f"{settings.ollama_ram_fraction:.0%} of it available to the KV cache"
        )
        if ram_tokens < _MIN_WORKABLE_TOKENS:
            # This one is an *estimate* — f16 cache, and blind to GPU or unified
            # memory offload — so unlike the two hard limits either side of it, it
            # gets a floor. Below this nothing runs at all, and a window of 200
            # tokens fails more confusingly than one that is merely tight. The
            # reason says the estimate was overruled, so the log does not pretend
            # the machine is comfortable.
            candidates.append(
                (
                    _MIN_WORKABLE_TOKENS,
                    note.format(window=ram_tokens)
                    + f", held up to {_MIN_WORKABLE_TOKENS:,} because nothing runs below "
                    "that — expect swapping, and use a smaller model",
                )
            )
        else:
            candidates.append((ram_tokens, note.format(window=ram_tokens)))

    ceiling = settings.ollama_context_ceiling
    if ceiling and ceiling > 0:
        candidates.append(
            (
                ceiling,
                f"clamped to the configured OLLAMA_CONTEXT_CEILING of {ceiling:,} tokens",
            )
        )

    # The model's own limit and a ceiling the user typed are both hard facts, and
    # neither is second-guessed: a cap of 2,048 means 2,048, even though that is a
    # window agents will find tight. Only the RAM estimate above gets a floor.
    return min(candidates, key=lambda c: c[0])


def _tokens_that_fit_in_ram(
    kv_bytes_per_token: Optional[int], ram_bytes: Optional[int]
) -> Optional[int]:
    """How many tokens of KV cache the machine can hold.

    The weights are deliberately not part of this. Ollama mmaps them, so they are
    page-cache backed and evictable rather than a fixed deduction from what is
    available, and on unified-memory machines they may not sit in system RAM at all.
    Subtracting them turned an ordinary 8 GiB laptop running a 7B model into a
    2,048-token window — a worse outcome than having no clamp, which is the wrong
    way for a safety margin to be wrong. The fraction below the total is the margin.
    """
    if not kv_bytes_per_token or not ram_bytes:
        return None
    spare = ram_bytes * settings.ollama_ram_fraction
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
