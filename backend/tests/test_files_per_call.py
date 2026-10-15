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
    assert claude.max_output_tokens == settings.anthropic_max_output_tokens == 32_000
    assert OpenAIProvider().profile("gpt-4o").max_output_tokens == 16_384
    assert GeminiProvider().profile("gemini-1.5-pro").max_output_tokens == 8_192
    # The bill guard is unchanged: a 200K window still budgets at most MAX_PROMPT_TOKENS.
    assert claude.prompt_token_budget == settings.max_prompt_tokens


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
