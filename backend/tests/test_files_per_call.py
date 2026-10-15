"""#101: a model whose reply can hold several files writes several files per call.

The reply was capped at MAX_OUTPUT_TOKENS (4,096) for every model, and batching needed
8,192 — so every model, a 200K-window cloud model included, wrote one file per call
like a 7B. Now a cloud model's reply is what its provider publishes, a local model's
the configured default (which a per-model setting can raise as well as lower), and a
phase batches whenever the reply holds two files and the prompt can hold what they
import. MAX_PROMPT_TOKENS still guards the bill.
"""
from __future__ import annotations

from app.agents.base import AgentContext
from app.core.config import settings
from app.router.model_profile import build_profile, files_per_call
from app.router.providers.anthropic_provider import AnthropicProvider
from app.router.providers.gemini_provider import GeminiProvider
from app.router.providers.openai_provider import OpenAIProvider
from app.router.runtimes.types import ModelInfo
from tests.test_code_phases import Scripted, _profile, _run


def test_a_cloud_model_gets_its_providers_published_reply_ceiling():
    claude = AnthropicProvider().profile("claude-opus-4-8")
    # Under 21,333: above it, Anthropic's SDK refuses a request that isn't streamed.
    assert claude.max_output_tokens == settings.anthropic_max_output_tokens == 16_384
    assert OpenAIProvider().profile("gpt-4o").max_output_tokens == 16_384
    assert GeminiProvider().profile("gemini-1.5-pro").max_output_tokens == 8_192
    # The bill guard is unchanged: a 200K window still budgets at most MAX_PROMPT_TOKENS.
    assert claude.prompt_token_budget == settings.max_prompt_tokens


def test_a_key_checked_window_keeps_the_reply_ceiling_and_each_model_its_own_limit():
    """The key check reads a model's window and its own reply limit: the window must not
    cost the ceiling, and a model with a smaller limit is never sent the provider's."""
    claude = AnthropicProvider()
    claude.known_context["claude-opus-4-8"] = 200_000
    assert claude.profile("claude-opus-4-8").max_output_tokens == 16_384
    # A model whose own limit the check read: that limit, never above the ceiling.
    claude.known_output["claude-3-5-haiku"] = 8_192
    assert claude.profile("claude-3-5-haiku").max_output_tokens == 8_192
    claude.known_output["claude-opus-4-8"] = 32_000
    assert claude.profile("claude-opus-4-8").max_output_tokens == 16_384
    # A model nobody read a limit for: the default every model takes, as before.
    assert OpenAIProvider().profile("gpt-3.5-turbo").max_output_tokens == settings.max_output_tokens
    gemini = GeminiProvider()
    gemini.known_context["gemini-2.5-flash"] = 1_048_576
    assert gemini.profile("gemini-2.5-flash").max_output_tokens == settings.max_output_tokens


