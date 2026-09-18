"""Hybrid LLM router.

Resolves which (provider, model) to use for a request based on the selection mode, then
runs through a fallback chain so one failing backend never stalls the pipeline. The local
Ollama model is always the chain's safety net.

Selection modes
---------------
- local_only : only the local Ollama model is ever used.
- manual     : use the caller's preferred `provider:model`, fall back to the chain.
- auto       : pick by a rough complexity hint, cost, and which providers are configured,
               preferring local for cheap/simple work.

Roles
-----
Above all three sits the **role**: which agent, or which support task, is asking. A
user who pulls a second model through Settings can point one role at it, and that
role runs on it — which is the whole reason the download button exists. A role with
nothing recorded resolves exactly as it always did, so a fresh install is unchanged.

Retries
-------
Each link in the chain is attempted more than once before the chain moves on. A local
runtime drops requests while it loads a model, and one dropped request used to end an
eight-phase run. Only failures that could plausibly go differently are retried: a
missing key, or a model that was never pulled, is reported at once — those are
answers, not hiccups.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable, Optional

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core import model_roles
from app.core.config import settings
from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.router.model_profile import ModelProfile, fallback_profile
from app.router.providers.anthropic_provider import AnthropicProvider
from app.router.providers.gemini_provider import GeminiProvider
from app.router.providers.ollama import OllamaProvider
from app.router.providers.openai_provider import OpenAIProvider
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)


@dataclass(frozen=True)
class Readiness:
    """Whether a run can actually start, and what to do when it cannot.

    `available()` only ever asked whether Ollama was *reachable*. `has_model()` sat
    directly below it and was never called on the routing path — so a model the user
    had selected but not pulled sailed past every check and died eight seconds later
    inside the first agent, with a 404 for a message. This is that check, run before
    the run is claimed, naming the model and the role that wants it.
    """

    ok: bool
    #: One sentence, ready to show. None when everything is in order.
    reason: Optional[str] = None
    #: [{role, model}] — the selections that are not pulled.
    missing: tuple[dict, ...] = field(default_factory=tuple)
    #: True when the local runtime itself is not answering.
    unreachable: bool = False


class ModelRouter:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {
            "ollama": OllamaProvider(),
            "anthropic": AnthropicProvider(),
            "openai": OpenAIProvider(),
            "gemini": GeminiProvider(),
        }
        self._default_model: dict[str, str] = {
            "ollama": settings.ollama_default_model,
            "anthropic": settings.anthropic_default_model,
            "openai": settings.openai_default_model,
            "gemini": settings.gemini_default_model,
        }
        # Apply any keys/models saved at runtime via the Settings UI (overrides .env).
        self._load_persisted()

    CLOUD_PROVIDERS = ("anthropic", "openai", "gemini")
    #: Every provider a default model may be chosen for. The local runtime belongs
    #: here and used not to: it was excluded from the only endpoint that could set
    #: one, so a model downloaded through Settings could never be selected at all.
    ALL_PROVIDERS = ("ollama", *CLOUD_PROVIDERS)

    # ── public API ────────────────────────────────────────────────────────────
    def provider(self, name: str) -> Optional[LLMProvider]:
        return self._providers.get(name)

    def default_model(self, provider: str) -> Optional[str]:
        """The model `provider` runs when nothing more specific is chosen.

        The single answer to "what model is this install using?", so anything that
        needs to name one asks here rather than writing a name of its own.
        """
        return self._default_model.get(provider)

    def profile_for(
        self,
        mode: RoutingMode = RoutingMode.LOCAL_ONLY,
        preferred_model: Optional[str] = None,
        complexity: str = "medium",
        role: Optional[str] = None,
    ) -> ModelProfile:
        """The profile of the model this request will most likely land on.

        Callers size their prompts from it, so it resolves the same chain `complete`
        does — including the asking role's own model, or a phase pointed at a 128k
        model would go on being budgeted for the default's 32k.
        """
        chain = self._resolve_chain(mode, preferred_model, complexity, role)
        for pname, model in chain:
            prov = self._providers.get(pname)
            if prov is not None and prov.available():
                return prov.profile(model)
        if not chain:
            return fallback_profile("none", "unresolved")
        pname, model = chain[0]
        head = self._providers.get(pname)
        return fallback_profile(
            pname, model, local=bool(head is not None and head.is_local)
        )

    # ── runtime provider configuration (Settings UI) ───────────────────────────
    def _apply(
        self, provider: str, api_key: Optional[str], default_model: Optional[str]
    ) -> None:
        """Update one provider's key/model in memory + the shared settings object."""
        prov = self._providers.get(provider)
        if prov is None:
            return
        if api_key is not None and hasattr(prov, "set_api_key"):
            prov.set_api_key(api_key)
            # Reflect into settings so Auto-mode's `configured_cloud_providers()` sees it.
            setattr(settings, f"{provider}_api_key", api_key or None)
        if default_model:
            self._default_model[provider] = default_model
            setattr(settings, f"{provider}_default_model", default_model)
            if provider == "ollama" and hasattr(prov, "forget_profile"):
                # The window, the parameter count and whether decoding can be schema
                # constrained are all properties of the model, and the model just
                # changed. A stale probe here budgets every prompt for the previous one.
                prov.forget_profile()

    def _load_persisted(self) -> None:
        from app.core import secrets_store

        for pname, entry in secrets_store.get_all().items():
            if pname in self._providers:
                self._apply(pname, entry.get("api_key"), entry.get("default_model"))

    def set_provider_key(
        self,
        provider: str,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
    ) -> None:
        """Set/clear a provider's API key at runtime and persist it, with its model.

        api_key: None = leave unchanged, "" = clear, "sk-..." = set.

        A key is a cloud-only idea — the local runtime has nothing to authenticate —
        so setting one still refuses any other provider. Choosing a *model* is not a
        cloud-only idea, and used to be refused by the same guard: `PUT
        /providers/ollama` returned 400 whatever it carried, which is why a model
        pulled through Settings could be watched all the way to 100% and then never
        selected. The two halves are separate now, and only the key half is fenced.
        """
        if provider not in self.ALL_PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider}'. Expected one of {', '.join(self.ALL_PROVIDERS)}."
            )
        if api_key is not None and provider not in self.CLOUD_PROVIDERS:
            raise ValueError(
                f"'{provider}' runs on this machine and has no API key to set. "
                f"API keys apply to {', '.join(self.CLOUD_PROVIDERS)}."
            )
        self._apply(provider, api_key, default_model)
        from app.core import secrets_store

        secrets_store.set_provider(provider, api_key, default_model)

    def set_default_model(self, provider: str, model: str) -> None:
        """Point a provider at a different model, and remember it across restarts.

        Valid for the local runtime as much as for the cloud ones. This is what makes
        the download button mean something: pull `llama3.1:8b`, select it, and the
        next phase runs on it — no `.env` edit and no restart.
        """
        if not model or not model.strip():
            raise ValueError("Name the model to use — an empty choice is not a choice.")
        self.set_provider_key(provider, api_key=None, default_model=model.strip())

    def set_role_model(self, role: str, spec: Optional[str]) -> None:
        """Point one role at its own model. Blank puts it back on the default."""
        model_roles.set_role(role, spec)

    def provider_settings(self) -> dict:
        """Per-cloud-provider config for the Settings UI (never exposes the raw key)."""
        out: dict = {}
        for name in self.CLOUD_PROVIDERS:
            prov = self._providers[name]
            key = getattr(prov, "api_key", None)
            out[name] = {
                "configured": bool(key),
                "available": prov.available(),
                "key_hint": ("…" + key[-4:]) if key and len(key) >= 4 else ("set" if key else None),
                "default_model": self._default_model.get(name),
            }
        return out

    def local_status(self) -> dict:
        """Ollama reachability, which models are pulled, and what the default can do."""
        prov = self._providers["ollama"]
        default = self._default_model["ollama"]
        # One tag list and one reachability check for the whole answer. Asking four
        # times over made the Settings page wait four timeouts instead of one whenever
        # Ollama was down — which is exactly when someone is looking at that page.
        reachable = prov.available()
        models = prov.list_models() if reachable and hasattr(prov, "list_models") else []
        base = default.split(":", 1)[0]
        has_default = default in models or any(m.split(":", 1)[0] == base for m in models)
        # The probe is the same one the pipeline runs on, so the window shown here is
        # the window agents will actually get — not a second guess at it.
        profile = prov.profile(default).as_dict() if has_default else None
        return {
            "base_url": getattr(prov, "base_url", settings.ollama_base_url),
            "reachable": reachable,
            "models": models,
            "default_model": default,
            "has_default": has_default,
            "profile": profile,
            #: Pulled models whose name suggests they were trained for code. A hint
            #: for the code phases, offered — never a default the router reaches for,
            #: because a name is not a capability.
            "code_models": [m for m in models if _looks_like_a_coder(m)],
        }

    # ── which model each role runs on ────────────────────────────────────────
    def role_settings(self) -> dict:
        """Every role, what it is set to, and what it would actually run right now.

        `assigned` is the user's choice (absent = "use the default model"); `effective`
        is what the router resolves today. They differ when a choice cannot be honoured
        — a cloud model chosen for a build that runs Local-Only — and reporting both is
        how that stops being a surprise discovered three phases in.
        """
        assigned = model_roles.get_all()
        local = self._providers["ollama"]
        # Asked once. `local_status` 40 lines up documents why: every extra call is
        # another 2-second timeout the Settings page waits through when Ollama is
        # down, which is exactly when somebody is looking at this page.
        models = local.list_models() if hasattr(local, "list_models") else []
        rows = []
        for entry in model_roles.catalogue():
            role = entry["role"]
            spec = assigned.get(role)
            pair = self._parse_pair(spec) if spec else None
            rows.append(
                {
                    **entry,
                    "assigned": spec,
                    "provider": pair[0] if pair else None,
                    "model": pair[1] if pair else None,
                }
            )
        return {
            "roles": rows,
            "default_model": self._default_model["ollama"],
            "local_models": models,
            #: Derived once, here, so the Settings page does not carry a second copy
            #: of the rule that disagreed with this one about `codellama`.
            "code_models": [m for m in models if _looks_like_a_coder(m)],
            "cloud_models": [
                f"{name}:{self._default_model[name]}"
                for name in self.CLOUD_PROVIDERS
                if self._providers[name].available() and self._default_model.get(name)
            ],
        }

    def _role_pair(self, role: Optional[str]) -> Optional[tuple[str, str]]:
        spec = model_roles.get(role)
        return self._parse_pair(spec) if spec else None

    # ── is this run able to start? ───────────────────────────────────────────
    def readiness(
        self,
        mode: RoutingMode = RoutingMode.LOCAL_ONLY,
        preferred_model: Optional[str] = None,
        roles: Optional[Iterable[str]] = None,
    ) -> Readiness:
        """Check the models this run will reach for are actually there, before it starts.

        Only the local runtime can answer this: it lists what is pulled. A cloud model
        cannot be verified without spending a call, so a configured key is as far as
        this goes for those.
        """
        prov = self._providers["ollama"]
        # role -> the local model it would run, deduplicated by model so one missing
        # download is reported once however many phases point at it.
        wanted: dict[str, str] = {}
        for role in [*(roles or ()), None]:
            chain = self._resolve_chain(mode, preferred_model, "medium", role)
            if chain and chain[0][0] == "ollama":
                wanted.setdefault(chain[0][1], role or "the rest of the run")

        if not wanted:
            return Readiness(ok=True)

        if not prov.available():
            base = getattr(prov, "base_url", settings.ollama_base_url)
            return Readiness(
                ok=False,
                unreachable=True,
                reason=(
                    f"The local model runtime isn't answering at {base}. Start Ollama "
                    "and try again, or give this build a cloud provider in Settings."
                ),
            )

        missing = [
            {"role": role, "model": model}
            for model, role in wanted.items()
            if not prov.has_model(model)
        ]
        if not missing:
            return Readiness(ok=True)

        names = sorted({str(m["model"]) for m in missing})
        listed = ", ".join(f"'{n}'" for n in names)
        return Readiness(
            ok=False,
            missing=tuple(missing),
            reason=(
                f"This build is set to run on {listed}, which "
                f"{'has' if len(names) == 1 else 'have'} not been downloaded. "
                "Download it on the Settings page and start the build again — "
                "without it the run fails inside the first agent."
            ),
        )

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        mode: RoutingMode = RoutingMode.LOCAL_ONLY,
        preferred_model: Optional[str] = None,
        options: Optional[GenerationOptions] = None,
        complexity: str = "medium",  # "low" | "medium" | "high"
        role: Optional[str] = None,
    ) -> LLMResponse:
        options = options or GenerationOptions()
        chain = self._resolve_chain(mode, preferred_model, complexity, role)
        if not chain:
            raise ProviderError("No usable model could be resolved for this request.")

        attempts: list[dict] = []
        for idx, (pname, model) in enumerate(chain):
            prov = self._providers.get(pname)
            if prov is None or not prov.available():
                attempts.append(
                    {"provider": pname, "model": model, "error": "unavailable"}
                )
                continue
            try:
                resp = self._generate(prov, messages, model, options)
                resp.fallback_used = idx > 0
                resp.attempts = attempts
                if idx > 0:
                    # The caller sized its prompt against the head of this chain, and
                    # this is not that model. Nothing here can re-size a prompt that
                    # has already been sent, so the least this can do is not let the
                    # substitution pass unrecorded.
                    head = chain[0]
                    log.warning(
                        "Served %s:%s after %s:%s failed. The prompt was budgeted for "
                        "the latter's context window, so it may not have fitted this "
                        "one — check the phase's output before trusting it.",
                        pname,
                        model,
                        head[0],
                        head[1],
                    )
                return resp
            except ProviderError as e:
                log.warning("Provider %s/%s failed: %s", pname, model, e)
                attempts.append({"provider": pname, "model": model, "error": str(e)})

        raise ProviderError(
            "All providers in the routing chain failed. Attempts: "
            + "; ".join(f"{a['provider']}:{a['model']} -> {a['error']}" for a in attempts)
        )

    # ── one link of the chain, attempted more than once ──────────────────────
    @staticmethod
    def _worth_retrying(error: BaseException) -> bool:
        return isinstance(error, ProviderError) and error.retryable

    def _generate(
        self,
        prov: LLMProvider,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        """Call one provider, surviving a transient failure.

        `tenacity` was a declared dependency of this project with zero references in
        it, while a single Ollama hiccup became a `ProviderError`, which `continue_run`
        turned into a failed run — a whole eight-phase build lost to one dropped
        socket. The waits double from the configured start, so a runtime busy loading
        a model gets progressively more room, and the loop is skipped entirely for
        failures that are answers rather than hiccups.
        """
        rounds = max(settings.provider_retry_attempts, 0) + 1
        if rounds <= 1:
            return prov.generate(messages, model, options)

        def announce(state: RetryCallState) -> None:
            log.warning(
                "%s/%s failed (%s); retrying in %.1fs — attempt %d of %d.",
                prov.name,
                model,
                state.outcome.exception() if state.outcome else "unknown",
                getattr(state.next_action, "sleep", 0.0),
                state.attempt_number + 1,
                rounds,
            )

        for attempt in Retrying(
            stop=stop_after_attempt(rounds),
            wait=wait_exponential(
                multiplier=max(settings.provider_retry_backoff_seconds, 0.0),
                max=max(settings.provider_retry_max_backoff_seconds, 0.0),
            ),
            retry=retry_if_exception(self._worth_retrying),
            before_sleep=announce,
            # The provider's own error is what a person needs to read; tenacity's
            # RetryError wrapped around it would reach the UI as "RetryError[...]".
            reraise=True,
        ):
            with attempt:
                return prov.generate(messages, model, options)
        raise ProviderError(f"{prov.name}/{model} did not answer after {rounds} attempts.")

    def status(self) -> dict:
        """Snapshot of provider availability + the configured defaults (for the UI)."""
        return {
            "default_mode": settings.default_routing_mode,
            "providers": {
                name: {
                    "available": prov.available(),
                    "is_local": prov.is_local,
                    "default_model": self._default_model.get(name),
                }
                for name, prov in self._providers.items()
            },
            "fallback_chain": [f"{p}:{m}" for p, m in self._chain_tail()],
        }

    # ── chain resolution ──────────────────────────────────────────────────────
    def _chain_tail(self) -> list[tuple[str, str]]:
        """The configured fallback links, plus the local safety net, in order.

        The local model is appended here rather than also being named in
        `FALLBACK_CHAIN`. Written in both places the two drifted the moment anyone
        changed one: the chain went on pointing at a model that was no longer the
        default and no longer pulled, so the first transient failure fell through to
        an error naming a model the user had never chosen.
        """
        pairs = list(settings.fallback_pairs)
        local = ("ollama", self._default_model["ollama"])
        if local not in pairs:
            pairs.append(local)
        return pairs

    def _resolve_chain(
        self,
        mode: RoutingMode,
        preferred_model: Optional[str],
        complexity: str,
        role: Optional[str] = None,
    ) -> list[tuple[str, str]]:
        role_pair = self._role_pair(role)

        if mode == RoutingMode.LOCAL_ONLY:
            # Local-Only means local, so a role pointed at a cloud model is not
            # quietly honoured here — nor quietly dropped. The run continues on the
            # local default and says which choice it could not use.
            if role_pair and role_pair[0] != "ollama":
                log.warning(
                    "Role '%s' is set to %s:%s, but this build runs Local-Only, so it "
                    "will use %s instead. Switch the build to Auto or Manual routing "
                    "to use a cloud model for this role.",
                    role,
                    role_pair[0],
                    role_pair[1],
                    self._default_model["ollama"],
                )
                role_pair = None
            return [role_pair or ("ollama", self._default_model["ollama"])]

        chain: list[tuple[str, str]] = []

        # The role's own model leads: it is the most specific thing anyone said about
        # this particular call. Manual's project-wide choice comes next, then Auto's.
        if role_pair:
            chain.append(role_pair)
        if mode == RoutingMode.MANUAL and preferred_model:
            pair = self._parse_pair(preferred_model)
            if pair not in chain:
                chain.append(pair)
        elif mode == RoutingMode.AUTO:
            pair = self._auto_pick(complexity)
            if pair not in chain:
                chain.append(pair)

        for pair in self._chain_tail():
            if pair not in chain:
                chain.append(pair)
        return chain

    def _auto_pick(self, complexity: str) -> tuple[str, str]:
        """Heuristic primary choice for Auto mode.

        - High complexity + a cloud key available -> strongest configured cloud model.
        - Otherwise prefer the free local model when Ollama is reachable.
        """
        cloud = settings.configured_cloud_providers()
        local_up = self._providers["ollama"].available()

        if complexity == "high" and cloud:
            # Preference order by reasoning strength.
            for pname in ("anthropic", "openai", "gemini"):
                if pname in cloud:
                    return (pname, self._default_model[pname])

        if local_up:
            return ("ollama", self._default_model["ollama"])

        if cloud:
            pname = cloud[0]
            return (pname, self._default_model[pname])

        # Nothing configured and Ollama down — still return local so the error is clear.
        return ("ollama", self._default_model["ollama"])

    @staticmethod
    def _parse_pair(spec: str) -> tuple[str, str]:
        """'anthropic:claude-opus-4-8' -> ('anthropic', 'claude-opus-4-8').

        A bare name is a local model — the rule `FALLBACK_CHAIN` already used, and the
        one the Settings dropdown writes. Only a *known* provider before the colon
        counts as one, so a local tag of the form `name:size` keeps its colon rather
        than reading as a provider called `name` serving a model called `size`.
        """
        provider, _, model = spec.partition(":")
        provider, model = provider.strip(), model.strip()
        if model and provider in ModelRouter.ALL_PROVIDERS:
            return (provider, model)
        return ("ollama", spec.strip())


#: Word fragments that suggest a model was trained for code. Matched against the list
#: the user has actually pulled and offered as a dismissible suggestion for the code
#: phases — never consulted when routing, because a name is a marketing decision and
#: not a capability. Fragments rather than model names on purpose: a table of specific
#: models is a table that is wrong the week after it is written, and would put a
#: hardcoded model name back into the routing layer by the back door.
_CODER_HINTS = ("coder", "code", "codestral", "starcoder", "devstral")


def _looks_like_a_coder(model: str) -> bool:
    name = model.lower()
    return any(hint in name for hint in _CODER_HINTS)


# Shared singleton used across the app.
router = ModelRouter()
