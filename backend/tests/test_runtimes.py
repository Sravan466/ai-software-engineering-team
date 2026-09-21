"""Phase 1 of #26: any local runtime, reached through typed operations.

What is covered here is what the issue's "done when" list asks for, one layer at a
time: adapters read what their runtime reports (using payloads captured from a real
`llama-server`), detection tells runtimes apart by their answers rather than their
ports, the router routes by source rather than by product, embeddings work without
any one runtime, and old `.env` names keep working.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from typing import Optional

import pytest

from app.core import model_roles
from app.core.constants import RoutingMode
from app.router.base import ProviderError
from app.router.runtimes import detect, table
from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.llamacpp import LlamaCppAdapter
from app.router.runtimes.openai_compat import OpenAICompatAdapter
from app.router.runtimes.provider import Source, SourceProvider
from app.router.runtimes.servers import LocalAIAdapter
from app.router.runtimes.types import (
    ChatRequest,
    ChatResult,
    Hello,
    ModelEntry,
    ModelInfo,
)
from app.schemas.llm import ChatMessage, GenerationOptions

# ── payloads captured from llama-server (build b10964), trimmed ──────────────
LLAMACPP_MODELS = {
    "object": "list",
    "data": [
        {
            "id": "qwen2.5-7b-instruct",
            "object": "model",
            "owned_by": "llamacpp",
            "meta": {
                "n_ctx": 8192,
                "n_ctx_train": 32768,
                "n_embd": 3584,
                "n_params": 7615616512,
                "size": 4677120000,
                "ftype": "Q4_K - Medium",
            },
        }
    ],
}
LLAMACPP_PROPS = {
    "default_generation_settings": {"n_ctx": 8192},
    "total_slots": 4,
    "modalities": {"vision": False},
    "chat_template_caps": {"supports_reasoning_effort": False},
    "build_info": "b10964-b29c606e2",
}
NOT_SUPPORTED = {
    "error": {
        "code": 501,
        "message": "This server does not support embeddings. Start it with `--embeddings`",
        "type": "not_supported_error",
    }
}


class _Resp:
    def __init__(self, status: int, body: object = None, text: Optional[str] = None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=self)


def _serve(monkeypatch, routes: dict, posts: Optional[dict] = None, seen: Optional[list] = None):
    """Answer `httpx.get`/`httpx.post` from a table of `path -> response`."""
    import httpx

    def _path(url: str) -> str:
        return "/" + url.split("://", 1)[1].split("/", 1)[1] if url.count("/") > 2 else "/"

    def _get(url, **_):
        return routes.get(_path(url), _Resp(404, {"error": "not found"}))

    def _post(url, json=None, **_):
        if seen is not None:
            seen.append((_path(url), json))
        answer = (posts or {}).get(_path(url), _Resp(404, {"error": "not found"}))
        return answer(json) if callable(answer) else answer

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)


# ── llama.cpp ────────────────────────────────────────────────────────────────
def test_llama_server_is_recognised_by_its_answer_and_describes_its_window(monkeypatch):
    _serve(
        monkeypatch,
        {"/v1/models": _Resp(200, LLAMACPP_MODELS), "/props": _Resp(200, LLAMACPP_PROPS)},
        {"/v1/embeddings": _Resp(501, NOT_SUPPORTED)},
    )
    hello = LlamaCppAdapter.fingerprint("http://127.0.0.1:8080")
    assert hello == Hello(runtime="llamacpp", base_url="http://127.0.0.1:8080", version="b10964-b29c606e2")

    info = LlamaCppAdapter("http://127.0.0.1:8080").model_info("qwen2.5-7b-instruct")
    # The window the server was started with — not the 32k the model was trained for,
    # which the server would refuse — and a note on how to get more.
    assert info.context_window == 8192 and info.context_source == "reported"
    assert info.parameters_total == 7615616512 and info.parameter_label == "7.6B"
    assert info.quantization == "Q4_K - Medium"
    assert info.kind == "chat" and info.structured_output == "schema"
    assert any("-c" in w for w in info.warnings)


def test_an_embedding_only_llama_server_is_told_apart_by_asking(monkeypatch):
    """Its model list claims `completion` like any other; one cheap probe settles it."""
    _serve(
        monkeypatch,
        {"/v1/models": _Resp(200, LLAMACPP_MODELS), "/props": _Resp(200, LLAMACPP_PROPS)},
        {"/v1/embeddings": _Resp(200, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]})},
    )
    adapter = LlamaCppAdapter("http://127.0.0.1:8081")
    described = adapter.describe(adapter.list_models())
    assert described["qwen2.5-7b-instruct"].kind == "embedding"
    assert adapter.embed("qwen2.5-7b-instruct", ["hello"]) == [[0.1, 0.2]]


# ── ports are candidates, never identities ───────────────────────────────────
def test_llama_server_and_localai_on_the_same_port_are_each_identified(monkeypatch):
    """#28: each on :8080 in turn, identified correctly — by what answers."""
    _serve(monkeypatch, {"/v1/models": _Resp(200, LLAMACPP_MODELS), "/props": _Resp(200, LLAMACPP_PROPS)})
    assert detect.identify("http://127.0.0.1:8080").runtime == "llamacpp"

    _serve(
        monkeypatch,
        {
            "/v1/models": _Resp(200, {"object": "list", "data": [{"id": "phi-3", "object": "model"}]}),
            "/system": _Resp(200, {"backends": ["llama-cpp"], "loaded_models": []}),
            "/version": _Resp(200, {"version": "v2.24.0"}),
        },
    )
    hello = detect.identify("http://127.0.0.1:8080")
    assert hello is not None and hello.runtime == "localai" and hello.version == "v2.24.0"
    assert LocalAIAdapter.fingerprint("http://127.0.0.1:8080") is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_plain_http_server_is_unknown_and_never_adopted(monkeypatch):
    """#28: `python -m http.server` shows as unknown and isn't used.

    A real server on a real socket, because the claim is about what answers.
    """
    port = _free_port()

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}"
        assert detect.identify(url) is None
        monkeypatch.setattr(detect.settings, "local_detect", True)
        monkeypatch.setattr(detect.table, "probe_ports", lambda: (port,))
        found = detect.detect()
        assert found.found == []
        assert [u["base_url"] for u in found.unknown] == [url]
        assert found.unknown[0]["openai"] is False
    finally:
        server.shutdown()


