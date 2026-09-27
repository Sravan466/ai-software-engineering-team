"""The runtime adapter table: the one place a runtime's name, port or habits are written.

Everything else in the application talks to *sources* through typed operations and
learns what a source is from what it answers. This table is what it answers *with*:
a label a person recognises, the default addresses worth probing on this machine,
the adapter that speaks its dialect, and what to tell someone who wants another
model on it.

Ports are candidates, never identities. `8080` is the default of several runtimes
here, and anything can run on any port — so detection probes these ports on
loopback and then asks each adapter in turn whether the answer is its runtime.

Checked against each runtime's documentation on 2026-09-17, and against each one's
source on 2026-09-27 (Phase 6): every row now has an adapter and its `Facts` — what
the runtime reports and takes, with the link it was checked against. What the
connector refuses for each is `app.connector.protocol.REFUSED`, and the versions
older than a security fix are `advisories.json` beside this file.
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
    DockerModelRunnerAdapter,
    FoundryLocalAdapter,
    GPT4AllAdapter,
    JanAdapter,
    KoboldCppAdapter,
    LlamafileAdapter,
    LMStudioAdapter,
    LocalAIAdapter,
    MLXAdapter,
    SGLangAdapter,
    TextGenWebUIAdapter,
    VLLMAdapter,
)
from app.router.runtimes.types import (
    STRUCTURED_JSON,
    STRUCTURED_NONE,
    STRUCTURED_SCHEMA,
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
class Facts:
    """What a runtime reports and takes — the adapter table's columns, each checked.

    Descriptive: the adapter is what acts on them, and it still steps down when a
    runtime refuses what it was said to take.
    """

    #: Where its context window comes from, in words.
    context: str
    #: Whether it reports one at all — None when only some versions or servers do.
    #: When it doesn't, the configured fallback applies, Settings says so on the
    #: model, and the user sets the real one under Tune.
    context_reported: Optional[bool]
    #: The strongest structured mode asked for: `schema` / `grammar` / `json` / `none`.
    structured: str
    #: How thinking is switched, or "none".
    thinking: str
    #: Whether it serves `/v1/embeddings` (or its own equivalent).
    embeddings: bool
    #: Whether it listens on every interface unless told otherwise.
    listens_everywhere: bool
    #: Where these were checked.
    source: str


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
    facts: Optional[Facts] = None


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
        facts=Facts(
            context="`/api/show` model_info `<arch>.context_length`; sent per request as `num_ctx`",
            context_reported=True,
            structured=STRUCTURED_SCHEMA,
            thinking="`think`: true/false, or low/medium/high for models that take levels",
            embeddings=True,
            listens_everywhere=False,
            source="https://github.com/ollama/ollama/blob/main/docs/api.md",
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
        facts=Facts(
            context="`/api/v1/models` loaded_instances[].config.context_length (0.4+); else not reported",
            context_reported=None,
            structured=STRUCTURED_SCHEMA,
            thinking="`reasoning_effort`",
            embeddings=True,
            listens_everywhere=False,
            source="https://lmstudio.ai/docs/app/api/endpoints/rest",
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
        facts=Facts(
            context="`/v1/models` meta.n_ctx and `/props` default_generation_settings.n_ctx",
            context_reported=True,
            structured=STRUCTURED_SCHEMA,
            thinking="`chat_template_kwargs.enable_thinking`, `reasoning_effort`",
            embeddings=True,
            listens_everywhere=False,
            source="https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md",
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
        facts=Facts(
            context="`/v1/models` max_model_len",
            context_reported=True,
            structured=STRUCTURED_SCHEMA,
            thinking="`chat_template_kwargs.enable_thinking`, `reasoning_effort`",
            embeddings=True,
            listens_everywhere=True,
            source="https://docs.vllm.ai/en/stable/usage/security/",
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
        facts=Facts(
            context="`/v1/models` max_model_len, or `/get_model_info` max_context_length",
            context_reported=True,
            structured=STRUCTURED_SCHEMA,
            thinking="`chat_template_kwargs.enable_thinking`, `reasoning_effort`",
            embeddings=True,
            listens_everywhere=False,
            source="https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/entrypoints/http_server.py",
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
        facts=Facts(
            context="`/api/extra/true_max_context_length` value",
            context_reported=True,
            structured=STRUCTURED_SCHEMA,
            thinking="`reasoning_effort` (sent as effort levels)",
            embeddings=True,
            listens_everywhere=True,
            source="https://github.com/LostRuins/koboldcpp/blob/concedo/koboldcpp.py",
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
        facts=Facts(
            context="not reported (set per model in its YAML)",
            context_reported=False,
            structured=STRUCTURED_SCHEMA,
            thinking="`reasoning_effort`",
            embeddings=True,
            listens_everywhere=True,
            source="https://github.com/mudler/LocalAI/blob/master/core/cli/run.go",
        ),
    ),
    RuntimeSpec(
        id="llamafile",
        label="llamafile",
        ports=(8080,),
        adapter=LlamafileAdapter,
        add_model="A llamafile serves the model it was built with; run another llamafile on another port.",
        home="https://github.com/mozilla-ai/llamafile",
        window_hint="Restart it with a larger `-c`.",
        kv_hint="Set with `--cache-type-k` / `--cache-type-v`, as in llama.cpp.",
        setup=SetupGuide(
            install={
                "macos": "Download a `.llamafile` from github.com/mozilla-ai/llamafile and `chmod +x` it.",
                "windows": "Download a `.llamafile`, rename it to end in `.exe`, and run it.",
                "linux": "Download a `.llamafile` from github.com/mozilla-ai/llamafile and `chmod +x` it.",
            },
            download="Each `.llamafile` carries its model. Or run the bare `llamafile -m <model>.gguf`.",
            embeddings="Run a second llamafile with an embedding model and `--embedding` on another port.",
            serve="`./<model>.llamafile --server --nobrowser --host 127.0.0.1 --port 8080`",
            check="curl http://127.0.0.1:8080/health",
            exposure="It listens on 127.0.0.1 by default. Never pass `--host 0.0.0.0`.",
        ),
        facts=Facts(
            context="`/props` default_generation_settings.n_ctx (the llama.cpp server)",
            context_reported=True,
            structured=STRUCTURED_SCHEMA,
            thinking="`chat_template_kwargs.enable_thinking` (the llama.cpp server)",
            embeddings=True,
            listens_everywhere=False,
            source="https://docs.mozilla.ai/llamafile/using-llamafile/api",
        ),
    ),
    RuntimeSpec(
        id="mlx",
        label="MLX-LM",
        ports=(8080,),
        adapter=MLXAdapter,
        add_model="MLX-LM serves the model it was started with (`--model`).",
        home="https://github.com/ml-explore/mlx-lm",
        window_hint="MLX-LM doesn't report its window; set the fallback in Settings to what the model supports.",
        setup=SetupGuide(
            install={"macos": "`pip install mlx-lm` (Apple silicon only)."},
            download="MLX-LM downloads the Hugging Face model you name with `--model` (an `mlx-community/…` build).",
            embeddings="MLX-LM has no embeddings endpoint. Run a second runtime for embeddings, or go without.",
            serve="`mlx_lm.server --model <model> --host 127.0.0.1 --port 8080`",
            check="curl http://127.0.0.1:8080/v1/models",
            exposure="It listens on 127.0.0.1 by default. Leave `--host` alone.",
        ),
        facts=Facts(
            context="not reported",
            context_reported=False,
            structured=STRUCTURED_NONE,
            thinking="`chat_template_kwargs.enable_thinking`",
            embeddings=False,
            listens_everywhere=False,
            source="https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md",
        ),
    ),
    RuntimeSpec(
        id="jan",
        label="Jan",
        ports=(1337,),
        adapter=JanAdapter,
        add_model="Download models in Jan's Hub, then start its Local API Server.",
        home="https://jan.ai",
        window_hint="Raise the model's context size in Jan's model settings.",
        setup=SetupGuide(
            install={
                "macos": "Download Jan from jan.ai.",
                "windows": "Download and run the Jan installer from jan.ai.",
                "linux": "Download the AppImage or .deb from jan.ai.",
            },
            download="Download a model from Jan's Hub.",
            embeddings="Download an embedding model in the Hub next to the chat model.",
            serve="Settings → Local API Server → Start Server (127.0.0.1:1337).",
            check="curl http://127.0.0.1:1337/v1/models",
            exposure="Keep the server host at 127.0.0.1 in its settings.",
        ),
        facts=Facts(
            context="not reported",
            context_reported=False,
            structured=STRUCTURED_SCHEMA,
            thinking="passed through to its llama.cpp engine (`chat_template_kwargs`)",
            embeddings=True,
            listens_everywhere=False,
            source="https://github.com/janhq/jan/blob/main/src-tauri/src/core/server/proxy.rs",
        ),
    ),
    RuntimeSpec(
        id="tgw",
        label="text-generation-webui",
        ports=(5000,),
        adapter=TextGenWebUIAdapter,
        add_model="Load a model from text-generation-webui's Model tab.",
        home="https://github.com/oobabooga/text-generation-webui",
        window_hint="Load the model with a larger context length in its Model tab.",
        setup=SetupGuide(
            install={
                "macos": "Clone github.com/oobabooga/text-generation-webui and run `./start_macos.sh`.",
                "windows": "Clone it and run `start_windows.bat`.",
                "linux": "Clone it and run `./start_linux.sh`.",
            },
            download="Download a model in its Model tab, then load it there.",
            embeddings="Its `/v1/embeddings` uses a small sentence-transformers model it loads itself.",
            serve="Start it with `--api`; the API listens on 127.0.0.1:5000.",
            check="curl http://127.0.0.1:5000/v1/internal/model/info",
            exposure="Never start it with `--listen` or `--public-api`.",
        ),
        facts=Facts(
            context="not reported",
            context_reported=False,
            structured=STRUCTURED_NONE,
            thinking="`enable_thinking`, `reasoning_effort`",
            embeddings=True,
            listens_everywhere=False,
            source="https://github.com/oobabooga/text-generation-webui/blob/main/modules/api/typing.py",
        ),
    ),
    RuntimeSpec(
        id="gpt4all",
        label="GPT4All",
        ports=(4891,),
        adapter=GPT4AllAdapter,
        add_model="Download models in GPT4All's Models view.",
        home="https://www.nomic.ai/gpt4all",
        window_hint="Raise the context length in GPT4All's model settings.",
        setup=SetupGuide(
            install={
                "macos": "Download the installer from nomic.ai/gpt4all.",
                "windows": "Download the installer from nomic.ai/gpt4all.",
                "linux": "Download the installer from nomic.ai/gpt4all.",
            },
            download="Download a model in the Models view.",
            embeddings="GPT4All's API serves no embeddings. Run a second runtime for them, or go without.",
            serve="Settings → Application → Enable Local API Server (port 4891).",
            check="curl http://127.0.0.1:4891/v1/models",
            exposure="It listens on 127.0.0.1 only.",
        ),
        facts=Facts(
            context="not reported",
            context_reported=False,
            structured=STRUCTURED_NONE,
            thinking="none",
            embeddings=False,
            listens_everywhere=False,
            source="https://github.com/nomic-ai/gpt4all/blob/main/gpt4all-chat/src/server.cpp",
        ),
    ),
    RuntimeSpec(
        id="dmr",
        label="Docker Model Runner",
        ports=(12434,),
        adapter=DockerModelRunnerAdapter,
        add_model="Run `docker model pull <name>`.",
        home="https://docs.docker.com/ai/model-runner/",
        library="https://hub.docker.com/u/ai",
        window_hint="Set the context size with `docker model configure --context-size`.",
        setup=SetupGuide(
            install={
                "macos": "In Docker Desktop, turn on Docker Model Runner (Settings → AI).",
                "windows": "In Docker Desktop, turn on Docker Model Runner (Settings → AI).",
                "linux": "Install the `docker-model-plugin` package for Docker Engine.",
            },
            download="`docker model pull ai/<model>` — browse hub.docker.com/u/ai.",
            embeddings="Pull an embedding model too, e.g. one tagged embedding on hub.docker.com/u/ai.",
            serve="`docker desktop enable model-runner --tcp 12434` (Docker Engine serves it on 12434 already).",
            check="curl http://127.0.0.1:12434/engines/v1/models",
            exposure="Keep host-side TCP on 127.0.0.1.",
        ),
        facts=Facts(
            context="`/engines/v1/models` dmr.context_window",
            context_reported=True,
            structured=STRUCTURED_JSON,
            thinking="none documented",
            embeddings=True,
            listens_everywhere=False,
            source="https://docs.docker.com/ai/model-runner/api-reference/",
        ),
    ),
    RuntimeSpec(
        id="foundry",
        label="Foundry Local",
        # Chosen at start: never probed, added by its address.
        ports=(),
        adapter=FoundryLocalAdapter,
        add_model="Run `foundry model run <name>`.",
        home="https://learn.microsoft.com/azure/foundry-local/",
        window_hint="Foundry Local doesn't report its window; set the fallback in Settings.",
        setup=SetupGuide(
            install={
                "macos": "`brew install microsoft/foundrylocal/foundrylocal`.",
                "windows": "`winget install Microsoft.FoundryLocal`.",
            },
            download="`foundry model list`, then `foundry model download <name>`.",
            embeddings="Foundry Local 1.1 and later serve embedding models too.",
            serve=(
                "`foundry service start`, then `foundry service status` prints its address — its port "
                "changes each start, so add it: `aiteam-connect add-source http://127.0.0.1:<port>`."
            ),
            check="curl http://127.0.0.1:<port>/openai/status",
            exposure="It listens on 127.0.0.1 only.",
        ),
        facts=Facts(
            context="not reported",
            context_reported=False,
            structured=STRUCTURED_JSON,
            thinking="none documented",
            embeddings=True,
            listens_everywhere=False,
            source="https://learn.microsoft.com/en-us/azure/foundry-local/reference/reference-rest",
        ),
    ),
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
        facts=Facts(
            context="an extension field on `/v1/models` if it adds one, else not reported",
            context_reported=None,
            structured=STRUCTURED_SCHEMA,
            thinking="`reasoning_effort`",
            embeddings=True,
            listens_everywhere=False,
            source="https://platform.openai.com/docs/api-reference/chat",
        ),
    ),
)

BY_ID: dict[str, RuntimeSpec] = {spec.id: spec for spec in RUNTIMES}

#: Runtimes detection can recognise by their answer, in the order they are asked.
#: The ones whose fingerprint is most specific go first, the generic dialect never.
#: Runtimes nothing in their answer tells apart from another one's — llamafile
#: *is* a llama.cpp server. Never adopted by detection on their own; asked only
#: when someone has said that is what an address runs (`identify(prefer=…)`).
DECLARED_ONLY = ("llamafile",)
FINGERPRINTED: tuple[RuntimeSpec, ...] = tuple(
    spec for spec in RUNTIMES if spec.adapter is not None and spec.id != GENERIC and spec.id not in DECLARED_ONLY
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
                "facts": _facts(spec),
            }
        )
    return cards


def _facts(spec: RuntimeSpec) -> Optional[dict]:
    if spec.facts is None:
        return None
    f = spec.facts
    return {
        "context": f.context,
        "context_reported": f.context_reported,
        "structured": f.structured,
        "thinking": f.thinking,
        "embeddings": f.embeddings,
        "listens_everywhere": f.listens_everywhere,
        "source": f.source,
    }
