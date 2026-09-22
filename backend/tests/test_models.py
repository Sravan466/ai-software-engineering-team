"""Phase 2 of #26 (#29): any model works — or is stopped before the run, with a reason.

Covered layer by layer: the answer is separated from the reasoning on every runtime,
thinking is asked for in each runtime's own words, generation settings are
validated and reach the next request to one model only, and the compatibility check
blocks what cannot run, flags what runs badly, and never names a model to use
instead. Payloads mirror what a running Ollama 0.30 and `llama-server` returned
while this was written.
"""
from __future__ import annotations

import pytest

from app.core import model_settings
from app.core.constants import RoutingMode
from app.router import compat, generation
from app.router.model_profile import build_profile, bits_per_weight
from app.router.runtimes.llamacpp import LlamaCppAdapter, defaults_from_props, thinking_from_template
from app.router.runtimes.ollama import (
    OllamaAdapter,
    info_from_show,
    kv_layout,
)
from app.router.runtimes.openai_compat import OpenAICompatAdapter
from app.router.runtimes.reasoning import split_reasoning
from app.router.runtimes.servers import VLLMAdapter
from app.router.runtimes.types import (
    KIND_BASE,
    THINKS_LEVELS,
    THINKS_NONE,
    THINKS_TOGGLE,
    ChatRequest,
    ModelEntry,
    ModelInfo,
    thinking_for,
)
from app.schemas.llm import ChatMessage, GenerationOptions
from tests.test_runtimes import FakeAdapter, _Resp, _serve, _source, router_with  # noqa: F401

GIB = 2**30

# ── what a running Ollama said about a small thinking model ──────────────────
QWEN3_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "parameters": 'temperature 0.6\ntop_k 20\ntop_p 0.95\nrepeat_penalty 1\nstop "<|im_start|>"\nstop "<|im_end|>"',
    "template": "{{- if .Messages }}{{ if .Think }}<think>{{ end }}{{ .Prompt }}",
    "details": {"family": "qwen3", "parameter_size": "2.0B", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "qwen3",
        "qwen3.attention.head_count": 16,
        "qwen3.attention.head_count_kv": 8,
        "qwen3.attention.key_length": 128,
        "qwen3.attention.value_length": 128,
        "qwen3.block_count": 28,
        "qwen3.context_length": 40960,
        "qwen3.embedding_length": 2048,
    },
}


def _chat_request(**kw) -> ChatRequest:
    base = dict(model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=500, context_window=8192)
    base.update(kw)
    return ChatRequest(**base)


# ── 1. the answer, never the reasoning ───────────────────────────────────────
@pytest.mark.parametrize(
    "reply, answer, reasoning",
    [
        ('<think>try {"a": 1} first</think>\n{"a": 2}', '{"a": 2}', 'try {"a": 1} first'),
        # The template opened the block in the prompt, so only its close is here.
        ('Let me think about it.</think>{"a": 2}', '{"a": 2}', "Let me think about it."),
        # Ran out of budget mid-thought: there is no answer, and none is invented.
        ("<think>still going", "", "still going"),
        ('{"a": 2}', '{"a": 2}', None),
    ],
)
def test_reasoning_is_taken_out_of_the_answer(reply, answer, reasoning):
    assert split_reasoning(reply) == (answer, reasoning)


def test_a_tag_quoted_inside_an_answer_is_left_alone():
    """Generated code may mention the tag; an answer that starts as JSON is the answer."""
    reply = '{"code": "strip </think> from the text"}'
    assert split_reasoning(reply) == (reply, None)


def test_ollama_returns_the_answer_and_the_reasoning_separately(monkeypatch):
    seen: list = []
    _serve(
        monkeypatch,
        {},
        {
            "/api/chat": _Resp(
                200,
                {
                    "message": {"content": '{"answer": "51"}', "thinking": "17 times 3 is 51."},
                    "done_reason": "stop",
                    "prompt_eval_count": 20,
                    "eval_count": 8,
                },
            )
        },
        seen,
    )
    result = OllamaAdapter("http://127.0.0.1:11434").chat(_chat_request(thinking="on", json_mode=True))
    assert result.text == '{"answer": "51"}'
    assert result.reasoning == "17 times 3 is 51."
    assert seen[0][1]["think"] is True


