"""The generic adapter: any server that speaks the OpenAI Chat Completions API.

Almost every local runtime serves this dialect, which is why it is the adapter the
rest build on. What the dialect does not do is describe the model: `/v1/models` has
an id and little else — no window, no size, no kind. So this adapter reads whatever
extension fields a server adds to that list, and otherwise says "unknown" and lets
the configured fallback window stand, rather than assuming a size.

Two things are always sent, whatever the caller left unset: `max_tokens`, and the
sampling values. Some servers default to a 512-token reply or a greedy temperature,
and a runtime's own default is not something an agent's prompt was written for.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import ProviderError
from app.router.runtimes.base import (
    CHAT_TIMEOUT,
    EMBED_TIMEOUT,
    FINGERPRINT_TIMEOUT,
    LIST_TIMEOUT,
    RuntimeAdapter,
    get_json,
    http_error,
)
from app.router.runtimes.reasoning import merge, split_reasoning
from app.router.runtimes.types import (
    CONTEXT_REPORTED,
    STRUCTURED_JSON,
    STRUCTURED_NONE,
    STRUCTURED_SCHEMA,
    THINKING_EFFORTS,
    THINKING_OFF,
    ChatRequest,
    ChatResult,
    Hello,
    ModelEntry,
    ModelInfo,
    thinks,
)

log = get_logger(__name__)

#: The structured modes this dialect can ask for, strongest first.
_LADDER = (STRUCTURED_SCHEMA, STRUCTURED_JSON, STRUCTURED_NONE)
#: Words a 400 uses when it is the structured-output request that was refused, as
#: opposed to the prompt, the model or the key. Only those are worth retrying weaker.
_FORMAT_WORDS = ("response_format", "json_schema", "schema", "grammar", "json_object", "format")
#: Words a 400 uses when it is the thinking fields that were refused. A server that
#: will not take them is asked again without, and not asked with them again.
_THINKING_WORDS = ("chat_template_kwargs", "reasoning_effort", "enable_thinking")
#: The sampling settings every server of this dialect takes at the top level.
_STANDARD_SAMPLING = ("temperature", "top_p", "seed", "stop", "presence_penalty", "frequency_penalty")
#: Extension fields servers add to `/v1/models` entries that say how long a prompt
#: the model takes. Read in this order; the first positive integer wins.
_CONTEXT_FIELDS = ("max_model_len", "context_length", "max_context_length", "context_window")


def _positive(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def api_root(base_url: str) -> str:
    """The server's root, whichever way the address was written.

    People paste both `http://host:1234` and `http://host:1234/v1`. The source keeps
    the root, and this adapter adds `/v1` itself, so the two spellings are one source.
    """
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def speaks_openai(base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT) -> bool:
    """Whether `/v1/models` answers in the dialect's shape — a hint, never an identity."""
    data = get_json(api_root(base_url), "/v1/models", api_key, timeout=timeout)
    return isinstance(data, dict) and isinstance(data.get("data"), list)


