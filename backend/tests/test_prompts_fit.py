"""#101: every prompt fits its window — refused loudly, then shrunk and sent once more.

Ollama, by default, cuts an oversized prompt from the head and still answers 200: the
system prompt, and the JSON shape it carries, are what go. Now it is told not to
(`truncate: false`), and says so with the exact count; llama.cpp counts a prompt
before it is sent; every reply's real count teaches the router how many characters
make a token for that model; and the agent that built a prompt that doesn't fit
rebuilds it for the model that will answer, once.
"""
from __future__ import annotations

import json

import pytest

from app.agents.base import AgentContext
from app.core.config import settings
from app.core.constants import RoutingMode
from app.router.base import PromptTooLong, WindowTooSmall, overflow
from app.router.model_profile import ModelProfile, fitted
from app.router.runtimes.llamacpp import LlamaCppAdapter
from app.router.runtimes.ollama import OllamaAdapter
from app.router.runtimes.openai_compat import OpenAICompatAdapter
from app.router.runtimes.types import ModelEntry
from app.schemas.llm import ChatMessage, GenerationOptions
from tests.conftest import _fake_complete, stub
from tests.test_models import _chat_request
from tests.test_runtimes import FakeAdapter, _Resp, _serve, _source
from tests.test_runtimes import router_with  # noqa: F401 - the fixture is used by name

#: What Ollama 0.30.10 answered, checked on this machine: llama.cpp's error, as a JSON
#: string inside Ollama's own `error`.
OLLAMA_OVERFLOW = (
    '{"error":"{\\"error\\":{\\"code\\":400,\\"message\\":\\"request (1525 tokens) exceeds the available '
    'context size (512 tokens), try increasing it\\",\\"type\\":\\"exceed_context_size_error\\",'
    '\\"n_prompt_tokens\\":1525,\\"n_ctx\\":512}}"}'
)
LLAMA_OVERFLOW = json.dumps({"error": {
    "code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": 9000, "n_ctx": 8192,
    "message": "request (9000 tokens) exceeds the available context size (8192 tokens), try increasing it",
}})


def _profile(window: int, output: int, **kw) -> ModelProfile:
    return ModelProfile(provider="mock", model="m", context_limit=window, context_window=window,
                        max_output_tokens=output, **kw)


# ── the runtimes refuse instead of cutting ───────────────────────────────────
def test_an_overflow_answer_is_read_for_its_exact_count():
    assert overflow(OLLAMA_OVERFLOW) == (1525, 512)
    assert overflow(LLAMA_OVERFLOW) == (9000, 8192)
    assert overflow('{"error": "model \\"x\\" not found, try pulling it first"}') is None


def test_ollama_is_told_never_to_cut_and_its_refusal_is_not_taken_for_a_schema_one(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {"/api/chat": _Resp(400, text=OLLAMA_OVERFLOW)}, seen)
    with pytest.raises(PromptTooLong) as caught:
        OllamaAdapter("http://127.0.0.1:11434").chat(
            _chat_request(json_schema={"type": "object"}, structured_output="schema", context_window=512)
        )
    assert caught.value.prompt_tokens == 1525 and caught.value.context == 512
    # One request: the same prompt without its schema is no shorter, so it isn't sent.
    assert len(seen) == 1
    body = seen[0][1]
    assert body["truncate"] is False and body["shift"] is False


def test_llama_server_refusing_a_long_prompt_says_how_long(monkeypatch):
    _serve(monkeypatch, {}, {"/v1/chat/completions": _Resp(400, text=LLAMA_OVERFLOW)})
    with pytest.raises(PromptTooLong) as caught:
        OpenAICompatAdapter("http://127.0.0.1:8080").chat(_chat_request())
    assert caught.value.prompt_tokens == 9000 and not caught.value.retryable


def test_llama_server_counts_a_prompt_with_its_own_template_and_tokenizer(monkeypatch):
    seen: list = []
    _serve(monkeypatch, {}, {
        "/apply-template": _Resp(200, {"prompt": "<|im_start|>user\nhi<|im_end|>\n<|im_start|>assistant\n"}),
        "/tokenize": _Resp(200, {"tokens": list(range(11))}),
    }, seen)
    count = LlamaCppAdapter("http://127.0.0.1:8080").count_tokens("m", [{"role": "user", "content": "hi"}])
    assert count == 11
    assert [p for p, _ in seen] == ["/apply-template", "/tokenize"]
    assert seen[1][1]["content"].startswith("<|im_start|>")
    # A server without those endpoints can't count: nothing is guessed.
    _serve(monkeypatch, {}, {})
    assert LlamaCppAdapter("http://127.0.0.1:8080").count_tokens("m", [{"role": "user", "content": "hi"}]) is None


