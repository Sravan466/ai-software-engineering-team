"""The Ollama adapter: every piece of Ollama's own API, and nowhere else.

Two things happen here that the pipeline's correctness rests on. The request carries
a **context window** (`num_ctx`) and an **output budget** (`num_predict`) resolved from
the model's own metadata — without them every call ran at the server's small default
window and Ollama silently truncated the prompt from the head, taking the system
prompt (and with it the required output shape) first. And when the server and model
support it, `format` carries the **JSON Schema** the agent must return rather than
the bare string `"json"`, which only ever promised valid JSON, not the right JSON.

Other runtimes speak parts of this dialect too (`/api/tags`, `/api/show`, `/api/chat`
are served by more than one product), so this adapter is chosen by the one answer
only Ollama gives — its root page — never by the port or the paths it serves.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Iterator, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.router.base import ProviderError
from app.router.runtimes.base import (
    CHAT_TIMEOUT,
    EMBED_TIMEOUT,
    FINGERPRINT_TIMEOUT,
    INFO_TIMEOUT,
    LIST_TIMEOUT,
    RuntimeAdapter,
    get_json,
    http_error,
)
from app.router.runtimes.types import (
    CONTEXT_REPORTED,
    KIND_CHAT,
    KIND_EMBEDDING,
    KIND_VISION,
    STRUCTURED_JSON,
    STRUCTURED_NONE,
    STRUCTURED_SCHEMA,
    ChatRequest,
    ChatResult,
    Hello,
    ModelEntry,
    ModelInfo,
    writes,
)

log = get_logger(__name__)

#: Schema-constrained `format` landed in Ollama 0.5. Older servers accept the string
#: "json" only, and sending them a schema object is a 400 — so the version is asked
#: for rather than assumed, and anything below this degrades to plain JSON mode.
_SCHEMA_FORMAT_MIN_VERSION = (0, 5, 0)
#: The runtime's words for "writes text", "turns text into vectors", "reads images"
#: and "thinks first". `completion` and `embedding` are decided by the same branch
#: when the runtime reads the model file, so one without the other is definite.
_COMPLETION_CAPABILITY = "completion"
_EMBEDDING_CAPABILITY = "embedding"
_VISION_CAPABILITY = "vision"
_THINKING_CAPABILITY = "thinking"
#: A capability probe is one small JSON answer from a runtime that is already known to
#: be up — the tag list just came back. Anything slower than this is a runtime in
#: trouble, and the Settings page should not wait the ten seconds a profile probe
#: (which may be loading a model) is allowed.
_CAPABILITY_PROBE_TIMEOUT = 3.0
#: Probes for models the tag list did not describe run side by side, so ten pulled
#: models on an older runtime cost one timeout rather than ten in a row.
_MAX_PARALLEL_PROBES = 8
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
#: What the root page says. The one reply no other runtime gives.
_ROOT_BANNER = "Ollama is running"
#: Ollama's hosted models are listed beside local ones and run on ollama.com. Their
#: tag ends in `cloud` (`gpt-oss:120b-cloud`), and the tag list marks them with a
#: remote host. Either is enough: such a model is not local, whatever serves its name.
_CLOUD_TAG = re.compile(r"(^|[-:])cloud$")


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


def _canonical(model: str) -> str:
    """The one spelling a model's capabilities are kept under: always `name:tag`.

    One key per model, not one per spelling. With several, an answer written under
    `nomic-embed-text` (a failed probe, say) and a fresher one from the tag list under
    `nomic-embed-text:latest` both stayed live, and whichever spelling was looked up
    first won — so the stale one could hide the fresh one for its whole lifetime.
    """
    name, sep, tag = model.rpartition(":")
    return f"{model}:latest" if not sep or "/" in tag else model


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


def kind_from_capabilities(capabilities: Optional[Iterable[str]]) -> Optional[str]:
    """The model's kind, read from the runtime's capability list; None if unknown.

    Only positive evidence decides. The runtime reports `completion` or `embedding`
    after reading the model file; when it cannot read the file it reports neither,
    and whatever the template adds (`tools`, say) is all that is left. So `[]` and
    `["tools"]` are "could not tell", not "cannot write" — treating them as a no
    would refuse a working chat model the moment its file hiccupped.
    """
    if capabilities is None:
        return None
    reported = tuple(capabilities)
    if _COMPLETION_CAPABILITY in reported:
        return KIND_VISION if _VISION_CAPABILITY in reported else KIND_CHAT
    if _EMBEDDING_CAPABILITY in reported:
        return KIND_EMBEDDING
    return None


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


#: Bytes per element in the KV cache (f16 — the runtime's default). A property of the
#: cache format, not a budget: it is what one number in the cache weighs.
_KV_BYTES_PER_ELEMENT = 2
#: K and V are both cached, so a token costs two of everything below.
_KV_TENSORS_PER_TOKEN = 2


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


def context_length_from(model_info: dict, arch: str) -> Optional[int]:
    """The window the model was trained for, from `/api/show`'s `model_info`."""
    found = _int(model_info.get(f"{arch}.context_length")) if arch else None
    if found is not None:
        return found
    # Some architectures spell it differently; take the only context length there is.
    lengths = [n for n in (_int(v) for k, v in model_info.items() if k.endswith(".context_length")) if n]
    return max(lengths) if lengths else None


