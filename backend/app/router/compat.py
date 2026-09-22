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

import platform
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
#: Characters an agent's JSON is made of; a stop sequence holding one can end a reply
#: in the middle of its deliverable.
_JSON_CHARACTERS = set('{}[]":,\n')


def unified_memory() -> bool:
    """Whether the GPU shares system memory here, so RAM is the whole story.

    Elsewhere a model can sit in a GPU's own memory, which this backend cannot see —
    and a check that blocked on system RAM alone would refuse a model that runs
    fine there. Apple silicon is the case where the two are one pool.
    """
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "arm64e")


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
    loaded: Optional[bool] = None,
    unified: Optional[bool] = None,
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
    # What an agent's prompt and reply actually get: a thinking model spends part of
    # the same window reasoning first.
    window = profile.context_window
    usable = window - profile.reasoning_tokens
    if usable < MIN_WORKABLE_TOKENS:
        cause = (
            " That is the ceiling set for it in Settings."
            if profile.clamp_reason and "set for this model" in profile.clamp_reason
            else f" {window_hint}" if window_hint
            else ""
        )
        kept = (
            f", {profile.reasoning_tokens:,} of them kept for reasoning,"
            if profile.reasoning_tokens
            else ""
        )
        add(
            BLOCKED,
            f"It runs with a {window:,}-token window{kept} and an agent's prompt needs at least "
            f"{MIN_WORKABLE_TOKENS:,}.{cause}",
        )
        suggestions.append(
            f"Choose a model, or a runtime setting, that gives it at least {_TIGHT_WINDOW:,} tokens"
            + (", or turn its thinking off in Tune." if profile.reasoning_tokens else ".")
        )
    elif usable < _TIGHT_WINDOW:
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
    one_pool = unified_memory() if unified is None else unified
    facts["memory_checked"] = False
    if remote or ram_bytes is None:
        add(NOTE, "It runs on another computer, whose memory this backend can't see, so memory isn't checked.")
    elif not weights:
        add(NOTE, "Its runtime doesn't report how big the weights are, so memory isn't checked.")
    elif loaded:
        facts["memory_checked"] = True
        add(NOTE, "Its runtime already has it loaded, so it fits in memory.")
    else:
        facts["memory_checked"] = True
        cache = kv_bytes(profile, min(window, MIN_WORKABLE_TOKENS)) or 0
        need = weights + cache
        facts["memory_needed_bytes"] = need
        fits = ram_bytes * _TIGHT_MEMORY_SHARE * 8 / _SUGGESTED_BITS
        too_big = (
            f"Choose a model of about {_billions(fits)} parameters or fewer at 4-bit "
            "(Q4_K_M), or a smaller quantization of this one."
        )
        if weights >= ram_bytes * _WEIGHTS_BLOCK_SHARE and one_pool:
            add(
                BLOCKED,
                f"Its weights alone need about {_gib(weights)}, and the computer running it "
                f"has {_gib(ram_bytes)}.",
            )
            suggestions.append(too_big)
        elif weights >= ram_bytes * _WEIGHTS_BLOCK_SHARE:
            # Only system RAM is visible from here; a GPU with enough memory of its
            # own runs this fine, so it is said, not refused.
            add(
                DEGRADED,
                f"Its weights need about {_gib(weights)}, more than the {_gib(ram_bytes)} of "
                "system memory here. It runs only if a GPU with enough memory of its own holds "
                f"it — otherwise it won't load. {too_big}",
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
    wanted = sampling.get("thinking") or (settings.local_thinking or "").strip().lower()
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

    stops = [s for s in sampling.get("stop") or [] if isinstance(s, str)]
    risky = [s for s in stops if _JSON_CHARACTERS & set(s)]
    if risky:
        add(
            DEGRADED,
            f"The stop sequence {risky[0]!r} can appear inside an agent's JSON and cut its "
            "answer short.",
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
