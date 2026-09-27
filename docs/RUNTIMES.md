# Local model runtimes

Every runtime below works the same way in this app, in **direct mode** (the backend calls it) and through the **connector** (a paired computer calls it for the backend). None is preferred. This page is written from the adapter table in [`backend/app/router/runtimes/table.py`](../backend/app/router/runtimes/table.py) by `python -m scripts.runtime_docs`; each row links where it was checked (2026-09-27).

## What each reports and takes

| Runtime | Default port | Recognised by | Context window | Structured output | Thinking control | Embeddings | Listens on every interface by default | Source |
|---|---|---|---|---|---|---|---|---|
| Ollama | 11434 | its root banner, `Ollama is running` | `/api/show` model_info `<arch>.context_length`; sent per request as `num_ctx` | `schema` | `think`: true/false, or low/medium/high for models that take levels | yes | no | [link](https://github.com/ollama/ollama/blob/main/docs/api.md) |
| LM Studio | 1234 | `/api/v0/models` entries with `type` and `state` | `/api/v1/models` loaded_instances[].config.context_length (0.4+); else not reported | `schema` | `reasoning_effort` | yes | no | [link](https://lmstudio.ai/docs/app/api/endpoints/rest) |
| llama.cpp | 8080 | `owned_by: llamacpp` on `/v1/models`, `/props` | `/v1/models` meta.n_ctx and `/props` default_generation_settings.n_ctx | `schema` | `chat_template_kwargs.enable_thinking`, `reasoning_effort` | yes | no | [link](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) |
| vLLM | 8000 | `owned_by: vllm` on `/v1/models`, `/version` | `/v1/models` max_model_len | `schema` | `chat_template_kwargs.enable_thinking`, `reasoning_effort` | yes | **yes** | [link](https://docs.vllm.ai/en/stable/usage/security/) |
| SGLang | 30000 | `owned_by: sglang` on `/v1/models`, `/server_info` | `/v1/models` max_model_len, or `/get_model_info` max_context_length | `schema` | `chat_template_kwargs.enable_thinking`, `reasoning_effort` | yes | no | [link](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/entrypoints/http_server.py) |
| KoboldCpp | 5001 | `/api/extra/version` → `result: KoboldCpp` | `/api/extra/true_max_context_length` value | `schema` | `reasoning_effort` (sent as effort levels) | yes | **yes** | [link](https://github.com/LostRuins/koboldcpp/blob/concedo/koboldcpp.py) |
| LocalAI | 8080 | `/.well-known/localai.json`, or `/system` listing backends | not reported (set per model in its YAML) | `schema` | `reasoning_effort` | yes | **yes** | [link](https://github.com/mudler/LocalAI/blob/master/core/cli/run.go) |
| llamafile | 8080 | nothing of its own — it *is* a llama.cpp server, so it's read as llama.cpp unless you say `--runtime llamafile` | `/props` default_generation_settings.n_ctx (the llama.cpp server) | `schema` | `chat_template_kwargs.enable_thinking` (the llama.cpp server) | yes | no | [link](https://docs.mozilla.ai/llamafile/using-llamafile/api) |
| MLX-LM | 8080 | model entries without `owned_by`, `/health` ok, no `/props` | not reported | `none` | `chat_template_kwargs.enable_thinking` | no | no | [link](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md) |
| Jan | 1337 | `owned_by` of `llama.cpp`, `mlx` or `remote` | not reported | `schema` | passed through to its llama.cpp engine (`chat_template_kwargs`) | yes | no | [link](https://github.com/janhq/jan/blob/main/src-tauri/src/core/server/proxy.rs) |
| text-generation-webui | 5000 | `/v1/internal/model/info` with `model_name` and `loader` | not reported | `none` | `enable_thinking`, `reasoning_effort` | yes | no | [link](https://github.com/oobabooga/text-generation-webui/blob/main/modules/api/typing.py) |
| GPT4All | 4891 | `owned_by: humanity` on `/v1/models` | not reported | `none` | none | no | no | [link](https://github.com/nomic-ai/gpt4all/blob/main/gpt4all-chat/src/server.cpp) |
| Docker Model Runner | 12434 | its root banner, or `owned_by: docker` under `/engines/v1/models` | `/engines/v1/models` dmr.context_window | `json` | none documented | yes | no | [link](https://docs.docker.com/ai/model-runner/api-reference/) |
| Foundry Local | chosen at start — add it by address | `/openai/status` with `Endpoints` | not reported | `json` | none documented | yes | no | [link](https://learn.microsoft.com/en-us/azure/foundry-local/reference/reference-rest) |
| OpenAI-compatible server | any | `/v1/models` in the dialect's shape — used only once you add or confirm it | an extension field on `/v1/models` if it adds one, else not reported | `schema` | `reasoning_effort` | yes | no | [link](https://platform.openai.com/docs/api-reference/chat) |

Structured output, strongest first: `schema` (decoding held to a JSON Schema), `grammar`, `json` (valid JSON, any shape), `none` (the shape is in the prompt). Whatever the level, every answer is validated and repaired, and a runtime that refuses a level is asked with the next one down — and not asked again. A window that isn't reported uses the configured fallback (`MODEL_CONTEXT_FALLBACK_TOKENS`), Settings says so on the model, and you set the real one per model under **Settings → Tune → Context window**.

Ports are candidates, never identities: llama.cpp, llamafile, MLX-LM and LocalAI all default to 8080, and each is told apart by its answer. Something that answers on a default port but matches none of these — `python -m http.server 8000`, say — is listed as unknown and never used until you add it.

## Status in this release

Verified against running servers on this project's test machine: **Ollama** and **llama.cpp** (`llama-server`) — full builds in direct mode (#43, #44) and through the connector (#47). The others are built from each runtime's documentation and source (linked above), and their fingerprints and fields are covered by tests with payloads in each runtime's shape; the adapter reads every field defensively, so an answer that differs falls back to the generic OpenAI-compatible behaviour rather than failing. vLLM and SGLang need a supported GPU; MLX-LM needs Apple silicon; Foundry Local runs on Windows and macOS.

## What the connector refuses

The connector performs 7 typed operations — `hello`, `list_models`, `model_info`, `ping`, `chat`, `embed`, `cancel` — and refuses everything else, writing each refusal to `~/.aiteam-connect/connector.log`. It never calls these, whatever the server asks (`app/connector/protocol.py`, `REFUSED`):

- **Ollama**: pull, push, create, copy, delete, blobs, `/api/me`, `/api/signout`, `/api/user/keys`, `/api/experimental/*`
- **LM Studio**: model download, model load, model unload, `/api/v1/models/load`, `/api/v1/models/download`
- **llama.cpp**: model add, model delete, model load, model unload, `/slots/*?action=save|restore`, `POST /props`
- **llamafile**: `POST /props`, `/slots/*?action=save|restore|erase`, `POST /tools`
- **vLLM**: `/pause`, `/update_weights`, `/v1/load_lora_adapter`, `/v1/unload_lora_adapter`, `/sleep`, `/wake_up`, `/collective_rpc`, `/reset_prefix_cache`, `/start_profile`
- **SGLang**: `/update_weights_from_disk`, `/update_weights_from_tensor`, `/load_lora_adapter`, `/unload_lora_adapter`, `/release_memory_occupation`, `/flush_cache`, `/set_internal_state`, `/pause_generation`, `/configure_logging`, `/start_profile`
- **KoboldCpp**: `/api/admin/reload_config`, `/api/admin/load_state`, `/api/admin/save_state`, `/api/admin/clear_state`, `/api/extra/shutdown`, `/api/extra/abort`
- **LocalAI**: `/models/apply`, `/models/delete/*`, `/models/import`, `/models/edit/*`, `/backends/apply`, `/backends/delete/*`, `/backend/load`, `/backend/shutdown`, `/api/settings`, `/stores/set`
- **MLX-LM**: a model the server didn't list (it would download and load it)
- **Jan**: model download, model start, model stop
- **text-generation-webui**: model loading, `/v1/internal/model/load`, `/v1/internal/model/unload`, `/v1/internal/lora/load`, `/v1/internal/lora/unload`, `/v1/internal/stop-generation`
- **GPT4All**: model download
- **Docker Model Runner**: `/models/create`, `DELETE /models/*`, `/logs`
- **Foundry Local**: `/openai/download`, `/openai/load/*`, `/openai/unload/*`, `/openai/unloadall`, `/openai/setgpudevice/*`
- **OpenAI-compatible server**: anything beyond /v1/models, /v1/chat/completions and /v1/embeddings
- **Every runtime**: tools, shell commands, file writes, model downloads, raw URLs or paths

## Runtime hygiene

Settings, the Setup tab and the connector's terminal warn — never block — when a runtime:

- **is older than a known security fix.** The minimum-version table is data: [`backend/app/router/runtimes/advisories.json`](../backend/app/router/runtimes/advisories.json), one entry per advisory with its first fixed version and a link. Add an entry there, or point `RUNTIME_ADVISORIES_FILE` at your own copy, to update it without a release. A version that can't be read is never taken for an old one; a pre-release of the fixed version (`0.17.1-rc0`) counts as older.
- **is reachable from your network.** Checked by connecting to the runtime's port on the computer's own network address: a runtime bound to 127.0.0.1 refuses that, one bound to 0.0.0.0 accepts it. Nothing beyond the computer is contacted.

| Runtime | Advisory | First fixed |
|---|---|---|
| Ollama | [CVE-2026-7482](https://github.com/advisories/GHSA-x8qc-fggm-mpqg) | 0.17.1 |
| Ollama | [CVE-2025-51471](https://github.com/advisories/GHSA-x9hg-5q6g-q3jr) | 0.14.3 |
| Ollama | [CVE-2024-39720](https://www.oligo.security/blog/more-models-more-probllms) | 0.1.46 |
| Ollama | [CVE-2024-39722](https://www.oligo.security/blog/more-models-more-probllms) | 0.1.46 |
| Ollama | [CVE-2024-37032](https://nvd.nist.gov/vuln/detail/cve-2024-37032) | 0.1.34 |
| Ollama | [CVE-2024-39721](https://www.oligo.security/blog/more-models-more-probllms) | 0.1.34 |
| Ollama | [CVE-2024-28224](https://www.nccgroup.com/research-blog/technical-advisory-ollama-dns-rebinding-attack-cve-2024-28224/) | 0.1.29 |
| llama.cpp | [CVE-2025-52566](https://github.com/ggml-org/llama.cpp/security/advisories/GHSA-7rxv-5jhh-j6xx) | b5721 |
| llama.cpp | [CVE-2025-49847](https://github.com/ggml-org/llama.cpp/security/advisories/GHSA-8wwf-w4qm-gpqr) | b5662 |
| vLLM | [CVE-2025-62164](https://github.com/advisories/GHSA-mrw7-hf4f-83pf) | 0.11.1 |
| vLLM | [CVE-2025-47277](https://github.com/advisories/GHSA-hjq4-87xh-g4fv) | 0.8.5 |
| vLLM | [CVE-2025-32444](https://nvd.nist.gov/vuln/detail/cve-2025-32444) | 0.8.5 |
| SGLang | [CVE-2026-3059](https://osv.dev/vulnerability/GHSA-rgq9-fqf5-fv58) | 0.5.10 |

Not in the table (as of 2026-09-27): Ollama CVE-2024-39719 (sources disagree on the fix); Ollama CVE-2025-63389 (no patch — missing authentication on the model API; keep Ollama on loopback); LocalAI CVE-2024-6983 (fixed version disputed); SGLang CVE-2026-3060 (no fixed version published); text-generation-webui CVE-2026-35484, CVE-2026-35483, CVE-2026-35487 (fixed in 4.3) and CVE-2026-35050 (fixed in 4.1.1): its API reports no version, so they can't be checked — update it to 4.3 or later.

Never bind a runtime to `0.0.0.0` and never set `OLLAMA_ORIGINS=*` ([CVE-2024-28224, DNS rebinding](https://www.nccgroup.com/research-blog/technical-advisory-ollama-dns-rebinding-attack-cve-2024-28224/)). The connector reaches the runtime on its own computer and dials out itself, so it never needs either.

## The website never probes your computer

The website doesn't fetch `http://127.0.0.1` from your browser; the connector is the only thing that looks, and it's the source of truth for what your computer runs. A browser probe would meet [Chrome's Local Network Access prompt](https://developer.chrome.com/blog/local-network-access) (enforced from Chrome 142) and Safari's mixed-content block on `http://localhost` from an https page ([WebKit 171934](https://bugs.webkit.org/show_bug.cgi?id=171934)), and would only ever see model lists — so it isn't built. If it ever is, it may only `GET` model-list endpoints, and the connector's answer wins.