def info_from_show(
    model: str,
    show: dict,
    *,
    supports_schema: bool,
    version: Optional[str] = None,
    listen_address: Optional[str] = None,
) -> ModelInfo:
    """Turn one `/api/show` payload into the normalised record."""
    model_info = show.get("model_info") or {}
    details = show.get("details") or {}
    arch = str(model_info.get("general.architecture") or details.get("family") or "").strip()
    context = context_length_from(model_info, arch)
    reported = show.get("capabilities")
    caps = tuple(str(c) for c in reported) if isinstance(reported, (list, tuple)) else None
    return ModelInfo(
        name=model,
        context_window=context,
        context_source=CONTEXT_REPORTED if context else None,
        parameters_total=parameter_count_from(model_info, details),
        parameter_label=details.get("parameter_size") or None,
        quantization=details.get("quantization_level") or None,
        kind=kind_from_capabilities(caps),
        capabilities=caps,
        structured_output=STRUCTURED_SCHEMA if supports_schema else STRUCTURED_JSON,
        thinking=(
            None
            if caps is None
            else "toggle" if _THINKING_CAPABILITY in caps else "none"
        ),
        is_local=not _is_remote(model, show),
        architecture=arch or None,
        kv_bytes_per_token=kv_bytes_per_token(model_info, arch) if arch else None,
        runtime_version=version,
        listen_address=listen_address,
    )


def _is_remote(name: str, payload: dict) -> bool:
    """Whether the runtime sends this model elsewhere to run."""
    if payload.get("remote_host") or payload.get("remote_model"):
        return True
    _, sep, tag = name.rpartition(":")
    return bool(sep) and "/" not in tag and bool(_CLOUD_TAG.search(tag))


class _SchemaFormatRejected(RuntimeError):
    """The server would not take a JSON Schema in `format`; retry in plain JSON mode."""


def _as_provider_error(error: Exception, model: str) -> ProviderError:
    """Normalise a failed call, keeping the one hint that usually resolves it."""
    return http_error(
        error,
        "Ollama",
        advice=f". Is the model '{model}' pulled? Try `ollama pull {model}`.",
    )