class OpenAICompatAdapter(RuntimeAdapter):
    runtime = "openai-compatible"
    #: The dialect's own settings. Servers built on it add their samplers below.
    sampling_supported = frozenset(_STANDARD_SAMPLING)
    #: `reasoning_effort` is the dialect's one word about thinking — levels only.
    thinking_supported = THINKING_EFFORTS
    #: Neutral name -> this server's name, for samplers beyond the dialect's own.
    extra_sampling: dict[str, str] = {}
    #: Whether thinking is switched through the chat template (`enable_thinking`),
    #: which is how servers that render the template themselves take "off".
    thinking_via_template = False

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        super().__init__(api_root(base_url), api_key)
        #: The structured modes each model has refused, remembered per mode so each
        #: refusal is paid for once — and so a refusal of one mode never switches off
        #: another. A server that takes schemas but not bare JSON mode is common.
        self._refused: dict[str, set[str]] = {}
        #: The last list's raw entries by id, for the extension fields in them.
        self._raw: dict[str, dict] = {}
        #: Sampling defaults the server reported per model. A setting it has a
        #: default for is left to it; only one nobody can speak for is filled in.
        self._defaults: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── identification ───────────────────────────────────────────────────────
    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        """Any server that answers `/v1/models` in the dialect's shape.

        Detection never adopts a source on this alone — "speaks OpenAI" is true of
        a dozen products and of anything proxying one — so a match here is shown as
        unknown until someone confirms it. A source the user added is taken as this.
        """
        if not speaks_openai(base_url, api_key, timeout=timeout):
            return None
        return Hello(runtime=cls.runtime, base_url=api_root(base_url))

    # ── list_models ──────────────────────────────────────────────────────────
    def _models_payload(self) -> list[dict]:
        try:
            r = self._get("/v1/models", timeout=LIST_TIMEOUT)
            r.raise_for_status()
            data = r.json().get("data", []) or []
        except Exception as e:  # noqa: BLE001
            raise http_error(e, f"The model server at {self.base_url}") from e
        entries = [d for d in data if isinstance(d, dict) and d.get("id")]
        with self._lock:
            self._raw = {d["id"]: d for d in entries}
        return entries

    def list_models(self) -> list[ModelEntry]:
        return [ModelEntry(name=d["id"]) for d in self._models_payload()]

    # ── model_info ───────────────────────────────────────────────────────────
    def raw_entry(self, model: str) -> dict:
        with self._lock:
            raw = self._raw.get(model)
        if raw is None:
            try:
                self._models_payload()
            except ProviderError:
                return {}
            with self._lock:
                raw = self._raw.get(model)
        return raw or {}

    def model_info(self, model: str) -> Optional[ModelInfo]:
        raw = self.raw_entry(model)
        context = next((c for c in (_positive(raw.get(f)) for f in _CONTEXT_FIELDS) if c), None)
        return ModelInfo(
            name=model,
            context_window=context,
            context_source=CONTEXT_REPORTED if context else None,
            structured_output=self.structured_mode(model),
            listen_address=self.base_url,
        )

    def structured_mode(self, model: str) -> str:
        """The strongest mode this model has not refused — what a schema call asks for."""
        refused = self._refused.get(model, set())
        for mode in (STRUCTURED_SCHEMA, STRUCTURED_JSON):
            if mode not in refused:
                return mode
        return STRUCTURED_NONE

    # ── chat ─────────────────────────────────────────────────────────────────
    def chat(self, request: ChatRequest) -> ChatResult:
        wants_json = bool(request.json_schema) or request.json_mode
        refused = self._refused.setdefault(request.model, set())
        if not wants_json:
            modes = [STRUCTURED_NONE]
        else:
            modes = [m for m in _LADDER if m == STRUCTURED_NONE or m not in refused]
            if not request.json_schema:
                modes = [m for m in modes if m != STRUCTURED_SCHEMA]
            # Never stronger than the caller believes this model takes.
            if request.structured_output in modes:
                modes = modes[modes.index(request.structured_output) :]
            if thinks(request.thinking) and not self.schema_with_reasoning:
                # Decoding held to a shape from the first token leaves no room to
                # reason, and the two are known to conflict on servers that compile
                # the shape to a grammar. The shape stays in the prompt, and
                # validation with a repair round still holds the answer to it.
                modes = [STRUCTURED_NONE]

        rejected = False
        send_thinking = "thinking" not in refused
        last: Optional[Exception] = None
        index = 0
        while index < len(modes):
            mode = modes[index]
            body = self._body(request, mode, thinking=send_thinking)
            try:
                r = self._post_cancellable(
                    "/v1/chat/completions",
                    body,
                    timeout=CHAT_TIMEOUT,
                    request_id=request.request_id,
                )
            except Exception as e:  # noqa: BLE001
                raise http_error(e, f"The model server at {self.base_url}") from e
            said = (r.text or "").lower()
            if r.status_code in (400, 422) and send_thinking and any(w in said for w in _THINKING_WORDS) and (
                "chat_template_kwargs" in body or "reasoning_effort" in body
            ):
                # Refused the thinking fields, not the request: asked again without
                # them, and they are not sent to this model again.
                log.warning(
                    "%s at %s refused the thinking setting (%s); retrying without it.",
                    request.model,
                    self.base_url,
                    (r.text or "")[:160],
                )
                refused.add("thinking")
                send_thinking = False
                continue
            if (
                r.status_code in (400, 422)
                and "response_format" in body
                and any(w in said for w in _FORMAT_WORDS)
                and mode != modes[-1]
            ):
                # Refused the shape, not the request. Remembered, so the next call
                # asks for what this model takes instead of paying for this again.
                log.warning(
                    "%s at %s refused %s output (%s); retrying with less. Validation "
                    "and repair still apply.",
                    request.model,
                    self.base_url,
                    mode,
                    (r.text or "")[:160],
                )
                rejected = True
                refused.add(mode)
                index += 1
                continue
            try:
                r.raise_for_status()
                data = r.json()
            except Exception as e:  # noqa: BLE001
                last = e
                break
            unsent = self.unsent(request)
            if request.thinking is not None and not send_thinking and "thinking" not in unsent:
                unsent = (*unsent, "thinking")
            return self._result(data, mode, rejected, unsent)
        raise http_error(last or RuntimeError("no answer"), f"The model server at {self.base_url}")

    def _sampling(self, request: ChatRequest) -> dict:
        """The sampling fields, in this server's names — every one it can take.

        Temperature and top-p are always stated: some servers default to greedy
        decoding or a 512-token reply that no agent's prompt was written for. A value
        the server itself reported a default for is left to it; one nobody can speak
        for gets the configured default instead of the server's silence.
        """
        known = self._defaults.get(request.model, {})
        out: dict = {}
        for key in _STANDARD_SAMPLING:
            value = getattr(request, key)
            if value is not None:
                out[key] = value
        for key, wire in self.extra_sampling.items():
            value = getattr(request, key)
            if value is not None:
                out[wire] = value
        if "temperature" not in out and "temperature" not in known:
            out["temperature"] = settings.local_temperature
        if "top_p" not in out and "top_p" not in known:
            out["top_p"] = settings.local_top_p
        if thinks(request.thinking):
            temperature = out.get("temperature", known.get("temperature"))
            if temperature is not None and temperature <= 0:
                # A thinking model decoded greedily repeats itself, sometimes forever.
                log.warning(
                    "%s thinks, and greedy decoding makes thinking models repeat "
                    "themselves; using temperature %s instead of %s.",
                    request.model,
                    settings.local_temperature,
                    temperature,
                )
                out["temperature"] = settings.local_temperature
        return out

    def _thinking_fields(self, request: ChatRequest) -> dict:
        level = request.thinking
        if level is None:
            return {}
        out: dict = {}
        if self.thinking_via_template:
            kwargs: dict = {"enable_thinking": level != THINKING_OFF}
            if level in THINKING_EFFORTS:
                kwargs["reasoning_effort"] = level
            out["chat_template_kwargs"] = kwargs
        if level in THINKING_EFFORTS:
            out["reasoning_effort"] = level
        return out

    def _body(self, request: ChatRequest, mode: str, *, thinking: bool = True) -> dict:
        body: dict = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
            # Always explicit — see the module docstring.
            "max_tokens": request.max_tokens,
            **self._sampling(request),
        }
        if thinking:
            body.update(self._thinking_fields(request))
        if mode == STRUCTURED_SCHEMA and request.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": request.json_schema, "strict": False},
            }
        elif mode == STRUCTURED_JSON:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _result(data: dict, mode: str, rejected: bool, unsent: tuple[str, ...] = ()) -> ChatResult:
        choice = ((data.get("choices") or [{}])[0]) or {}
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        # Reasoning comes back in a field of its own under one of two names, or
        # inline when the server was started without a parser for it.
        answer, inline = split_reasoning(message.get("content") or "")
        field = message.get("reasoning_content") or message.get("reasoning")
        return ChatResult(
            text=answer,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choice.get("finish_reason"),
            structured_output=mode,
            structured_output_rejected=rejected,
            reasoning=merge(field if isinstance(field, str) else None, inline),
            unsent=unsent,
        )

    # ── embed ────────────────────────────────────────────────────────────────
    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        try:
            r = self._post(
                "/v1/embeddings", {"model": model, "input": inputs}, timeout=EMBED_TIMEOUT
            )
            r.raise_for_status()
            rows = r.json().get("data", []) or []
        except Exception as e:  # noqa: BLE001
            raise http_error(e, f"Embeddings from '{model}' at {self.base_url}") from e
        rows = sorted((row for row in rows if isinstance(row, dict)), key=lambda row: row.get("index", 0))
        return [row.get("embedding") or [] for row in rows]
