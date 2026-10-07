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

Accounts
--------
Each account has its own router (`routers.for_user`): its own cloud keys, its own
added sources, its own choice of model per role and per-model settings, and its own
caches — a profile probed for one account is never served to another, and an
override one account sets never reaches another's builds. `router`, the name the
agents import, resolves to the router of whichever account the work is bound to
(`app.core.identity`), and refuses to guess when none is.

Retries
-------
Each link in the chain is attempted more than once before the chain moves on. A local
runtime drops requests while it loads a model, and one dropped request used to end an
eight-phase run. Only failures that could plausibly go differently are retried: a
missing key, or a model that is not there, is reported at once — those are answers,
not hiccups.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core import identity, keyerrors, model_roles, model_settings, secrets_store
from app.core.config import settings
from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.router import compat, keycheck
from app.router.keycheck import KeyCheck
from app.connector.remote import ConnectorProvider, DeviceSources, looks_like_device_source
from app.router.base import CLOUD_PROVIDERS, ComputerDisconnected, LLMProvider, ProviderError, RequestCancelled
from app.router.model_profile import FILES_ALL, ModelProfile, fallback_profile
from app.router.providers.anthropic_provider import AnthropicProvider
from app.router.providers.gemini_provider import GeminiProvider
from app.router.providers.openai_provider import OpenAIProvider
from app.router.runtimes import hygiene, table
from app.router.runtimes.provider import STATE_TTL_SECONDS, SourceProvider
from app.router.runtimes.sources import SourceError, SourceRegistry
from app.router.runtimes.types import KIND_EMBEDDING, ModelEntry, writes
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse

log = get_logger(__name__)
#: Keys added, replaced, removed and checked — with the outcome, never the key.
audit = get_logger("app.audit")


#: Longer than any model name a runtime lists; a spec past this is refused unread.
_MAX_SPEC_CHARS = 512
#: The most files a person may ask one code-writing call for, short of "all".
_MAX_FILES_PER_CALL = 50

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
    #: The compatibility check of every local model the run will use — each with its
    #: verdict (fits, degraded, blocked, unknown), reasons, and the roles using it.
    checks: tuple[dict, ...] = field(default_factory=tuple)


