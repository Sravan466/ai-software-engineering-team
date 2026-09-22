"""Will this model run a build here? Answered before Start, from what is known.

One verdict per model on its source, in three words and a reason:

  * **fits** — nothing known stands in its way;
  * **degraded** — it will run, with reduced quality or speed, and says which;
  * **blocked** — it won't run: the reason, and what to pick instead.

Everything here reads the profile — the runtime-neutral record every adapter fills
— so the same check applies to any source, direct or behind a connector. A
suggestion describes a model by size and quantization, never by name: which models
exist is the user's runtime's business, not this file's.

Memory is judged against the computer the model runs on, which is this one only
when the source shares its memory. Otherwise it is not judged at all, and the check
says so rather than measuring the wrong machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.config import settings
from app.router.model_profile import (
    MIN_WORKABLE_TOKENS,
    ModelProfile,
    bits_per_weight,
    kv_bytes,
)
from app.router.runtimes.types import (
    KIND_BASE,
    KIND_EMBEDDING,
    STRUCTURED_NONE,
    THINKING_OFF,
    THINKS_ALWAYS,
    THINKS_LEVELS,
    thinks,
)

FITS = "fits"
DEGRADED = "degraded"
BLOCKED = "blocked"
UNKNOWN = "unknown"
#: Reasons that only inform, and never change the verdict.
NOTE = "note"
_RANK = {BLOCKED: 3, DEGRADED: 2, UNKNOWN: 1, FITS: 0}

#: A window below twice the minimum runs, but the later phases see little of the
#: earlier ones: each phase's share of the prompt is what is left after the shape.
_TIGHT_WINDOW = 2 * MIN_WORKABLE_TOKENS
#: Weights past this share of RAM cannot be held beside anything else at all.
_WEIGHTS_BLOCK_SHARE = 0.95
#: Weights plus a minimal cache past this share still load, but push the machine
#: into swap; the rest is the OS and whatever else is running.
_TIGHT_MEMORY_SHARE = 0.75
#: Below this many bits a weight, quantization costs noticeable quality.
_LOW_BITS = 4.0
#: The width a suggestion is sized at: 4-bit with block scales, the usual default.
_SUGGESTED_BITS = 4.5


@dataclass
class Compatibility:
    spec: str
    level: str
    summary: str
    #: [{level, text}] — each finding, with its own weight.
    reasons: list[dict] = field(default_factory=list)
    #: What to pick instead, when it is blocked. Sizes and quantizations, not names.
    suggestion: Optional[str] = None
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "spec": self.spec,
            "level": self.level,
            "summary": self.summary,
            "reasons": list(self.reasons),
            "suggestion": self.suggestion,
            "facts": dict(self.facts),
        }


def _gib(n: int) -> str:
    value = n / 2**30
    return f"{value:.1f} GB" if value < 10 else f"{value:.0f} GB"


def _billions(n: float) -> str:
    return f"{n / 1e9:.0f}B" if n >= 10e9 else f"{n / 1e9:.1f}B"


def assess(
    spec: str,
    profile: ModelProfile,
    *,
    ram_bytes: Optional[int],
    remote: bool,
    probed: bool = True,
    window_hint: str = "",
    kv_hint: str = "",
    tuning: Optional[dict] = None,
) -> Compatibility:
    """The verdict for one model, from its profile and the machine it runs on.

    `ram_bytes` is the memory of the computer running the model, or None when that
    is not this one. `probed` is False when the runtime described nothing at all —
    the check then says "not assessed" instead of passing a model it knows nothing
    about.
    """
    reasons: list[dict] = []
    suggestions: list[str] = []

    def add(level: str, text: str) -> None:
        reasons.append({"level": level, "text": text})

    facts: dict = {
        "context_window": profile.context_window,
        "context_limit": profile.context_limit,
        "parameters": profile.parameter_count,
        "parameter_size": profile.parameter_size,
        "quantization": profile.quantization,
        "weights_bytes": profile.weights_bytes,
        "ram_bytes": ram_bytes,
        "experts_total": profile.experts_total,
        "experts_active": profile.experts_active,
        "thinking": profile.thinking,
        "thinking_level": profile.thinking_level,
        "reasoning_tokens": profile.reasoning_tokens,
        "kv_cache_type": profile.kv_cache_type,
    }

    if not probed:
        add(
            UNKNOWN,
            "Its runtime reports nothing about this model, so it can't be checked ahead of "
            "time. It will be tried as it is.",
        )
        return _verdict(spec, reasons, suggestions, facts)

    # ── what kind of model it is ───────────────────────────────────────────
    if profile.kind == KIND_EMBEDDING:
        add(BLOCKED, "It's an embedding model: it turns text into vectors and can't write an answer.")
        suggestions.append("Choose a chat or instruct model. This one can still power memory and search.")
    elif profile.kind == KIND_BASE:
        add(
            BLOCKED,
            "It's a base model with no chat template, so it continues text instead of "
            "following an agent's instructions.",
        )
        suggestions.append("Choose the instruct or chat version of this model.")

    # ── the window it runs at ──────────────────────────────────────────────
    window = profile.context_window
    if window < MIN_WORKABLE_TOKENS:
        cause = (
            " That is the ceiling set for it in Settings."
            if profile.clamp_reason and "set for this model" in profile.clamp_reason
            else f" {window_hint}" if window_hint
            else ""
        )
        add(
            BLOCKED,
            f"It runs with a {window:,}-token window, and an agent's prompt needs at least "
            f"{MIN_WORKABLE_TOKENS:,}.{cause}",
        )
        suggestions.append(
            f"Choose a model, or a runtime setting, that gives it at least {_TIGHT_WINDOW:,} tokens."
        )
    elif window < _TIGHT_WINDOW:
        add(
            DEGRADED,
            f"A {window:,}-token window is tight: later phases see less of the earlier work.",
        )
    if profile.source == "fallback" and profile.context_limit is None:
        add(
            DEGRADED,
            "Its runtime doesn't report the window it runs this model at, so "
            f"{settings.model_context_fallback_tokens:,} tokens is assumed — if it runs with "
            "less, long prompts are cut short.",
        )

    # ── memory, on the computer that runs it ───────────────────────────────
    weights = profile.weights_bytes
    if remote or ram_bytes is None:
        add(NOTE, "It runs on another computer, whose memory this backend can't see, so memory isn't checked.")
    elif not weights:
        add(NOTE, "Its runtime doesn't report how big the weights are, so memory isn't checked.")
    else:
        cache = kv_bytes(profile, min(window, MIN_WORKABLE_TOKENS)) or 0
        need = weights + cache
        facts["memory_needed_bytes"] = need
        if weights >= ram_bytes * _WEIGHTS_BLOCK_SHARE:
            add(
                BLOCKED,
                f"Its weights alone need about {_gib(weights)}, and the computer running it "
                f"has {_gib(ram_bytes)}.",
            )
            fits = ram_bytes * _TIGHT_MEMORY_SHARE * 8 / _SUGGESTED_BITS
            suggestions.append(
                f"Choose a model of about {_billions(fits)} parameters or fewer at 4-bit "
                "(Q4_K_M), or a smaller quantization of this one."
            )
        elif need > ram_bytes * _TIGHT_MEMORY_SHARE:
            add(
                DEGRADED,
                f"It needs about {_gib(need)} of the {_gib(ram_bytes)} this computer has, so "
                "expect swapping and slow replies. Close other apps while it builds, or use a "
                "smaller quantization.",
            )
    if profile.experts_total and profile.experts_active and profile.experts_active < profile.experts_total:
        add(
            NOTE,
            f"A mixture of experts: {profile.experts_active} of {profile.experts_total} experts "
            "run per token. It needs the memory of its full size, and runs faster than a dense "
            "model that size.",
        )

    # ── size and quantization ──────────────────────────────────────────────
    if profile.is_small:
        add(
            DEGRADED,
            f"{profile.parameter_size or _billions(profile.parameter_count or 0)} parameters is a "
            "small model: it follows the output shape, but the content inside it will be thin.",
        )
    bits = bits_per_weight(profile.quantization)
    if bits is not None and bits < _LOW_BITS:
        add(
            DEGRADED,
            f"It's quantized to {profile.quantization} — below 4-bit, answers get noticeably weaker.",
        )

    # ── thinking ───────────────────────────────────────────────────────────
    sampling = (tuning or {}).get("sampling") or {}
    wanted = sampling.get("thinking") or settings.local_thinking
    if profile.thinking == THINKS_ALWAYS:
        add(
            DEGRADED,
            "It always reasons before it answers, so every call spends up to "
            f"{profile.reasoning_tokens:,} tokens thinking and takes longer.",
        )
    elif profile.thinking == THINKS_LEVELS and wanted == THINKING_OFF:
        add(
            NOTE,
            f"It can't be switched off, so it thinks at the lowest level ({profile.thinking_level}).",
        )
    if thinks(profile.thinking_level):
        temperature = sampling.get("temperature", profile.defaults.get("temperature"))
        if temperature is not None and temperature <= 0:
            add(
                DEGRADED,
                "It thinks, and greedy decoding (temperature 0) makes thinking models repeat "
                "themselves; a higher temperature is used instead.",
            )

    # ── shape, hosting and the cache estimate ──────────────────────────────
    if profile.structured_output == STRUCTURED_NONE and profile.kind != KIND_EMBEDDING:
        add(
            NOTE,
            "Its runtime can't hold replies to a JSON shape, so each answer relies on the "
            "prompt and a repair round.",
        )
    if not profile.is_local:
        add(NOTE, "Its runtime sends it to a hosted service to run, so Local builds refuse it.")
    if profile.kv_cache_type != "f16" and kv_hint:
        add(NOTE, f"The memory estimate assumes a {profile.kv_cache_type} KV cache. {kv_hint}")

    return _verdict(spec, reasons, suggestions, facts)


def _verdict(spec: str, reasons: list[dict], suggestions: list[str], facts: dict) -> Compatibility:
    ranked = [r for r in reasons if r["level"] in _RANK]
    level = max((r["level"] for r in ranked), key=lambda lv: _RANK[lv], default=FITS)
    if level == FITS:
        summary = "Fits: nothing known stands in its way."
    else:
        summary = next(r["text"] for r in ranked if r["level"] == level)
    return Compatibility(
        spec=spec,
        level=level,
        summary=summary,
        reasons=reasons,
        suggestion=" ".join(dict.fromkeys(suggestions)) if level == BLOCKED and suggestions else None,
        facts=facts,
    )