def test_an_openai_style_server_returns_reasoning_in_its_own_field(monkeypatch):
    _serve(
        monkeypatch,
        {},
        {
            "/v1/chat/completions": _Resp(
                200,
                {
                    "choices": [
                        {
                            "message": {"content": '{"answer": 51}', "reasoning_content": "Multiply."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 30},
                },
            )
        },
    )
    result = LlamaCppAdapter("http://127.0.0.1:8080").chat(_chat_request())
    assert result.text == '{"answer": 51}' and result.reasoning == "Multiply."


def test_the_agent_parser_reads_only_the_answer(router_with):
    """End to end: a reply with reasoning reaches the agent as the answer alone."""
    src = _source("lmstudio", [ModelEntry(name="thinker", kind="chat")], reply='<think>{"wrong": 1}</think>{"ok": 1}')
    src.adapter.chat = lambda request, _chat=src.adapter.chat: _with_split(_chat(request))  # type: ignore[method-assign]
    model_router = router_with(src)
    resp = model_router.complete(
        [ChatMessage(role="user", content="hi")], preferred_model="lmstudio:thinker", mode=RoutingMode.MANUAL
    )
    assert resp.text == '{"ok": 1}'
    assert resp.reasoning == '{"wrong": 1}'
    assert "reasoning" not in resp.model_dump(), "reasoning must never be serialised or stored"


def _with_split(result):
    result.text, result.reasoning = split_reasoning(result.text)
    return result


# ── 2. thinking, in each runtime's own words ─────────────────────────────────
def test_thinking_is_fitted_to_what_the_model_takes():
    assert thinking_for(THINKS_NONE, "high") is None, "a model that does not think is told nothing"
    assert thinking_for(THINKS_TOGGLE, "high") == "on"
    assert thinking_for(THINKS_TOGGLE, "off") == "off"
    assert thinking_for(THINKS_LEVELS, "off") == "low", "a level-only model can't be switched off"
    assert thinking_for(THINKS_LEVELS, "on") == "medium"


def test_ollama_reports_thinking_style_defaults_and_base_models():
    info = info_from_show("qwen3:1.7b", QWEN3_SHOW, supports_schema=True)
    assert info.thinking == THINKS_TOGGLE
    assert info.defaults["temperature"] == 0.6 and info.defaults["top_k"] == 20
    assert info.defaults["stop"] == ["<|im_start|>", "<|im_end|>"]

    levels = dict(QWEN3_SHOW, template="{{ if .ThinkLevel }}Reasoning: {{ .ThinkLevel }}{{ end }}")
    assert info_from_show("m", levels, supports_schema=True).thinking == THINKS_LEVELS

    base = dict(QWEN3_SHOW, capabilities=["completion"], template="{{ .Prompt }}")
    assert info_from_show("m", base, supports_schema=True).kind == KIND_BASE


def test_ollama_sends_levels_to_a_level_model_and_every_setting_it_takes(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {"/api/chat": _Resp(200, {"message": {"content": "{}"}})}, seen)
    OllamaAdapter("http://127.0.0.1:11434").chat(
        _chat_request(
            thinking="high", top_p=0.8, top_k=30, min_p=0.05, seed=7, stop=["END"],
            repeat_penalty=1.1, gpu_layers=12, threads=6, keep_alive="10m",
        )
    )
    body = seen[0][1]
    assert body["think"] == "high"
    assert body["keep_alive"] == "10m"
    assert body["options"] == {
        "top_p": 0.8, "top_k": 30, "min_p": 0.05, "seed": 7, "stop": ["END"],
        "repeat_penalty": 1.1, "num_gpu": 12, "num_thread": 6, "num_ctx": 8192, "num_predict": 500,
    }


def test_nothing_about_thinking_is_sent_to_a_model_that_does_not_think(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {"/api/chat": _Resp(200, {"message": {"content": "{}"}})}, seen)
    OllamaAdapter("http://127.0.0.1:11434").chat(_chat_request(thinking=None))
    assert "think" not in seen[0][1], "asking a non-thinking model to think is a 400"


def test_llama_server_switches_thinking_through_its_template(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {"/v1/chat/completions": _Resp(200, {"choices": [{"message": {"content": "{}"}}]})}, seen)
    LlamaCppAdapter("http://127.0.0.1:8080").chat(_chat_request(thinking="off", top_k=20, min_p=0.1))
    body = seen[0][1]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["top_k"] == 20 and body["min_p"] == 0.1


def test_a_server_that_refuses_the_thinking_fields_is_asked_again_without(monkeypatch):
    seen: list = []

    def _chat(body):
        if "chat_template_kwargs" in body:
            return _Resp(400, {"error": "unrecognized field: chat_template_kwargs"})
        return _Resp(200, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    _serve(monkeypatch, {}, {"/v1/chat/completions": _chat}, seen)
    adapter = VLLMAdapter("http://127.0.0.1:8000")
    result = adapter.chat(_chat_request(thinking="off"))
    assert len(seen) == 2 and "thinking" in result.unsent
    seen.clear()
    adapter.chat(_chat_request(thinking="off"))
    assert len(seen) == 1 and "chat_template_kwargs" not in seen[0][1], "the refusal was paid for again"


def test_vllm_gets_its_own_name_for_the_repeat_penalty(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {"/v1/chat/completions": _Resp(200, {"choices": [{"message": {"content": "{}"}}]})}, seen)
    VLLMAdapter("http://127.0.0.1:8000").chat(_chat_request(repeat_penalty=1.2))
    assert seen[0][1]["repetition_penalty"] == 1.2 and "repeat_penalty" not in seen[0][1]


def test_a_setting_a_runtime_cannot_take_is_reported_never_dropped_silently(monkeypatch):
    _serve(monkeypatch, {}, {"/v1/chat/completions": _Resp(200, {"choices": [{"message": {"content": "{}"}}]})})
    result = OpenAICompatAdapter("http://127.0.0.1:1337").chat(_chat_request(top_k=20, min_p=0.1, seed=3))
    assert set(result.unsent) == {"top_k", "min_p"}


def test_a_thinking_model_is_never_decoded_greedily_on_any_runtime(router_with, tmp_path, monkeypatch):
    """The guard lives above the adapters, so it holds whichever runtime serves the model."""
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")

    class _Thinks(FakeAdapter):
        def model_info(self, model):
            return ModelInfo(name=model, context_window=16384, context_source="reported", kind="chat",
                             thinking=THINKS_TOGGLE)

    src = _source("ollama", [ModelEntry(name="thinker", kind="chat")])
    src.adapter = _Thinks(src.adapter.models)
    model_router = router_with(src)
    model_router.set_model_generation("ollama:thinker", {"sampling": {"thinking": "on", "temperature": 0}})
    model_router.complete([ChatMessage(role="user", content="hi")], preferred_model="ollama:thinker",
                          mode=RoutingMode.MANUAL)
    call = src.adapter.calls[-1]
    assert call.thinking == "on" and call.temperature > 0


def test_where_reasoning_and_a_schema_conflict_the_schema_steps_aside(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {"/v1/chat/completions": _Resp(200, {"choices": [{"message": {"content": "{}"}}]})}, seen)
    OpenAICompatAdapter("http://127.0.0.1:1337").chat(
        _chat_request(thinking="high", json_schema={"type": "object"}, structured_output="schema")
    )
    assert "response_format" not in seen[0][1]


def test_llama_server_reports_its_defaults_and_how_the_model_thinks():
    props = {"default_generation_settings": {"params": {"temperature": 0.8, "top_k": 40, "seed": 4294967295}}}
    assert defaults_from_props(props) == {"temperature": 0.8, "top_k": 40}, "a random seed is not a default"
    assert thinking_from_template("{% if enable_thinking %}<think>{% endif %}", {}) == THINKS_TOGGLE
    assert thinking_from_template("Reasoning: {{ reasoning_effort }}", {}) == THINKS_LEVELS
    assert thinking_from_template("<|im_start|>{{ content }}", {}) == THINKS_NONE


# ── 3. what fits, measured from the model's own metadata ─────────────────────
def test_a_hybrid_model_is_charged_only_for_the_layers_that_cache():
    info = {
        "arch.block_count": 4,
        "arch.attention.head_count": 8,
        "arch.embedding_length": 1024,
        # Mamba layers keep a fixed state instead of a cache: they report 0 heads.
        "arch.attention.head_count_kv": [0, 0, 0, 8],
    }
    full, windowed, _ = kv_layout(info, "arch")
    assert full == 8 * (128 + 128) * 2, "one attention layer, not four"
    assert windowed == 0


def test_sliding_window_layers_stop_growing_past_the_window():
    info = {
        "g.block_count": 6,
        "g.attention.head_count": 8,
        "g.attention.head_count_kv": 4,
        "g.attention.key_length": 256,
        "g.attention.value_length": 256,
        "g.attention.sliding_window": 1024,
        "g.attention.sliding_window_pattern": 6,  # five local layers per global one
    }
    full, windowed, window = kv_layout(info, "g")
    assert window == 1024 and windowed == 5 * full


def test_quantization_widths_are_read_from_the_name():
    assert bits_per_weight("Q4_K_M") == 4.5
    assert bits_per_weight("IQ2_XXS") == 2.5
    assert bits_per_weight("Q8_0") == 8.5
    assert bits_per_weight("F16") == 16.0
    assert bits_per_weight("MXFP4") is None


def _profile(**info_kw):
    base = dict(name="m", context_window=32768, context_source="reported", kind="chat",
                structured_output="schema", kv_bytes_per_token=57344)
    base.update(info_kw)
    tuning = base.pop("tuning", None)
    ram = base.pop("ram", 16 * GIB)
    return build_profile(provider="src", model="m", info=ModelInfo(**base), ram_bytes=ram, tuning=tuning)


def _check(profile, ram=16 * GIB, **kw):
    return compat.assess("src:m", profile, ram_bytes=ram, remote=False, **kw)


def test_a_mid_size_model_fits():
    check = _check(_profile(parameters_total=7_600_000_000, quantization="Q4_K_M", weights_bytes=4_700_000_000))
    assert check.level == compat.FITS


def test_a_model_too_large_for_the_machine_is_blocked_with_a_size_not_a_name():
    check = _check(_profile(parameters_total=70_000_000_000, quantization="Q4_K_M", weights_bytes=40 * GIB))
    assert check.level == compat.BLOCKED
    assert "16 GB" in check.summary
    assert check.suggestion and "parameters or fewer at 4-bit" in check.suggestion


def test_a_model_at_the_edge_of_memory_runs_but_says_so():
    check = _check(_profile(parameters_total=21_000_000_000, weights_bytes=13 * GIB, experts_total=32, experts_active=4))
    assert check.level == compat.DEGRADED
    assert any("mixture of experts" in r["text"].lower() for r in check.reasons)


def test_a_short_window_is_stopped_before_the_run_with_the_runtimes_own_fix():
    check = _check(_profile(context_window=2048), window_hint="Restart llama-server with a larger `-c`.")
    assert check.level == compat.BLOCKED and "-c" in check.summary


@pytest.mark.parametrize("kind", ["embedding", KIND_BASE])
def test_models_that_cannot_follow_an_agent_are_blocked(kind):
    assert _check(_profile(kind=kind)).level == compat.BLOCKED


def test_a_small_or_heavily_quantized_model_is_flagged_not_blocked():
    check = _check(_profile(parameters_total=1_500_000_000, parameter_label="1.5B", quantization="Q3_K_S"))
    assert check.level == compat.DEGRADED
    assert len([r for r in check.reasons if r["level"] == compat.DEGRADED]) == 2


def test_memory_on_another_computer_is_not_judged_by_this_one():
    check = compat.assess(
        "src:m", _profile(weights_bytes=40 * GIB), ram_bytes=None, remote=True
    )
    assert check.level != compat.BLOCKED
    assert any("another computer" in r["text"] for r in check.reasons)


def test_a_quantized_kv_cache_lets_a_longer_window_fit():
    tight = _profile(context_window=131072, ram=2 * GIB)
    quantized = _profile(context_window=131072, ram=2 * GIB, tuning={"machine": {"kv_cache_type": "q4_0"}})
    assert quantized.context_window > tight.context_window * 3


# ── 4. the reasoning budget ─────────────────────────────────────────────────
def test_thinking_keeps_a_reasoning_budget_out_of_the_prompt():
    off = _profile(thinking=THINKS_TOGGLE)
    on = _profile(thinking=THINKS_TOGGLE, tuning={"sampling": {"thinking": "on"}, "limits": {"reasoning_tokens": 3000}})
    assert off.reasoning_tokens == 0 and on.reasoning_tokens == 3000
    assert on.prompt_token_budget < off.prompt_token_budget


def test_per_model_ceilings_only_lower_what_the_model_allows():
    capped = _profile(tuning={"limits": {"context_window": 16384, "max_output_tokens": 1024}})
    assert capped.context_window == 16384 and capped.max_output_tokens == 1024
    assert "set for this model" in (capped.clamp_reason or "")
    raised = _profile(context_window=8192, tuning={"limits": {"context_window": 65536}})
    assert raised.context_window == 8192


# ── 5. settings: validated, saved per model, applied to the next call ────────
@pytest.mark.parametrize(
    "values, words",
    [
        ({"sampling": {"top_p": 1.5}}, "between 0 and 1"),
        ({"sampling": {"temperature": "hot"}}, "a number"),
        ({"sampling": {"seed": 1.5}}, "whole number"),
        ({"sampling": {"thinking": "maybe"}}, "one of"),
        ({"sampling": {"stop": ["a", "b", "c", "d", "e"]}}, "at most 4"),
        ({"sampling": {"num_ctx": 4}}, "isn't a sampling setting"),
        ({"machine": {"keep_alive": "forever"}}, "duration"),
        ({"limits": {"context_window": 4096, "max_output_tokens": 4096}}, "half"),
        ({"gpu": {}}, "isn't a group"),
    ],
)
def test_out_of_range_settings_are_refused_with_a_reason(values, words):
    with pytest.raises(generation.SettingsError) as caught:
        generation.validate(values)
    assert words in str(caught.value)


def test_a_hand_edited_bad_value_is_dropped_not_fatal():
    assert generation.read({"sampling": {"top_p": 9, "seed": 4}}) == {"sampling": {"seed": 4}}


def test_changing_one_models_top_p_changes_its_next_request_and_no_other(router_with, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    tuned = _source("lmstudio", [ModelEntry(name="a", kind="chat"), ModelEntry(name="b", kind="chat")])
    model_router = router_with(tuned)
    ask = lambda spec: model_router.complete(  # noqa: E731
        [ChatMessage(role="user", content="hi")], preferred_model=spec, mode=RoutingMode.MANUAL,
        options=GenerationOptions(temperature=0.2),
    )
    ask("lmstudio:a")
    assert tuned.adapter.calls[-1].top_p is None and tuned.adapter.calls[-1].seed is None

    view = model_router.set_model_generation(
        "lmstudio:a", {"sampling": {"top_p": 0.5, "seed": 42, "temperature": 0.6}}
    )
    assert view["values"] == {"sampling": {"top_p": 0.5, "seed": 42, "temperature": 0.6}}
    ask("lmstudio:a")
    call = tuned.adapter.calls[-1]
    assert (call.top_p, call.seed, call.temperature) == (0.5, 42, 0.6), "the saved setting beats the agent's"
    ask("lmstudio:b")
    other = tuned.adapter.calls[-1]
    assert (other.top_p, other.seed, other.temperature) == (None, None, 0.2), "another model was touched"

    model_router.set_model_generation("lmstudio:a", None)
    ask("lmstudio:a")
    assert tuned.adapter.calls[-1].seed is None


def test_a_reply_ceiling_takes_effect_without_a_restart(router_with, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("lmstudio", [ModelEntry(name="a", kind="chat")])
    model_router = router_with(src)
    model_router.complete([ChatMessage(role="user", content="hi")], preferred_model="lmstudio:a", mode=RoutingMode.MANUAL)
    before = src.adapter.calls[-1].max_tokens
    model_router.set_model_generation("lmstudio:a", {"limits": {"max_output_tokens": 256}})
    model_router.complete([ChatMessage(role="user", content="hi")], preferred_model="lmstudio:a", mode=RoutingMode.MANUAL)
    assert src.adapter.calls[-1].max_tokens == 256 < before


def test_the_api_refuses_an_out_of_range_value(client, router_with, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    router_with(_source("lmstudio", [ModelEntry(name="a", kind="chat")]))
    r = client.put(
        "/api/settings/models/generation",
        json={"spec": "lmstudio:a", "values": {"sampling": {"top_p": 3}}},
        headers={"host": "localhost:8000"},
    )
    assert r.status_code == 400 and "Top-p" in r.json()["detail"]
    assert model_settings.get("lmstudio:a") is None, "a refused value was saved anyway"
    ok = client.get("/api/settings/models/generation", params={"spec": "lmstudio:a"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["supported"]["top_k"] is False, "the generic dialect can't send top_k, and says so"
    assert body["check"]["level"] == compat.FITS


# ── 6. stopped before the run, never halfway through ─────────────────────────
class _ShortWindow(FakeAdapter):
    def model_info(self, model):
        return ModelInfo(name=model, context_window=2048, context_source="reported", kind="chat")


def test_a_model_that_cannot_run_is_refused_before_start_with_the_reason(router_with):
    src = _source("llamacpp", [ModelEntry(name="tiny-window", kind="chat")])
    src.adapter = _ShortWindow(src.adapter.models)
    model_router = router_with(src, chosen="llamacpp:tiny-window")
    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, roles=["product_manager"])
    assert not ready.ok
    assert "2,048-token window" in (ready.reason or "")
    assert ready.checks and ready.checks[0]["level"] == compat.BLOCKED
    assert ready.checks[0]["roles"] == ["product_manager"]


def test_a_degraded_model_starts_and_the_check_travels_with_it(router_with):
    class _Small(FakeAdapter):
        def model_info(self, model):
            return ModelInfo(name=model, context_window=16384, context_source="reported", kind="chat",
                             parameters_total=1_500_000_000, parameter_label="1.5B")

    src = _source("ollama", [ModelEntry(name="small", kind="chat")])
    src.adapter = _Small(src.adapter.models)
    model_router = router_with(src, chosen="ollama:small")
    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, roles=["product_manager"])
    assert ready.ok and ready.checks[0]["level"] == compat.DEGRADED


def test_the_preflight_says_what_the_check_found(client, monkeypatch):
    from app.router.router import Readiness, router as model_router

    row = {"spec": "s:m", "level": "degraded", "summary": "tight", "reasons": [], "suggestion": None,
           "facts": {}, "model": "m", "source_label": "S", "roles": []}
    monkeypatch.setattr(model_router, "readiness", lambda *a, **k: Readiness(ok=True, checks=(row,)))
    r = client.post("/api/projects/preflight", json={"routing_mode": "local_only"})
    assert r.json()["checks"] == [row]


def test_a_model_that_reasons_unasked_gets_a_budget_from_then_on(router_with):
    src = _source("lmstudio", [ModelEntry(name="thinker", kind="chat")])
    adapter = src.adapter

    def _chat(request, _chat=adapter.chat):
        result = _chat(request)
        result.reasoning = "thinking anyway"
        return result

    adapter.chat = _chat  # type: ignore[method-assign]
    model_router = router_with(src)
    ask = lambda: model_router.complete(  # noqa: E731
        [ChatMessage(role="user", content="hi")], preferred_model="lmstudio:thinker", mode=RoutingMode.MANUAL
    )
    ask()
    first = adapter.calls[-1].max_tokens
    ask()
    assert adapter.calls[-1].max_tokens > first
    assert adapter.calls[-1].thinking == adapter.calls[0].thinking, "what is sent must not change"


# ── round 1 of review: what it found, pinned ─────────────────────────────────
def test_keep_alive_without_a_unit_reaches_the_runtime_as_seconds(monkeypatch):
    """A string is a duration to the runtime and needs a unit; "-1" as a string is a 400."""
    seen: list = []
    _serve(monkeypatch, {}, {"/api/chat": _Resp(200, {"message": {"content": "{}"}})}, seen)
    adapter = OllamaAdapter("http://127.0.0.1:11434")
    for sent, wire in (("-1", -1), ("600", 600), ("10m", "10m")):
        adapter.chat(_chat_request(keep_alive=sent))
        assert seen[-1][1]["keep_alive"] == wire


def test_machine_settings_never_reach_another_computer(router_with, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("lmstudio", [ModelEntry(name="a", kind="chat")])
    src.source.base_url = "http://192.168.1.20:1234"
    src.source.same_machine_override = False
    model_router = router_with(src)
    model_router.set_model_generation("lmstudio:a", {"machine": {"threads": 4, "keep_alive": "5m"}})
    model_router.complete([ChatMessage(role="user", content="hi")], preferred_model="lmstudio:a", mode=RoutingMode.MANUAL)
    call = src.adapter.calls[-1]
    assert call.threads is None and call.keep_alive is None
    assert model_router.model_generation("lmstudio:a")["machine_applies"] is False


@pytest.mark.parametrize(
    "values, words",
    [
        ({"sampling": {"seed": 10**400}}, "between"),
        ({"sampling": {"repeat_penalty": 0}}, "between"),
        ({"sampling": {"stop": ["\n"]}}, "every reply"),
        ({"sampling": {"stop": ["}"]}}, "every reply"),
        ({"sampling": {"stop": ["a\u202eb"]}}, "formatting"),
    ],
)
def test_values_that_would_break_generation_are_refused(values, words):
    with pytest.raises(generation.SettingsError) as caught:
        generation.validate(values)
    assert words in str(caught.value)


def test_an_unreadable_settings_file_is_set_aside_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "model_settings.local.json"
    monkeypatch.setattr(model_settings, "_PATH", path)
    path.write_text('{"src:a": {"sampling": {"seed": 1}}, oops')
    model_settings.put("src:b", {"sampling": {"seed": 2}})
    [aside] = tmp_path.glob("model_settings.local.json.unreadable-*")
    assert aside.read_text().startswith('{"src:a"')
    assert model_settings.get("src:b") == {"sampling": {"seed": 2}}


def test_settings_are_refused_for_a_model_the_source_does_not_serve(router_with, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    model_router = router_with(_source("lmstudio", [ModelEntry(name="a", kind="chat")]))
    with pytest.raises(ValueError, match="doesn't serve"):
        model_router.set_model_generation("lmstudio:" + "x" * 40, {"sampling": {"seed": 1}})
    assert model_settings.all_settings() == {}


def test_the_new_settings_routes_refuse_an_untrusted_host(client, router_with, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    router_with(_source("lmstudio", [ModelEntry(name="a", kind="chat")]))
    r = client.put(
        "/api/settings/models/generation",
        json={"spec": "lmstudio:a", "values": {"sampling": {"seed": 1}}},
        headers={"host": "attacker.example"},
    )
    assert r.status_code == 403 and model_settings.get("lmstudio:a") is None


def test_ram_alone_blocks_only_where_the_gpu_shares_it():
    big = _profile(weights_bytes=20 * GIB)
    assert compat.assess("s:m", big, ram_bytes=16 * GIB, remote=False, unified=True).level == compat.BLOCKED
    split = compat.assess("s:m", big, ram_bytes=16 * GIB, remote=False, unified=False)
    assert split.level == compat.DEGRADED and "GPU" in split.summary
    loaded = compat.assess("s:m", big, ram_bytes=16 * GIB, remote=False, unified=True, loaded=True)
    assert loaded.level == compat.FITS, "a model its runtime already holds demonstrably fits"


def test_the_window_is_judged_after_the_reasoning_budget():
    thinking = _profile(context_window=4096, thinking=THINKS_TOGGLE, tuning={"sampling": {"thinking": "on"}})
    check = _check(thinking)
    assert check.level == compat.BLOCKED and "thinking off" in (check.suggestion or "")


def test_a_chat_model_drawn_by_a_built_in_renderer_is_not_a_base_model():
    rendered = dict(QWEN3_SHOW, capabilities=["completion"], template="{{ .Prompt }}", renderer="qwen3")
    assert info_from_show("m", rendered, supports_schema=True).kind != KIND_BASE
    with_tools = dict(QWEN3_SHOW, capabilities=["completion", "tools"], template="{{ .Prompt }}")
    assert info_from_show("m", with_tools, supports_schema=True).kind != KIND_BASE


def test_a_template_that_only_strips_old_reasoning_does_not_think():
    strips = "{% for m in messages %}{{ m.content.split('</think>')[-1] }}{% endfor %}" \
        "{% if add_generation_prompt %}<|im_start|>assistant{% endif %}"
    assert thinking_from_template(strips.replace("</think>", "<think>"), {}) == THINKS_NONE
    opens = "{% if add_generation_prompt %}<|im_start|>assistant\n<think>\n{% endif %}"
    assert thinking_from_template(opens, {}) == "always"


def test_a_risky_stop_sequence_is_flagged_before_the_run():
    tuning = {"sampling": {"stop": ["\n\n"]}}
    check = _check(_profile(tuning=tuning), tuning=tuning)
    assert check.level == compat.DEGRADED and "stop sequence" in check.summary


def test_a_zero_reasoning_budget_is_learned_once_not_on_every_call(router_with, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("lmstudio", [ModelEntry(name="t", kind="chat")])

    def _chat(request, _chat=src.adapter.chat):
        result = _chat(request)
        result.reasoning = "anyway"
        return result

    src.adapter.chat = _chat  # type: ignore[method-assign]
    model_router = router_with(src)
    model_router.set_model_generation("lmstudio:t", {"limits": {"reasoning_tokens": 0}})
    with caplog.at_level("WARNING"):
        for _ in range(3):
            model_router.complete([ChatMessage(role="user", content="hi")], preferred_model="lmstudio:t",
                                  mode=RoutingMode.MANUAL)
    assert sum("although it was not asked" in r.message for r in caplog.records) == 1


# ── round 2 of review ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "reply, opened, answer, reasoning",
    [
        # Reasoning a template opened, full of braces, on a server with no parser.
        ('The user wants JSON like {"a": 1}, so\n</think>\n\n{"a": 2}', False, '{"a": 2}',
         'The user wants JSON like {"a": 1}, so'),
        ('Plan: `{}` first.</think>Done: {"a": 2}', True, 'Done: {"a": 2}', 'Plan: `{}` first.'),
        # Prose that merely mentions the tag is an answer, not a thought.
        ("Here is how to strip </think> tags from text.", False,
         "Here is how to strip </think> tags from text.", None),
        ('```json\n{"content": "s = \'</think>\'"}\n```', False,
         '```json\n{"content": "s = \'</think>\'"}\n```', None),
    ],
)
def test_the_splitter_keeps_reasoning_out_without_eating_answers(reply, opened, answer, reasoning):
    assert split_reasoning(reply, opened=opened) == (answer, reasoning)


def test_a_model_its_runtime_already_holds_is_not_refused_for_memory(router_with):
    class _Big(FakeAdapter):
        def model_info(self, model):
            return ModelInfo(name=model, context_window=16384, context_source="reported", kind="chat",
                             weights_bytes=10**15)

    src = _source("llamacpp", [ModelEntry(name="huge", kind="chat", loaded=True)])
    src.adapter = _Big(src.adapter.models)
    router_with(src)
    assert src.compatibility("huge").level != compat.BLOCKED


# ── final review ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "reply, opened",
    [
        # Code that strips reasoning tags, inside the JSON deliverable.
        ('{"c": "x.split(\\"</think>\\")[-1]"}', True),
        ('{"c": "x.split(\\"</think>\\")[-1]"}', False),
        # Prose, then an answer whose own string or code holds the tag.
        ("Here's the file:\n```js\nconst t = `</think>`;\n```", False),
        ('Here you go:\n{"content": "a</think>{b}"}', False),
    ],
)
def test_a_close_tag_inside_an_answer_is_never_taken_for_reasoning(reply, opened):
    assert split_reasoning(reply, opened=opened) == (reply, None)


def test_content_is_the_answer_when_the_runtime_split_the_reasoning_out(monkeypatch):
    answer = '{"files": [{"content": "s.split(\\"</think>\\")[-1].strip()"}]}'
    _serve(monkeypatch, {}, {"/api/chat": _Resp(200, {"message": {"content": answer, "thinking": "ok"}})})
    result = OllamaAdapter("http://127.0.0.1:11434").chat(_chat_request(thinking="on"))
    assert result.text == answer and result.reasoning == "ok"


def test_the_short_window_advice_only_offers_what_could_work():
    always = _profile(context_window=2048, thinking="always")
    assert "reasoning budget" not in (_check(always).suggestion or "")
    toggled = _profile(context_window=4096, thinking=THINKS_TOGGLE, tuning={"sampling": {"thinking": "on"}})
    assert "thinking off" in (_check(toggled).suggestion or "")
