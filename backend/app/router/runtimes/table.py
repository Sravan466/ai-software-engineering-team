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
class SetupGuide:
    """How a person gets this runtime going on their own computer — the Setup tab's
    card for it. Text, with `code` in backticks; each OS links the official source.

    Advice about *which* model to download is not here: that depends on the memory
    of the computer, which only the connector knows, and it is given as a size and
    a quantization, never a model name.
    """

    #: `{macos, windows, linux}` → how to install it there. Missing means unsupported.
    install: dict
    #: How to get a model onto it.
    download: str
    #: Which kind of model serves embeddings on it, and how to get one.
    embeddings: str
    #: What "running" means for it — the server the connector talks to.
    serve: str
    #: A command that proves it is running, run on the same computer.
    check: str
    #: Its own way of listening on more than this computer, when it has one.
    exposure: Optional[str] = None


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
    #: Its card on the Setup tab. None for runtimes the Setup tab doesn't offer.
    setup: Optional[SetupGuide] = None


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
        setup=SetupGuide(
            install={
                "macos": "Download the app from ollama.com/download and open it, or `brew install ollama`.",
                "windows": "Download and run OllamaSetup.exe from ollama.com/download.",
                "linux": "Run the install script from ollama.com/download: `curl -fsSL https://ollama.com/install.sh | sh`.",
            },
            download="Browse ollama.com/library and run `ollama pull <name>` in a terminal.",
            embeddings=(
                "Pull a second, small model tagged Embedding in the library. Without one, builds still "
                "run, but your uploaded documents and the crew's memory aren't searched."
            ),
            serve="The Ollama app runs the server while it is open. Without the app, run `ollama serve`.",
            check="curl http://127.0.0.1:11434  →  Ollama is running",
            exposure="Leave `OLLAMA_HOST` unset (it listens on 127.0.0.1), and never set `OLLAMA_ORIGINS=*`.",
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
        setup=SetupGuide(
            install={
                "macos": "Download LM Studio from lmstudio.ai and move it to Applications.",
                "windows": "Download and run the installer from lmstudio.ai.",
                "linux": "Download the AppImage from lmstudio.ai and make it executable.",
            },
            download="Search the Discover tab, download a GGUF (or MLX on Apple silicon) build, then load it.",
            embeddings=(
                "Download a model marked Embedding in Discover and load it next to the chat model. "
                "Without one, uploaded documents and the crew's memory aren't searched."
            ),
            serve="Open the Developer tab and switch the server on, or run `lms server start`.",
            check="curl http://127.0.0.1:1234/v1/models",
            exposure="Keep \"Serve on Local Network\" off in the server settings.",
        ),
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
        setup=SetupGuide(
            install={
                "macos": "`brew install llama.cpp`, or a prebuilt release from github.com/ggml-org/llama.cpp/releases.",
                "windows": "`winget install llama.cpp`, or a prebuilt release from github.com/ggml-org/llama.cpp/releases.",
                "linux": "`brew install llama.cpp`, or a prebuilt release from github.com/ggml-org/llama.cpp/releases.",
            },
            download="Download a `.gguf` file from Hugging Face; llama-server serves the one file you start it with.",
            embeddings=(
                "Start a second `llama-server --embeddings -m <embedding model>.gguf --port 8081`. "
                "Without one, uploaded documents and the crew's memory aren't searched."
            ),
            serve="`llama-server -m <model>.gguf --port 8080 -c 16384` — it runs as long as that terminal does.",
            check="curl http://127.0.0.1:8080/health",
            exposure="It listens on 127.0.0.1 by default. Never start it with `--host 0.0.0.0`.",
        ),
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
        setup=SetupGuide(
            install={
                "linux": "`pip install vllm` in a virtual environment (needs a supported GPU). See docs.vllm.ai.",
                "windows": "Not native: install it inside WSL 2 following the Linux steps at docs.vllm.ai.",
            },
            download="vLLM downloads the Hugging Face model you name when it starts.",
            embeddings="vLLM serves one model per server; start a second one with an embedding model on another port.",
            serve="`vllm serve <model> --host 127.0.0.1 --port 8000`",
            check="curl http://127.0.0.1:8000/v1/models",
            exposure="vLLM listens on every interface unless you pass `--host 127.0.0.1`. Always pass it.",
        ),
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
        setup=SetupGuide(
            install={
                "linux": "`pip install \"sglang[all]\"` in a virtual environment (needs a supported GPU). See docs.sglang.ai.",
            },
            download="SGLang downloads the Hugging Face model you name with `--model-path`.",
            embeddings="Launch a second server with an embedding model and `--is-embedding` on another port.",
            serve="`python -m sglang.launch_server --model-path <model> --host 127.0.0.1 --port 30000`",
            check="curl http://127.0.0.1:30000/v1/models",
            exposure="Pass `--host 127.0.0.1` so it isn't reachable from your network.",
        ),
    ),
    RuntimeSpec(
        id="koboldcpp",
        label="KoboldCpp",
        ports=(5001,),
        adapter=KoboldCppAdapter,
        add_model="KoboldCpp serves the model it was launched with.",
        home="https://github.com/LostRuins/koboldcpp",
        window_hint="Relaunch KoboldCpp with a larger `--contextsize`.",
        setup=SetupGuide(
            install={
                "macos": "Download the macOS binary from github.com/LostRuins/koboldcpp/releases.",
                "windows": "Download koboldcpp.exe from github.com/LostRuins/koboldcpp/releases.",
                "linux": "Download the Linux binary from github.com/LostRuins/koboldcpp/releases.",
            },
            download="Download a `.gguf` file from Hugging Face and choose it when KoboldCpp starts.",
            embeddings="KoboldCpp can load an embeddings model with `--embeddingsmodel <file>.gguf`.",
            serve="Launch KoboldCpp with your model; it serves while its window is open.",
            check="curl http://127.0.0.1:5001/api/v1/model",
            exposure="Pass `--host 127.0.0.1`; KoboldCpp otherwise listens on every interface.",
        ),
    ),
    RuntimeSpec(
        id="localai",
        label="LocalAI",
        ports=(8080,),
        adapter=LocalAIAdapter,
        add_model="Install models from LocalAI's model gallery.",
        home="https://localai.io",
        window_hint="Raise `context_size` in the model's LocalAI config.",
        setup=SetupGuide(
            install={
                "macos": "Download the macOS app or binary from localai.io.",
                "linux": "Run the install script from localai.io: `curl https://localai.io/install.sh | sh`.",
            },
            download="Install a model from the gallery at localai.io, or `local-ai run <model>`.",
            embeddings="Install an embedding model from the gallery next to the chat model.",
            serve="`local-ai run --address 127.0.0.1:8080`",
            check="curl http://127.0.0.1:8080/v1/models",
            exposure="LocalAI listens on every interface by default; pass `--address 127.0.0.1:8080`.",
        ),
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
        setup=SetupGuide(
            install={
                "macos": "Follow the server's own install steps.",
                "windows": "Follow the server's own install steps.",
                "linux": "Follow the server's own install steps.",
            },
            download="Add models the way the server's documentation describes.",
            embeddings="If it serves `/v1/embeddings`, load an embedding model on it too.",
            serve=(
                "Start it on this computer, then tell the connector where it is — on that computer, "
                "never from this website: `aiteam-connect add-source http://127.0.0.1:<port>`."
            ),
            check="curl http://127.0.0.1:<port>/v1/models",
            exposure="Make it listen on 127.0.0.1 only.",
        ),
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


def setup_cards() -> list[dict]:
    """The Setup tab's runtime cards, in table order: data, not component text."""
    cards = []
    for spec in RUNTIMES:
        if spec.setup is None:
            continue
        guide = spec.setup
        cards.append(
            {
                "id": spec.id,
                "label": spec.label,
                "home": spec.home,
                "library": spec.library,
                "port": spec.ports[0] if spec.ports else None,
                "generic": spec.id == GENERIC,
                "install": dict(guide.install),
                "download": guide.download,
                "embeddings": guide.embeddings,
                "serve": guide.serve,
                "check": guide.check,
                "exposure": guide.exposure,
            }
        )
    return cards
