"""Hybrid LLM router.

Resolves which (provider, model) to use for a request based on the selection mode, then
runs through a fallback chain so one failing backend never stalls the pipeline. The
user's local default — on whichever source it lives — is always the chain's safety net.

Providers
---------
Two kinds, and the router treats them the same way. The cloud providers are fixed.
Everything else is a **model source**: a local runtime found on loopback, configured
in `.env`, or added in Settings — one provider per source, each speaking the typed
operations of its runtime adapter. Nothing here knows which runtime a source is.

Model references
----------------
Every model is named with its source: `source:model`, or `provider:model` for the
cloud. A name with nothing in front of it is an error, not a guess at a runtime —
with one exception, settings saved before sources existed, which meant one runtime
and are read with that meaning (`table.LEGACY_BARE_RUNTIME`).

Selection modes
---------------
- local_only : only local models are used — any model, from any source, that runs
               on hardware the user controls.
- manual     : use the caller's preferred `source:model`, fall back to the chain.
- auto       : pick by a rough complexity hint, cost, and which providers are configured,
               preferring local for cheap/simple work.

Roles
-----
Above all three sits the **role**: which agent, or which support task, is asking. A
role pointed at its own model runs on it. A role with nothing recorded resolves
exactly as it always did, so a fresh install is unchanged.

Retries
-------
Each link in the chain is attempted more than once before the chain moves on. A local
runtime drops requests while it loads a model, and one dropped request used to end an
eight-phase run. Only failures that could plausibly go differently are retried: a
missing key, or a model that is not there, is reported at once — those are answers,
not hiccups.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core import model_roles, secrets_store
from app.core.config import settings
from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.router.base import CLOUD_PROVIDERS, LLMProvider, ProviderError
from app.router.model_profile import ModelProfile, fallback_profile
from app.router.providers.anthropic_provider import AnthropicProvider
from app.router.providers.gemini_provider import GeminiProvider
from app.router.providers.openai_provider import OpenAIProvider
from app.router.runtimes import table
from app.router.runtimes.provider import STATE_TTL_SECONDS, SourceProvider
from app.router.runtimes.sources import SourceError, SourceRegistry
from app.router.runtimes.types import KIND_EMBEDDING, ModelEntry, writes
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)


#: How the local default was arrived at, for the Settings page to say.
DEFAULT_CHOSEN = "chosen"  # picked in Settings
DEFAULT_CONFIGURED = "configured"  # named in `.env` and found on a source
DEFAULT_DETECTED = "detected"  # the first model that writes, on the first source up


class UnresolvedModel(ValueError):
    """A model reference that does not say which source serves it."""


@dataclass(frozen=True)
class Readiness:
    """Whether a run can actually start, and what to do when it cannot.

    Checked before the run is claimed, naming the model and the role that wants it —
    so a model that is not there, or cannot write, or a runtime that is not running,
    is a sentence before the build rather than a failure inside its first agent.
    """

    ok: bool
    #: One sentence, ready to show. None when everything is in order.
    reason: Optional[str] = None
    #: [{role, model, source}] — the selections the source does not have.
    missing: tuple[dict, ...] = field(default_factory=tuple)
    #: True when no local runtime the run needs is answering.
    unreachable: bool = False
    #: [{role, model, capabilities}] — present, but the runtime says they cannot write.
    incapable: tuple[dict, ...] = field(default_factory=tuple)


class ModelRouter:
    CLOUD_PROVIDERS = CLOUD_PROVIDERS

    def __init__(self) -> None:
        self._cloud: dict[str, LLMProvider] = {
            "anthropic": AnthropicProvider(),
            "openai": OpenAIProvider(),
            "gemini": GeminiProvider(),
        }
        self._default_model: dict[str, str] = {
            "anthropic": settings.anthropic_default_model,
            "openai": settings.openai_default_model,
            "gemini": settings.gemini_default_model,
        }
        self.sources = SourceRegistry()
        #: The local default the user chose in Settings, as `source:model`.
        self._chosen_local: Optional[str] = None
        #: The embedding model found automatically, kept once found. Re-resolving it
        #: on every call switched models whenever a source came or went — and the
        #: vectors of two models in one collection are either an error or, when the
        #: sizes happen to match, search results that are quietly wrong.
        self._embedding_pin: Optional[tuple[tuple[str, str], str]] = None
        # Apply any keys/models saved at runtime via the Settings UI (overrides .env).
        self._load_persisted()

    # ── providers ────────────────────────────────────────────────────────────
    def provider(self, name: str) -> Optional[LLMProvider]:
        if name in self._cloud:
            return self._cloud[name]
        return self.sources.get(name)

    def source(self, name: str) -> Optional[SourceProvider]:
        return self.sources.get(name)

    def default_model(self, provider: str) -> Optional[str]:
        """The model a cloud provider runs when nothing more specific is chosen."""
        return self._default_model.get(provider)

    def is_local_provider(self, name: Optional[str]) -> bool:
        """Whether calls recorded under this provider ran locally, for rows written
        before each call recorded that itself. Only the cloud providers were not."""
        return bool(name) and name not in CLOUD_PROVIDERS

    # ── model references ─────────────────────────────────────────────────────
    def _is_provider_id(self, name: str) -> bool:
        if name in CLOUD_PROVIDERS or table.looks_like_source_id(name):
            return True
        # Loading the configured and saved sources is free (no network), and without
        # it an id derived from an address (`local-8081`) reads as a bare model name
        # in the moment before anything else has asked.
        self.sources.ensure_loaded()
        return name in self.sources.ids()

    def parse(self, spec: str) -> tuple[str, str]:
        """'source:model' -> ('source', 'model'). A bare name is refused, not guessed.

        Only a known provider or source before the first colon counts, so a model
        tag of the form `name:size` is recognised as missing its source rather than
        read as a source called `name` serving a model called `size`.
        """
        text = (spec or "").strip()
        provider, sep, model = text.partition(":")
        provider, model = provider.strip(), model.strip()
        if sep and model and self._is_provider_id(provider):
            return (provider, model)
        raise UnresolvedModel(
            f"'{text}' doesn't say which source serves it. Name a model as "
            "`source:model`, the way the model pickers write it."
        )

    def _saved_pair(self, spec: str) -> tuple[str, str]:
        """Read a saved choice, giving one written before sources their old meaning."""
        try:
            return self.parse(spec)
        except UnresolvedModel:
            return (table.LEGACY_BARE_RUNTIME, spec.strip())

    @staticmethod
    def _spec(pair: tuple[str, str]) -> str:
        return f"{pair[0]}:{pair[1]}"

    def _listed(self, pair: tuple[str, str]) -> tuple[str, str]:
        """`pair`, spelled the way its source lists the model — when the source lists it.

        A default written as `nomic-embed-text` and a list that says
        `nomic-embed-text:latest` are one model to the runtime and two strings to a
        page comparing them, which then marks neither as the default. Every spec the
        pages are handed is spelled the list's way.
        """
        prov = self.sources.get(pair[0])
        if prov is None:
            return pair
        for name in prov.list_models():
            if prov.resolves(pair[1], [name]):
                return (pair[0], name)
        return pair

    # ── the local default ────────────────────────────────────────────────────
    def local_default(self) -> Optional[tuple[str, str]]:
        return self._local_default()[0]

    def _local_default(self) -> tuple[Optional[tuple[str, str]], Optional[str]]:
        """(the local model every role falls back to, how it was arrived at).

        The user's choice first — kept even while its source is down, because a
        choice is a choice, and readiness says what to do about it. Then the name in
        `.env`, wherever a running source has it. Then the first model that can
        write, on the first source that is up: what makes a machine with one runtime
        and no configuration able to build at all.
        """
        if self._chosen_local:
            return self._saved_pair(self._chosen_local), DEFAULT_CHOSEN
        self.sources.ensure()
        hint = (settings.local_default_model or "").strip()
        if hint:
            try:
                pair = self.parse(hint)
                if pair[0] not in CLOUD_PROVIDERS:
                    return pair, DEFAULT_CONFIGURED
            except UnresolvedModel:
                found = self._find(hint)
                if found:
                    return found, DEFAULT_CONFIGURED
        first = self._first_local(lambda entry: writes(entry.kind) is not False)
        return (first, DEFAULT_DETECTED) if first else (None, None)

    def _find(self, model: str) -> Optional[tuple[str, str]]:
        """The first running source that serves a model called `model`."""
        for prov in self.sources.providers():
            names = prov.list_models()
            if names and prov.resolves(model, names):
                return (prov.name, model)
        return None

    def _first_local(self, wanted) -> Optional[tuple[str, str]]:
        """The first local model on any running source that `wanted` accepts.

        A model whose kind is known to fit is preferred to one the runtime says
        nothing about, so an unlabeled embedding model on one runtime does not win
        over a labelled chat model on another.
        """
        unknown: Optional[tuple[str, str]] = None
        for prov in self.sources.providers():
            if not prov.available():
                continue
            for name, entry in prov.describe().items():
                if not entry.is_local or not wanted(entry):
                    continue
                if entry.kind is not None:
                    return (prov.name, name)
                unknown = unknown or (prov.name, name)
        return unknown

    # ── profiles ─────────────────────────────────────────────────────────────
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
            prov = self.provider(pname)
            if prov is not None and prov.available():
                return prov.profile(model)
        if not chain:
            return fallback_profile("none", "unresolved")
        pname, model = chain[0]
        return fallback_profile(pname, model, local=pname not in CLOUD_PROVIDERS)

    # ── runtime provider configuration (Settings UI) ───────────────────────────
    def _apply(
        self, provider: str, api_key: Optional[str], default_model: Optional[str]
    ) -> None:
        """Update one cloud provider's key/model in memory + the shared settings object."""
        prov = self._cloud.get(provider)
        if prov is None:
            return
        if api_key is not None and hasattr(prov, "set_api_key"):
            prov.set_api_key(api_key)
            # Reflect into settings so Auto-mode's `configured_cloud_providers()` sees it.
            setattr(settings, f"{provider}_api_key", api_key or None)
        if default_model:
            self._default_model[provider] = default_model
            setattr(settings, f"{provider}_default_model", default_model)

    def _load_persisted(self) -> None:
        for pname, entry in secrets_store.get_all().items():
            if pname in self._cloud:
                self._apply(pname, entry.get("api_key"), entry.get("default_model"))
        self._chosen_local = secrets_store.get_local_default(CLOUD_PROVIDERS)

    def set_provider_key(
        self,
        provider: str,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
    ) -> None:
        """Set/clear a cloud provider's API key at runtime and persist it, with its model.

        api_key: None = leave unchanged, "" = clear, "sk-..." = set. A local source's
        key is set on the source (`set_source_key`); its model is the local default.
        """
        if provider not in CLOUD_PROVIDERS:
            raise ValueError(
                f"'{provider}' isn't a cloud provider. Expected one of "
                f"{', '.join(CLOUD_PROVIDERS)}; a local source's model is chosen as the "
                "local default."
            )
        self._apply(provider, api_key, default_model)
        secrets_store.set_provider(provider, api_key, default_model)

    def set_default_model(self, provider: str, model: str) -> None:
        """Point a cloud provider, or the local default, at a different model."""
        if not model or not model.strip():
            raise ValueError("Name the model to use — an empty choice is not a choice.")
        if provider in CLOUD_PROVIDERS:
            self.set_provider_key(provider, api_key=None, default_model=model.strip())
        else:
            self.set_local_default(f"{provider}:{model.strip()}")

    def set_local_default(self, spec: str) -> None:
        """Make `source:model` the model every role falls back to, and remember it.

        This is what makes adding a model mean something: add it, choose it, and the
        next phase runs on it — no `.env` edit and no restart.
        """
        if not spec or not spec.strip():
            raise ValueError("Name the model to use — an empty choice is not a choice.")
        pair = self.parse(spec)
        if pair[0] in CLOUD_PROVIDERS:
            raise ValueError(
                f"'{spec}' is a cloud model. The local default has to come from a model "
                "source on hardware you control; cloud models are chosen per agent."
            )
        prov = self.sources.get(pair[0])
        if prov is None:
            raise ValueError(f"No model source is called '{pair[0]}'.")
        self._refuse_if_it_cannot_write(pair, "the default every agent runs on")
        entry = self._entry(prov, pair[1])
        if entry is not None and not entry.is_local:
            raise ValueError(
                f"'{pair[1]}' runs on a hosted service that {prov.source.label} sends it to, "
                "so it can't be the local default — Local builds would refuse it. Pin it to "
                "one agent instead."
            )
        self._chosen_local = self._spec(pair)
        secrets_store.set_local_default(self._chosen_local, CLOUD_PROVIDERS)
        # The window, the parameter count and whether decoding can be schema
        # constrained are all properties of the model, and the model just changed.
        prov.forget()

    def set_role_model(self, role: str, spec: Optional[str]) -> None:
        """Point one role at its own model. Blank puts it back on the default."""
        if spec and spec.strip():
            pair = self.parse(spec.strip())
            if role == model_roles.EMBEDDINGS_ROLE:
                if pair[0] in CLOUD_PROVIDERS:
                    raise ValueError(
                        f"'{spec}' is a cloud model. Embeddings come from a model source — "
                        "choose one of the embedding models a local runtime serves."
                    )
            elif pair[0] not in CLOUD_PROVIDERS:
                self._refuse_if_it_cannot_write(pair, "an agent's model")
            spec = self._spec(pair)
        model_roles.set_role(role, spec)
        if role == model_roles.EMBEDDINGS_ROLE:
            # A choice made — or cleared — is a new answer to "which model embeds".
            self._embedding_pin = None

    def _refuse_if_it_cannot_write(self, pair: tuple[str, str], what: str) -> None:
        """Refuse a model the runtime says cannot write, at the moment it is chosen.

        The pickers never offer one, but they are one client of this API. Accepted
        here, it becomes a setting every later build is refused on — the refusal
        arriving far from the choice that caused it. Only a definite answer refuses;
        a runtime that cannot say lets the choice through, as everywhere.
        """
        prov = self.sources.get(pair[0])
        if prov is None:
            return
        entry = self._entry(prov, pair[1])
        if entry is not None and writes(entry.kind) is False:
            raise ValueError(
                f"'{pair[1]}' can't be {what}: the runtime lists it as "
                f"{', '.join(entry.capabilities or (entry.kind or 'unknown',))}, not completion, "
                "and every agent has to write. Choose a model that writes."
            )

    @staticmethod
    def _entry(prov: SourceProvider, model: str) -> Optional[ModelEntry]:
        """What the source says about `model`, however the runtime spells it."""
        described = prov.describe()
        for name, entry in described.items():
            if prov.resolves(model, [name]):
                return entry
        return prov.entry(model)

    # ── model sources (Settings UI) ──────────────────────────────────────────
    def add_source(
        self,
        base_url: str,
        *,
        label: Optional[str] = None,
        api_key: Optional[str] = None,
        confirm_remote: bool = False,
    ) -> SourceProvider:
        return self.sources.add(base_url, label=label, api_key=api_key, confirm_remote=confirm_remote)

    def remove_source(self, source_id: str) -> None:
        self.sources.remove(source_id)

    def set_source_key(self, source_id: str, api_key: Optional[str]) -> None:
        self.sources.set_key(source_id, api_key)

    def rescan(self) -> None:
        """Look again, now: load what is configured first, then probe, then re-list."""
        self.sources.ensure(max_age=0, wait=True)
        for prov in self.sources.providers():
            prov.invalidate()

    def pull(self, source_id: str, model: str) -> Iterator[dict]:
        """Ask a source to download a model, where its runtime can."""
        prov = self.sources.get(source_id)
        if prov is None:
            raise SourceError(f"No model source is called '{source_id}'.")
        if not prov.adapter.can_download:
            raise SourceError(
                f"{prov.source.label} doesn't download models through its API. "
                f"{table.spec_for(prov.source.runtime).add_model}"
            )
        try:
            yield from prov.adapter.pull(model)
        finally:
            # The model on disk just changed, so whatever was probed about it is
            # stale. In a `finally`, because the usual end is the user closing the
            # tab mid-download — a GeneratorExit no `except Exception` sees.
            prov.forget(model)

    # ── the view the Settings page and the pickers read ──────────────────────
    def provider_settings(self) -> dict:
        """Per-cloud-provider config for the Settings UI (never exposes the raw key)."""
        out: dict = {}
        for name in CLOUD_PROVIDERS:
            prov = self._cloud[name]
            key = getattr(prov, "api_key", None)
            out[name] = {
                "configured": bool(key),
                "available": prov.available(),
                "key_hint": ("…" + key[-4:]) if key and len(key) >= 4 else ("set" if key else None),
                "default_model": self._default_model.get(name),
            }
        return out

    def _local_view(self, *, refresh: bool = False) -> dict:
        """Every source and model, with each model's verdict decided once, here.

        `model_capabilities` is the runtime's own words, for quoting. `cannot_build`
        is the verdict — the pages read the list rather than re-deriving it, because
        two copies of this rule already disagreed once about what an empty
        capability list means. Models are named by spec (`source:model`) throughout.
        Only a source that answered is described: while one is down, a verdict
        remembered from before says nothing about a run that might go elsewhere.
        """
        if refresh:
            self.rescan()
        else:
            self.sources.ensure()
        sources: list[dict] = []
        specs: list[str] = []
        caps: dict[str, list[str]] = {}
        cannot: list[str] = []
        code: list[str] = []
        embedding: list[str] = []
        for prov in self.sources.providers():
            state = prov.state(max_age=0 if refresh else STATE_TTL_SECONDS)
            described = prov.describe() if state.reachable else {}
            rows = []
            for entry in state.models:
                entry = described.get(entry.name, entry)
                spec = f"{prov.name}:{entry.name}"
                verdict = writes(entry.kind)
                rows.append(
                    {
                        "spec": spec,
                        "name": entry.name,
                        "kind": entry.kind,
                        "capabilities": list(entry.capabilities) if entry.capabilities is not None else None,
                        "is_local": entry.is_local,
                        "can_build": verdict is not False,
                    }
                )
                specs.append(spec)
                if entry.capabilities is not None:
                    caps[spec] = list(entry.capabilities)
                if verdict is False:
                    cannot.append(spec)
                if entry.kind == KIND_EMBEDDING:
                    embedding.append(spec)
                elif _looks_like_a_coder(entry.name) and verdict is not False and entry.is_local:
                    # A hosted model is never suggested for the building phases: the
                    # suggestion pins four agents at once, and Local builds refuse it.
                    code.append(spec)
            meta = table.spec_for(prov.source.runtime)
            sources.append(
                {
                    "id": prov.name,
                    "label": prov.source.label,
                    "runtime": prov.source.runtime,
                    "runtime_label": meta.label if prov.source.runtime else None,
                    "base_url": prov.source.base_url,
                    "origin": prov.source.origin,
                    "remote": prov.source.remote,
                    "same_machine": prov.source.same_machine,
                    "reachable": state.reachable,
                    "error": None if state.reachable else state.error,
                    "version": prov.source.version,
                    "models": rows,
                    "can_download": prov.adapter.can_download,
                    "add_model": meta.add_model,
                    "library": meta.library,
                    "home": meta.home,
                    "key_hint": prov.source.key_hint,
                    "removable": prov.source.origin == "added",
                }
            )

        default, origin = self._local_default()
        if default is not None:
            default = self._listed(default)
        default_spec = self._spec(default) if default else None
        # The configured default need not be spelled the way the list spells it
        # (`nomic-embed-text` against `nomic-embed-text:latest`); it is judged by the
        # runtime's naming rule, from what is already known — never probed.
        has_default = False
        profile = None
        if default is not None:
            prov = self.sources.get(default[0])
            if prov is not None and prov.state().reachable and prov.resolves(default[1]):
                has_default = True
                entry = self._entry(prov, default[1])
                if default_spec not in caps and entry is not None and entry.capabilities is not None:
                    caps[default_spec] = list(entry.capabilities)
                if entry is not None and writes(entry.kind) is False and default_spec not in cannot:
                    cannot.append(default_spec)
                if default_spec not in cannot:
                    profile = prov.profile(default[1]).as_dict()
        target, target_origin = self._embedding_target()
        automatic, automatic_origin = self._embedding_target(automatic=True)
        return {
            "sources": sources,
            "unknown": self.sources.unknown,
            "tried": self._tried(),
            "reachable": any(s["reachable"] for s in sources),
            "default_model": default_spec,
            "default_origin": origin,
            "has_default": has_default,
            "profile": profile,
            "models": specs,
            "model_capabilities": caps,
            "cannot_build": cannot,
            "code_models": code,
            "embedding_models": embedding,
            "embedding_model": self._spec(self._listed(target)) if target else None,
            "embedding_origin": target_origin,
            #: What "Automatic" would embed with — not the same as the above when a
            #: model has been chosen, and the Settings row has to say which is which.
            "embedding_automatic": self._spec(self._listed(automatic)) if automatic else None,
            "embedding_automatic_origin": automatic_origin,
        }

    def local_status(self, *, refresh: bool = False) -> dict:
        """Every model source, what it serves, and what the local default can do."""
        return self._local_view(refresh=refresh)

    def role_settings(self) -> dict:
        """Every role, what it is set to, and what could be chosen for it.

        `assigned` is the user's choice (absent = "use the default model"). Choices
        saved before sources existed are shown with the source they always meant.
        """
        view = self._local_view()
        rows = []
        for entry in model_roles.catalogue():
            spec = model_roles.get(entry["role"])
            pair = self._listed(self._saved_pair(spec)) if spec else None
            rows.append(
                {
                    **entry,
                    "assigned": self._spec(pair) if pair else None,
                    "provider": pair[0] if pair else None,
                    "model": pair[1] if pair else None,
                }
            )
        return {
            "roles": rows,
            "default_model": view["default_model"],
            "default_origin": view["default_origin"],
            "local_models": [m for m in view["models"]],
            "sources": [
                {"id": s["id"], "label": s["label"], "reachable": s["reachable"]} for s in view["sources"]
            ],
            "code_models": view["code_models"],
            "model_capabilities": view["model_capabilities"],
            "cannot_build": view["cannot_build"],
            "embedding_models": view["embedding_models"],
            "embedding_model": view["embedding_model"],
            "embedding_origin": view["embedding_origin"],
            "embedding_automatic": view["embedding_automatic"],
            "cloud_models": [
                f"{name}:{self._default_model[name]}"
                for name in CLOUD_PROVIDERS
                if self._cloud[name].available() and self._default_model.get(name)
            ],
        }

    def _tried(self) -> list[str]:
        """Every address a runtime was looked for at and none answered, once each.

        Ports that answered as something else are left out — they did answer — and
        every spelling of loopback is one address, so none is listed twice.
        """
        from app.router.runtimes.detect import same_address

        answered = [u["base_url"] for u in self.sources.unknown]
        configured = [p.source.base_url for p in self.sources.providers() if p.source.origin != "detected"]
        out: list[str] = []
        for url in [*configured, *self.sources.tried]:
            if any(same_address(url, other) for other in [*answered, *out]):
                continue
            out.append(url)
        return out

    def _nothing_local(self) -> Readiness:
        """Readiness when no local model can be resolved at all."""
        up = [p for p in self.sources.providers() if p.available()]
        if not up:
            tried = self._tried()
            where = f" Tried {', '.join(t.replace('http://', '') for t in tried)}." if tried else ""
            return Readiness(
                ok=False,
                unreachable=True,
                reason=(
                    "No local runtime is reachable, so there is no model to run this "
                    f"build on.{where} Start one, add its address in Settings, or give "
                    "this build a cloud provider."
                ),
            )
        labels = ", ".join(p.source.label for p in up)
        return Readiness(
            ok=False,
            reason=(
                f"{labels} {'is' if len(up) == 1 else 'are'} running, but serve"
                f"{'s' if len(up) == 1 else ''} no model that can write. Add one there, "
                "then choose it in Settings."
            ),
        )

    # ── is this run able to start? ───────────────────────────────────────────
    def readiness(
        self,
        mode: RoutingMode = RoutingMode.LOCAL_ONLY,
        preferred_model: Optional[str] = None,
        roles: Optional[Iterable[str]] = None,
    ) -> Readiness:
        """Check the models this run will reach for are actually there, before it starts.

        Only a local source can answer this: it lists what it serves. A cloud model
        cannot be verified without spending a call, so a configured key is as far as
        this goes for those.
        """
        self.sources.ensure()
        # (source, model) -> the role that wants it, so one missing model is
        # reported once however many phases point at it.
        wanted: dict[tuple[str, str], str] = {}
        for role in [*(roles or ()), None]:
            try:
                chain = self._resolve_chain(mode, preferred_model, "medium", role)
            except UnresolvedModel as e:
                return Readiness(ok=False, reason=str(e))
            if not chain:
                return self._nothing_local()
            head = chain[0]
            if head[0] not in CLOUD_PROVIDERS:
                wanted.setdefault(head, role or "the rest of the run")

        if not wanted:
            return Readiness(ok=True)

        by_source: dict[str, list[tuple[str, str]]] = {}
        for (source_id, model), role in wanted.items():
            by_source.setdefault(source_id, []).append((model, role))

        missing: list[dict] = []
        for source_id, items in by_source.items():
            prov = self.sources.get(source_id)
            if prov is None:
                names = ", ".join(f"'{m}'" for m, _ in items)
                return Readiness(
                    ok=False,
                    unreachable=True,
                    reason=(
                        f"This build is set to run on {names} from '{source_id}', a model "
                        "source this backend can't find. Start that runtime, or choose a "
                        "model from one that is running in Settings."
                    ),
                )
            state = prov.state()
            if not state.reachable:
                return Readiness(
                    ok=False,
                    unreachable=True,
                    reason=(
                        f"{prov.source.label} isn't answering at {prov.source.base_url}. "
                        "Start it and try again, choose a model from a runtime that is "
                        "running, or give this build a cloud provider in Settings."
                    ),
                )
            names = [e.name for e in state.models]
            for model, role in items:
                if not prov.resolves(model, names):
                    missing.append({"role": role, "model": model, "source": source_id})
        if missing:
            return self._missing(missing)
        return self._able_to_write(mode, by_source)

    def _missing(self, missing: list[dict]) -> Readiness:
        names = sorted({str(m["model"]) for m in missing})
        listed = ", ".join(f"'{n}'" for n in names)
        prov = self.sources.get(missing[0]["source"])
        label = prov.source.label if prov else missing[0]["source"]
        fix = (
            "Download it on the Settings page"
            if prov is not None and prov.adapter.can_download
            else f"Add it in {label}"
        )
        return Readiness(
            ok=False,
            missing=tuple(missing),
            reason=(
                f"This build is set to run on {listed}, which {label} doesn't have. "
                f"{fix} and start the build again — without it the run fails inside "
                "the first agent."
            ),
        )

    def _able_to_write(
        self, mode: RoutingMode, by_source: dict[str, list[tuple[str, str]]]
    ) -> Readiness:
        """Present is not the same as able to write — or as local.

        Every model here is served; this asks whether each one completes text. The
        pickers already keep such a model out of reach, but they are one client of
        this API — a default set in `.env`, a role pinned through the API, or a
        resume all reach `/run` without passing through them. An unknown answer never
        refuses anything.
        """
        incapable: list[dict] = []
        elsewhere: list[str] = []
        for source_id, items in by_source.items():
            prov = self.sources.get(source_id)
            assert prov is not None
            for model, role in items:
                entry = self._entry(prov, model)
                if entry is None:
                    continue
                if writes(entry.kind) is False:
                    incapable.append(
                        {
                            "role": role,
                            "model": model,
                            "capabilities": list(entry.capabilities or (entry.kind or "",)),
                        }
                    )
                elif mode == RoutingMode.LOCAL_ONLY and not entry.is_local:
                    elsewhere.append(model)
        if incapable:
            first = incapable[0]
            does = ", ".join(first["capabilities"])
            names = sorted({str(m["model"]) for m in incapable})
            listed = ", ".join(f"'{n}'" for n in names)
            return Readiness(
                ok=False,
                incapable=tuple(incapable),
                reason=(
                    f"This build is set to run on {listed}, which cannot write — the runtime "
                    f"lists {'it' if len(names) == 1 else first['model']} as {does}, not "
                    "completion. Every agent has to produce prose, code and JSON, so choose "
                    "a model that writes on the Settings page and start the build again."
                ),
            )
        if elsewhere:
            listed = ", ".join(f"'{n}'" for n in sorted(set(elsewhere)))
            return Readiness(
                ok=False,
                reason=(
                    f"This build runs Local-Only, but {listed} is served by a local runtime "
                    "that sends it to a hosted service to run. Choose a model that runs on "
                    "your own hardware, or switch the build to Auto or Manual."
                ),
            )
        return Readiness(ok=True)

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
        try:
            chain = self._resolve_chain(mode, preferred_model, complexity, role)
        except UnresolvedModel as e:
            raise ProviderError(str(e), retryable=False) from e
        if not chain:
            raise ProviderError(
                self._nothing_local().reason or "No usable model could be resolved.",
                retryable=False,
            )

        attempts: list[dict] = []
        for idx, (pname, model) in enumerate(chain):
            prov = self.provider(pname)
            if prov is None or not prov.available():
                attempts.append({"provider": pname, "model": model, "error": "unavailable"})
                continue
            try:
                resp = self._generate(prov, messages, model, options)
                resp.fallback_used = idx > 0
                resp.attempts = attempts
                if resp.is_local is None:
                    resp.is_local = prov.is_local_model(model)
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

    # ── embeddings ───────────────────────────────────────────────────────────
    def embedding_target(self) -> Optional[tuple[str, str]]:
        return self._embedding_target()[0]

    def _embedding_target(
        self, *, automatic: bool = False
    ) -> tuple[Optional[tuple[str, str]], Optional[str]]:
        """(the model RAG and memory embed with, how it was arrived at).

        Chosen in Settings first; then `EMBEDDING_MODEL`, wherever a running source
        has it; then the first model any running source reports as embedding-only.
        None means no source serves one, and document search and memory are off.
        `automatic` skips the choice, for saying what "Automatic" would pick.

        What is found automatically is kept for as long as its source is known, even
        while it is down: a switch to another model would put that model's vectors
        into collections built with this one's.
        """
        if not automatic:
            spec = model_roles.get(model_roles.EMBEDDINGS_ROLE)
            if spec:
                pair = self._saved_pair(spec)
                if pair[0] not in CLOUD_PROVIDERS:
                    return pair, DEFAULT_CHOSEN
                log.warning(
                    "Embeddings are set to '%s', a cloud model; they come from a local "
                    "source. Choosing one automatically instead.",
                    spec,
                )
        pinned = self._embedding_pin
        if pinned is not None and self._pin_still_holds(pinned):
            return pinned
        found = self._resolve_embedding()
        if found[0] is not None:
            self._embedding_pin = (found[0], found[1] or DEFAULT_DETECTED)
        return found

    def _pin_still_holds(self, pinned: tuple[tuple[str, str], str]) -> bool:
        """Whether the kept embedding model is still the right one to keep.

        Kept while its source is known — even down, so a source that restarts does
        not hand the collections to another model — but not once its runtime no
        longer serves it, and not over the model `EMBEDDING_MODEL` names once that
        appears: that is the stated choice, and what the next restart would pick.
        """
        (source_id, model), origin = pinned
        prov = self.sources.get(source_id)
        if prov is None:
            return False
        state = prov.state()
        if state.reachable and not prov.resolves(model, [e.name for e in state.models]):
            return False
        if origin == DEFAULT_DETECTED and (settings.embedding_model or "").strip():
            configured = self._resolve_embedding(configured_only=True)[0]
            if configured is not None and configured != (source_id, model):
                return False
        return True

    def _resolve_embedding(
        self, *, configured_only: bool = False
    ) -> tuple[Optional[tuple[str, str]], Optional[str]]:
        self.sources.ensure()
        hint = (settings.embedding_model or "").strip()
        if hint:
            try:
                pair = self.parse(hint)
                if pair[0] not in CLOUD_PROVIDERS:
                    return pair, DEFAULT_CONFIGURED
            except UnresolvedModel:
                found = self._find(hint)
                if found:
                    return found, DEFAULT_CONFIGURED
        if configured_only:
            return None, None
        first = self._first_local(lambda entry: entry.kind == KIND_EMBEDDING)
        return (first, DEFAULT_DETECTED) if first else (None, None)

    def embed(self, inputs: list[str]) -> list[list[float]]:
        """Embed `inputs` with the embedding model, on whichever source serves it."""
        if not inputs:
            return []
        target = self.embedding_target()
        if target is None:
            raise ProviderError(
                "No local runtime is serving an embedding model, so document search and "
                "memory are off. Add one to a runtime and it is used automatically.",
                retryable=False,
            )
        prov = self.sources.get(target[0])
        if prov is None or not prov.available():
            raise ProviderError(
                f"The embedding model '{target[1]}' is on '{target[0]}', which isn't answering.",
                unreachable=True,
            )
        return prov.embed(target[1], inputs)

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

        The waits double from the configured start, so a runtime busy loading a model
        gets progressively more room, and the loop is skipped entirely for failures
        that are answers rather than hiccups.
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
        self.sources.ensure()
        providers: dict = {
            name: {
                "available": prov.available(),
                "is_local": False,
                "default_model": self._default_model.get(name),
                "label": name,
            }
            for name, prov in self._cloud.items()
        }
        for prov in self.sources.providers():
            providers[prov.name] = {
                "available": prov.available(),
                "is_local": True,
                "default_model": None,
                "label": prov.source.label,
            }
        return {
            "default_mode": settings.default_routing_mode,
            "providers": providers,
            "fallback_chain": [self._spec(pair) for pair in self._chain_tail()],
        }

    # ── chain resolution ──────────────────────────────────────────────────────
    def _chain_tail(self) -> list[tuple[str, str]]:
        """The configured fallback links, plus the local safety net, in order.

        The local default is appended here rather than also being named in
        `FALLBACK_CHAIN`. Written in both places the two drifted the moment anyone
        changed one, and the first transient failure fell through to an error naming
        a model the user had never chosen.
        """
        pairs = list(settings.fallback_pairs)
        local = self.local_default()
        if local is not None and local not in pairs:
            pairs.append(local)
        return pairs

    def _resolve_chain(
        self,
        mode: RoutingMode,
        preferred_model: Optional[str],
        complexity: str,
        role: Optional[str] = None,
    ) -> list[tuple[str, str]]:
        # Cheap when fresh: a clock read. What makes a source started a moment ago —
        # or configured, before anything else asked — part of the very first chain.
        self.sources.ensure()
        role_spec = model_roles.get(role)
        role_pair = self._saved_pair(role_spec) if role_spec else None

        if mode == RoutingMode.LOCAL_ONLY:
            # Local-Only means local, so a role pointed at a cloud model is not
            # quietly honoured here — nor quietly dropped. The run continues on the
            # local default and says which choice it could not use.
            if role_pair and role_pair[0] in CLOUD_PROVIDERS:
                log.warning(
                    "Role '%s' is set to %s, but this build runs Local-Only, so it will "
                    "use the local default instead. Switch the build to Auto or Manual "
                    "routing to use a cloud model for this role.",
                    role,
                    self._spec(role_pair),
                )
                role_pair = None
            pick = role_pair or self.local_default()
            return [pick] if pick else []

        chain: list[tuple[str, str]] = []

        # The role's own model leads: it is the most specific thing anyone said about
        # this particular call. Manual's project-wide choice comes next, then Auto's.
        if role_pair:
            chain.append(role_pair)
        if mode == RoutingMode.MANUAL and preferred_model:
            # A project row saved before sources existed keeps its old meaning; new
            # ones are refused unprefixed when the project is created.
            pair = self._saved_pair(preferred_model)
            if pair not in chain:
                chain.append(pair)
        elif mode == RoutingMode.AUTO:
            pair = self._auto_pick(complexity)
            if pair and pair not in chain:
                chain.append(pair)

        for pair in self._chain_tail():
            if pair not in chain:
                chain.append(pair)
        return chain

    def _auto_pick(self, complexity: str) -> Optional[tuple[str, str]]:
        """Heuristic primary choice for Auto mode.

        - High complexity + a cloud key available -> strongest configured cloud model.
        - Otherwise prefer the free local default when its source is answering.
        """
        cloud = settings.configured_cloud_providers()
        local = self.local_default()
        local_source = self.sources.get(local[0]) if local else None
        local_up = bool(local_source is not None and local_source.available())

        if complexity == "high" and cloud:
            # Preference order by reasoning strength.
            for pname in CLOUD_PROVIDERS:
                if pname in cloud:
                    return (pname, self._default_model[pname])

        if local_up:
            return local

        if cloud:
            pname = cloud[0]
            return (pname, self._default_model[pname])

        # Nothing configured and no local source up — still return local so the error
        # names the model the run would have used.
        return local


#: Word fragments that suggest a model was trained for code. Matched against the list
#: the user actually has and offered as a dismissible suggestion for the code
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