# ── the router learns each model's characters per token ──────────────────────
class _Counting(FakeAdapter):
    """Reports `ratio` characters per token for what it is sent."""

    def __init__(self, models, ratio: float, window: int = 16384):
        super().__init__(models)
        self.ratio, self.window = ratio, window

    def model_info(self, model):
        info = super().model_info(model)
        return info.__class__(**{**info.__dict__, "context_window": self.window})

    def chat(self, request):
        result = super().chat(request)
        chars = sum(len(m["content"]) for m in request.messages)
        return result.__class__(**{**result.__dict__, "prompt_tokens": int(chars / self.ratio)})


def _say(model_router, spec: str, chars: int = 4000):
    return model_router.complete([ChatMessage(role="user", content="x" * chars)], preferred_model=spec,
                                 mode=RoutingMode.MANUAL, options=GenerationOptions())


def test_a_models_real_ratio_lowers_the_budget_at_once_and_raises_it_only_once_calls_agree(router_with):  # noqa: F811
    dense = _source("dense", [ModelEntry(name="m", kind="chat")])
    dense.adapter = _Counting(dense.adapter.models, ratio=2.0)
    roomy = _source("roomy", [ModelEntry(name="m", kind="chat")])
    roomy.adapter = _Counting(roomy.adapter.models, ratio=4.0)
    model_router = router_with(dense, roomy)

    before = model_router.profile_for(RoutingMode.MANUAL, "dense:m")
    assert before.chars_per_token is None
    _say(model_router, "dense:m")
    # One call says the estimate was optimistic: believed at once, less a margin.
    after = model_router.profile_for(RoutingMode.MANUAL, "dense:m")
    assert after.chars_per_token == pytest.approx(2.0 * 0.92, abs=0.01)
    assert after.prompt_char_budget < before.prompt_char_budget

    # A roomier model buys more only once three calls agree — never on one odd prompt.
    _say(model_router, "roomy:m")
    assert model_router.profile_for(RoutingMode.MANUAL, "roomy:m").chars_per_token == settings.approx_chars_per_token
    _say(model_router, "roomy:m")
    _say(model_router, "roomy:m")
    assert model_router.profile_for(RoutingMode.MANUAL, "roomy:m").chars_per_token == pytest.approx(4.0 * 0.92, abs=0.01)


def test_a_prompt_that_meets_a_smaller_fallback_is_handed_back_for_resizing(router_with, monkeypatch):  # noqa: F811
    big = _source("big", [ModelEntry(name="m", kind="chat")], up=False)
    small = _source("small", [ModelEntry(name="tiny", kind="chat")])
    small.adapter = _Counting(small.adapter.models, ratio=3.0, window=4096)
    model_router = router_with(big, small)
    monkeypatch.setattr(model_router, "_resolve_chain", lambda *a, **k: [("big", "m"), ("small", "tiny")])
    with pytest.raises(PromptTooLong) as caught:
        model_router.complete([ChatMessage(role="user", content="x" * 30_000)], mode=RoutingMode.MANUAL,
                              options=GenerationOptions())
    e = caught.value
    assert e.model == "small:tiny" and e.profile.context_window == 4096
    assert not small.adapter.calls, "never sent to be cut"
    # Resized for that model, the prompt fits and is held to it.
    smaller = fitted(e, _profile(32768, 4096))
    assert smaller.context_window == 4096
    resp = model_router.complete([ChatMessage(role="user", content="x" * smaller.prompt_char_budget)],
                                 mode=RoutingMode.MANUAL, options=GenerationOptions(), pin=e.model)
    assert resp.model == "tiny"


def test_a_refused_prompt_is_never_passed_to_the_next_model(router_with, monkeypatch):  # noqa: F811
    first = _source("first", [ModelEntry(name="m", kind="chat")])
    second = _source("second", [ModelEntry(name="m", kind="chat")])

    def refuse(request):
        raise PromptTooLong("too long", prompt_tokens=9000, context=8192)

    first.adapter.chat = refuse
    model_router = router_with(first, second)
    monkeypatch.setattr(model_router, "_resolve_chain", lambda *a, **k: [("first", "m"), ("second", "m")])
    with pytest.raises(PromptTooLong) as caught:
        _say(model_router, "first:m", chars=27_000)
    assert caught.value.model == "first:m" and caught.value.chars == 27_000
    assert not second.adapter.calls
    # And the count was learned: 27,000 characters were 9,000 tokens.
    assert model_router.chars_per_token("first", "m") == pytest.approx(3.0 * 0.92, abs=0.01)