class OllamaAdapter(RuntimeAdapter):
    runtime = "ollama"
    can_download = True

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        super().__init__(base_url, api_key)
        self._version: Optional[tuple[int, ...]] = None
        #: What each model says it can do: `(answer, expires_at)` on the monotonic
        #: clock. A plain dict is enough: every write is the same answer to the same
        #: question, so two threads racing cost one duplicate HTTP call and nothing
        #: else.
        self._capabilities: dict[str, tuple[object, float]] = {}
        #: Models the tag list marked as running elsewhere, by canonical name.
        self._remote: set[str] = set()

    # ── identification ───────────────────────────────────────────────────────
    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = base_url.rstrip("/")
        try:
            r = httpx.get(f"{base}/", timeout=timeout)
        except Exception:  # noqa: BLE001
            return None
        if r.status_code != 200 or _ROOT_BANNER not in (r.text or ""):
            return None
        version = get_json(base, "/api/version", api_key, timeout=timeout)
        return Hello(
            runtime=cls.runtime,
            base_url=base,
            version=str(version.get("version")) if isinstance(version, dict) else None,
        )

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
            r = self._get("/api/version", timeout=2.0)
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

    # ── list_models ──────────────────────────────────────────────────────────
    def list_models(self) -> list[ModelEntry]:
        try:
            r = self._get("/api/tags", timeout=LIST_TIMEOUT)
            r.raise_for_status()
            entries = r.json().get("models", []) or []
        except Exception as e:  # noqa: BLE001
            raise http_error(e, f"Ollama at {self.base_url}") from e
        out: list[ModelEntry] = []
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            # Recent servers report capabilities in the tag list itself, which answers
            # for every pulled model in the one round trip the caller was making
            # anyway. Only an entry that actually carries the key is remembered:
            # recording a missing key as "reported nothing" would cache an older
            # server's silence as an answer and stop the probe that *can* get one.
            if isinstance(entry.get("capabilities"), (list, tuple)):
                self._remember_capabilities(name, entry)
            remote = _is_remote(name, entry)
            (self._remote.add if remote else self._remote.discard)(_canonical(name))
            caps = self.known_capabilities(name)
            out.append(
                ModelEntry(
                    name=name,
                    kind=kind_from_capabilities(caps),
                    capabilities=caps,
                    is_local=not remote,
                    size_bytes=entry.get("size") if isinstance(entry.get("size"), int) else None,
                )
            )
        return out

    def describe(self, entries: list[ModelEntry]) -> dict[str, ModelEntry]:
        """The tag list's entries, with capabilities probed where it left them out.

        Normally free — a recent tag list already said. What it did not describe is
        probed in parallel, with the short timeout, so an older runtime with many
        models costs one wait rather than one per model. A model the runtime will
        not describe keeps `capabilities=None`: unknown is not the same as empty.
        """
        self.capabilities_for([e.name for e in entries])
        out: dict[str, ModelEntry] = {}
        for entry in entries:
            caps = self.known_capabilities(entry.name)
            out[entry.name] = ModelEntry(
                name=entry.name,
                kind=kind_from_capabilities(caps),
                capabilities=caps,
                is_local=entry.is_local,
                size_bytes=entry.size_bytes,
            )
        return out

    def known_kind(self, model: str) -> Optional[ModelEntry]:
        caps = self.known_capabilities(model)
        if caps is None:
            return None
        return ModelEntry(
            name=model,
            kind=kind_from_capabilities(caps),
            capabilities=caps,
            is_local=_canonical(model) not in self._remote,
        )

    def resolves(self, model: str, names: Iterable[str]) -> bool:
        """Whether `model` names one of `names`, by the runtime's own rule.

        An untagged name means `:latest` and nothing else. The looser rule this
        replaced — any pulled model with the same base — called `nomic-embed-text`
        present when only `nomic-embed-text:v1.5` was, which the runtime would then
        refuse to load; and it disagreed with the capability lookup, so the two
        checks before a run could reach opposite answers about one name.
        """
        pulled = set(names)
        return any(name in pulled for name in _spellings(model))

    # ── model_info ───────────────────────────────────────────────────────────
    def model_info(self, model: str) -> Optional[ModelInfo]:
        show = self._show(model)
        if show is None:
            return None
        capabilities = self._remember_capabilities(model, show)
        version = self.server_version()
        supports_schema = bool(
            version
            and version >= _SCHEMA_FORMAT_MIN_VERSION
            # Only a model the runtime positively says cannot write is refused here —
            # an older server that reports nothing, or a file it could not read, is
            # taken on the version.
            and writes(kind_from_capabilities(capabilities)) is not False
        )
        return info_from_show(
            model,
            show,
            supports_schema=supports_schema,
            version=".".join(str(p) for p in version) if version else None,
            listen_address=self.base_url,
        )

    # ── the capability cache ─────────────────────────────────────────────────
    def capabilities(self, model: str) -> Optional[tuple[str, ...]]:
        """What the runtime says this model can do — or None when it will not say.

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
            self._capabilities[_canonical(model)] = (
                _FAILED,
                time.monotonic() + _FAILED_PROBE_TTL_SECONDS,
            )
            return None
        return self._remember_capabilities(model, show)

    def known_capabilities(self, model: str) -> Optional[tuple[str, ...]]:
        """What is already remembered about `model` — never a network call."""
        remembered = self._remembered_capabilities(model)
        if remembered is _MISSING or remembered is _FAILED:
            return None
        return remembered  # type: ignore[return-value]

    def capabilities_for(self, models: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """`{model: capabilities}` for every model the runtime will describe."""
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

    def _remembered_capabilities(self, model: str) -> object:
        """The live answer for `model` however it is spelled, `_FAILED`, or `_MISSING`."""
        entry = self._capabilities.get(_canonical(model))
        if entry is not None and entry[1] > time.monotonic():
            return entry[0]
        return _MISSING

    def _remember_capabilities(self, model: str, show: dict) -> Optional[tuple[str, ...]]:
        """Record what one payload said about capabilities, and return it.

        Shared so that the capability probe and the profile probe cannot drift, and
        so a profiled model does not get asked a second time for the half of the
        payload the first call already had in its hands.
        """
        reported = show.get("capabilities")
        caps = (
            tuple(str(c) for c in reported) if isinstance(reported, (list, tuple)) else None
        )
        self._capabilities[_canonical(model)] = (
            caps,
            time.monotonic() + _CAPABILITY_TTL_SECONDS,
        )
        return caps

    def _show(
        self, model: str, *, note: Optional[str] = None, timeout: float = INFO_TIMEOUT
    ) -> Optional[dict]:
        try:
            r = self._post("/api/show", {"model": model}, timeout=timeout)
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

    def forget(self, model: Optional[str] = None) -> None:
        """Drop what a pull made stale — for a *named* model only.

        A pull replaces that model's weights, so what it can do may have changed with
        them — under every spelling of its name, or re-pulling `llama3.1` would leave
        the answer cached under `llama3.1:latest` untouched. A new default changes
        which model runs, not what any model can do, so it forgets nothing here;
        wiping every answer made choosing a model re-probe all of the others inline.
        """
        if model:
            self._capabilities.pop(_canonical(model), None)

    # ── chat ─────────────────────────────────────────────────────────────────
    def chat(self, request: ChatRequest) -> ChatResult:
        rejected = False
        try:
            data = self._chat(self._payload(request, schema=True), request.request_id)
        except _SchemaFormatRejected as e:
            # The server took the schema badly (an old build, or one whose grammar
            # converter cannot express this shape). Valid JSON plus the shape written
            # into the prompt is the documented degradation — and validation with a
            # repair round still catches anything that drifts.
            log.warning(
                "Ollama rejected schema-constrained decoding for %s (%s); retrying in "
                "plain JSON mode. Validation and repair still apply.",
                request.model,
                e,
            )
            rejected = True
            # The plain-JSON retry is the real attempt now, so its failure is the
            # one worth reporting: a 400 that was never about `format` (a model that
            # is not pulled, say) still reaches the user as the advice they need.
            try:
                data = self._chat(self._payload(request, schema=False), request.request_id)
            except Exception as inner:  # noqa: BLE001
                raise _as_provider_error(inner, request.model) from inner
        except Exception as e:  # noqa: BLE001
            raise _as_provider_error(e, request.model) from e

        schema_sent = not rejected and bool(request.json_schema) and (
            request.structured_output == STRUCTURED_SCHEMA
        )
        return ChatResult(
            text=(data.get("message") or {}).get("content", ""),
            prompt_tokens=data.get("prompt_eval_count", 0) or 0,
            completion_tokens=data.get("eval_count", 0) or 0,
            finish_reason=data.get("done_reason"),
            structured_output=(
                STRUCTURED_SCHEMA
                if schema_sent
                else STRUCTURED_JSON if (request.json_mode or request.json_schema) else STRUCTURED_NONE
            ),
            structured_output_rejected=rejected,
        )

    def _payload(self, request: ChatRequest, *, schema: bool) -> dict:
        """The request body, with the window and the output budget always present."""
        payload: dict = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
            "options": {
                # The two that were missing. Without num_ctx the server falls back to
                # its own small default and truncates the prompt from the head; without
                # num_predict the output budget an agent asked for was never honoured.
                "num_ctx": request.context_window,
                "num_predict": request.max_tokens,
            },
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        if request.top_p is not None:
            payload["options"]["top_p"] = request.top_p

        if request.json_schema and schema and request.structured_output == STRUCTURED_SCHEMA:
            payload["format"] = request.json_schema
        elif request.json_mode or request.json_schema:
            payload["format"] = "json"
        return payload

    def _chat(self, payload: dict, request_id: Optional[str] = None) -> dict:
        r = self._post_cancellable(
            "/api/chat", payload, timeout=CHAT_TIMEOUT, request_id=request_id
        )
        # Only a 400 is worth retrying without the schema. A 404 is a model that is
        # not pulled and a 5xx is a server in trouble; neither gets better by asking
        # again, and the second attempt would only delay the real error.
        if r.status_code == 400 and isinstance(payload.get("format"), dict):
            raise _SchemaFormatRejected(r.text[:200])
        r.raise_for_status()
        return r.json()

    # ── embed ────────────────────────────────────────────────────────────────
    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        try:
            r = self._post("/api/embed", {"model": model, "input": inputs}, timeout=EMBED_TIMEOUT)
            r.raise_for_status()
            return r.json().get("embeddings", []) or []
        except Exception as e:  # noqa: BLE001
            raise http_error(
                e,
                f"Ollama embeddings (model '{model}')",
                advice=f". Pull it with `ollama pull {model}`.",
            ) from e

    # ── downloads ────────────────────────────────────────────────────────────
    def pull(self, model: str) -> Iterator[dict]:
        """Stream `/api/pull`, one progress object per line."""
        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/api/pull",
                json={"name": model},
                headers=self.headers(),
                timeout=None,
            ) as r:
                if r.status_code != 200:
                    r.read()
                    yield {"error": f"Ollama returned {r.status_code}: {r.text[:200]}"}
                    return
                for line in r.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except Exception as e:  # noqa: BLE001 - surface a clean error line to the client
            yield {"error": f"Could not reach Ollama at {self.base_url}: {e}."}
        finally:
            self.forget(model)
