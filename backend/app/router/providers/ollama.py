"""Local model provider via Ollama's HTTP API (the default, zero-cost backend).

Two things happen here that did not before, and the pipeline's correctness rests on
both. The request now carries a **context window** (`num_ctx`) and an **output budget**
(`num_predict`), resolved from the model's own metadata — without them every call ran
at the server's small default window and Ollama silently truncated the prompt from the
head, taking the system prompt (and with it the required output shape) first. And when
the server and model support it, `format` carries the **JSON Schema** the agent must
return rather than the bare string `"json"`, which only ever promised valid JSON, not
the right JSON.
"""
from __future__ import annotations
from typing import Iterable, Optional

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import LLMProvider, ProviderError
from app.router.model_profile import (
    ModelProfile,
    ProfileCache,
    build_profile,
    fallback_profile,
    total_ram_bytes,
)
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse, Usage

log = get_logger(__name__)

#: Schema-constrained `format` landed in Ollama 0.5. Older servers accept the string
#: "json" only, and sending them a schema object is a 400 — so the version is asked
#: for rather than assumed, and anything below this degrades to plain JSON mode.
_SCHEMA_FORMAT_MIN_VERSION = (0, 5, 0)
#: A model that cannot complete text (an embedding model, say) cannot be constrained.
_COMPLETION_CAPABILITY = "completion"
#: A capability probe is one small JSON answer from a runtime that is already known to
#: be up — the tag list just came back. Anything slower than this is a runtime in
#: trouble, and the Settings page should not wait the ten seconds a profile probe
#: (which may be loading a model) is allowed.
_CAPABILITY_PROBE_TIMEOUT = 3.0
#: Probes for models the tag list did not describe run side by side, so ten pulled
#: models on an older runtime cost one timeout rather than ten in a row.
_MAX_PARALLEL_PROBES = 8
#: The runtime's word for a model that turns text into vectors instead of writing.
#: Its presence *without* `completion` is the one definite "cannot write": the two are
#: decided by the same branch when the runtime reads the model file.
_EMBEDDING_CAPABILITY = "embedding"
#: How long an answer from `/api/show` is trusted. The tag list refreshes its own
#: answers every time it is read; this bounds the ones it does not carry, so a model
#: re-created from the CLI under the same name is re-read within minutes rather than
#: at the next restart.
_CAPABILITY_TTL_SECONDS = 300.0
#: How long a *failed* probe is left alone before it is tried again. Long enough that
#: a model whose blob is broken does not cost every Settings poll a timeout; short
#: enough that one pulled a moment from now is picked up almost at once.
_FAILED_PROBE_TTL_SECONDS = 30.0
#: "Not remembered" — distinct from a remembered `None`, which is an answer.
_MISSING = object()
#: "Asked, and the probe failed" — unknown, but not worth asking again just yet.
_FAILED = object()


def _spellings(model: str) -> tuple[str, ...]:
    """Every name the runtime treats as this model: an untagged name means `:latest`.

    The tag list always reports the full `name:tag`, while a configured default or a
    pull request is often written without one. Looking either up under only the
    spelling it arrived in is how a cached answer went unfound — and, the other way,
    how dropping it after a re-pull left the stale one behind. The tag is whatever
    follows the *last* colon, unless that colon belongs to a registry `host:port`.
    """
    name, sep, tag = model.rpartition(":")
    if not sep or "/" in tag:
        return (model, f"{model}:latest")
    return (model, name) if tag == "latest" else (model,)


def _parse_version(text: str) -> tuple[int, int, int]:
    """'0.30.10' -> (0, 30, 10). Always three parts, so comparisons mean what they read.

    A two-part version is padded rather than left short: `"0.5"` as `(0, 5)` compares
    *below* `(0, 5, 0)`, which would reject the very first release that supports the
    feature being checked for. `()` is returned for anything unparseable, and the
    caller treats that as "did not answer" rather than "answered zero".
    """
    parts: list[int] = []
    for chunk in str(text).strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        return ()  # type: ignore[return-value]
    parts += [0] * (3 - len(parts))
    return tuple(parts[:3])  # type: ignore[return-value]