# ── the agent shrinks and sends once more ────────────────────────────────────
def test_an_agent_whose_prompt_is_refused_rebuilds_it_smaller_and_sends_it_once_more(monkeypatch, stub_router):
    sizes: list = []
    pins: list = []

    def complete(messages, **kwargs):
        chars = sum(len(m.content) for m in messages)
        sizes.append(chars)
        pins.append(kwargs.get("pin"))
        if len(sizes) == 1:
            # The runtime counted it: a third more tokens than the estimate allowed for.
            error = PromptTooLong("too long", prompt_tokens=int(chars / 2.2), context=8192, chars=chars)
            error.model = "ollama:qwen2.5:7b"
            raise error
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", complete)
    stub(monkeypatch, "profile_for", lambda *a, **k: _profile(8192, 4096))
    from app.agents.qa_engineer import QAEngineerAgent

    ctx = AgentContext(idea="A tool library " * 200, prior_outputs={}, rag_context="r" * 60_000)
    result = QAEngineerAgent().run(ctx)
    assert len(sizes) == 2, "refused once, then sent once — never twice in a row"
    assert sizes[1] < sizes[0] * 0.8
    assert pins == [None, "ollama:qwen2.5:7b"], "held to the model it was rebuilt for"
    assert result.output


def test_instructions_that_dont_fit_stop_the_phase_with_a_plain_reason(monkeypatch, stub_router):
    calls: list = []
    stub(monkeypatch, "complete", lambda messages, **kw: calls.append(1) or _fake_complete(messages, **kw))
    stub(monkeypatch, "profile_for", lambda *a, **k: _profile(1024, 512))
    from app.agents.qa_engineer import QAEngineerAgent

    with pytest.raises(WindowTooSmall) as caught:
        QAEngineerAgent().run(AgentContext(idea="A tool library"))
    assert not calls, "nothing is sent that would lose its shape"
    message = str(caught.value)
    assert message.startswith("m's 1,024-token window is too small for the QA Engineer: its instructions need about")
    assert "Use a model with a larger window for this phase" in message and not caught.value.retryable


def test_a_build_on_a_window_too_small_for_a_phase_fails_saying_so(client, monkeypatch, stub_router):
    from tests.conftest import through_question_gates

    def small(*a, role=None, **k):
        return _profile(1024, 512) if role == "qa_engineer" else _profile(32768, 4096)

    stub(monkeypatch, "profile_for", small)
    pid = client.post("/api/projects", json={"idea": "A tool library", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = through_question_gates(client, pid)
    assert project["status"] == "failed"
    assert "too small for the QA Engineer" in (project.get("last_error") or "")


def test_the_required_keys_are_said_once_more_at_the_end_of_the_ask(stub_router):
    from app.agents.product_manager import ProductManagerAgent

    agent = ProductManagerAgent()
    ask = agent._build_messages(AgentContext(idea="A tool library"), _profile(32768, 4096))
    last = ask.messages[-1].content.rstrip().splitlines()[-1]
    keys = [k for k, f in agent.output_model.model_fields.items() if f.is_required()]
    assert last == f"Answer with one JSON object with these keys: {', '.join(keys)}."
    # Editing returns only what changes, so there is no list of keys to repeat.
    editing = agent._build_messages(
        AgentContext(idea="x", change="rename it", base_output={"summary": "s"}), _profile(32768, 4096)
    )
    assert "Answer with one JSON object with these keys" not in editing.messages[-1].content


def test_a_code_phase_write_call_is_rebuilt_to_fit_and_the_rest_are_sized_alike(monkeypatch):
    from tests.test_code_phases import SMALL, Scripted, _run

    model = Scripted("Backend Engineer", [{"path": "app/a.py", "purpose": "a"}, {"path": "app/b.py", "purpose": "b"}])
    sizes: list = []

    def refusing(messages, **kwargs):
        writing = "# Write now" in messages[-1].content and not getattr(kwargs.get("options"), "json_schema", None)
        if writing:
            sizes.append(sum(len(m.content) for m in messages))
            if len(sizes) == 1:
                raise PromptTooLong("too long", prompt_tokens=int(sizes[0] / 2.4), context=SMALL.context_window,
                                    chars=sizes[0])
        return model(messages, **kwargs)

    result = _run(monkeypatch, "backend_engineer", Scripted("Backend Engineer", model.plan_files), SMALL)
    assert result  # warm-up: the scripted phase runs as is
    stub(monkeypatch, "complete", refusing)
    from app.agents import get_agent

    get_agent("backend_engineer").run(AgentContext(idea="A tool library " * 300, prior_outputs={}))
    assert len(sizes) == 3, "one refusal, its resend, then the second file"
    assert sizes[1] < sizes[0] and sizes[2] <= sizes[1] * 1.2