def test_the_key_check_reads_each_models_own_reply_limit():
    from app.router import keycheck

    assert keycheck._output_tokens("anthropic", {"max_input_tokens": 200_000, "max_tokens": 32_000}) == 32_000
    assert keycheck._output_tokens("gemini", {"inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536}) == 65_536
    assert keycheck._output_tokens("openai", {"id": "gpt-4o"}) is None
    assert keycheck._output_tokens("anthropic", None) is None
    saved = keycheck.KeyCheck(status=keycheck.VALID, reason="ok", message="The key works.", model="m", output_tokens=8_192)
    assert keycheck.KeyCheck.from_dict(saved.to_dict()).output_tokens == 8_192


def test_a_local_model_keeps_the_default_and_a_per_model_setting_can_raise_it():
    info = ModelInfo(name="qwen2.5:7b", context_window=32_768, context_source="reported", kind="chat")
    plain = build_profile(provider="ollama", model="qwen2.5:7b", info=info, ram_bytes=None)
    assert plain.max_output_tokens == settings.max_output_tokens == 4_096
    raised = build_profile(provider="ollama", model="qwen2.5:7b", info=info, ram_bytes=None,
                           tuning={"limits": {"max_output_tokens": 8_192}})
    assert raised.max_output_tokens == 8_192
    lowered = build_profile(provider="ollama", model="qwen2.5:7b", info=info, ram_bytes=None,
                            tuning={"limits": {"max_output_tokens": 1_024}})
    assert lowered.max_output_tokens == 1_024
    # Never more than half the window, whatever was set.
    small = ModelInfo(name="qwen2.5:7b", context_window=8_192, context_source="reported", kind="chat")
    capped = build_profile(provider="ollama", model="qwen2.5:7b", info=small, ram_bytes=None,
                           tuning={"limits": {"max_output_tokens": 8_192}})
    assert capped.max_output_tokens == 4_096


def test_a_cloud_model_writes_the_frontend_in_a_few_calls_and_a_7b_at_4k_one_by_one(monkeypatch):
    """Acceptance: the same twelve-file plan, two models."""
    plan = [{"path": f"components/Part{i}.tsx", "purpose": f"part {i}"} for i in range(12)]
    # About what qwen2.5:7b writes: the median file it wrote in this install's builds
    # was 554 characters.
    body = "".join(f"      <li key=\"{n}\">Item {n} of the list, with a little copy</li>\n" for n in range(8))
    code = {
        f"frontend/components/Part{i}.tsx": f"export function Part{i}() {{\n  return (\n    <ul>\n{body}    </ul>\n  );\n}}\n"
        for i in range(12)
    }

    claude = AnthropicProvider().profile("claude-opus-4-8")
    model = Scripted("Frontend Engineer", plan, code=code)
    result = _run(monkeypatch, "frontend_engineer", model, claude, AgentContext(idea="A landing page"))
    assert len(model.writes) <= 2 and sum(len(w) for w in model.writes) == 12
    assert result.handoff["generation"]["mode"] == "batch" and result.build_status == "ok"

    seven_b = _profile(4096, 2048, model="qwen2.5:7b")
    model = Scripted("Frontend Engineer", plan, code=code)
    result = _run(monkeypatch, "frontend_engineer", model, seven_b, AgentContext(idea="A landing page"))
    assert [len(w) for w in model.writes] == [1] * 12
    assert result.handoff["generation"]["mode"] == "one"


def test_the_batch_is_what_both_the_reply_and_the_prompt_hold():
    # A reply that holds six files, a prompt that can be written against two.
    assert files_per_call(_profile(16_384, 8_192), 40, overhead=3_000) == 2
    # Larger files than the default: fewer of them. A cloud model's reply holds eight of
    # these, but MAX_PROMPT_TOKENS — the bill guard — leaves room to write two against.
    assert files_per_call(_profile(200_000, 32_000), 40, avg_file_tokens=4_000, overhead=3_000) == 2
    assert files_per_call(_profile(200_000, 32_000), 40, avg_file_tokens=12_000, overhead=3_000) == 1


def test_a_batch_leaves_room_in_the_reply():
    """Six tenths of the reply, over the average file and its fence: a file larger than
    average, and a cloud model's thinking, fit in what is left."""
    seven_b = _profile(32_768, 4_096, model="qwen2.5:7b")
    assert files_per_call(seven_b, 40, avg_file_tokens=1_200) == 2
    assert files_per_call(seven_b, 40, avg_file_tokens=354) == 6
    # Gemini's 8,192 holds four of the default, not six.
    assert files_per_call(_profile(1_000_000, 8_192), 40, overhead=3_000) == 4


def test_a_batch_repair_shows_every_file_whole(monkeypatch):
    """Failing files whose echoes don't fit one fix call together are fixed in more than
    one, each shown whole, never clipped to a slice of the room."""
    from app.agents.base import _TRUNCATED

    paths = [f"backend/app/f{i}.py" for i in range(4)]
    body = "".join(f"VALUE_{n} = {n}  # a line of the module, long enough to count\n" for n in range(150))
    fix_prompts: list[str] = []

    def reply(model, wanted, n):
        prompt = model.prompts[-1]
        if "# Your last version of" in prompt:
            fix_prompts.append(prompt)
            return "".join(f"### {p}\n```python\n{body}```\n\n" for p in wanted)
        return "".join(f"### {p}\n```python\ndef broken(:\n{body}```\n\n" for p in wanted)

    plan = [{"path": p.removeprefix("backend/"), "purpose": "a module"} for p in paths]
    model = Scripted("Backend Engineer", plan, reply=reply)
    result = _run(monkeypatch, "backend_engineer", model, _profile(200_000, 16_384), AgentContext(idea="A tool library"))
    assert len(fix_prompts) > 1, "four files too large to show together: more than one fix call"
    for prompt in fix_prompts:
        assert _TRUNCATED not in prompt, "every echo whole"
    shown = [p for prompt in fix_prompts for p in paths if f"# Your last version of {p}" in prompt]
    assert sorted(shown) == sorted(paths)
    assert result.build_status == "ok"


def test_settings_figures_against_the_instructions_a_run_really_had(client, monkeypatch):
    from app.router.router import router as model_router

    roles = client.get("/api/settings/roles").json()["roles"]
    before = next(x for x in roles if x["role"] == "frontend_engineer")["files_per_call_auto"]
    model_router.note_write_overhead("frontend_engineer", 60_000)
    roles = client.get("/api/settings/roles").json()["roles"]
    after = next(x for x in roles if x["role"] == "frontend_engineer")["files_per_call_auto"]
    assert before >= 2 and after == 1


def _refusing_client(limit_message: str, sent: list):
    from types import SimpleNamespace

    def create(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            raise RuntimeError(limit_message)
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5, input_tokens=3, output_tokens=2)
        choice = SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage, content=[SimpleNamespace(type="text", text="{}")],
                               stop_reason="end_turn")
    return create


def test_a_model_that_refuses_a_reply_ceiling_names_its_own_and_is_sent_once_more(monkeypatch):
    from types import SimpleNamespace

    from app.schemas.llm import ChatMessage, GenerationOptions

    sent: list = []
    openai = OpenAIProvider(api_key="sk-test")
    monkeypatch.setattr(openai, "available", lambda: True)
    openai._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_refusing_client(
        "Error code: 400 - max_tokens is too large: 16384. This model supports at most 4096 completion tokens, "
        "whereas you provided 16384.", sent))))
    openai.default_model = "gpt-4-turbo"
    openai.generate([ChatMessage(role="user", content="hi")], "gpt-4-turbo", GenerationOptions())
    assert [k["max_tokens"] for k in sent] == [16_384, 4_096]
    assert openai.profile("gpt-4-turbo").max_output_tokens == 4_096, "learned for every later call"

    sent.clear()
    claude = AnthropicProvider(api_key="sk-ant-test")
    monkeypatch.setattr(claude, "available", lambda: True)
    claude._client = SimpleNamespace(messages=SimpleNamespace(create=_refusing_client(
        "Error code: 400 - max_tokens: 16384 > 8192, which is the maximum allowed number of output tokens "
        "for claude-3-5-haiku-20241022", sent)))
    claude.generate([ChatMessage(role="user", content="hi")], "claude-opus-4-8", GenerationOptions())
    assert [k["max_tokens"] for k in sent] == [16_384, 8_192]
    # An explicit timeout, so a newer SDK never refuses the unstreamed call itself.
    assert all(k["timeout"] >= 600 for k in sent)