def test_detection_is_loopback_only():
    """The candidates are this machine's loopback addresses and nothing else."""
    assert detect.LOOPBACK_HOSTS == ("127.0.0.1", "::1")
    assert detect.is_loopback("http://localhost:1234")
    assert detect.is_loopback("http://[::1]:1234")
    assert not detect.is_loopback("http://192.168.1.20:1234")
    assert detect.same_address("http://localhost:11434", "http://127.0.0.1:11434")
    assert not detect.same_address("http://127.0.0.1:8080", "http://127.0.0.1:8081")


def test_detection_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(detect.settings, "local_detect", False)
    assert detect.detect().found == [] and detect.detect().tried == []


# ── the generic OpenAI-compatible adapter ────────────────────────────────────
def test_the_generic_adapter_always_states_its_limits_and_steps_down_on_refusal(monkeypatch):
    """Always explicit `max_tokens` and sampling; a refused schema costs one call, once."""
    seen: list = []

    def _chat(body):
        if body.get("response_format", {}).get("type") == "json_schema":
            return _Resp(400, {"error": "response_format json_schema is not supported"})
        return _Resp(
            200,
            {
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    _serve(monkeypatch, {}, {"/v1/chat/completions": _chat}, seen)
    adapter = OpenAICompatAdapter("http://127.0.0.1:1337/v1")
    assert adapter.base_url == "http://127.0.0.1:1337", "the /v1 spelling is the same source"
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=900,
        context_window=8192,
        json_schema={"type": "object"},
        json_mode=True,
        structured_output="schema",
    )
    result = adapter.chat(request)
    assert result.structured_output == "json" and result.structured_output_rejected
    assert [body["response_format"]["type"] for _, body in seen] == ["json_schema", "json_object"]
    for _, body in seen:
        assert body["max_tokens"] == 900
        assert body["temperature"] is not None and body["top_p"] is not None

    seen.clear()
    adapter.chat(request)
    assert [body["response_format"]["type"] for _, body in seen] == ["json_object"], (
        "the refusal was paid for again"
    )


def test_a_400_about_the_prompt_is_not_mistaken_for_a_refused_schema(monkeypatch):
    seen: list = []
    _serve(
        monkeypatch,
        {},
        {"/v1/chat/completions": _Resp(400, {"error": "the request exceeds the available context size"})},
        seen,
    )
    adapter = OpenAICompatAdapter("http://127.0.0.1:8080")
    with pytest.raises(ProviderError) as caught:
        adapter.chat(
            ChatRequest(
                model="m", messages=[], max_tokens=10, context_window=10,
                json_schema={"type": "object"}, structured_output="schema",
            )
        )
    assert len(seen) == 1, "retried weaker although the schema was never the problem"
    assert "context size" in str(caught.value) and caught.value.retryable is False


# ── model references carry their source ──────────────────────────────────────
@pytest.fixture
def router_with(monkeypatch):
    """The router with exactly the sources a test hands it, and no saved choices."""
    from app.router.router import router as model_router

    def _install(*providers: SourceProvider, chosen: Optional[str] = None):
        monkeypatch.setattr(model_router.sources, "_providers", {p.name: p for p in providers})
        monkeypatch.setattr(model_router.sources, "_loaded", True)
        monkeypatch.setattr(model_router.sources, "_detected_at", time.monotonic() + 3600)
        monkeypatch.setattr(model_router.sources, "_tried", ["http://127.0.0.1:11434", "http://127.0.0.1:1234"])
        monkeypatch.setattr(model_router, "_chosen_local", chosen)
        monkeypatch.setattr(model_roles, "get", lambda role: None)
        return model_router

    return _install


class FakeAdapter(RuntimeAdapter):
    """A runtime that answers from memory, for routing tests."""

    runtime = "openai-compatible"

    def __init__(self, models: list[ModelEntry], *, up: bool = True, reply: str = "{}"):
        super().__init__("http://127.0.0.1:1")
        self.models, self.up, self.reply = models, up, reply
        self.calls: list[ChatRequest] = []
        self.embedded: list[tuple[str, list[str]]] = []

    @classmethod
    def fingerprint(cls, base_url, api_key=None, *, timeout=1.0):
        return None

    def list_models(self):
        if not self.up:
            raise ProviderError("connection refused", unreachable=True)
        return list(self.models)

    def model_info(self, model):
        entry = next((m for m in self.models if m.name == model), None)
        return ModelInfo(name=model, context_window=16384, context_source="reported",
                         kind=entry.kind if entry else None, is_local=entry.is_local if entry else True,
                         structured_output="schema")

    def chat(self, request):
        self.calls.append(request)
        return ChatResult(text=self.reply, prompt_tokens=5, completion_tokens=2, finish_reason="stop",
                          structured_output=request.structured_output)

    def embed(self, model, inputs):
        self.embedded.append((model, inputs))
        return [[1.0, 0.0] for _ in inputs]


def _source(source_id: str, models: list[ModelEntry], **kw) -> SourceProvider:
    return SourceProvider(
        Source(id=source_id, label=source_id.upper(), base_url=f"http://127.0.0.1:{abs(hash(source_id)) % 50000 + 1024}",
               runtime="openai-compatible", origin="detected"),
        FakeAdapter(models, **kw),
    )


CHAT = ModelEntry(name="writer", kind="chat")
EMBED = ModelEntry(name="vectors", kind="embedding")


def test_an_unprefixed_model_is_an_error_not_a_guess(router_with):
    model_router = router_with(_source("lmstudio", [CHAT]))
    assert model_router.parse("lmstudio:writer") == ("lmstudio", "writer")
    assert model_router.parse("anthropic:claude-opus-4-8") == ("anthropic", "claude-opus-4-8")
    # A source that is not running right now is still a source, not a model name.
    assert model_router.parse("ollama:qwen2.5:7b") == ("ollama", "qwen2.5:7b")
    assert model_router.parse("llamacpp-8081:nomic") == ("llamacpp-8081", "nomic")
    for bare in ("qwen2.5:7b", "mistral:7b", "writer"):
        with pytest.raises(ValueError, match="doesn't say which source"):
            model_router.parse(bare)
    with pytest.raises(ValueError):
        model_router.set_role_model("backend_engineer", "writer")


def test_a_choice_saved_before_sources_keeps_its_meaning(router_with):
    model_router = router_with(_source("lmstudio", [CHAT]))
    assert model_router._saved_pair("qwen2.5:7b") == (table.LEGACY_BARE_RUNTIME, "qwen2.5:7b")
    assert model_router._saved_pair("lmstudio:writer") == ("lmstudio", "writer")


# ── routing by source, not by product ────────────────────────────────────────
def test_with_no_configuration_the_first_model_that_writes_is_the_default(router_with, monkeypatch):
    """#28: a build runs on LM Studio alone, or llama-server alone, with no .env edits."""
    from app.router import router as router_module

    monkeypatch.setattr(router_module.settings, "local_default_model", "a-model-nobody-has")
    model_router = router_with(_source("lmstudio", [EMBED, CHAT]))
    assert model_router.local_default() == ("lmstudio", "writer")
    assert model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"]).ok

    resp = model_router.complete([ChatMessage(role="user", content="hi")])
    assert (resp.provider, resp.model, resp.is_local) == ("lmstudio", "writer", True)


def test_two_runtimes_at_once_both_list_and_either_can_run_a_build(router_with):
    """#28: models from both appear, and either one can run the build."""
    first = _source("lmstudio", [CHAT])
    second = _source("llamacpp", [ModelEntry(name="qwen", kind="chat")])
    model_router = router_with(first, second, chosen="lmstudio:writer")

    status = model_router.local_status()
    assert status["models"] == ["lmstudio:writer", "llamacpp:qwen"]
    assert [s["id"] for s in status["sources"]] == ["lmstudio", "llamacpp"]

    model_router.complete([ChatMessage(role="user", content="hi")])
    assert len(first.adapter.calls) == 1 and not second.adapter.calls

    resp = model_router.complete(
        [ChatMessage(role="user", content="hi")],
        mode=RoutingMode.MANUAL,
        preferred_model="llamacpp:qwen",
        options=GenerationOptions(json_mode=True, json_schema={"type": "object"}),
    )
    assert resp.provider == "llamacpp" and len(second.adapter.calls) == 1
    # The window and the output budget always travel with the call.
    sent = second.adapter.calls[0]
    assert sent.context_window == 16384 and sent.max_tokens > 0


def test_nothing_running_is_refused_before_the_run_naming_what_was_tried(router_with):
    model_router = router_with()
    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"])
    assert not ready.ok and ready.unreachable
    assert "No local runtime is reachable" in ready.reason
    assert "127.0.0.1:11434" in ready.reason and "127.0.0.1:1234" in ready.reason


def test_a_chosen_default_on_a_stopped_runtime_says_so(router_with):
    model_router = router_with(_source("lmstudio", [CHAT], up=False), chosen="lmstudio:writer")
    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"])
    assert not ready.ok and ready.unreachable and "LMSTUDIO isn't answering" in ready.reason


def test_a_model_a_local_runtime_sends_elsewhere_is_not_local(router_with):
    """A hosted model behind a local runtime's name must not run a Local-Only build."""
    hosted = ModelEntry(name="big:120b-cloud", kind="chat", is_local=False)
    model_router = router_with(_source("ollama", [hosted]), chosen="ollama:big:120b-cloud")
    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"])
    assert not ready.ok and "hosted service" in ready.reason
    assert model_router.readiness(RoutingMode.AUTO, None, roles=["product_manager"]).ok


def test_the_ollama_adapter_marks_hosted_models_as_not_local():
    from app.router.runtimes.ollama import _is_remote

    assert _is_remote("gpt-oss:120b-cloud", {})
    assert _is_remote("anything", {"remote_host": "https://ollama.com:443"})
    assert not _is_remote("qwen2.5:7b", {})
    assert not _is_remote("registry:5000/team/model", {})


def test_the_state_is_cached_rather_than_asked_before_every_call(router_with):
    """Availability comes from a cached per-source state, not a round trip per call."""
    source = _source("lmstudio", [CHAT])
    listed = []
    real = source.adapter.list_models
    source.adapter.list_models = lambda: listed.append(1) or real()
    model_router = router_with(source, chosen="lmstudio:writer")
    for _ in range(5):
        model_router.complete([ChatMessage(role="user", content="hi")])
    assert len(listed) == 1


def test_a_source_that_refuses_the_connection_is_marked_down_at_once(router_with):
    source = _source("lmstudio", [CHAT])

    def _refused(request):
        raise ProviderError("connection refused", unreachable=True)

    source.adapter.chat = _refused
    model_router = router_with(source, chosen="lmstudio:writer")
    assert source.available()
    with pytest.raises(ProviderError):
        model_router.complete([ChatMessage(role="user", content="hi")])
    assert source.available() is False, "the next link would wait on a source known to be down"


# ── embeddings through `embed`, from whichever source has one ────────────────
def test_embeddings_come_from_any_source_that_serves_them(router_with, monkeypatch):
    """#28: document search and memory work with no particular runtime installed."""
    from app.rag.embeddings import SourceEmbeddingFunction
    from app.router import router as router_module

    monkeypatch.setattr(router_module.settings, "embedding_model", "not-here")
    writer = _source("lmstudio", [CHAT])
    vectors = _source("llamacpp-8081", [EMBED])
    model_router = router_with(writer, vectors, chosen="lmstudio:writer")
    assert model_router.embedding_target() == ("llamacpp-8081", "vectors")
    assert SourceEmbeddingFunction()(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert vectors.adapter.embedded == [("vectors", ["a", "b"])]
    assert model_router.local_status()["embedding_model"] == "llamacpp-8081:vectors"


def test_with_no_embedding_model_anywhere_memory_is_off_and_says_why(router_with, monkeypatch):
    from app.router import router as router_module

    monkeypatch.setattr(router_module.settings, "embedding_model", "not-here")
    model_router = router_with(_source("lmstudio", [CHAT]), chosen="lmstudio:writer")
    assert model_router.embedding_target() is None
    with pytest.raises(ProviderError, match="No local runtime is serving an embedding model"):
        model_router.embed(["x"])


# ── sources added in Settings ────────────────────────────────────────────────
def test_an_address_on_another_computer_needs_explicit_confirmation(router_with, monkeypatch):
    from app.router.runtimes import sources as sources_module
    from app.router.runtimes.sources import SourceError

    model_router = router_with()
    saved: list = []
    monkeypatch.setattr(sources_module.secrets_store, "save_sources", saved.append)
    monkeypatch.setattr(
        sources_module.detect, "identify",
        lambda url, key=None, **_: Hello(runtime="vllm", base_url=url, version="0.9"),
    )
    with pytest.raises(SourceError, match="another computer"):
        model_router.add_source("http://10.0.0.5:8000")
    provider = model_router.add_source("http://10.0.0.5:8000/v1", label="GPU box", api_key="sk-secret-1234", confirm_remote=True)
    assert provider.name == "gpu-box" and provider.source.remote and provider.source.runtime == "vllm"
    assert provider.source.same_machine is False, "another computer's RAM is not this one's"
    (stored,) = saved[-1]
    assert stored["base_url"] == "http://10.0.0.5:8000" and stored["api_key"] == "sk-secret-1234"
    # The key is stored like a cloud key and only ever shown as a hint.
    shown = next(s for s in model_router.local_status()["sources"] if s["id"] == "gpu-box")
    assert shown["key_hint"] == "…1234" and "sk-secret" not in json.dumps(shown)
    assert shown["remote"] is True


@pytest.mark.parametrize(
    "url",
    ["ftp://127.0.0.1:1", "http://user:pw@127.0.0.1:1234", "http://127.0.0.1:1234/api/pull", "http://?x"],
)
def test_a_malformed_address_is_refused(url):
    from app.router.runtimes.sources import SourceError, normalise_url

    with pytest.raises(SourceError):
        normalise_url(url)


def test_a_detected_or_configured_source_cannot_be_removed_from_settings(router_with):
    from app.router.runtimes.sources import SourceError

    model_router = router_with(_source("llamacpp", [CHAT]))
    with pytest.raises(SourceError, match="found running"):
        model_router.remove_source("llamacpp")


def test_downloads_are_offered_only_where_the_runtime_has_an_api(router_with):
    from fastapi.testclient import TestClient

    from app.main import app

    router_with(_source("llamacpp", [CHAT]))
    with TestClient(app) as client:
        refused = client.post("/api/settings/sources/llamacpp/pull", json={"model": "x"})
        assert refused.status_code == 400 and "doesn't download" in refused.json()["detail"]
        assert client.post("/api/settings/sources/llamacpp/pull", json={"model": "../etc"}).status_code == 400


# ── config: new names, old names still read ──────────────────────────────────
def test_old_env_names_still_work_and_new_ones_win():
    from app.core.config import Settings

    old = Settings(_env_file=None, ollama_ram_fraction="0.4", ollama_same_machine="true",
                   ollama_default_model="llama3.1:8b")
    assert old.local_ram_fraction == 0.4 and old.local_same_machine is True
    assert old.local_default_model == "llama3.1:8b"
    both = Settings(_env_file=None, ollama_ram_fraction="0.4", local_ram_fraction="0.5")
    assert both.local_ram_fraction == 0.5


def test_configured_sources_read_every_spelling():
    from app.core.config import Settings

    listed = Settings(_env_file=None, local_sources="http://127.0.0.1:1234, box=http://10.0.0.2:8080")
    assert listed.configured_sources == [
        {"base_url": "http://127.0.0.1:1234"},
        {"label": "box", "base_url": "http://10.0.0.2:8080"},
    ]
    as_json = Settings(_env_file=None, local_sources='[{"label": "GPU", "base_url": "http://gpu:8000", "runtime": "vllm"}]')
    assert as_json.configured_sources[0]["runtime"] == "vllm"
    legacy = Settings(_env_file=None, ollama_base_url="http://localhost:11434")
    assert legacy.configured_sources == [{"base_url": "http://localhost:11434", "runtime_hint": "legacy"}]
    assert Settings(_env_file=None, local_sources="[not json").configured_sources == []


def test_the_old_runtime_address_becomes_a_source_under_its_old_id(monkeypatch):
    """#28: an existing `.env` using OLLAMA_* names still works — saved choices too."""
    from app.router.runtimes import sources as sources_module
    from app.router.runtimes.sources import SourceRegistry

    monkeypatch.setattr(sources_module.settings, "local_sources", "")
    monkeypatch.setattr(sources_module.settings, "ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(sources_module.secrets_store, "get_sources", lambda: [])
    registry = SourceRegistry()
    registry._load()
    (provider,) = registry.providers()
    assert provider.name == table.LEGACY_BARE_RUNTIME
    assert provider.source.origin == "configured" and provider.adapter.runtime == "ollama"


def test_a_local_default_saved_the_old_way_is_read_with_its_source(tmp_path, monkeypatch):
    from app.core import secrets_store

    monkeypatch.setattr(secrets_store, "_PATH", tmp_path / "providers.local.json")
    secrets_store._write({"ollama": {"default_model": "qwen2.5:7b"}, "anthropic": {"api_key": "k"}})
    cloud = ("anthropic", "openai", "gemini")
    assert secrets_store.get_local_default(cloud) == "ollama:qwen2.5:7b"

    secrets_store.set_local_default("llamacpp:qwen", cloud)
    data = json.loads((tmp_path / "providers.local.json").read_text())
    assert data == {"anthropic": {"api_key": "k"}, "local": {"default_model": "llamacpp:qwen"}}
    assert secrets_store.get_local_default(cloud) == "llamacpp:qwen"


# ── what a call costs, and where it ran ──────────────────────────────────────
def test_local_is_free_because_the_call_says_so_not_because_of_a_name():
    from app.router.registry import estimate_cost

    assert estimate_cost("anything", 1000, 1000, provider="lmstudio", is_local=True) == 0.0
    assert estimate_cost("gpt-oss:120b-cloud", 1000, 1000, provider="ollama", is_local=False) is None
    assert estimate_cost("gpt-4o", 1_000_000, 0, provider="openai", is_local=False) == 2.5


def test_older_phase_rows_are_answered_for_from_their_provider():
    from datetime import datetime, timezone

    from app.schemas.project import PhaseResultOut

    base = dict(id="1", phase="p", agent="a", status="approved", output={}, content_md="",
                created_at=datetime.now(timezone.utc))
    assert PhaseResultOut(**base, provider_used="ollama").is_local is True
    assert PhaseResultOut(**base, provider_used="anthropic").is_local is False
    assert PhaseResultOut(**base, provider_used="ollama", is_local=False).is_local is False


def test_where_a_call_ran_is_a_column_an_existing_database_gains():
    from app.db.migrations import ADDITIVE_COLUMNS

    assert "is_local" in ADDITIVE_COLUMNS["phase_results"]
    assert "is_local" in ADDITIVE_COLUMNS["usage_events"]


def test_cancel_closes_the_request_it_names():
    adapter = OpenAICompatAdapter("http://127.0.0.1:9")
    assert adapter.cancel("nothing") is False

    class _Client:
        closed = False

        def close(self):
            self.closed = True

    client = _Client()
    adapter._inflight["req-1"] = client
    assert adapter.cancel("req-1") is True and client.closed