class ModelRouter:
    CLOUD_PROVIDERS = CLOUD_PROVIDERS

    def __init__(self, user_id: Optional[str] = None, *, owner: bool = True) -> None:
        """A router for one account — or, with no `user_id`, for the settings files
        from before accounts, which is what the tests and the migration read.

        `owner` is whether this account owns the install. Only the owner's router
        starts from the keys in `.env`: whoever wrote that file runs this backend,
        and its keys are theirs to spend, not every account's.
        """
        self.user_id = user_id
        self.owner = owner
        if user_id:
            self._secrets = secrets_store.for_user(user_id)
            self._roles = model_roles.for_user(user_id)
            self.tuning = model_settings.for_user(user_id)
        else:
            # The modules' own functions: the global files, read through their
            # module-level paths, so a test that points one elsewhere is honoured.
            self._secrets = secrets_store.default_store
            self._roles = model_roles
            self.tuning = model_settings
        self._cloud: dict[str, LLMProvider] = {
            "anthropic": AnthropicProvider(),
            "openai": OpenAIProvider(),
            "gemini": GeminiProvider(),
        }
        if not owner:
            for prov in self._cloud.values():
                prov.set_api_key(None)
        self._default_model: dict[str, str] = {
            "anthropic": settings.anthropic_default_model,
            "openai": settings.openai_default_model,
            "gemini": settings.gemini_default_model,
        }
        self.sources = SourceRegistry(self._secrets, self.tuning, may_add_local=owner)
        #: This account's paired computers: their runtimes are sources too.
        self.devices = DeviceSources(user_id, self.tuning)
        self.sources.devices = self.devices
        #: The local default the user chose in Settings, as `source:model`.
        self._chosen_local: Optional[str] = None
        #: Each cloud key's last check, and the providers whose saved key can't be
        #: decrypted (kept in the file, not used).
        self._checks: dict[str, KeyCheck] = {}
        #: Checks of a key against a model other than its default — the one a role or a
        #: Manual build chose. In memory: the saved standing is about the default.
        self._model_checks: dict[tuple[str, str], KeyCheck] = {}
        self._locked: set[str] = set()
        self._check_lock = threading.Lock()
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
        if name in CLOUD_PROVIDERS or table.looks_like_source_id(name) or looks_like_device_source(name):
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
        # The model chosen on the account's own computer, on the Setup tab — what a
        # hosted build with no model on the server runs on.
        device = self.devices.chat_choice()
        if device is not None:
            return device, DEFAULT_CHOSEN
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
        pin: Optional[str] = None,
    ) -> ModelProfile:
        """The profile of the model this request will most likely land on.

        Callers size their prompts from it, so it resolves the same chain `complete`
        does — including the asking role's own model, or a phase pointed at a 128k
        model would go on being budgeted for the default's 32k.
        """
        chain = self._pinned(self._resolve_chain(mode, preferred_model, complexity, role), pin)
        for pname, model in chain:
            prov = self.provider(pname)
            if prov is not None and prov.available():
                return prov.profile(model)
        if not chain:
            return fallback_profile("none", "unresolved")
        pname, model = chain[0]
        return fallback_profile(pname, model, local=pname not in CLOUD_PROVIDERS)

    def _pinned(self, chain: list[tuple[str, str]], pin: Optional[str]) -> list[tuple[str, str]]:
        """`chain` with `pin` tried first — the fix loop's stronger model."""
        if not pin:
            return chain
        try:
            pair = self._saved_pair(pin)
        except Exception:  # noqa: BLE001 - a pin that no longer resolves is ignored
            return chain
        return [pair, *(p for p in chain if p != pair)]

    def strongest_for(
        self,
        mode: RoutingMode = RoutingMode.LOCAL_ONLY,
        preferred_model: Optional[str] = None,
        role: Optional[str] = None,
    ) -> Optional[str]:
        """The most capable model this call could run on, when it is not already on it.

        For the fix loop's last round. Chosen by what the router knows, never by name:
        where the routing mode allows the cloud, the strongest configured cloud model
        in the router's own reasoning-strength order; otherwise the largest model the
        same local source serves that can write an answer. None when nothing
        reachable is stronger than what the call would use anyway.
        """
        try:
            chain = self._resolve_chain(mode, preferred_model, "high", role)
        except Exception:  # noqa: BLE001 - no pick is not an error here
            return None
        current = None
        for pname, model in chain:
            prov = self.provider(pname)
            if prov is not None and prov.available():
                current = (pname, model)
                break
        # The cloud only where the router was already free to choose it: Auto, with no
        # model pinned to this role. A Manual build or a role someone pointed at a
        # local model is a decision about where the code goes, and a fix round does
        # not get to overrule it.
        if mode == RoutingMode.AUTO and not self._roles.get(role):
            for pname in CLOUD_PROVIDERS:
                model = self._default_model.get(pname) or ""
                if model and self._cloud[pname].available() and not self._refuses(pname, model):
                    return None if (pname, model) == current else self._spec((pname, model))
        if current is None or current[0] in CLOUD_PROVIDERS:
            return None
        src = self.sources.get(current[0])
        if src is None or not src.available():
            return None
        now = src.entry(current[1])
        floor = (now.size_bytes or 0) if now is not None else 0
        bigger = sorted(
            (
                e
                for e in src.entries()
                if e.size_bytes
                and e.size_bytes > floor
                and e.name != current[1]
                and src.writes(e.name) is not False
                and (e.kind or "chat") not in ("embedding", "base")
            ),
            key=lambda e: e.size_bytes,
            reverse=True,
        )
        # Largest first, but only one this machine can actually run: a model the Start
        # check would block fails to load, and the call falls back with a prompt sized
        # for the model it could not reach.
        for entry in bigger[:4]:
            try:
                if src.compatibility(entry.name).level == "fits":
                    return self._spec((current[0], entry.name))
            except Exception:  # noqa: BLE001 - an unanswerable check is a no
                continue
        return None

    # ── runtime provider configuration (Settings UI) ───────────────────────────
    def _apply(
        self, provider: str, api_key: Optional[str], default_model: Optional[str]
    ) -> None:
        """Update one cloud provider's key/model on this account's router.

        Never written into the shared settings object: that is every account's, and a
        key set there by one would be spent by all.
        """
        prov = self._cloud.get(provider)
        if prov is None:
            return
        if api_key is not None and hasattr(prov, "set_api_key"):
            prov.set_api_key(api_key)
        if default_model:
            self._default_model[provider] = default_model

    def _load_persisted(self) -> None:
        for pname, entry in self._secrets.get_all().items():
            if pname in self._cloud:
                if entry.get("locked"):
                    self._locked.add(pname)
                self._apply(pname, entry.get("api_key"), entry.get("default_model"))
                found = KeyCheck.from_dict(entry.get("check"))
                prov = self._cloud[pname]
                # A verdict is about one key: a key from `.env` changed since, or a
                # different key saved by hand, starts unchecked.
                if found is not None and prov.secret() and found.key_id == keycheck.key_id(prov.secret()):
                    self._remember(pname, found)
        self._chosen_local = self._secrets.get_local_default(CLOUD_PROVIDERS)

    # ── is each cloud key any good ───────────────────────────────────────────
    def _remember(self, provider: str, found: Optional[KeyCheck], *, persist: bool = False) -> None:
        """Take `found` as this provider's key's standing — routing reads it from here."""
        prov = self._cloud[provider]
        if found is not None and found.key_id and found.key_id != keycheck.key_id(prov.secret() or ""):
            # About a key that has since been replaced — a re-check that finished after
            # a new key was saved. The new key's standing is not this one's.
            return
        if found is None or found.key_id is None or found.status in keycheck.KEY_REJECTED:
            # A new key, no key, or a verdict on the key itself: every per-model check
            # of the old standing is moot.
            for pair in [p for p in self._model_checks if p[0] == provider]:
                self._model_checks.pop(pair, None)
        elif found.model:
            # The standing is now a fresh check of this model; an older one gives way.
            self._model_checks.pop((provider, found.model), None)
        if found is None:
            self._checks.pop(provider, None)
            prov.usable = True
        else:
            self._checks[provider] = found
            # Rejected, no credit, unreadable: the key. A model it can't use: that
            # model only (`_refuses`), not every other one it can.
            prov.usable = found.status not in keycheck.KEY_REJECTED
            if found.context_tokens and found.model:
                prov.known_context[found.model] = found.context_tokens
        if persist:
            self._secrets.set_check(provider, found.to_dict() if found else None)

    def _refuses(self, provider: str, model: str) -> Optional[KeyCheck]:
        """The check that rules out `provider:model`, if one does."""
        if provider not in self._cloud or not self._cloud[provider].has_key:
            return None
        found = self.key_check(provider)
        if found.status in keycheck.KEY_REJECTED:
            return found
        if found.status == keycheck.MODEL_UNAVAILABLE and found.model == model:
            return found
        other = self._model_checks.get((provider, model))
        if (
            other is not None
            and other.status == keycheck.MODEL_UNAVAILABLE
            and other.key_id == keycheck.key_id(self._cloud[provider].secret() or "")
            and other.age_seconds() <= settings.key_recheck_seconds
        ):
            return other
        return None

    def _check_chosen_model(self, provider: str, model: str) -> None:
        """Before a run: check the key against a model it leads with that isn't the
        provider's default — the model actually chosen. Kept for the re-check interval."""
        prov = self._cloud.get(provider)
        standing = self.key_check(provider) if prov is not None else None
        if prov is None or not prov.has_key or standing is None or standing.model == model:
            return
        kid = keycheck.key_id(prov.secret() or "")
        known = self._model_checks.get((provider, model))
        if known is not None and known.key_id == kid and known.age_seconds() <= settings.key_recheck_seconds:
            return
        found = keycheck.check(provider, prov.secret() or "", model)
        self._model_checks[(provider, model)] = found
        if found.status in keycheck.KEY_REJECTED:
            # About the key, not the model: that is its standing everywhere.
            try:
                self._remember(provider, found, persist=True)
            except Exception:  # noqa: BLE001 - the verdict still holds in memory
                self._remember(provider, found)

    def key_check(self, provider: str) -> KeyCheck:
        """The standing of a provider's key — unchecked if it has never been checked."""
        prov = self._cloud[provider]
        if not prov.has_key:
            if provider in self._locked:
                return KeyCheck(
                    status=keycheck.LOCKED,
                    reason="locked",
                    message=(
                        "The saved key can't be decrypted — it was saved under a different "
                        "encryption key. Restore that key (SECRETS_ENCRYPTION_KEY or its key "
                        "file), or enter the API key again."
                    ),
                )
            return KeyCheck(status="none", reason="no_key", message="No key saved.")
        found = self._checks.get(provider)
        if found is None or found.key_id != keycheck.key_id(prov.secret() or ""):
            return keycheck.unchecked(prov.secret(), self._default_model.get(provider))
        return found

    def _cloud_name(self, provider: str) -> None:
        if provider not in CLOUD_PROVIDERS:
            raise ValueError(
                f"'{provider}' isn't a cloud provider. Expected one of {', '.join(CLOUD_PROVIDERS)}."
            )

    def save_provider_key(
        self, provider: str, api_key: Optional[str] = None, default_model: Optional[str] = None
    ) -> dict:
        """Save from Settings: check first, then keep whichever key works.

        - A new key is checked against the model it will be used with. One the provider
          rejects is never saved; one that authenticates but can't be used (no credit,
          model not there) never replaces a key that works.
        - A new model with the current key is checked too, and one the key can't use is
          not switched to — the models it can use come back instead.
        - A key the provider couldn't be reached to check is saved, marked unverified.

        Returns `{applied, check}`: whether the change was made, and what the check said.
        """
        self._cloud_name(provider)
        prov = self._cloud[provider]
        key = (api_key or "").strip() or None
        model = (default_model or "").strip() or None
        if api_key == "":
            self.remove_provider_key(provider)
            return {"applied": True, "check": None}
        if api_key is not None and key is None:
            raise ValueError("That key is blank. Paste the whole key, or use Remove to delete the saved one.")
        if key is not None:
            from app.router.runtimes.sources import SourceError, clean_key

            try:
                key = clean_key(key)
            except SourceError as e:
                raise ValueError(str(e)) from e
        target = model or self._default_model.get(provider) or ""
        if key is None and model is None:
            return {"applied": False, "check": None}
        if key is None and not prov.has_key:
            # A model and no key: nothing to check it with yet.
            self.set_provider_key(provider, default_model=model)
            return {"applied": True, "check": None}

        replacing = prov.has_key
        found = keycheck.check(provider, key or prov.secret() or "", target)
        current = self.key_check(provider) if replacing else None
        current_works = bool(current and not current.rejected)

        if key is not None:
            if found.status == keycheck.INVALID or (found.rejected and current_works):
                audit.info(
                    "key refused: provider=%s account=%s outcome=%s/%s (the %s key is kept)",
                    provider, self.user_id, found.status, found.reason, "current" if replacing else "no",
                )
                return {"applied": False, "check": found.to_dict()}
            self.set_provider_key(provider, api_key=key, default_model=model)
            self._remember(provider, found, persist=True)
            audit.info(
                "key %s: provider=%s account=%s outcome=%s/%s",
                "replaced" if replacing else "added", provider, self.user_id, found.status, found.reason,
            )
            return {"applied": True, "check": found.to_dict()}

        # The current key, a new model.
        if found.status == keycheck.MODEL_UNAVAILABLE:
            audit.info("model refused: provider=%s account=%s model=%s", provider, self.user_id, target)
            return {"applied": False, "check": found.to_dict()}
        self.set_provider_key(provider, default_model=model)
        self._remember(provider, found, persist=True)
        audit.info("key checked: provider=%s account=%s outcome=%s/%s", provider, self.user_id, found.status, found.reason)
        return {"applied": True, "check": found.to_dict()}

    def recheck_provider_key(self, provider: str) -> KeyCheck:
        """Check the saved key again, now, against its default model."""
        self._cloud_name(provider)
        prov = self._cloud[provider]
        if not prov.has_key:
            return self.key_check(provider)
        found = keycheck.check(provider, prov.secret() or "", self._default_model.get(provider) or "")
        self._remember(provider, found, persist=True)
        audit.info("key checked: provider=%s account=%s outcome=%s/%s", provider, self.user_id, found.status, found.reason)
        return found

    def remove_provider_key(self, provider: str) -> None:
        self._cloud_name(provider)
        self.set_provider_key(provider, api_key="")
        self._locked.discard(provider)
        self._remember(provider, None)
        audit.info("key removed: provider=%s account=%s", provider, self.user_id)

    def _cloud_reach(
        self, mode: RoutingMode, preferred_model: Optional[str], roles: Optional[Iterable[str]]
    ) -> list[str]:
        """The cloud providers a run may call: those in its chains — and, in Auto, any
        it holds a key for, since high-complexity phases go to whichever is up."""
        reach: list[str] = []
        if mode == RoutingMode.AUTO:
            reach += [n for n in CLOUD_PROVIDERS if self._cloud[n].has_key]
        for role in [*(roles or ()), None]:
            try:
                chain = self._resolve_chain(mode, preferred_model, "medium", role)
            except UnresolvedModel:
                continue
            reach += [p for p, _ in chain if p in CLOUD_PROVIDERS]
        return list(dict.fromkeys(reach))

    def recheck_stale(self, providers: Iterable[str]) -> None:
        """Before a build: check again any of these keys whose standing is old or
        unsettled, or that a build saw rejected. In parallel, each bounded."""
        due = []
        for name in dict.fromkeys(providers):
            if name not in self._cloud or not self._cloud[name].has_key:
                continue
            found = self.key_check(name)
            if (
                found.status in keycheck.UNSETTLED
                or found.during_build
                or found.reason == keycheck.REJECTED_DURING_BUILD
                or found.age_seconds() > settings.key_recheck_seconds
            ):
                due.append(name)
        if not due:
            return
        with self._check_lock:
            threads = [threading.Thread(target=self.recheck_provider_key, args=(n,), daemon=True) for n in due]
            for t in threads:
                t.start()
            # A check is up to three requests (model, one token, the model list), and
            # the timeout bounds each stage of each; wait for all of that.
            bound = max(settings.key_check_timeout_seconds, 1)
            for t in threads:
                t.join(timeout=3 * 3 * bound + 5)

    def _note_failure(self, provider: str, model: str, error: ProviderError) -> None:
        """A build's own call to a cloud provider failed: if the answer condemns the
        key, say so in Settings and stop routing to it, instead of falling back quietly."""
        prov = self._cloud.get(provider)
        if prov is None or not prov.has_key:
            return
        kind = getattr(error, "kind", None)
        if kind is not None:
            found = keycheck.verdict_for(provider, kind, model)
        else:
            found = keycheck.verdict_from_error(provider, getattr(error, "status", None), str(error), model)
        if found is None:
            return
        found.key_id = keycheck.key_id(prov.secret() or "")
        found.model = model
        log.warning("%s's key was %s during a build; it won't be used until it passes a check.", provider, found.status)
        try:
            self._remember(provider, found, persist=True)
        except Exception as e:  # noqa: BLE001 - never at the expense of the fallback chain
            log.warning("Couldn't save that verdict (%s); it holds until restart.", type(e).__name__)
            self._remember(provider, found)

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
        # Written first: a write that fails (an unreadable file) must not leave the
        # key it refused to save live in memory.
        self._secrets.set_provider(provider, api_key, default_model)
        self._apply(provider, api_key, default_model)
        if api_key is not None:
            for pair in [p for p in self._model_checks if p[0] == provider]:
                self._model_checks.pop(pair, None)

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
        chosen = self._spec(pair)
        # Written first, so a choice the file refused is not in use either.
        self._secrets.set_local_default(chosen, CLOUD_PROVIDERS)
        self._chosen_local = chosen
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
        self._roles.set_role(role, spec)
        if role == model_roles.EMBEDDINGS_ROLE:
            # A choice made — or cleared — is a new answer to "which model embeds".
            self._embedding_pin = None

    # ── how many files a code phase writes per call (#81) ────────────────────
    def files_per_call_choice(self, role: Optional[str]) -> object:
        """The person's choice for `role` — a count or `"all"` — or None for automatic."""
        if role not in model_roles.FILES_PER_CALL_ROLES:
            return None
        value = self._roles.option(role, "files_per_call")
        if value == FILES_ALL:
            return FILES_ALL
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    def set_files_per_call(self, role: str, value: object) -> None:
        """Save how many files `role` writes per call; None puts it back on automatic."""
        if role not in model_roles.FILES_PER_CALL_ROLES:
            raise ValueError(f"'{role}' doesn't write code file by file.")
        if value is not None and value != FILES_ALL:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_FILES_PER_CALL:
                raise ValueError(
                    f"Files per call is a whole number from 1 to {_MAX_FILES_PER_CALL}, or \"all\"."
                )
        self._roles.set_option(role, "files_per_call", value)

    def _automatic_files_per_call(self, role: str, pair: Optional[tuple[str, str]]) -> Optional[int]:
        """What the budgets alone choose for the model this role's picker shows — its own
        model, or the local default its "Default" option names. A hint for the page:
        every run sizes its batches from the model it actually lands on."""

        mode = RoutingMode.AUTO if pair and pair[0] in CLOUD_PROVIDERS else RoutingMode.LOCAL_ONLY
        try:
            return self.profile_for(mode, None, "high", role).files_per_call
        except Exception:  # noqa: BLE001 - nothing resolvable is "no automatic answer"
            return None

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

    def is_shared(self, prov: SourceProvider) -> bool:
        """Whether `prov` is the server's runtime and this account doesn't own the install.

        Judged by address as well as origin: setting a key on a detected source makes
        it "added" for that account, and it is still the server's runtime afterwards.
        """
        if self.owner:
            return False
        from app.router.runtimes.detect import reaches_server_network

        return prov.source.origin != "added" or reaches_server_network(prov.source.base_url)

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
        if self.is_shared(prov):
            raise SourceError(
                f"{prov.source.label} runs on the machine this backend is on, and only the "
                "account that owns this install can download models onto it."
            )
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
        """Per-cloud-provider config for the Settings UI.

        Write-only: whether a key is set, its last four characters, and its last check —
        status, our own sentence, when, and the models it can use. Never the key.
        """
        out: dict = {}
        for name in CLOUD_PROVIDERS:
            prov = self._cloud[name]
            found = self.key_check(name)
            out[name] = {
                "configured": prov.has_key,
                "available": prov.available(),
                "key_hint": prov.key_hint(),
                "default_model": self._default_model.get(name),
                "status": found.status,
                "reason": found.reason,
                "message": found.message,
                "checked_at": found.checked_at or None,
                "checked_model": found.model,
                "models": list(found.models),
                "during_build": found.during_build,
                # The badge, sentence and one action for a refused key (#63).
                "advice": self._advice(name, found),
            }
        return out

    @staticmethod
    def _advice(provider: str, found: KeyCheck) -> Optional[dict]:
        kind = keycheck.kind_of(found)
        if kind is None or found.status not in (keycheck.INVALID, keycheck.BILLING, keycheck.RATE_LIMITED, keycheck.UNVERIFIED):
            return None
        return keyerrors.advice(kind, provider).as_dict()

    def store_error(self) -> Optional[str]:
        """Whether the settings file can be read — asked of the file now, not remembered."""
        self._secrets._read()
        return self._secrets.error

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
                    "warnings": hygiene.warnings(
                        prov.source.runtime,
                        prov.source.version,
                        # Which addresses this server listens on is the owner's to see:
                        # on a shared install, another account can't act on it anyway.
                        prov.source.exposed if self.sources.may_add_local or prov.source.origin == "connector" else None,
                    ),
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
            spec = self._roles.get(entry["role"])
            pair = self._listed(self._saved_pair(spec)) if spec else None
            row = {
                **entry,
                "assigned": self._spec(pair) if pair else None,
                "provider": pair[0] if pair else None,
                "model": pair[1] if pair else None,
            }
            if entry["role"] in model_roles.FILES_PER_CALL_ROLES:
                row["files_per_call"] = self.files_per_call_choice(entry["role"])
                row["files_per_call_auto"] = self._automatic_files_per_call(entry["role"], pair)
            rows.append(row)
        return {
            "roles": rows,
            # Warden reviewing on the very model that wrote the code (#77): shown as a
            # one-line note, since the scanners now carry the verdicts and the model's
            # review is an opinion — a second model makes it a second opinion.
            "auditor_shares_builders": self._auditor_shares_builders(rows, view["default_model"]),
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
                if self._cloud[name].available()
                and self._default_model.get(name)
                and not self._refuses(name, self._default_model[name])
            ],
        }

    @staticmethod
    def _auditor_shares_builders(rows: list[dict], default_model: Optional[str]) -> bool:
        """Whether the security review runs, unchosen, on the builders' own model.

        Only when nobody chose a model for Warden: a choice, even of the same model, is
        a decision made with this in view. The builders are compared by what they run
        on — their own choice, or the default everyone without one shares.
        """
        from app.core.constants import Phase

        by_role = {r["role"]: r for r in rows}
        warden = by_role.get(Phase.SECURITY_ENGINEER.value)
        if warden is None or warden.get("assigned") or not default_model:
            return False
        builders = (Phase.BACKEND_ENGINEER.value, Phase.FRONTEND_ENGINEER.value)
        return all((by_role.get(b) or {}).get("assigned") in (None, default_model) for b in builders)

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
        *,
        recheck_keys: bool = False,
    ) -> Readiness:
        """Check the models this run will reach for are actually there, before it starts.

        A local source lists what it serves. A cloud key is checked again here when its
        last check is old, unsettled, or a build saw it rejected — so a key revoked at
        the provider is caught before the first phase, not inside it. Only when a run
        is actually starting (`recheck_keys`): the composer's preflight asks this on
        every change, and reads the verdict it has rather than spending a call.
        """
        self.sources.ensure()
        if recheck_keys and mode != RoutingMode.LOCAL_ONLY:
            # Before the chains are resolved, so Auto's choice — made from `available()`
            # — already reflects what the re-check found.
            self.recheck_stale(self._cloud_reach(mode, preferred_model, roles))
        # (source, model) -> the role that wants it, so one missing model is
        # reported once however many phases point at it.
        wanted: dict[tuple[str, str], str] = {}
        #: And every role that wants it, for the pre-Start check to name.
        users: dict[tuple[str, str], list[str]] = {}
        #: Cloud models the run leads with.
        cloud_heads: dict[tuple[str, str], str] = {}
        for role in [*(roles or ()), None]:
            try:
                chain = self._resolve_chain(mode, preferred_model, "medium", role)
            except UnresolvedModel as e:
                return Readiness(ok=False, reason=str(e))
            if not chain:
                return self._nothing_local()
            head = chain[0]
            if head[0] in CLOUD_PROVIDERS:
                cloud_heads.setdefault(head, role or "the rest of the run")
            else:
                wanted.setdefault(head, role or "the rest of the run")
                if role:
                    users.setdefault(head, []).append(role)
        for (pname, model), role in cloud_heads.items():
            if recheck_keys:
                self._check_chosen_model(pname, model)
            found = self._refuses(pname, model)
            if found is not None:
                who = "this build" if role == "the rest of the run" else f"the {role.replace('_', ' ')} agent"
                return Readiness(
                    ok=False,
                    reason=(
                        f"{pname}:{model} is set for {who}, but {found.message} Choose a model this key "
                        "can use (Settings → Cloud API keys lists them)."
                        if found.status == keycheck.MODEL_UNAVAILABLE
                        else f"{pname}:{model} is set for {who}, but the {keycheck.LABEL.get(pname, pname)} key "
                        f"isn't working: {found.message} Fix it in Settings → Cloud API keys, or choose another model."
                    ),
                )

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
            if not state.reachable and isinstance(prov, ConnectorProvider):
                return Readiness(ok=False, unreachable=True, reason=prov.down_reason())
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
        able = self._able_to_write(mode, by_source)
        if not able.ok:
            return able
        return self._compatible(by_source, users)

    def _compatible(
        self, by_source: dict[str, list[tuple[str, str]]], users: dict[tuple[str, str], list[str]]
    ) -> Readiness:
        """Will each model actually run here? The last question before Start.

        Every model the run will reach for is checked against what is known about it
        and the computer it runs on. A blocked one stops the run now, with the reason
        and what to choose instead, rather than halfway through for a reason that was
        knowable up front. A degraded one lets it start, and the page says why.
        """
        checks: list[dict] = []
        for source_id, items in by_source.items():
            prov = self.sources.get(source_id)
            assert prov is not None
            for model, _role in items:
                try:
                    check = prov.compatibility(model)
                except Exception as e:  # noqa: BLE001 - a check that breaks never blocks a run
                    log.warning("Could not check %s:%s before the run: %s", source_id, model, e)
                    continue
                checks.append(
                    {
                        **check.as_dict(),
                        "model": model,
                        "source_label": prov.source.label,
                        "roles": users.get((source_id, model), []),
                    }
                )
        blocked = [c for c in checks if c["level"] == compat.BLOCKED]
        if not blocked:
            return Readiness(ok=True, checks=tuple(checks))
        first = blocked[0]
        reason = f"This build is set to run on '{first['model']}', which won't run here: {first['summary']}"
        if first.get("suggestion"):
            reason += f" {first['suggestion']}"
        return Readiness(ok=False, reason=reason, checks=tuple(checks))

    # ── the compatibility check and per-model settings (Settings UI) ─────────
    def compatibility_view(self) -> dict:
        """Every model on every answering source, checked — for the Settings page.

        Each check needs the model described, which is one round trip per model the
        first time; they run side by side, and the answers are cached with the
        profile, so the page asks again for free.
        """
        from concurrent.futures import ThreadPoolExecutor

        self.sources.ensure()
        jobs: list[tuple[SourceProvider, str]] = []
        for prov in self.sources.providers():
            state = prov.state()
            if state.reachable:
                jobs.extend((prov, entry.name) for entry in state.models)

        def one(job: tuple[SourceProvider, str]) -> Optional[dict]:
            prov, model = job
            try:
                return prov.compatibility(model).as_dict()
            except Exception as e:  # noqa: BLE001 - one model's odd answer is not the page's
                log.warning("Could not check %s:%s: %s", prov.name, model, e)
                return None

        checks: dict[str, dict] = {}
        if jobs:
            with ThreadPoolExecutor(max_workers=min(len(jobs), 6), thread_name_prefix="compat") as pool:
                for (prov, model), row in zip(jobs, pool.map(one, jobs)):
                    if row is not None:
                        checks[f"{prov.name}:{model}"] = row
        from app.router.model_profile import total_ram_bytes

        return {"checks": checks, "ram_bytes": total_ram_bytes()}

    def _source_model(self, spec: str, *, saved_ok: bool = False) -> tuple[SourceProvider, str]:
        """The source and model a settings request names — one the source serves.

        A name the source doesn't list is refused, so nothing arbitrary is written to
        the settings file or sent to the runtime to be described. The one exception
        is putting back a model's saved settings, which must work after it is gone.
        """
        if len(spec or "") > _MAX_SPEC_CHARS:
            raise ValueError("That model name is too long to be one.")
        pair = self.parse(spec)
        if pair[0] in CLOUD_PROVIDERS:
            raise ValueError(
                f"'{spec}' is a cloud model. Generation settings are per local model; a cloud "
                "provider's own defaults apply to it."
            )
        prov = self.sources.get(pair[0])
        if prov is None:
            raise ValueError(f"No model source is called '{pair[0]}'.")
        model = self._listed(pair)[1]
        if saved_ok:
            if self.tuning.get(prov.settings_key(model)) is not None:
                return prov, model
        state = prov.state()
        if not state.reachable:
            raise ValueError(f"{prov.source.label} isn't answering, so '{model}' can't be tuned now.")
        if not prov.resolves(model, [e.name for e in state.models]):
            raise ValueError(f"{prov.source.label} doesn't serve a model called '{model}'.")
        return prov, model

    def model_generation(self, spec: str) -> dict:
        """One model's generation settings, what its server defaults to, and what it takes."""
        prov, model = self._source_model(spec)
        return {**prov.generation_view(model), "check": prov.compatibility(model).as_dict()}

    def set_model_generation(self, spec: str, values: Optional[dict]) -> dict:
        """Save one model's settings — validated first — and use them from the next call.

        The next request to that model reads them; nothing else changes, and no
        other model is touched. `None` or `{}` puts every field back on its default.
        """
        from app.router import generation

        prov, model = self._source_model(spec, saved_ok=not values)
        cleaned = generation.validate(values or {})
        if cleaned.get("machine") and self.is_shared(prov):
            # GPU layers, threads and keep-alive decide how the server's own machine
            # is used, for everyone on it. Sampling stays each account's own.
            raise ValueError(
                f"Machine settings for {prov.source.label} decide how the server's own hardware is "
                "used, so only the account that owns this install can set them. Sampling and "
                "limits are yours to change."
            )
        self.tuning.put(prov.settings_key(model), cleaned or None)
        # Window, reply and reasoning ceilings live in the profile; sampling is read
        # per call. Dropping the profile is what makes the first kind apply too.
        prov.forget_profile(model)
        if not prov.state().reachable or not prov.resolves(model):
            return {"spec": prov.settings_key(model), "values": cleaned, "reset": not cleaned}
        return self.model_generation(spec)

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
        pin: Optional[str] = None,
    ) -> LLMResponse:
        options = options or GenerationOptions()
        try:
            chain = self._pinned(self._resolve_chain(mode, preferred_model, complexity, role), pin)
        except UnresolvedModel as e:
            raise ProviderError(str(e), retryable=False) from e
        if not chain:
            raise ProviderError(
                self._nothing_local().reason or "No usable model could be resolved.",
                retryable=False,
            )

        attempts: list[dict] = []
        last_error: Optional[ProviderError] = None
        for idx, (pname, model) in enumerate(chain):
            prov = self.provider(pname)
            if isinstance(prov, ConnectorProvider) and not prov.available():
                # The user's own computer: gone, the build waits for it (pauses)
                # rather than falling through to a model nobody picked. Connected
                # but its runtime down, that is an error to fix there, not a pause.
                raise prov.unavailable_error()
            refused = self._refuses(pname, model) if prov is not None and prov.available() else None
            if prov is None or not prov.available() or refused is not None:
                attempts.append(self._skipped(pname, model, refused))
                continue
            try:
                resp = self._generate(prov, messages, model, options)
                resp.fallback_used = idx > 0
                resp.attempts = attempts
                if resp.is_local is None:
                    resp.is_local = prov.is_local_model(model)
                refused = next((a for a in attempts if a.get("kind") in keyerrors.BLOCKING), None)
                if refused is not None:
                    # "OpenAI has no credit left; continued on Gemini." — the fallback
                    # the person configured worked, and they should know why it ran.
                    resp.fallback_note = (
                        f"{_label(refused['provider'])}: {refused['why']}; continued on "
                        f"{_label(pname)} ({model})."
                    )
                    log.warning("%s", resp.fallback_note)
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
            except (RequestCancelled, ComputerDisconnected):
                raise  # a Stop, or a computer to wait for: never the next link's job
            except ProviderError as e:
                log.warning("Provider %s/%s failed: %s", pname, model, e)
                attempts.append({
                    "provider": pname, "model": model, "error": str(e),
                    "kind": getattr(e, "kind", None), "why": _short(getattr(e, "kind", None)),
                })
                last_error = e
                if pname in CLOUD_PROVIDERS:
                    self._note_failure(pname, model, e)

        if len(chain) == 1 and last_error is not None:
            # One model was ever in play: its own words are the whole story, and
            # "all providers failed" would only bury them.
            raise last_error
        # Every model failed: name each one's reason in plain words, and carry the
        # first cloud refusal's kind so the page can offer its fix.
        # A refusal that needs the person (no credit, expired) beats a rate limit.
        first = next((a for a in attempts if a.get("kind") in keyerrors.BLOCKING), None)
        raise ProviderError(
            "No model in this build's chain could answer. "
            + "; ".join(
                f"{_label(a['provider'])} ({a['model']}): {a.get('why') or a['error']}" for a in attempts
            )
            + ".",
            retryable=False,
            kind=first["kind"] if first else None,
            provider=first["provider"] if first else None,
        )

    def _skipped(self, pname: str, model: str, refused: Optional[KeyCheck]) -> dict:
        """An attempt not made, and why — "openai:gpt-x → out of credit", not "unavailable"."""
        prov = self.provider(pname)
        if refused is None and pname in CLOUD_PROVIDERS and prov is not None and getattr(prov, "has_key", False):
            # Ruled out earlier (not `available()`): its standing still says why.
            standing = self.key_check(pname)
            if standing.status in keycheck.REJECTED:
                refused = standing
        if refused is not None:
            kind = keycheck.kind_of(refused)
            return {
                "provider": pname, "model": model, "error": refused.message,
                "kind": kind, "why": _short(kind) if kind else refused.message.rstrip("."),
            }
        if pname in CLOUD_PROVIDERS and prov is not None and not getattr(prov, "has_key", True):
            why = "no API key saved"
        elif pname in CLOUD_PROVIDERS:
            why = "its key failed a check"
        else:
            why = "not running"
        return {"provider": pname, "model": model, "error": "unavailable", "kind": None, "why": why}

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
            spec = self._roles.get(model_roles.EMBEDDINGS_ROLE)
            if spec:
                pair = self._saved_pair(spec)
                if pair[0] not in CLOUD_PROVIDERS:
                    return pair, DEFAULT_CHOSEN
                log.warning(
                    "Embeddings are set to '%s', a cloud model; they come from a local "
                    "source. Choosing one automatically instead.",
                    spec,
                )
            device = self.devices.embed_choice()
            if device is not None:
                return device, DEFAULT_CHOSEN
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
        if isinstance(prov, ConnectorProvider) and not prov.available():
            raise prov.unavailable_error()
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
        role_spec = self._roles.get(role)
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
        cloud = [
            name
            for name in CLOUD_PROVIDERS
            if self._cloud[name].available() and not self._refuses(name, self._default_model.get(name) or "")
        ]
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


