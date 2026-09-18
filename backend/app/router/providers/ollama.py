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
from typing import Optional

import time
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
        """True if `model` (exact tag, or same base when no tag given) is pulled."""
        models = self.list_models()
        if model in models:
            return True
        base = model.split(":", 1)[0]
        return any(m.split(":", 1)[0] == base for m in models)

    # ── capability probe ──────────────────────────────────────────────────────
    def _tags(self) -> list[dict]:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            r.raise_for_status()
            return r.json().get("models", []) or []
        except Exception:  # noqa: BLE001 - server not running / unexpected payload
            return []

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
        ram = total_ram_bytes() if self.is_same_machine() else None

        capabilities = [str(c) for c in (show.get("capabilities") or [])]
        version = self.server_version()
        supports_schema = bool(
            version
            and version >= _SCHEMA_FORMAT_MIN_VERSION
            # An empty capability list means an older server that does not report
            # them; take the version's word for it rather than refusing to constrain.
            and (not capabilities or _COMPLETION_CAPABILITY in capabilities)
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

    def _show(self, model: str) -> Optional[dict]:
        try:
            r = httpx.post(f"{self.base_url}/api/show", json={"model": model}, timeout=10.0)
            r.raise_for_status()
            payload = r.json()
            return payload if isinstance(payload, dict) else None
        except Exception as e:  # noqa: BLE001 - unreachable, or the model is not pulled
            log.warning(
                "Could not probe '%s' on %s (%s); falling back to the configured window "
                "of %s tokens.",
                model,
                self.base_url,
                e,
                settings.model_context_fallback_tokens,
            )
            return None

    def is_same_machine(self) -> bool:
        """Whether Ollama runs where this process does, so local RAM is its RAM."""
        host = urlparse(self.base_url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    def forget_profile(self, model: Optional[str] = None) -> None:
        """Drop cached probes — after a pull, or when the host changes underneath us."""
        self._profiles.forget((self.base_url, model) if model else None)

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
