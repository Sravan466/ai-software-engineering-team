"""The runtime adapter table: the one place a runtime's name, port or habits are written.

Everything else in the application talks to *sources* through typed operations and
learns what a source is from what it answers. This table is what it answers *with*:
a label a person recognises, the default addresses worth probing on this machine,
the adapter that speaks its dialect, and what to tell someone who wants another
model on it.

Ports are candidates, never identities. `8080` is the default of several runtimes
here, and anything can run on any port — so detection probes these ports on
loopback and then asks each adapter in turn whether the answer is its runtime.

Checked against each runtime's documentation on 2026-09-17; a row with no adapter of
its own is served by the generic OpenAI-compatible one once someone confirms it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Type

from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.llamacpp import LlamaCppAdapter
from app.router.runtimes.ollama import OllamaAdapter
from app.router.runtimes.openai_compat import OpenAICompatAdapter
from app.router.runtimes.servers import (
    KoboldCppAdapter,
    LMStudioAdapter,
    LocalAIAdapter,
    SGLangAdapter,
    VLLMAdapter,
)


@dataclass(frozen=True)
class RuntimeSpec:
    id: str
    label: str
    #: Default ports, probed on loopback only. Empty for a runtime with no default.
    ports: tuple[int, ...]
    #: The adapter that speaks to it. Runtimes without one of their own use the
    #: generic adapter, and are never adopted by detection without confirmation.
    adapter: Optional[Type[RuntimeAdapter]]
    #: How to get another model onto it, when the API cannot do it for you.
    add_model: str
    #: Where to get the runtime itself.
    home: Optional[str] = None
    #: Where its models are browsed, when it downloads them itself.
    library: Optional[str] = None
    #: How to give a model a longer window on it, when the window is too short.
    window_hint: str = "Start the server with a larger context window."
    #: Where its KV cache type is set, and what it does when it cannot use one.
    kv_hint: str = "Set it where the server is started."


GENERIC = "openai-compatible"

RUNTIMES: tuple[RuntimeSpec, ...] = (
    RuntimeSpec(
        id="ollama",
        label="Ollama",
        ports=(11434,),
        adapter=OllamaAdapter,
        add_model="Download one below, or run `ollama pull <name>` on the machine it runs on.",
        home="https://ollama.com/download",
        library="https://ollama.com/library",
        window_hint="Ollama runs a model at the window each call asks for, so this is the model's own limit.",
        kv_hint=(
            "Ollama sets it with OLLAMA_KV_CACHE_TYPE, and falls back to f16 without saying "
            "so on architectures that can't use a quantized cache."
        ),
    ),
    RuntimeSpec(
        id="lmstudio",
        label="LM Studio",
        ports=(1234,),
        adapter=LMStudioAdapter,
        add_model="Download models from LM Studio's Discover tab, then load one in its Developer tab.",
        home="https://lmstudio.ai",
        window_hint="Load it in LM Studio with a larger context length.",
        kv_hint="LM Studio sets it in the model's load settings.",
    ),
    RuntimeSpec(
        id="llamacpp",
        label="llama.cpp",
        ports=(8080,),
        adapter=LlamaCppAdapter,
        add_model=(
            "llama-server serves the model it was started with (`-m`). Start another on a "
            "different port for a second model, or run it in router mode."
        ),
        home="https://github.com/ggml-org/llama.cpp",
        window_hint="Restart llama-server with a larger `-c`.",
        kv_hint="llama-server sets it with `--cache-type-k` and `--cache-type-v`.",
    ),
    RuntimeSpec(
        id="vllm",
        label="vLLM",
        ports=(8000,),
        adapter=VLLMAdapter,
        add_model="vLLM serves the model it was started with (`vllm serve <model>`).",
        home="https://docs.vllm.ai",
        window_hint="Restart vLLM with a larger `--max-model-len`.",
        kv_hint="vLLM sets it with `--kv-cache-dtype`.",
    ),
    RuntimeSpec(
        id="sglang",
        label="SGLang",
        ports=(30000,),
        adapter=SGLangAdapter,
        add_model="SGLang serves the model it was launched with (`--model-path`).",
        home="https://docs.sglang.ai",
        window_hint="Relaunch SGLang with a larger `--context-length`.",
        kv_hint="SGLang sets it with `--kv-cache-dtype`.",
    ),
    RuntimeSpec(
        id="koboldcpp",
        label="KoboldCpp",
        ports=(5001,),
        adapter=KoboldCppAdapter,
        add_model="KoboldCpp serves the model it was launched with.",
        home="https://github.com/LostRuins/koboldcpp",
        window_hint="Relaunch KoboldCpp with a larger `--contextsize`.",
    ),
    RuntimeSpec(
        id="localai",
        label="LocalAI",
        ports=(8080,),
        adapter=LocalAIAdapter,
        add_model="Install models from LocalAI's model gallery.",
        home="https://localai.io",
        window_hint="Raise `context_size` in the model's LocalAI config.",
    ),
    # No adapter of their own yet: they answer as the generic dialect, so detection
    # shows them as unknown until someone confirms what they are.
    RuntimeSpec("jan", "Jan", (1337,), None, "Download models in Jan's Hub.", "https://jan.ai"),
    RuntimeSpec("llamafile", "llamafile", (8080,), None, "A llamafile serves the model it was built with."),
    RuntimeSpec(
        "tgw",
        "text-generation-webui",
        (5000,),
        None,
        "Load a model from text-generation-webui's Model tab.",
    ),
    RuntimeSpec("gpt4all", "GPT4All", (4891,), None, "Download models in GPT4All's Models view."),
    RuntimeSpec("mlx", "MLX-LM", (8080,), None, "MLX-LM serves the model it was started with."),
    RuntimeSpec("dmr", "Docker Model Runner", (12434,), None, "Run `docker model pull <name>`."),
    RuntimeSpec(
        GENERIC,
        "OpenAI-compatible server",
        (),
        OpenAICompatAdapter,
        "Add models the way this server's own documentation describes.",
    ),
)

BY_ID: dict[str, RuntimeSpec] = {spec.id: spec for spec in RUNTIMES}

#: Runtimes detection can recognise by their answer, in the order they are asked.
#: The ones whose fingerprint is most specific go first, the generic dialect never.
FINGERPRINTED: tuple[RuntimeSpec, ...] = tuple(
    spec for spec in RUNTIMES if spec.adapter is not None and spec.id != GENERIC
)

#: Before model sources existed, a model name with no source in front of it — in a
#: saved role, a saved default, or `FALLBACK_CHAIN` — meant this runtime. Settings
#: written then are read with it; nothing written now is ever unprefixed.
LEGACY_BARE_RUNTIME = "ollama"

#: What a source id looks like: a runtime id, optionally with a suffix that keeps
#: two sources of the same runtime apart (`llamacpp-8081`).
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def spec_for(runtime: Optional[str]) -> RuntimeSpec:
    return BY_ID.get(runtime or "", BY_ID[GENERIC])


def adapter_for(runtime: Optional[str]) -> Type[RuntimeAdapter]:
    return spec_for(runtime).adapter or OpenAICompatAdapter


def probe_ports() -> tuple[int, ...]:
    """Every default port in the table, once each, in table order."""
    return tuple(dict.fromkeys(port for spec in RUNTIMES for port in spec.ports))


def looks_like_source_id(value: str) -> bool:
    """Whether `value` could be a source id: a runtime id, or one with a suffix.

    Used to read `source:model` when that source is not connected right now — a
    saved choice for a runtime that is stopped is still a choice for that runtime,
    not a model called `ollama` with a tag.
    """
    if not _SOURCE_ID.match(value):
        return False
    base = value
    while base and base not in BY_ID:
        head, sep, _ = base.rpartition("-")
        if not sep:
            return False
        base = head
    return bool(base)