def test_a_model_chosen_for_a_run_keeps_what_its_check_read(monkeypatch):
    from app.router import keycheck
    from app.router.router import ModelRouter

    model_router = ModelRouter()
    prov = model_router._cloud["anthropic"]
    prov.set_api_key("sk-ant-test-key-0000")
    monkeypatch.setattr(model_router, "key_check", lambda provider: keycheck.KeyCheck(
        status=keycheck.VALID, reason="ok", message="ok", model="claude-opus-4-8"))
    monkeypatch.setattr(keycheck, "check", lambda provider, key, model: keycheck.KeyCheck(
        status=keycheck.VALID, reason="ok", message="ok", model=model, context_tokens=200_000, output_tokens=8_192))
    model_router._check_chosen_model("anthropic", "claude-3-5-haiku")
    assert prov.profile("claude-3-5-haiku").max_output_tokens == 8_192


def test_the_default_chosen_in_settings_gets_the_providers_ceiling():
    from app.router.router import ModelRouter

    model_router = ModelRouter()
    model_router._apply("openai", None, "gpt-4.1")
    prov = model_router._cloud["openai"]
    assert prov.profile("gpt-4.1").max_output_tokens == settings.openai_max_output_tokens
    assert prov.profile("gpt-4o").max_output_tokens == settings.max_output_tokens