class _SchemaFormatRejected(RuntimeError):
    """The server would not take a JSON Schema in `format`; retry in plain JSON mode."""


#: Status codes worth asking again for. Everything else in the 4xx range is a
#: statement about the request — a model that is not pulled, a malformed body — and
#: will say exactly the same thing on the third attempt as it did on the first.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _as_provider_error(error: Exception, model: str) -> ProviderError:
    """Normalise a failed call, keeping the one hint that usually resolves it."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return ProviderError(
            f"Ollama returned {status}: {error.response.text[:200]}. "
            f"Is the model '{model}' pulled? Try `ollama pull {model}`.",
            retryable=status in _RETRYABLE_STATUS,
        )
    # A timeout, a refused connection, a half-closed socket. A local runtime does all
    # three while it swaps a model into memory, and each one used to end the whole
    # run — these are the failures retrying exists for.
    return ProviderError(f"Ollama call failed: {error}")


class OllamaProvider(LLMProvider):
    name = "ollama"
    is_local = True

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self._profiles = ProfileCache()
        self._version: Optional[tuple[int, ...]] = None
        #: What each model says it can do: `(answer, expires_at)` on the monotonic
        #: clock. A plain dict is enough: every write is the same answer to the same
        #: question, so two threads racing cost one duplicate HTTP call and nothing
        #: else.
        self._capabilities: dict[tuple[str, str], tuple[object, float]] = {}

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:  # noqa: BLE001 - server simply not running
            return False

    def list_models(self) -> list:
        """Names of models currently pulled on the Ollama host (empty if unreachable)."""
        return [m.get("name", "") for m in self._tags() if m.get("name")]

    def has_model(self, model: str) -> bool:
        """True if `model`, as written, names something that is pulled."""
        return self.resolves(model, self.list_models())

    @staticmethod
    def resolves(model: str, pulled: Iterable[str]) -> bool:
        """Whether `model` names one of `pulled`, by the runtime's own rule.

        An untagged name means `:latest` and nothing else. The looser rule this
        replaced — any pulled model with the same base — called `nomic-embed-text`
        present when only `nomic-embed-text:v1.5` was, which the runtime would then
        refuse to load; and it disagreed with the capability lookup, so the two
        checks before a run could reach opposite answers about one name.
        """
        names = set(pulled)
        return any(name in names for name in _spellings(model))

    # ── capability probe ──────────────────────────────────────────────────────
    def _tags(self) -> list[dict]:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            r.raise_for_status()
            entries = r.json().get("models", []) or []
        except Exception:  # noqa: BLE001 - server not running / unexpected payload
            return []
        # Recent servers report capabilities in the tag list itself, which answers
        # for every pulled model in the one round trip the caller was making anyway.
        # Taken as it goes past, so `capabilities` below falls back to a probe per
        # model only on a server old enough not to say — and so the answers refresh
        # whenever the list is read, rather than aging in a cache of their own.
        #
        # Only an entry that actually carries the key is remembered. Recording a
        # missing key as "reported nothing" would cache the older server's silence
        # as an answer and stop the probe that *can* get one.
        for entry in entries:
            name = entry.get("name")
            if name and isinstance(entry.get("capabilities"), (list, tuple)):
                self._remember_capabilities(name, entry)
        return entries

    def server_version(self) -> Optional[tuple[int, ...]]:
        """The server's version, asked once — but only remembered once it answers.

        A version that could not be read is not cached. Storing the empty parse would
        make the not-`None` check pass forever after, and schema-constrained decoding
        would stay off for every model on this host for the life of the process, with
        nothing said about why.
        """
        if self._version:
            return self._version
        try:
            r = httpx.get(f"{self.base_url}/api/version", timeout=2.0)
            r.raise_for_status()
            parsed = _parse_version(r.json().get("version", ""))
        except Exception:  # noqa: BLE001 - unreachable, or a build with no such route
            return None
        if not parsed:
            log.warning(
                "Ollama at %s reported a version this cannot read; schema-constrained "
                "decoding stays off until it reports one that can be.",
                self.base_url,
            )
            return None
        self._version = parsed
        return self._version

    def profile(self, model: str) -> ModelProfile:
        """Everything known about `model`, probed once and remembered.

        A failed probe is deliberately not cached: a model pulled a minute from now,
        or an Ollama that has only just started, must be picked up on the next call
        rather than leaving the run on fallback budgets until the server restarts.
        """
        key = (self.base_url, model)
        cached = self._profiles.get(key)
        if cached is not None:
            return cached

        show = self._show(model)
        if show is None:
            return fallback_profile(self.name, model, local=True)

        # The KV cache lives in the Ollama process. When that is on another host — or
        # in its own container — this machine's RAM says nothing about what fits there,
        # and a confident clamp built on it would be a number about the wrong computer.
        # This is the only place that may read local RAM, because it is the only place
        # that knows where the runtime is; `None` reaches `build_profile` as "unknown,
        # so do not clamp", which is a different answer from "not passed".
        ram = total_ram_bytes() if self.is_same_machine() else None

        capabilities = list(self._remember_capabilities(model, show) or ())
        version = self.server_version()
        supports_schema = bool(
            version
            and version >= _SCHEMA_FORMAT_MIN_VERSION
            # Only a model the runtime positively says cannot write is refused here —
            # the same reading `writes` gives the same list, so an older server that
            # reports nothing, or a file it could not read, is taken on the version.
            and self.writes(capabilities) is not False
        )

        return self._profiles.put(
            key,
            build_profile(
                provider=self.name,
                model=model,
                show=show,
                supports_schema_format=supports_schema,
                ram_bytes=ram,
            ),
        )

    def capabilities(self, model: str) -> Optional[tuple[str, ...]]:
        """What the runtime says this model can do — or None when it will not say.

        The list is returned as reported; what it *means* is `writes`'s decision,
        not the caller's. `None` is a server too old to report capabilities, or one
        that could not be reached or could not describe the model.

        Usually free: a server that reports capabilities in `/api/tags` has already
        filled the cache this reads — under the full `name:tag`, and found here under
        any spelling of it. The `/api/show` fallback is for servers that do not, and
        its answer is kept for the same reason: the tag list is read on every
        Settings poll, and a probe per model behind each one is a page that waits. A
        *failed* probe is kept only briefly, and as "unknown" — a model whose blob
        will not read should not cost every poll a timeout, and one pulled a moment
        from now has to be picked up soon after.
        """
        remembered = self._remembered_capabilities(model)
        if remembered is _FAILED:
            return None
        if remembered is not _MISSING:
            return remembered  # type: ignore[return-value]
        show = self._show(
            model,
            note="; its capabilities are unknown until it answers.",
            timeout=_CAPABILITY_PROBE_TIMEOUT,
        )
        if show is None:
            self._capabilities[(self.base_url, model)] = (
                _FAILED,
                time.monotonic() + _FAILED_PROBE_TTL_SECONDS,
            )
            return None
        return self._remember_capabilities(model, show)

    def known_capabilities(self, model: str) -> Optional[tuple[str, ...]]:
        """What is already remembered about `model` — never a network call.

        For callers that must not wait: a Settings request asking about the default
        model while the runtime is down would otherwise spend a probe timeout on
        every poll, which is exactly what `local_status` exists not to do.
        """
        remembered = self._remembered_capabilities(model)
        if remembered is _MISSING or remembered is _FAILED:
            return None
        return remembered  # type: ignore[return-value]

    def capabilities_for(self, models: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """`{model: capabilities}` for every model the runtime will describe.

        Normally free — the tag list that produced `models` already filled the cache.
        What it did not describe is probed in parallel, with the short timeout, so an
        older runtime with many models costs one wait rather than one per model.
        Models it will not describe are left out: unknown is not the same as empty.
        """
        names = list(dict.fromkeys(m for m in models if m))
        unasked = [m for m in names if self._remembered_capabilities(m) is _MISSING]
        if unasked:
            workers = min(len(unasked), _MAX_PARALLEL_PROBES)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="caps") as pool:
                list(pool.map(self.capabilities, unasked))
        out: dict[str, tuple[str, ...]] = {}
        for name in names:
            caps = self.known_capabilities(name)
            if caps is not None:
                out[name] = caps
        return out

    @staticmethod
    def writes(capabilities: Optional[Iterable[str]]) -> Optional[bool]:
        """Whether a capability list says the model completes text; None if unknown.

        The one place the runtime's vocabulary is interpreted, and it only says no on
        positive evidence. The runtime decides `completion` against `embedding` by
        reading the model file; when it cannot read the file it reports neither, and
        whatever the template adds (`tools`, say) is all that is left. So `[]` and
        `["tools"]` are "could not tell", not "cannot write" — treating them as a no
        would refuse a working chat model the moment its file hiccupped. What is a
        no is `embedding` reported without `completion`. Unknown never blocks.
        """
        if capabilities is None:
            return None
        reported = tuple(capabilities)
        if _COMPLETION_CAPABILITY in reported:
            return True
        if _EMBEDDING_CAPABILITY in reported:
            return False
        return None

    def _remembered_capabilities(self, model: str) -> object:
        """The live answer under any spelling of `model`, `_FAILED`, or `_MISSING`."""
        now = time.monotonic()
        for name in _spellings(model):
            entry = self._capabilities.get((self.base_url, name))
            if entry is not None and entry[1] > now:
                return entry[0]
        return _MISSING

    def _remember_capabilities(self, model: str, show: dict) -> Optional[tuple[str, ...]]:
        """Record what one `/api/show` payload said about capabilities, and return it.

        Shared so that the capability probe and the profile probe cannot drift, and
        so a profiled model does not get asked a second time for the half of the
        payload the first call already had in its hands.
        """
        reported = show.get("capabilities")
        caps = (
            tuple(str(c) for c in reported) if isinstance(reported, (list, tuple)) else None
        )
        self._capabilities[(self.base_url, model)] = (
            caps,
            time.monotonic() + _CAPABILITY_TTL_SECONDS,
        )
        return caps

    def _show(
        self, model: str, *, note: Optional[str] = None, timeout: float = 10.0
    ) -> Optional[dict]:
        try:
            r = httpx.post(f"{self.base_url}/api/show", json={"model": model}, timeout=timeout)
            r.raise_for_status()
            payload = r.json()
            return payload if isinstance(payload, dict) else None
        except Exception as e:  # noqa: BLE001 - unreachable, or the model is not pulled
            log.warning(
                "Could not probe '%s' on %s (%s)%s",
                model,
                self.base_url,
                e,
                note
                or (
                    "; falling back to the configured window of "
                    f"{settings.model_context_fallback_tokens} tokens."
                ),
            )
            return None

    def is_same_machine(self) -> bool:
        """Whether the runtime shares this machine's memory, so local RAM is its RAM.

        Configured when it is set (`OLLAMA_SAME_MACHINE`), inferred from the address
        when it is not. The address can only say "loopback": a runtime in a sibling
        container under docker compose is `http://ollama:11434` and shares this host's
        RAM all the same, and leaving it unclamped sends a model's full trained window
        — 128k tokens is tens of GiB of KV cache — to a machine that cannot hold it.
        """
        if settings.ollama_same_machine is not None:
            return bool(settings.ollama_same_machine)
        host = urlparse(self.base_url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    def forget_profile(self, model: Optional[str] = None) -> None:
        """Drop cached probes — after a pull, or when the default model changes.

        Capabilities are dropped only for a *named* model. A pull replaces that
        model's weights, so what it can do may have changed with them — under every
        spelling of its name, or re-pulling `llama3.1` would leave the answer cached
        under `llama3.1:latest` untouched. A new default changes which model runs,
        not what any model can do, so it leaves every capability exactly where it is;
        wiping them made choosing a model re-probe all of the others inline.
        """
        if not model:
            self._profiles.forget(None)
            return
        # Both caches, under every spelling. The profile holds the window and the
        # schema support the next prompt is budgeted with, and a pull of `llama3.1`
        # that left `llama3.1:latest`'s profile behind kept budgeting for the old one.
        for name in _spellings(model):
            self._profiles.forget((self.base_url, name))
            self._capabilities.pop((self.base_url, name), None)

    # ── generation ────────────────────────────────────────────────────────────
    def generate(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
    ) -> LLMResponse:
        profile = self.profile(model)

        started = time.perf_counter()
        try:
            data = self._chat(self._payload(messages, model, options, profile, schema=True))
        except _SchemaFormatRejected as e:
            # The server took the schema badly (an old build, or one whose grammar
            # converter cannot express this shape). Valid JSON plus the shape written
            # into the prompt is the documented degradation — and validation with a
            # repair round still catches anything that drifts.
            log.warning(
                "Ollama rejected schema-constrained decoding for %s (%s); retrying in "
                "plain JSON mode. Validation and repair still apply.",
                model,
                e,
            )
            self._profiles.put(
                (self.base_url, model), replace(profile, supports_schema_format=False)
            )
            # The plain-JSON retry is the real attempt now, so its failure is the
            # one worth reporting: a 400 that was never about `format` (a model that
            # is not pulled, say) still reaches the user as the advice they need.
            try:
                data = self._chat(
                    self._payload(messages, model, options, profile, schema=False)
                )
            except Exception as inner:  # noqa: BLE001
                raise _as_provider_error(inner, model) from inner
        except Exception as e:  # noqa: BLE001
            raise _as_provider_error(e, model) from e

        latency = int((time.perf_counter() - started) * 1000)
        text = (data.get("message") or {}).get("content", "")
        self._report_limits(data, model, profile, options)
        usage = Usage(
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        )
        return LLMResponse(
            text=text, provider=self.name, model=model, usage=usage, latency_ms=latency
        )

    def _report_limits(
        self, data: dict, model: str, profile: ModelProfile, options: GenerationOptions
    ) -> None:
        """Say when a call actually hit a limit, rather than leaving it to be guessed.

        Every character budget upstream rests on an estimate of how many characters
        make a token, and an estimate can be wrong the expensive way. Ollama reports
        what really happened — `prompt_eval_count` for what the prompt cost, and
        `done_reason` for why generation stopped — so the two failures this whole
        change exists to make visible are read off the response instead of inferred
        three phases later from a schema that did not match.
        """
        prompt_tokens = data.get("prompt_eval_count") or 0
        if prompt_tokens and prompt_tokens >= profile.context_window * 0.95:
            log.warning(
                "Prompt for %s used %s of a %s-token window — at or past the point "
                "Ollama truncates from the head, which drops the system prompt and "
                "the required output shape with it. Lower APPROX_CHARS_PER_TOKEN "
                "(currently %s) so prompts are budgeted more conservatively.",
                model,
                f"{prompt_tokens:,}",
                f"{profile.context_window:,}",
                settings.approx_chars_per_token,
            )
        if data.get("done_reason") == "length":
            log.warning(
                "%s stopped at the %s-token output limit rather than finishing. Its "
                "reply is cut off, so it will not parse as the shape it was asked for. "
                "Raise MAX_OUTPUT_TOKENS (currently %s) or give the model a larger "
                "window.",
                model,
                f"{options.resolve_max_tokens(profile.max_output_tokens):,}",
                settings.max_output_tokens,
            )

    def _payload(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions,
        profile: ModelProfile,
        *,
        schema: bool,
    ) -> dict:
        """The request body, with the window and the output budget always present."""
        payload: dict = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {
                # The two that were missing. Without num_ctx the server falls back to
                # its own small default and truncates the prompt from the head; without
                # num_predict the output budget an agent asked for was never honoured.
                "num_ctx": profile.context_window,
                "num_predict": options.resolve_max_tokens(profile.max_output_tokens),
            },
        }
        if options.temperature is not None:
            payload["options"]["temperature"] = options.temperature

        if options.json_schema and schema and profile.supports_schema_format:
            payload["format"] = options.json_schema
        elif options.json_mode or options.json_schema:
            payload["format"] = "json"
        return payload

    def _chat(self, payload: dict) -> dict:
        # Local generation can be slow on CPU; give it room.
        r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=600.0)
        # Only a 400 is worth retrying without the schema. A 404 is a model that is
        # not pulled and a 5xx is a server in trouble; neither gets better by asking
        # again, and the second attempt would only delay the real error.
        if r.status_code == 400 and isinstance(payload.get("format"), dict):
            raise _SchemaFormatRejected(r.text[:200])
        r.raise_for_status()
        return r.json()
