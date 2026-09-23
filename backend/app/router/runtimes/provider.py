"""One model source, as the router sees it: an `LLMProvider` over typed operations.

The router registers one of these per source and never looks past it. Below it sits
an adapter — the runtime's own dialect — and above it nothing needs to know which
runtime that is. What lives here is everything that is the same for every runtime:

  * **a cached state** — reachable or not, and what it serves — refreshed on a short
    timer and marked down the moment a call cannot connect, instead of a network
    round trip before every call;
  * **the profile** each prompt is budgeted with, built from the adapter's
    `ModelInfo` and this source's RAM — which is this machine's only when the
    runtime shares it;
  * **generation**, as a `ChatRequest` that always states its window and its output
    budget, and says afterwards when a call actually ran into either.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Optional

from app.core import model_settings
from app.core.config import settings
from app.core.logging import get_logger
from app.router import compat, generation
from app.router.base import LLMProvider, ProviderError
from app.router.model_profile import (
    ModelProfile,
    ProfileCache,
    build_profile,
    fallback_profile,
    total_ram_bytes,
)
from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.types import (
    MACHINE_KEYS,
    ModelInfo,
    SAMPLING_KEYS,
    STRUCTURED_SCHEMA,
    THINKING_ON,
    THINKING_SETTINGS,
    THINKS_ALWAYS,
    THINKS_NONE,
    ChatRequest,
    ModelEntry,
    thinking_for,
    thinks,
    writes,
)
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)

#: How long a model that could not be described is left alone before it is asked
#: again — so a broken one costs the Settings page one timeout, not one per load.
_FAILED_DESCRIBE_SECONDS = 30.0
#: How long a source's model list and reachability are trusted. Short, because the
#: list is how a model downloaded a moment ago appears; long enough that one run's
#: calls do not each pay a round trip to learn what the last one already knew.
STATE_TTL_SECONDS = 10.0


@dataclass
class Source:
    """Where models come from: an address, and what answered there."""

    id: str
    label: str
    base_url: str
    #: The runtime's id in the adapter table, or None until something answers.
    runtime: Optional[str]
    #: "detected" (found on loopback), "configured" (`.env`) or "added" (Settings).
    origin: str
    api_key: Optional[str] = None
    #: Set when the address alone cannot say whether the runtime shares this
    #: machine's memory — a sibling container, say.
    same_machine_override: Optional[bool] = None
    version: Optional[str] = None
    #: Identified only as "speaks the OpenAI API" — used, but asked again, because
    #: that is also how a runtime answers while it is still starting.
    provisional: bool = False

    @property
    def remote(self) -> bool:
        """Another computer: not loopback, and not said to share this machine.

        A sibling container marked `same_machine` is on this host whatever its name,
        so prompts sent to it do not leave this computer.
        """
        from app.router.runtimes.detect import is_loopback

        return not is_loopback(self.base_url) and self.same_machine_override is not True

    @property
    def same_machine(self) -> bool:
        """Whether this machine's RAM is the runtime's RAM, so it may clamp the window.

        Configured when it is set, inferred from the address when it is not. The
        address can only say "loopback": a runtime in a sibling container under
        docker compose has a hostname of its own and shares this host's RAM all the
        same, and leaving it unclamped sends a model's full trained window — tens of
        GiB of KV cache — to a machine that cannot hold it.
        """
        if self.same_machine_override is not None:
            return self.same_machine_override
        if settings.local_same_machine is not None:
            return bool(settings.local_same_machine)
        return not self.remote

    @property
    def key_hint(self) -> Optional[str]:
        key = self.api_key
        if not key:
            return None
        return ("…" + key[-4:]) if len(key) >= 8 else "set"


@dataclass
class SourceState:
    reachable: bool
    models: list[ModelEntry] = field(default_factory=list)
    error: Optional[str] = None
    checked_at: float = 0.0


class SourceProvider(LLMProvider):
    is_local = True

    def __init__(self, source: Source, adapter: RuntimeAdapter, tuning=None) -> None:
        self.source = source
        self.adapter = adapter
        self.name = source.id
        #: Where this account's per-model settings are kept — `model_settings`'s own
        #: functions (the pre-accounts file) when no store is given.
        self.tuning_store = tuning if tuning is not None else model_settings
        #: Asked before every request this source sends, when set: raises if the
        #: request must not go. Set on a source an account that doesn't own the
        #: install added — its name is resolved again each time, so a name that
        #: later points at the server's own network is refused, not followed.
        self.guard: Optional[Callable[[], None]] = None
        self._profiles = ProfileCache()
        self._state: Optional[SourceState] = None
        self._state_lock = threading.Lock()
        #: (model, setting) pairs already reported as unsendable, so a setting the
        #: runtime cannot take is said once per model rather than once per call.
        self._reported_unsent: set[tuple[str, str]] = set()
        #: What the runtime last said about each model, so the untuned budgets can be
        #: shown beside tuned ones without asking it again.
        self._infos: dict[str, ModelInfo] = {}
        #: When describing a model last failed. Not an answer — it is asked again —
        #: but not every Settings load pays a timeout for the same broken model.
        self._failed_at: dict[str, float] = {}
        #: Bumped by anything that makes the current state wrong (a new key, a
        #: refused connection). A refresh that started before the bump does not get
        #: to write its now-stale answer over the change.
        self._generation = 0

    # ── state ────────────────────────────────────────────────────────────────
    def state(self, max_age: float = STATE_TTL_SECONDS) -> SourceState:
        """Reachability and the model list, as of no more than `max_age` seconds ago.

        One list call is the whole check, so a source that is down costs one short
        timeout per refresh — and refreshes are serialised, so ten callers asking at
        once while it is down wait for one timeout, not ten.
        """
        current = self._state
        if current is not None and time.monotonic() - current.checked_at < max_age:
            return current
        with self._state_lock:
            current = self._state
            if current is not None and time.monotonic() - current.checked_at < max_age:
                return current
            generation = self._generation
            try:
                self._check_guard()
                models = self.adapter.list_models()
                current = SourceState(reachable=True, models=models, checked_at=time.monotonic())
            except ProviderError as e:
                current = SourceState(reachable=False, error=self.scrub(str(e)), checked_at=time.monotonic())
            except Exception as e:  # noqa: BLE001 - a malformed answer is "down", not a crash
                current = SourceState(reachable=False, error=self.scrub(str(e)), checked_at=time.monotonic())
            if generation == self._generation:
                self._state = current
            return current

    def _check_guard(self) -> None:
        if self.guard is not None:
            self.guard()

    def invalidate(self) -> None:
        self._generation += 1
        self._state = None

    def _mark_down(self, error: str) -> None:
        self._generation += 1
        self._state = SourceState(reachable=False, error=self.scrub(error), checked_at=time.monotonic())

    def scrub(self, text: str) -> str:
        """`text` with this source's key taken out, wherever an error quoted it."""
        key = self.source.api_key
        return text.replace(key, "…") if key else text

    def _scrubbed(self, error: ProviderError) -> ProviderError:
        message = self.scrub(str(error))
        if message == str(error):
            return error
        return ProviderError(message, retryable=error.retryable, unreachable=error.unreachable)

    def available(self) -> bool:
        return self.state().reachable

    def list_models(self) -> list[str]:
        return [m.name for m in self.state().models]

    def entries(self) -> list[ModelEntry]:
        return list(self.state().models)

    # ── what each model is for ───────────────────────────────────────────────
    def describe(self) -> dict[str, ModelEntry]:
        """`{name: entry}` for every model, with kinds filled in where the runtime says."""
        current = self.state()
        if not current.reachable:
            return {}
        try:
            return self.adapter.describe(current.models)
        except Exception as e:  # noqa: BLE001 - a description is a nicety, never a failure
            log.warning("Could not describe the models on %s: %s", self.source.base_url, e)
            return {m.name: m for m in current.models}

    def resolves(self, model: str, names: Optional[Iterable[str]] = None) -> bool:
        return self.adapter.resolves(model, self.list_models() if names is None else names)

    def entry(self, model: str) -> Optional[ModelEntry]:
        """What is known about `model` without asking the runtime anything new."""
        known = self.adapter.known_kind(model)
        if known is not None:
            return known
        for entry in self.state().models:
            if self.adapter.resolves(model, [entry.name]):
                return entry
        return None

    def writes(self, model: str) -> Optional[bool]:
        entry = self.entry(model)
        return writes(entry.kind) if entry is not None else None

    def is_local_model(self, model: str) -> bool:
        entry = self.entry(model)
        return entry.is_local if entry is not None else True

    def is_same_machine(self) -> bool:
        return self.source.same_machine

    def forget(self, model: Optional[str] = None) -> None:
        """Drop cached profiles — after a download, or when the default changes.

        A download replaces that model's weights, so its profile and what the runtime
        said it can do are both stale, under every spelling of its name. A new
        default changes which model runs, not what any model can do: only profiles
        are dropped then, never what the runtime reported.
        """
        if model is None:
            self._profiles.forget(None)
            return
        self._profiles.forget_where(lambda key: self.adapter.resolves(model, [key[1]]) or key[1] == model)
        self.adapter.forget(model)
        self.invalidate()

    # ── per-model settings ───────────────────────────────────────────────────
    def settings_key(self, model: str) -> str:
        """The spec `model`'s settings are saved under, however it is spelled here.

        A saved spelling that names the same model by the runtime's rule is used, so
        tuning `qwen3` and then running `qwen3:latest` is one model, not two.
        """
        prefix = f"{self.source.id}:"
        for spec in self.tuning_store.all_settings():
            if spec.startswith(prefix) and self.adapter.resolves(model, [spec[len(prefix):]]):
                return spec
        return f"{prefix}{model}"

    def tuning(self, model: str) -> dict:
        """The settings saved for `model`, re-validated — `{}` when none are."""
        return generation.read(self.tuning_store.get(self.settings_key(model)))

    # ── the profile ──────────────────────────────────────────────────────────
    def profile(self, model: str) -> ModelProfile:
        """Everything known about `model`, asked once and remembered.

        A failed answer is deliberately not cached: a model added a minute from now,
        or a runtime that has only just started, must be picked up on the next call
        rather than leaving the run on fallback budgets until the backend restarts.
        """
        key = (self.source.id, model)
        cached = self._profiles.get(key)
        if cached is not None:
            return cached
        failed = self._failed_at.get(model)
        if failed is not None and time.monotonic() - failed < _FAILED_DESCRIBE_SECONDS:
            return fallback_profile(self.name, model, local=True)
        try:
            self._check_guard()
            info = self.adapter.model_info(model)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not describe '%s' on %s: %s", model, self.source.base_url, e)
            info = None
        if info is None:
            self._failed_at[model] = time.monotonic()
            return fallback_profile(self.name, model, local=True)
        self._failed_at.pop(model, None)
        self._infos[model] = info
        # The KV cache lives in the runtime's process. When that is on another host
        # this machine's RAM says nothing about what fits there, and a clamp built on
        # it would be a number about the wrong computer. `None` reaches
        # `build_profile` as "unknown, so do not clamp".
        ram = total_ram_bytes() if self.source.same_machine else None
        return self._profiles.put(
            key,
            build_profile(
                provider=self.name, model=model, info=info, ram_bytes=ram, tuning=self.tuning(model)
            ),
        )

    def untuned(self, model: str) -> ModelProfile:
        """The profile `model` would have with nothing saved — what "default" means."""
        info = self._infos.get(model)
        if info is None:
            return self.profile(model)
        ram = total_ram_bytes() if self.source.same_machine else None
        return build_profile(provider=self.name, model=model, info=info, ram_bytes=ram, tuning=None)

    def forget_profile(self, model: str) -> None:
        """Drop the profile only — after the model's settings changed, not its weights."""
        self._profiles.forget_where(lambda key: self.adapter.resolves(model, [key[1]]) or key[1] == model)

    def compatibility(self, model: str) -> compat.Compatibility:
        """Whether `model` will run a build on this source, and why not if it won't."""
        from app.router.runtimes import table

        profile = self.profile(model)
        entry = self.entry(model)
        if entry is not None and entry.kind is not None and profile.kind is None:
            # The list said what the model is for even though the profile did not.
            profile = replace(profile, kind=entry.kind)
        meta = table.spec_for(self.source.runtime)
        return compat.assess(
            f"{self.source.id}:{model}",
            profile,
            ram_bytes=total_ram_bytes() if self.source.same_machine else None,
            remote=not self.source.same_machine,
            probed=profile.described or profile.kind is not None,
            window_hint=meta.window_hint,
            kv_hint=meta.kv_hint,
            tuning=self.tuning(model),
            loaded=self._loaded(model),
        )

    def _loaded(self, model: str) -> Optional[bool]:
        """Whether the runtime says `model` is in memory now — from its own list."""
        for listed in self.state().models:
            if self.adapter.resolves(model, [listed.name]):
                return listed.loaded
        return None

    def generation_view(self, model: str) -> dict:
        """What the Settings page needs to tune `model`: every field, what is saved,
        what the server says by default, and which of them this runtime can send."""
        profile = self.profile(model)
        stored = self.tuning(model)
        supported = {
            **{k: k in self.adapter.sampling_supported for k in SAMPLING_KEYS if k != "thinking"},
            **{k: k in self.adapter.machine_supported for k in MACHINE_KEYS},
            # Only ever read by the memory estimate, so every runtime "takes" it.
            "kv_cache_type": True,
            "context_window": True,
            "max_output_tokens": True,
            "reasoning_tokens": True,
        }
        if profile.thinking == THINKS_NONE:
            thinking_options: list[str] = []
        elif profile.thinking == THINKS_ALWAYS:
            thinking_options = [THINKING_ON]
        else:
            fitted = {thinking_for(profile.thinking, s) for s in THINKING_SETTINGS}
            thinking_options = [
                s for s in THINKING_SETTINGS if s in fitted and s in self.adapter.thinking_supported
            ]
        supported["thinking"] = bool(thinking_options)
        base = self.untuned(model)
        return {
            "spec": self.settings_key(model),
            "source": self.source.id,
            "runtime": self.source.runtime,
            "source_label": self.source.label,
            "untuned": {
                "context_window": base.context_window,
                "max_output_tokens": base.max_output_tokens,
                "thinking_level": base.thinking_level,
            },
            #: Machine settings reach a runtime on this computer only.
            "machine_applies": self.source.same_machine,
            "fields": [f.as_dict() for f in generation.FIELDS],
            "values": stored,
            "defaults": dict(profile.defaults),
            "supported": supported,
            "thinking": profile.thinking,
            "thinking_options": thinking_options,
            "thinking_level": profile.thinking_level,
            "profile": profile.as_dict(),
            #: What is sent when a field is left unset and the server reports no
            #: default for it; None means the runtime's own default applies.
            "sent_when_unset": {
                key: (getattr(settings, f"local_{key}") if self.adapter.fills_sampling_defaults else None)
                for key in ("temperature", "top_p")
            },
            "fallbacks": {
                "temperature": settings.local_temperature,
                "top_p": settings.local_top_p,
                "thinking": settings.local_thinking,
                "reasoning_tokens": settings.local_reasoning_tokens,
                "kv_cache_type": settings.local_kv_cache_type,
            },
        }

    # ── generation ───────────────────────────────────────────────────────────
    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        profile = self.profile(model)
        tuning = self.tuning(model)
        sampling = tuning.get(generation.SAMPLING) or {}
        machine = tuning.get(generation.MACHINE) or {}
        # A setting saved for this model is the most specific thing anyone said about
        # it, so it wins over the calling agent's own temperature. Anything unset is
        # left unset, and the runtime's (or the model file's) default applies.
        values = {key: sampling.get(key) for key in SAMPLING_KEYS if key != "thinking"}
        if values["temperature"] is None:
            values["temperature"] = options.temperature
        if thinks(profile.thinking_level):
            # A thinking model decoded greedily repeats itself, sometimes forever —
            # whichever runtime serves it, and whoever asked for temperature 0.
            temperature = values["temperature"]
            if temperature is None:
                temperature = profile.defaults.get("temperature")
            if isinstance(temperature, (int, float)) and temperature <= 0:
                log.warning(
                    "%s thinks, and greedy decoding makes thinking models repeat themselves; "
                    "using temperature %s instead of %s.",
                    model,
                    settings.local_temperature,
                    temperature,
                )
                values["temperature"] = settings.local_temperature
        # GPU layers, threads and how long the model stays loaded are about the
        # computer it runs on, and are this machine's to decide only for this machine.
        local = self.source.same_machine
        request = ChatRequest(
            model=model,
            messages=[m.model_dump() for m in messages],
            # The reply's ceiling, plus what is kept for reasoning: a runtime counts
            # both against the one number, and reasoning comes first.
            max_tokens=options.resolve_max_tokens(profile.max_output_tokens) + profile.reasoning_tokens,
            context_window=profile.context_window,
            **values,
            thinking=profile.thinking_level,
            gpu_layers=machine.get("gpu_layers") if local else None,
            threads=machine.get("threads") if local else None,
            keep_alive=machine.get("keep_alive") if local else None,
            json_schema=options.json_schema,
            json_mode=options.json_mode,
            structured_output=profile.structured_output,
        )
        started = time.perf_counter()
        try:
            self._check_guard()
            result = self.adapter.chat(request)
        except ProviderError as e:
            if e.unreachable:
                self._mark_down(str(e))
            clean = self._scrubbed(e)
            if clean is e:
                raise
            raise clean from None
        latency = int((time.perf_counter() - started) * 1000)
        self._report_unsent(model, result.unsent, sampling, machine)
        profile = self._note_reasoning(model, profile, result.reasoning, result.text, result.finish_reason)

        if result.structured_output_rejected and request.json_schema:
            # Remembered, so the next call asks for what this model takes instead of
            # paying for the refusal again. Only a refused *schema* request says
            # anything about schemas: a call that sent none (the debate asks for plain
            # JSON) and was refused JSON mode must not switch schemas off for every
            # agent after it.
            self._profiles.put(
                (self.source.id, model),
                replace(
                    profile,
                    structured_output=result.structured_output,
                    supports_schema_format=result.structured_output == STRUCTURED_SCHEMA,
                ),
            )
        self._report_limits(result.prompt_tokens, result.finish_reason, model, profile, request)
        return LLMResponse(
            text=result.text,
            provider=self.name,
            model=model,
            usage=Usage(
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.prompt_tokens + result.completion_tokens,
            ),
            latency_ms=latency,
            is_local=profile.is_local,
            reasoning=result.reasoning,
        )

    def _report_unsent(self, model: str, unsent: tuple[str, ...], sampling: dict, machine: dict) -> None:
        """Say — once per model — that a setting someone saved can't reach this runtime.

        Only what somebody set is reported: the configured thinking default reaching
        a runtime with no word for "off" is not news, but a saved seed that never
        arrives is exactly the silent drop the Settings page promises not to make.
        """
        for key in unsent:
            if key not in sampling and key not in machine:
                continue
            if (model, key) in self._reported_unsent:
                continue
            self._reported_unsent.add((model, key))
            log.warning(
                "%s on %s: the saved '%s' setting can't be sent — %s has no way to take it. "
                "Settings shows it as unsupported.",
                model,
                self.source.label,
                key,
                self.source.label,
            )

    def _note_reasoning(
        self,
        model: str,
        profile: ModelProfile,
        reasoning: Optional[str],
        answer: str,
        finish_reason: Optional[str],
    ) -> ModelProfile:
        """Learn from a reply that reasoned when it was not expected to.

        A model the runtime did not describe as thinking — or one told not to that
        did anyway — has no reasoning budget, so its thoughts eat the answer's. Once
        seen, its profile keeps the budget from then on. A reply whose reasoning used
        up every token, leaving no answer at all, is said for what it is.
        """
        if not reasoning:
            return profile
        if not answer.strip() and finish_reason == "length":
            log.warning(
                "%s spent its whole output budget reasoning and never answered. Give it a "
                "larger reasoning budget in Settings, or turn its thinking down.",
                model,
            )
        if thinks(profile.thinking_level) or profile.reasoning_tokens or profile.thinking == THINKS_ALWAYS:
            return profile  # a budget is already kept, or was deliberately set to none
        wanted = self.tuning(model).get(generation.LIMITS, {}).get("reasoning_tokens")
        budget = settings.local_reasoning_tokens if wanted is None else int(wanted)
        # What is *sent* stays as it was: a runtime that says the model cannot think
        # refuses to be told to. Only the budget changes, because it thinks anyway.
        learned = replace(
            profile,
            thinking=THINKS_ALWAYS,
            reasoning_tokens=max(0, min(budget, profile.context_window // 4)),
        )
        log.warning(
            "%s reasoned before answering although it was not asked to. Its replies now keep "
            "%s tokens for reasoning, so the answer is not cut short.",
            model,
            f"{learned.reasoning_tokens:,}",
        )
        return self._profiles.put((self.source.id, model), learned)

    def _report_limits(
        self,
        prompt_tokens: int,
        finish_reason: Optional[str],
        model: str,
        profile: ModelProfile,
        request: ChatRequest,
    ) -> None:
        """Say when a call actually hit a limit, rather than leaving it to be guessed.

        Every character budget upstream rests on an estimate of how many characters
        make a token, and an estimate can be wrong the expensive way. The runtime
        reports what really happened — the prompt's real token count, and why
        generation stopped — so the two failures this whole design exists to make
        visible are read off the response instead of inferred three phases later
        from a schema that did not match.
        """
        if prompt_tokens and prompt_tokens >= profile.context_window * 0.95:
            log.warning(
                "Prompt for %s used %s of a %s-token window — at or past the point a "
                "runtime truncates it or refuses it, which drops the system prompt and "
                "the required output shape with it. Lower APPROX_CHARS_PER_TOKEN "
                "(currently %s) so prompts are budgeted more conservatively.",
                model,
                f"{prompt_tokens:,}",
                f"{profile.context_window:,}",
                settings.approx_chars_per_token,
            )
        if finish_reason == "length":
            log.warning(
                "%s stopped at the %s-token output limit rather than finishing. Its "
                "reply is cut off, so it will not parse as the shape it was asked for. "
                "Raise MAX_OUTPUT_TOKENS (currently %s) or give the model a larger "
                "window.",
                model,
                f"{request.max_tokens:,}",
                settings.max_output_tokens,
            )

    # ── embeddings ───────────────────────────────────────────────────────────
    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        try:
            self._check_guard()
            return self.adapter.embed(model, inputs)
        except ProviderError as e:
            if e.unreachable:
                self._mark_down(str(e))
            clean = self._scrubbed(e)
            if clean is e:
                raise
            raise clean from None

    def cancel(self, request_id: str) -> bool:
        return self.adapter.cancel(request_id)
