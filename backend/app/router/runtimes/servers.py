"""Adapters for runtimes that speak the OpenAI dialect with a few extras of their own.

Each is the generic adapter plus the one answer that identifies it and whatever it
reports that the plain dialect does not — usually the context window. Where a field
below is marked unverified, it is taken from the runtime's documentation and has not
yet been seen from a running server; it is read defensively, so a runtime that
answers differently degrades to the generic adapter's "unknown", never to an error.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from app.router.runtimes.base import FINGERPRINT_TIMEOUT, LIST_TIMEOUT, get_json, http_error
from app.router.runtimes.openai_compat import OpenAICompatAdapter, _positive, api_root
import httpx

from app.router.runtimes.llamacpp import LlamaCppAdapter
from app.router.runtimes.types import (
    CONTEXT_REPORTED,
    KIND_CHAT,
    KIND_EMBEDDING,
    KIND_VISION,
    STRUCTURED_JSON,
    STRUCTURED_NONE,
    THINKING_SETTINGS,
    Hello,
    ModelEntry,
    ModelInfo,
)


def _model_entries(
    base_url: str, api_key: Optional[str], timeout: float, path: str = "/v1/models"
) -> Optional[list[dict]]:
    """The entries of an OpenAI-shaped model list — None unless it is one, non-empty,
    of objects. What every `owned_by` fingerprint reads first."""
    data = get_json(base_url, path, api_key, timeout=timeout)
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        return None
    return entries


def _owned_by(base_url: str, owner: str, api_key: Optional[str], timeout: float, path: str = "/v1/models") -> bool:
    entries = _model_entries(base_url, api_key, timeout, path)
    return entries is not None and all(e.get("owned_by") == owner for e in entries)


# ── LM Studio ────────────────────────────────────────────────────────────────
#: LM Studio's own REST API types each model: `llm`, `vlm` or `embeddings`.
_LMSTUDIO_KINDS = {"llm": KIND_CHAT, "vlm": KIND_VISION, "embeddings": KIND_EMBEDDING}


class LMStudioAdapter(OpenAICompatAdapter):
    """LM Studio: recognised by its own REST listing, `/api/v0/models`.

    That listing types each model and reports the longest window it can be loaded
    with (`max_context_length`) and whether it is loaded now (`state`). Chat and
    embeddings go through the OpenAI dialect like every other server.
    """

    runtime = "lmstudio"
    #: LM Studio documents `top_k` and `repeat_penalty` on its OpenAI endpoint
    #: (unverified against a running server); `min_p` is not listed, so not sent.
    sampling_supported = frozenset({*OpenAICompatAdapter.sampling_supported, "top_k", "repeat_penalty"})
    extra_sampling = {"top_k": "top_k", "repeat_penalty": "repeat_penalty"}

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        data = get_json(base, "/api/v0/models", api_key, timeout=timeout)
        if not (isinstance(data, dict) and isinstance(data.get("data"), list)):
            return None
        entries = data["data"]
        if entries and not all(isinstance(e, dict) and "type" in e and "state" in e for e in entries):
            return None
        return Hello(runtime=cls.runtime, base_url=base)

    def _rich(self) -> list[dict]:
        try:
            r = self._get("/api/v0/models", timeout=LIST_TIMEOUT)
            r.raise_for_status()
            data = r.json().get("data", []) or []
        except Exception as e:  # noqa: BLE001
            raise http_error(e, f"LM Studio at {self.base_url}") from e
        entries = [d for d in data if isinstance(d, dict) and d.get("id")]
        with self._lock:
            self._raw = {d["id"]: d for d in entries}
        return entries

    def list_models(self) -> list[ModelEntry]:
        return [
            ModelEntry(
                name=d["id"],
                kind=_LMSTUDIO_KINDS.get(str(d.get("type"))),
                capabilities=(str(d["type"]),) if d.get("type") else None,
                loaded=d.get("state") == "loaded" if d.get("state") else None,
            )
            for d in self._rich()
        ]

    def _loaded_window(self, model: str) -> Optional[int]:
        """The window a loaded instance was loaded with, from LM Studio's v1 listing.

        Only v1 (LM Studio 0.4 and later) reports it — `loaded_instances[].config.
        context_length` — and it is read defensively (unverified against a running
        server): any other shape reads as "not reported".
        """
        data = get_json(self.base_url, "/api/v1/models", self.api_key, timeout=FINGERPRINT_TIMEOUT)
        entries = data.get("models") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or model not in (entry.get("key"), entry.get("id")):
                continue
            for inst in entry.get("loaded_instances") or []:
                window = _positive(((inst or {}).get("config") or {}).get("context_length"))
                if window:
                    return window
        return None

    def model_info(self, model: str) -> Optional[ModelInfo]:
        raw = self.raw_entry(model)
        if not raw:
            try:
                self._rich()
            except Exception:  # noqa: BLE001
                return None
            raw = self.raw_entry(model)
        kind = _LMSTUDIO_KINDS.get(str(raw.get("type")))
        loaded = self._loaded_window(model)
        longest = _positive(raw.get("max_context_length"))
        warnings: tuple[str, ...] = ()
        if loaded:
            window, source = loaded, CONTEXT_REPORTED
        else:
            # The window it will actually run at is the one it is loaded with, which
            # this LM Studio does not report — and it is often far below the longest
            # the model supports. Budgeting for the longest is how a prompt gets cut;
            # the configured fallback is the stated assumption instead.
            window, source = None, None
            warnings = (
                "LM Studio doesn't report the window this model is loaded with"
                + (f" (it supports up to {longest:,} tokens)" if longest else "")
                + ", so the configured fallback is in force. Load it in LM Studio with at "
                "least that window, or long prompts are cut short.",
            )
        return ModelInfo(
            name=model,
            context_window=window,
            context_source=source,
            quantization=raw.get("quantization") or None,
            kind=kind,
            capabilities=(str(raw["type"]),) if raw.get("type") else None,
            structured_output=self.structured_mode(model) if kind != KIND_EMBEDDING else STRUCTURED_NONE,
            architecture=raw.get("arch") or None,
            listen_address=self.base_url,
            warnings=warnings,
        )


# ── vLLM ─────────────────────────────────────────────────────────────────────
#: vLLM and SGLang take their own samplers beside the dialect's, and switch thinking
#: through the chat template (unverified against running servers; from their docs).
_TEMPLATE_SERVER_SAMPLING = {"top_k": "top_k", "min_p": "min_p", "repeat_penalty": "repetition_penalty"}


class VLLMAdapter(OpenAICompatAdapter):
    """vLLM: `owned_by: "vllm"`, with `max_model_len` on every model entry."""

    runtime = "vllm"
    sampling_supported = frozenset({*OpenAICompatAdapter.sampling_supported, *_TEMPLATE_SERVER_SAMPLING})
    extra_sampling = _TEMPLATE_SERVER_SAMPLING
    thinking_supported = THINKING_SETTINGS
    thinking_via_template = True

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        if not _owned_by(base, "vllm", api_key, timeout):
            return None
        version = get_json(base, "/version", api_key, timeout=timeout)
        return Hello(
            runtime=cls.runtime,
            base_url=base,
            version=str(version.get("version")) if isinstance(version, dict) else None,
        )


# ── SGLang ───────────────────────────────────────────────────────────────────
class SGLangAdapter(OpenAICompatAdapter):
    """SGLang: `owned_by: "sglang"` (unverified); window from `/get_model_info`."""

    runtime = "sglang"
    sampling_supported = VLLMAdapter.sampling_supported
    extra_sampling = _TEMPLATE_SERVER_SAMPLING
    thinking_supported = THINKING_SETTINGS
    thinking_via_template = True

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        if not _owned_by(base, "sglang", api_key, timeout):
            return None
        # `/server_info` (older builds: `/get_server_info`) carries the version.
        info = get_json(base, "/server_info", api_key, timeout=timeout) or get_json(
            base, "/get_server_info", api_key, timeout=timeout
        )
        version = info.get("version") if isinstance(info, dict) else None
        return Hello(runtime=cls.runtime, base_url=base, version=str(version) if version else None)

    def model_info(self, model: str) -> Optional[ModelInfo]:
        info = super().model_info(model)
        extra = get_json(self.base_url, "/get_model_info", self.api_key, timeout=FINGERPRINT_TIMEOUT)
        window = _positive(extra.get("max_context_length")) if isinstance(extra, dict) else None
        if info is None or not window or info.context_window:
            return info
        return replace(info, context_window=window, context_source=CONTEXT_REPORTED)


# ── KoboldCpp ────────────────────────────────────────────────────────────────
class KoboldCppAdapter(OpenAICompatAdapter):
    """KoboldCpp: `/api/extra/version` names it; `/api/extra/true_max_context_length`
    is the window it was started with."""

    runtime = "koboldcpp"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        data = get_json(base, "/api/extra/version", api_key, timeout=timeout)
        if not (isinstance(data, dict) and str(data.get("result", "")).lower() == "koboldcpp"):
            return None
        return Hello(runtime=cls.runtime, base_url=base, version=str(data.get("version") or "") or None)

    def model_info(self, model: str) -> Optional[ModelInfo]:
        info = super().model_info(model)
        extra = get_json(
            self.base_url, "/api/extra/true_max_context_length", self.api_key, timeout=FINGERPRINT_TIMEOUT
        )
        window = _positive(extra.get("value")) if isinstance(extra, dict) else None
        if info is None or not window:
            return info
        return replace(info, context_window=window, context_source=CONTEXT_REPORTED)


# ── LocalAI ──────────────────────────────────────────────────────────────────
class LocalAIAdapter(OpenAICompatAdapter):
    """LocalAI: `/.well-known/localai.json`, or `/system` listing its backends.

    It shares llama.cpp's default port, so only this answer tells them apart — never
    the port, and never `/v1/models`, which LocalAI serves in the plain dialect. It
    also answers Ollama's routes (and says it is Ollama 0.9.0 there), which is why
    the Ollama fingerprint reads the root banner, not `/api/version`.
    Source: github.com/mudler/LocalAI core/http/routes/localai.go.
    """

    runtime = "localai"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        known = get_json(base, "/.well-known/localai.json", api_key, timeout=timeout)
        if not isinstance(known, dict):
            system = get_json(base, "/system", api_key, timeout=timeout)
            if not (isinstance(system, dict) and "backends" in system):
                return None
        version = get_json(base, "/version", api_key, timeout=timeout)
        return Hello(
            runtime=cls.runtime,
            base_url=base,
            version=str(version.get("version")) if isinstance(version, dict) else None,
        )



# ── Jan ──────────────────────────────────────────────────────────────────────
#: Jan's local API server names the engine behind each model in `owned_by` — and
#: `remote` for a hosted model it proxies. llama-server itself says `llamacpp`.
_JAN_OWNERS = {"llama.cpp", "mlx", "remote"}


class JanAdapter(OpenAICompatAdapter):
    """Jan's API server (127.0.0.1:1337): `owned_by` of `llama.cpp`, `mlx` or `remote`.

    Source: github.com/janhq/jan src-tauri/src/core/server/proxy.rs. No version
    endpoint and no window are reported. A model owned by `remote` runs on a hosted
    service, so it is listed as not local. Requests pass through to llama.cpp or
    MLX, so structured output and thinking are theirs; a refusal steps down.
    """

    runtime = "jan"
    thinking_supported = THINKING_SETTINGS
    thinking_via_template = True

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        entries = _model_entries(base, api_key, timeout)
        if entries is None or not all(e.get("owned_by") in _JAN_OWNERS for e in entries):
            return None
        return Hello(runtime=cls.runtime, base_url=base)

    def list_models(self) -> list[ModelEntry]:
        return [
            ModelEntry(name=d["id"], is_local=d.get("owned_by") != "remote") for d in self._models_payload()
        ]

    def model_info(self, model: str) -> Optional[ModelInfo]:
        info = super().model_info(model)
        if info is not None and self.raw_entry(model).get("owned_by") == "remote":
            return replace(info, is_local=False)
        return info


# ── llamafile ────────────────────────────────────────────────────────────────
class LlamafileAdapter(LlamaCppAdapter):
    """llamafile: a llama.cpp server in one file — the same API, answered the same way.

    Nothing it answers tells it apart from llama-server reliably (checked against
    docs.mozilla.ai/llamafile/using-llamafile/api), so detection reads it as
    llama.cpp — which is exactly the dialect it speaks. It is this runtime only
    when someone says so
    (`aiteam-connect add-source … --runtime llamafile`, or `runtime` in
    LOCAL_SOURCES); it then still has to answer as a llama.cpp server.
    """

    runtime = "llamafile"

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        hello = LlamaCppAdapter.fingerprint(base_url, api_key, timeout=timeout)
        if hello is None:
            return None
        return Hello(runtime=cls.runtime, base_url=hello.base_url, version=hello.version)


# ── text-generation-webui ────────────────────────────────────────────────────
class TextGenWebUIAdapter(OpenAICompatAdapter):
    """text-generation-webui's API (port 5000): `/v1/internal/model/info`.

    Source: github.com/oobabooga/text-generation-webui modules/api/script.py and
    typing.py. It takes a GBNF `grammar_string` but no `response_format` on chat,
    so no structured mode is asked for — the shape lives in the prompt, and
    validation with a repair round holds the answer to it. No window is reported.
    """

    runtime = "tgw"
    structured_ceiling = STRUCTURED_NONE
    sampling_supported = frozenset(
        {*OpenAICompatAdapter.sampling_supported, "top_k", "min_p", "repeat_penalty"}
    )
    extra_sampling = {"top_k": "top_k", "min_p": "min_p", "repeat_penalty": "repetition_penalty"}

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        info = get_json(base, "/v1/internal/model/info", api_key, timeout=timeout)
        if not (isinstance(info, dict) and "model_name" in info and "loader" in info):
            return None
        return Hello(runtime=cls.runtime, base_url=base)

    def _thinking_fields(self, request) -> dict:
        # Its chat endpoint takes these at the top level (modules/api/typing.py).
        level = request.thinking
        if level is None:
            return {}
        out: dict = {"enable_thinking": level != "off"}
        if level in ("low", "medium", "high"):
            out["reasoning_effort"] = level
        return out

    thinking_supported = THINKING_SETTINGS


# ── GPT4All ──────────────────────────────────────────────────────────────────
class GPT4AllAdapter(OpenAICompatAdapter):
    """GPT4All's API server (127.0.0.1:4891): every model `owned_by: "humanity"`.

    Source: github.com/nomic-ai/gpt4all gpt4all-chat/src/server.cpp. No window, no
    structured output, no thinking switch and no `/v1/embeddings` are documented,
    so none is asked for.
    """

    runtime = "gpt4all"
    structured_ceiling = STRUCTURED_NONE
    thinking_supported = ()
    serves_embeddings = False

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        return Hello(runtime=cls.runtime, base_url=base) if _owned_by(base, "humanity", api_key, timeout) else None

    def _thinking_fields(self, request) -> dict:
        return {}

    def list_models(self) -> list[ModelEntry]:
        return [ModelEntry(name=d["id"], kind=KIND_CHAT) for d in self._models_payload()]


# ── MLX-LM ───────────────────────────────────────────────────────────────────
class MLXAdapter(OpenAICompatAdapter):
    """`mlx_lm.server` (127.0.0.1:8080): model entries with no `owned_by`, a
    `/health` of `{"status": "ok"}`, and no llama.cpp `/props`.

    Source: github.com/ml-explore/mlx-lm mlx_lm/server.py. It reports no window and
    takes no `response_format`. Thinking goes through `chat_template_kwargs`. It has
    no embeddings endpoint.

    **It loads whatever a request names** — any Hugging Face repo or local path,
    downloading it first. So a request here only ever names a model the server
    already listed; the connector refuses anything else.
    """

    runtime = "mlx"
    structured_ceiling = STRUCTURED_NONE
    thinking_supported = THINKING_SETTINGS
    thinking_via_template = True
    serves_embeddings = False
    sampling_supported = frozenset({*OpenAICompatAdapter.sampling_supported, "top_k", "min_p", "repeat_penalty"})
    extra_sampling = {"top_k": "top_k", "min_p": "min_p", "repeat_penalty": "repetition_penalty"}

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        entries = _model_entries(base, api_key, timeout)
        if entries is None or any("owned_by" in e for e in entries):
            return None
        health = get_json(base, "/health", api_key, timeout=timeout)
        if not (isinstance(health, dict) and health.get("status") == "ok"):
            return None
        if get_json(base, "/props", api_key, timeout=timeout) is not None:
            return None  # llama.cpp answers this; mlx_lm.server doesn't
        return Hello(runtime=cls.runtime, base_url=base)

    def _thinking_fields(self, request) -> dict:
        # Only the template switch: it has no `reasoning_effort` of its own.
        if request.thinking is None:
            return {}
        return {"chat_template_kwargs": {"enable_thinking": request.thinking != "off"}}

    def list_models(self) -> list[ModelEntry]:
        return [ModelEntry(name=d["id"], kind=KIND_CHAT) for d in self._models_payload()]


# ── Docker Model Runner ──────────────────────────────────────────────────────
_DMR_BANNER = "Docker Model Runner is running"


def _dmr_root(base_url: str) -> str:
    """The server's root, however its address was written.

    Docker documents the base as `…:12434/engines/v1`; `api_root` takes the `/v1`
    off, and this the `/engines` — which the adapter adds back itself.
    """
    base = api_root(base_url)
    return base[: -len("/engines")] if base.endswith("/engines") else base


class DockerModelRunnerAdapter(OpenAICompatAdapter):
    """Docker Model Runner (TCP 12434): the OpenAI dialect under `/engines`.

    Sources: docs.docker.com/ai/model-runner/api-reference and github.com/docker/
    model-runner pkg/inference/models/api.go. Recognised by its root banner or by
    `owned_by: "docker"` under `/engines/v1/models`; its window is
    `dmr.context_window` on each entry; it documents `json_object`, not schemas.
    """

    runtime = "dmr"
    api_prefix = "/engines"
    structured_ceiling = STRUCTURED_JSON

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        super().__init__(_dmr_root(base_url), api_key)

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = _dmr_root(base_url)
        banner = False
        try:
            r = httpx.get(f"{base}/", timeout=timeout)
            banner = r.status_code == 200 and _DMR_BANNER in (r.text or "")
        except Exception:  # noqa: BLE001
            pass
        if not banner and not _owned_by(base, "docker", api_key, timeout, "/engines/v1/models"):
            return None
        version = get_json(base, "/version", api_key, timeout=timeout)
        return Hello(
            runtime=cls.runtime,
            base_url=base,
            version=str(version.get("version")) if isinstance(version, dict) and version.get("version") else None,
        )

    def model_info(self, model: str) -> Optional[ModelInfo]:
        info = super().model_info(model)
        dmr = self.raw_entry(model).get("dmr")
        window = _positive(dmr.get("context_window")) if isinstance(dmr, dict) else None
        if info is None or not window:
            return info
        return replace(info, context_window=window, context_source=CONTEXT_REPORTED)


# ── Foundry Local ────────────────────────────────────────────────────────────
class FoundryLocalAdapter(OpenAICompatAdapter):
    """Microsoft Foundry Local: `/openai/status` answers `{Endpoints, ModelDirPath, …}`.

    Source: learn.microsoft.com/azure/foundry-local/reference/reference-rest. Its
    port is chosen at start, so it is never probed — it is added by its address
    (`foundry service status` prints it). No window is reported; structured output
    and thinking aren't documented, so JSON mode is tried and stepped down from on
    a refusal. Embeddings since 1.1.
    """

    runtime = "foundry"
    structured_ceiling = STRUCTURED_JSON
    thinking_supported = ()

    @classmethod
    def fingerprint(
        cls, base_url: str, api_key: Optional[str] = None, *, timeout: float = FINGERPRINT_TIMEOUT
    ) -> Optional[Hello]:
        base = api_root(base_url)
        status = get_json(base, "/openai/status", api_key, timeout=timeout)
        if not (isinstance(status, dict) and "Endpoints" in status):
            return None
        return Hello(runtime=cls.runtime, base_url=base)

    def _thinking_fields(self, request) -> dict:
        return {}
