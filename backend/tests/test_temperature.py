"""#101: JSON and code are sampled low on a local model; thinking models keep their
card's values; cloud models keep their provider's defaults.

An agent's reply is exact output — a schema to match, a file that has to build — and
sampling it at Ollama's default 0.8 is how a 7B model invents a package or drifts from
the shape. Agents say so (`exact`), and the runtime decides what that means: a local
model samples at `AGENT_TEMPERATURE`, a thinking model at its own card's values (decoded
near greedy it repeats itself), and a cloud model at its provider's tuned default —
several reject a temperature, or loop below it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.base import AgentContext
from app.core import model_settings
from app.core.config import settings
from app.core.constants import RoutingMode
from app.router.providers import openai_provider
from app.router.runtimes.types import THINKS_TOGGLE, ModelEntry, ModelInfo
from app.schemas.llm import ChatMessage, GenerationOptions
from tests.conftest import _fake_complete, stub
from tests.test_code_phases import SMALL, Scripted
from tests.test_runtimes import FakeAdapter, _source
from tests.test_runtimes import router_with  # noqa: F401 - the fixture is used by name


def test_agent_json_calls_code_write_calls_and_the_debate_ask_for_exact_output(monkeypatch, stub_router):
    seen: list = []

    def complete(messages, **kwargs):
        seen.append(kwargs.get("options"))
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", complete)
    from app.agents.product_manager import ProductManagerAgent

    ProductManagerAgent().run(AgentContext(idea="A tool library"))
    assert seen and all(o.exact and o.temperature is None for o in seen)

    model = Scripted("Backend Engineer", [{"path": "app/a.py", "purpose": "a"}])
    flags: list = []

    def scripted(messages, **kwargs):
        flags.append(kwargs["options"].exact)
        return model(messages, **kwargs)

    stub(monkeypatch, "complete", scripted)
    stub(monkeypatch, "profile_for", lambda *a, **k: SMALL)
    from app.agents import get_agent

    get_agent("backend_engineer").run(AgentContext(idea="A tool library", prior_outputs={}))
    assert flags and set(flags) == {True}, "the plan call and every write call"

    seen.clear()
    stub(monkeypatch, "complete", complete)
    from app.orchestration import debate

    debate.conduct_debate(debate.TOPIC, "A tool library", mode=RoutingMode.LOCAL_ONLY, preferred_model=None)
    assert len(seen) == 1 and seen[0].exact and seen[0].json_mode


class _Thinks(FakeAdapter):
    def __init__(self, models, defaults=None):
        super().__init__(models)
        self.defaults = defaults or {}

    def model_info(self, model):
        return ModelInfo(name=model, context_window=16384, context_source="reported", kind="chat",
                         thinking=THINKS_TOGGLE, defaults=dict(self.defaults))


def _ask(model_router, spec: str, temperature=None, exact=True):
    model_router.complete([ChatMessage(role="user", content="hi")], preferred_model=spec, mode=RoutingMode.MANUAL,
                          options=GenerationOptions(temperature=temperature, exact=exact))


def test_a_local_model_samples_exact_output_at_the_agent_temperature(router_with, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("ollama", [ModelEntry(name="qwen2.5:7b", kind="chat")])
    model_router = router_with(src)
    _ask(model_router, "ollama:qwen2.5:7b")
    assert src.adapter.calls[-1].temperature == settings.agent_temperature == 0.2
    monkeypatch.setattr(settings, "agent_temperature", 0.35)
    _ask(model_router, "ollama:qwen2.5:7b")
    assert src.adapter.calls[-1].temperature == 0.35
    # Not exact output — prose — is left to the runtime's own default.
    _ask(model_router, "ollama:qwen2.5:7b", exact=False)
    assert src.adapter.calls[-1].temperature is None


@pytest.mark.parametrize("defaults, sent", [({"temperature": 0.6, "top_k": 20, "top_p": 0.95}, 0.6), ({}, 0.7)])
def test_a_thinking_model_keeps_its_cards_temperature(router_with, tmp_path, monkeypatch, defaults, sent):  # noqa: F811
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("ollama", [ModelEntry(name="thinker", kind="chat")])
    src.adapter = _Thinks(src.adapter.models, defaults)
    model_router = router_with(src)
    _ask(model_router, "ollama:thinker")
    assert src.adapter.calls[-1].temperature == sent
    # A caller that names its own temperature — the mockup's 0.3 — still gets it.
    _ask(model_router, "ollama:thinker", temperature=0.3)
    assert src.adapter.calls[-1].temperature == 0.3
    # What a person saved for the model wins over both.
    model_router.set_model_generation("ollama:thinker", {"sampling": {"temperature": 0.9}})
    _ask(model_router, "ollama:thinker")
    assert src.adapter.calls[-1].temperature == 0.9


def test_a_cloud_model_keeps_its_providers_default(monkeypatch):
    sent: list = []

    class Completions:
        def create(self, **kwargs):
            sent.append(kwargs)
            usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
            choice = SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")
            return SimpleNamespace(choices=[choice], usage=usage)

    provider = openai_provider.OpenAIProvider(api_key="sk-test")
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(provider, "available", lambda: True)
    provider.generate([ChatMessage(role="user", content="hi")], "gpt-4o", GenerationOptions(json_mode=True, exact=True))
    assert "temperature" not in sent[-1], "an agent's call never sends a cloud model a temperature"
    provider.generate([ChatMessage(role="user", content="hi")], "gpt-4o", GenerationOptions(temperature=0.3))
    assert sent[-1]["temperature"] == 0.3, "a caller's own temperature still does"


def test_what_exact_output_is_sampled_at_by_model():
    from app.router.model_profile import ModelProfile
    from app.router.runtimes.provider import exact_temperature

    def profile(**kw):
        return ModelProfile(provider="ollama", model="m", context_limit=8192, context_window=8192, max_output_tokens=2048, **kw)

    assert exact_temperature(profile()) == 0.2
    # A model the runtime only lists and sends elsewhere to run keeps its host's default.
    assert exact_temperature(profile(is_local=False)) is None
    # A thinking model's card, read whatever form the runtime reported it in.
    assert exact_temperature(profile(thinking=THINKS_TOGGLE, defaults={"temperature": "0.6"})) == 0.6
    assert exact_temperature(profile(thinking=THINKS_TOGGLE, defaults={"temperature": True})) == settings.local_temperature


def test_settings_says_what_agents_send(router_with, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("ollama", [ModelEntry(name="qwen2.5:7b", kind="chat")])
    router_with(src)
    assert src.generation_view("qwen2.5:7b")["agent_sends"] == {"temperature": 0.2}
