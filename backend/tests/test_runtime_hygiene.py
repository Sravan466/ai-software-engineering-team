"""Phase 6 of #26: every runtime in the table, and runtime hygiene.

Each new adapter is recognised by the answer its runtime gives (payload shapes
taken from each runtime's source, linked in `table.Facts.source`), runtimes that
share a default port are told apart, every runtime has a refused-operation list the
connector enforces, and a runtime that is outdated or reachable from the network
is warned about — from a table that is data, not code.
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from tests.test_runtimes import _Resp, _serve, router_with  # noqa: F401 - fixture

from app.connector import protocol as P
from app.router.runtimes import detect, hygiene, table
from app.router.runtimes.servers import (
    DockerModelRunnerAdapter,
    FoundryLocalAdapter,
    GPT4AllAdapter,
    JanAdapter,
    MLXAdapter,
    TextGenWebUIAdapter,
)
from app.router.base import ProviderError
from app.router.runtimes.types import ChatRequest, Hello

LLAMACPP_MODELS = {"object": "list", "data": [{"id": "qwen", "object": "model", "owned_by": "llamacpp", "meta": {}}]}
PROPS = {"default_generation_settings": {"n_ctx": 8192}, "build_info": "b5000-abc123"}
MLX_MODELS = {"object": "list", "data": [{"id": "mlx-community/Qwen3-4B-4bit", "object": "model", "created": 1}]}
URL = "http://127.0.0.1:8080"


# ── the table ────────────────────────────────────────────────────────────────
def test_every_runtime_in_the_table_has_an_adapter_facts_and_a_source():
    for spec in table.RUNTIMES:
        assert spec.adapter is not None, spec.id
        assert spec.facts is not None, spec.id
        assert spec.facts.structured in ("schema", "grammar", "json", "none"), spec.id
        assert spec.facts.context, spec.id
        assert spec.facts.source.startswith("https://"), spec.id


def test_the_runtimes_that_listen_everywhere_by_default_are_the_documented_ones():
    everywhere = {s.id for s in table.RUNTIMES if s.facts.listens_everywhere}
    assert everywhere == {"vllm", "koboldcpp", "localai"}


def test_every_runtime_has_a_refused_operation_list():
    for spec in table.RUNTIMES:
        assert P.REFUSED.get(spec.id), spec.id


@pytest.mark.parametrize("runtime", sorted(k for k in P.REFUSED if k != "*"))
def test_the_connector_refuses_every_listed_operation_for_each_runtime(runtime, tmp_path, monkeypatch):
    from aiteam_connect import store
    from aiteam_connect.local import Agent, Refused

    agent = Agent("0" * 32)
    for op in P.REFUSED[runtime]:
        with pytest.raises(Refused):
            agent.handle(op, {"model": "x", "source": runtime})
    log = (store.home() / "connector.log").read_text(encoding="utf-8")
    assert log.count("refused op") >= len(P.REFUSED[runtime])


def test_the_connector_refuses_a_model_the_runtime_didnt_list(monkeypatch):
    """MLX-LM downloads and loads whatever a request names; only listed models go."""
    from aiteam_connect.local import Agent, Refused, Source

    agent = Agent("0" * 32)
    adapter = MLXAdapter(URL)
    agent._sources = {"mlx": Source("mlx", "mlx", URL, adapter, remote=False, version=None)}
    agent._sources["mlx"].models = ["mlx-community/Qwen3-4B-4bit"]
    monkeypatch.setattr(agent, "scan", lambda: ([], [], []))
    with pytest.raises(Refused):
        agent._source_for("mlx", "someone/else-70B")


def test_setup_advice_never_tells_anyone_to_open_a_runtime_up():
    """No card recommends binding to 0.0.0.0 or `OLLAMA_ORIGINS=*` — only warns against it."""
    for spec in table.RUNTIMES:
        guide = spec.setup
        if guide is None:
            continue
        for text in (guide.serve, guide.exposure or "", guide.check, *guide.install.values()):
            for sentence in text.split(". "):
                if "0.0.0.0" in sentence or "ORIGINS=*" in sentence:
                    assert "never" in sentence.lower(), (spec.id, sentence)


# ── fingerprints ─────────────────────────────────────────────────────────────
def test_same_port_llama_server_llamafile_mlx_and_localai_are_each_identified(monkeypatch):
    cases = {
        "llamacpp": {"/v1/models": _Resp(200, LLAMACPP_MODELS), "/props": _Resp(200, PROPS)},
        "llamafile": {
            "/v1/models": _Resp(200, LLAMACPP_MODELS),
            "/props": _Resp(200, PROPS),
            "/tools": _Resp(200, {"tools": []}),
        },
        "mlx": {"/v1/models": _Resp(200, MLX_MODELS), "/health": _Resp(200, {"status": "ok"})},
        "localai": {
            "/v1/models": _Resp(200, {"data": [{"id": "phi", "object": "model"}]}),
            "/.well-known/localai.json": _Resp(200, {"version": "v3.5.0"}),
            "/version": _Resp(200, {"version": "v3.5.0"}),
            "/health": _Resp(200, {"status": "ok"}),
        },
    }
    for expected, routes in cases.items():
        _serve(monkeypatch, routes)
        hello = detect.identify(URL)
        assert hello is not None and hello.runtime == expected, (expected, hello)


def test_an_unknown_openai_server_on_a_default_port_is_not_mistaken_for_mlx(monkeypatch):
    # No owned_by, but no MLX health answer either: unknown, not MLX.
    _serve(monkeypatch, {"/v1/models": _Resp(200, MLX_MODELS)})
    assert detect.identify(URL) is None


def test_jan_is_recognised_and_its_hosted_models_are_not_local(monkeypatch):
    models = {
        "data": [
            {"id": "qwen3-4b", "object": "model", "created": 1, "owned_by": "llama.cpp"},
            {"id": "gpt-4o", "object": "model", "created": 1, "owned_by": "remote"},
        ]
    }
    _serve(monkeypatch, {"/v1/models": _Resp(200, models)})
    assert detect.identify("http://127.0.0.1:1337") == Hello("jan", "http://127.0.0.1:1337")
    listed = {m.name: m.is_local for m in JanAdapter("http://127.0.0.1:1337").list_models()}
    assert listed == {"qwen3-4b": True, "gpt-4o": False}


def test_text_generation_webui_is_recognised_and_never_sent_a_response_format(monkeypatch):
    seen: list = []
    _serve(
        monkeypatch,
        {"/v1/internal/model/info": _Resp(200, {"model_name": "q", "lora_names": [], "loader": "llama.cpp"})},
        {"/v1/chat/completions": _Resp(200, {"choices": [{"message": {"content": "{}"}}]})},
        seen,
    )
    assert detect.identify("http://127.0.0.1:5000").runtime == "tgw"
    TextGenWebUIAdapter("http://127.0.0.1:5000").chat(
        ChatRequest(model="q", messages=[{"role": "user", "content": "hi"}], max_tokens=10,
                    context_window=4096, json_schema={"type": "object"})
    )
    assert "response_format" not in seen[0][1]


def test_gpt4all_is_recognised_and_says_it_has_no_embeddings(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {"/v1/models": _Resp(200, {"data": [{"id": "Llama 3", "owned_by": "humanity"}]})}, {}, seen)
    assert detect.identify("http://127.0.0.1:4891").runtime == "gpt4all"
    with pytest.raises(ProviderError):
        GPT4AllAdapter("http://127.0.0.1:4891").embed("Llama 3", ["x"])
    assert seen == []  # said at once, not by a request that could only fail


def test_docker_model_runner_speaks_under_engines_and_reports_its_window(monkeypatch):
    seen: list = []
    models = {"data": [{"id": "ai/smollm2", "owned_by": "docker", "dmr": {"context_window": 8192}}]}
    _serve(
        monkeypatch,
        {"/engines/v1/models": _Resp(200, models), "/version": _Resp(200, {"version": "1.0.3"})},
        {"/engines/v1/chat/completions": _Resp(200, {"choices": [{"message": {"content": "ok"}}]})},
        seen,
    )
    hello = detect.identify("http://127.0.0.1:12434")
    assert hello == Hello("dmr", "http://127.0.0.1:12434", "1.0.3")
    adapter = DockerModelRunnerAdapter("http://127.0.0.1:12434")
    info = adapter.model_info("ai/smollm2")
    assert info.context_window == 8192 and info.structured_output == "json"
    adapter.chat(ChatRequest(model="ai/smollm2", messages=[{"role": "user", "content": "hi"}],
                             max_tokens=10, context_window=4096))
    assert seen[0][0] == "/engines/v1/chat/completions"


def test_foundry_local_is_never_probed_but_is_identified_by_its_address(monkeypatch):
    assert not table.BY_ID["foundry"].ports
    status = {"Endpoints": ["http://127.0.0.1:5273"], "ModelDirPath": "/x", "PipeName": "inference_agent"}
    _serve(monkeypatch, {"/openai/status": _Resp(200, status), "/v1/models": _Resp(200, {"data": []})})
    assert detect.identify("http://127.0.0.1:5273") == Hello("foundry", "http://127.0.0.1:5273")
    assert FoundryLocalAdapter.fingerprint("http://127.0.0.1:5273/v1") is not None


# ── the minimum-version table ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text, parsed",
    [("0.17.1", (0, 17, 1)), ("v3.5.0", (3, 5, 0)), ("b5662-3f8a9c0", (5662,)), ("dev", None), (None, None)],
)
def test_versions_are_read_as_numbers_or_not_at_all(text, parsed):
    assert hygiene.parse_version(text) == parsed


def test_an_ollama_older_than_the_fix_is_warned_about_and_a_fixed_one_is_not():
    old = hygiene.warnings("ollama", "0.17.0")
    assert [w["kind"] for w in old] == ["outdated"]
    assert "CVE-2026-7482" in old[0]["ids"] and old[0]["fixed"] == "0.17.1"
    assert old[0]["url"].startswith("https://")
    assert hygiene.warnings("ollama", "0.17.1") == []
    assert hygiene.warnings("ollama", "0.18") == []
    assert hygiene.warnings("ollama", "not a version") == []  # unreadable is never "old"
    assert hygiene.warnings("ollama", None) == []


def test_llama_cpp_is_compared_by_build_number():
    assert hygiene.advisories("llamacpp", "b5000-abc") and not hygiene.advisories("llamacpp", "b10964-b29c606e2")


def test_every_advisory_has_a_source_link_and_a_readable_fixed_version():
    raw = json.loads(hygiene.ADVISORIES_FILE.read_text(encoding="utf-8"))
    for runtime, entries in raw["runtimes"].items():
        assert runtime in table.BY_ID, runtime
        for entry in entries:
            assert entry["url"].startswith("https://"), entry
            assert hygiene.parse_version(entry["fixed"]) is not None, entry


def test_the_table_is_data_replaced_without_a_code_change(tmp_path, monkeypatch):
    replacement = tmp_path / "advisories.json"
    replacement.write_text(json.dumps({"runtimes": {"lmstudio": [
        {"id": "TEST-1", "fixed": "0.4.0", "url": "https://example.com/a"},
        {"id": "BROKEN", "fixed": "soon", "url": "https://example.com/b"},
    ]}}))
    monkeypatch.setenv(hygiene.ADVISORIES_ENV, str(replacement))
    assert [a["id"] for a in hygiene.advisories("lmstudio", "0.3.9")] == ["TEST-1"]
    assert hygiene.advisories("ollama", "0.1.0") == []  # replaced, not merged
    monkeypatch.setenv(hygiene.ADVISORIES_ENV, str(tmp_path / "missing.json"))
    assert hygiene.advisories("lmstudio", "0.3.9") == []  # unreadable: no warnings, no crash


# ── who can reach it ─────────────────────────────────────────────────────────
class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _server(host: str):
    srv = http.server.ThreadingHTTPServer((host, 0), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_runtime_bound_to_every_interface_is_seen_and_one_on_loopback_is_not():
    addresses = [a for a in hygiene.own_addresses() if ":" not in a]
    if not addresses:
        pytest.skip("this machine has no network address to check against")
    wide, narrow = _server("0.0.0.0"), _server("127.0.0.1")
    try:
        exposed = hygiene.exposed_on(f"http://127.0.0.1:{wide.server_address[1]}", addresses)
        assert exposed == addresses
        assert hygiene.exposed_on(f"http://127.0.0.1:{narrow.server_address[1]}", addresses) == []
        # Another computer: whether *it* listens widely can't be seen from here.
        assert hygiene.exposed_on("http://192.0.2.10:8000", addresses) is None
    finally:
        wide.shutdown()
        narrow.shutdown()


def test_the_exposed_warning_says_how_to_close_it_and_never_to_open_more():
    [w] = hygiene.warnings("vllm", None, ["192.168.1.20"])
    assert w["kind"] == "exposed" and "192.168.1.20" in w["detail"]
    assert "--host 127.0.0.1" in w["detail"]


def test_the_connector_prints_each_warning_once_and_reports_the_addresses(monkeypatch):
    from aiteam_connect.local import Agent

    said: list[str] = []
    agent = Agent("0" * 32, say=said.append)
    monkeypatch.setattr(detect, "detect", lambda skip_ports=None: detect.Detection(
        found=[Hello("ollama", "http://127.0.0.1:11434", "0.16.0")]))
    monkeypatch.setattr(hygiene, "own_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(hygiene, "_accepts", lambda host, port: True)
    from app.router.runtimes.ollama import OllamaAdapter

    monkeypatch.setattr(OllamaAdapter, "list_models", lambda self: [])
    sources, _, _ = agent.scan()
    assert sources[0]["exposed_on"] == ["192.168.1.20"]
    P.SourceReport.model_validate(sources[0])  # the new field is on the schema
    text = "\n".join(said)
    assert "older than a known security fix" in text and "reachable from your network" in text
    count = len(said)
    agent.scan()
    assert len(said) == count  # once a session, not every scan


def test_a_paired_computers_runtime_is_judged_on_the_server(monkeypatch):
    from app.api.routes.devices import _warnings

    hello = {"sources": [
        {"id": "ollama", "runtime": "ollama", "version": "0.15.0", "exposed_on": []},
        {"id": "vllm", "runtime": "vllm", "version": "0.11.1", "exposed_on": ["10.0.0.4"]},
        {"id": "lmstudio", "runtime": "lmstudio", "version": None},
    ]}
    found = _warnings(hello)
    assert [w["kind"] for w in found["ollama"]] == ["outdated"]
    assert [w["kind"] for w in found["vllm"]] == ["exposed"]
    assert "lmstudio" not in found


def test_settings_lists_each_sources_warnings(router_with):
    from tests.test_runtimes import CHAT, _source

    provider = _source("ollama", [CHAT])
    provider.source.runtime, provider.source.version, provider.source.exposed = "ollama", "0.12.0", ["192.168.1.20"]
    clean = _source("lmstudio", [CHAT])
    status = router_with(provider, clean).local_status()
    rows = {s["id"]: s["warnings"] for s in status["sources"]}
    assert {w["kind"] for w in rows["ollama"]} == {"outdated", "exposed"}
    assert rows["lmstudio"] == []


def test_the_runtime_docs_list_every_runtime_and_advisory():
    import pathlib

    doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "RUNTIMES.md").read_text(encoding="utf-8")
    for spec in table.RUNTIMES:
        assert f"| {spec.label} |" in doc, spec.id
    for entries in hygiene.table().values():
        for entry in entries:
            assert entry["id"] in doc, entry["id"]