def _short(kind: Optional[str]) -> Optional[str]:
    return keyerrors.SHORT.get(kind) if kind else None


def _label(provider: str) -> str:
    return keyerrors.LABELS.get(provider, provider)


def _looks_like_a_coder(model: str) -> bool:
    name = model.lower()
    return any(hint in name for hint in _CODER_HINTS)


class RouterRegistry:
    """One router per account, made the first time the account needs one."""

    def __init__(self) -> None:
        self._routers: dict[str, ModelRouter] = {}
        self._lock = threading.Lock()

    def for_user(self, user_id: str) -> ModelRouter:
        with self._lock:
            found = self._routers.get(user_id)
        if found is not None:
            return found
        # Built outside the lock — it reads the account's files — and the first one
        # built wins, so two requests racing to make it end up sharing one.
        made = ModelRouter(user_id, owner=_is_owner(user_id))
        with self._lock:
            return self._routers.setdefault(user_id, made)

    def current(self) -> ModelRouter:
        """The router of the account this work is bound to. Never anyone else's."""
        return self.for_user(identity.require_user_id())

    def loaded(self) -> list[ModelRouter]:
        with self._lock:
            return list(self._routers.values())

    def forget(self, user_id: str) -> None:
        with self._lock:
            self._routers.pop(user_id, None)


def _is_owner(user_id: str) -> bool:
    from app.db.base import SessionLocal
    from app.db.models import User

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        return bool(user is not None and user.is_owner)
    finally:
        db.close()


routers = RouterRegistry()


class _CurrentRouter:
    """`router`: whichever account's router the current work is bound to.

    The agents, the debate, the mockup and the embedding function all import this
    one name and call it without a user in hand; the binding (`app.core.identity`)
    says whose router answers. Attribute reads go through on every access, so a
    long-lived reference never pins one account's router.
    """

    def __getattr__(self, name: str):
        return getattr(routers.current(), name)

    def __setattr__(self, name: str, value) -> None:
        target = routers.current()
        # Putting back what the class already provides — the old value a test's
        # monkeypatch restores on the way out — clears the override rather than
        # pinning a copy of it to this one router, where it would shadow the class.
        inherited = getattr(type(target), name, None)
        if inherited is not None and (
            value is inherited
            or (getattr(value, "__self__", None) is target and getattr(value, "__func__", None) is inherited)
        ):
            target.__dict__.pop(name, None)
            return
        setattr(target, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(routers.current(), name)

    def __repr__(self) -> str:
        return "<router for the current account>"


router = _CurrentRouter()
