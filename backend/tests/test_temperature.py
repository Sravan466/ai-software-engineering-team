"""#101: JSON and code are sampled low; thinking models keep their card's values.

An agent's reply is exact output — a schema to match, a file that has to build — and
sampling it at Ollama's default 0.8 is how a 7B model invents a package or drifts from
the shape. A thinking model is the exception its own card makes: decoded near greedy it
repeats itself, so it keeps the temperature its model file says, whatever an agent asks.
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
from tests.test_code_phases import SMALL, Scripted, _run
from tests.test_runtimes import FakeAdapter, _source, router_with  # noqa: F401


def test_agent_json_calls_and_code_write_calls_ask_for_the_agent_temperature(monkeypatch, stub_router):
    seen: list = []

    def complete(messages, **kwargs):
        seen.append(kwargs.get("options"))
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", complete)
    from app.agents.product_manager import ProductManagerAgent

    ProductManagerAgent().run(AgentContext(idea="A tool library"))
    assert seen and all(o.temperature == settings.agent_temperature == 0.2 for o in seen)

    model = Scripted("Backend Engineer", [{"path": "app/a.py", "purpose": "a"}])
    temps: list = []

    def scripted(messages, **kwargs):
        temps.append(kwargs["options"].temperature)
        return model(messages, **kwargs)

    stub(monkeypatch, "complete", scripted)
    stub(monkeypatch, "profile_for", lambda *a, **k: SMALL)
    from app.agents import get_agent

    get_agent("backend_engineer").run(AgentContext(idea="A tool library", prior_outputs={}))
    assert temps and set(temps) == {0.2}, "the plan call and every write call"


def test_the_agent_temperature_is_a_setting(monkeypatch, stub_router):
    monkeypatch.setattr(settings, "agent_temperature", 0.35)
    seen: list = []
    stub(monkeypatch, "complete", lambda messages, **kw: seen.append(kw["options"]) or _fake_complete(messages, **kw))
    from app.agents.system_design import SystemDesignAgent

    SystemDesignAgent().run(AgentContext(idea="x"))
    assert {o.temperature for o in seen} == {0.35}


class _Thinks(FakeAdapter):
    def __init__(self, models, defaults=None):
        super().__init__(models)
        self.defaults = defaults or {}

    def model_info(self, model):
        return ModelInfo(name=model, context_window=16384, context_source="reported", kind="chat",
                         thinking=THINKS_TOGGLE, defaults=dict(self.defaults))


def _ask(model_router, spec: str, temperature=0.2):
    model_router.complete([ChatMessage(role="user", content="hi")], preferred_model=spec, mode=RoutingMode.MANUAL,
                          options=GenerationOptions(temperature=temperature))


@pytest.mark.parametrize("defaults, sent", [({"temperature": 0.6, "top_k": 20, "top_p": 0.95}, 0.6), ({}, 0.7)])
def test_a_thinking_model_keeps_its_cards_temperature_not_the_agents(router_with, tmp_path, monkeypatch, defaults, sent):  # noqa: F811
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("ollama", [ModelEntry(name="thinker", kind="chat")])
    src.adapter = _Thinks(src.adapter.models, defaults)
    model_router = router_with(src)
    _ask(model_router, "ollama:thinker")
    assert src.adapter.calls[-1].temperature == sent
    # A call that names no temperature leaves the runtime (and the model file) to decide.
    _ask(model_router, "ollama:thinker", temperature=None)
    assert src.adapter.calls[-1].temperature is None
    # What a person saved for the model still wins — and never greedy.
    model_router.set_model_generation("ollama:thinker", {"sampling": {"temperature": 0.9}})
    _ask(model_router, "ollama:thinker")
    assert src.adapter.calls[-1].temperature == 0.9


def test_a_model_that_does_not_think_gets_the_agents_temperature(router_with, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(model_settings, "_PATH", tmp_path / "model_settings.local.json")
    src = _source("ollama", [ModelEntry(name="qwen2.5:7b", kind="chat")])
    model_router = router_with(src)
    _ask(model_router, "ollama:qwen2.5:7b")
    assert src.adapter.calls[-1].temperature == 0.2


class _Refusal(Exception):
    status_code = 400

    def __str__(self) -> str:
        return "Error code: 400 - Unsupported value: 'temperature' does not support 0.2 with this model. Only the default (1) value is supported."


def test_an_openai_model_that_takes_no_temperature_is_asked_again_without_and_remembered(monkeypatch):
    calls: list = []

    class Completions:
        def create(self, **kwargs):
            calls.append(dict(kwargs))
            if "temperature" in kwargs:
                raise _Refusal()
            usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
            choice = SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")
            return SimpleNamespace(choices=[choice], usage=usage)

    provider = openai_provider.OpenAIProvider(api_key="sk-test")
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(provider, "available", lambda: True)
    options = GenerationOptions(temperature=0.2, json_mode=True)
    for _ in range(2):
        resp = provider.generate([ChatMessage(role="user", content="hi")], "o-reasoner", options)
        assert resp.text == "{}"
    assert ["temperature" in c for c in calls] == [True, False, False], "refused once, then never sent"
    # A model that takes one keeps getting it.
    calls.clear()

    class Takes(Completions):
        def create(self, **kwargs):
            calls.append(dict(kwargs))
            return super().create(**{k: v for k, v in kwargs.items() if k != "temperature"})

    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=Takes()))
    provider.generate([ChatMessage(role="user", content="hi")], "gpt-4o", options)
    assert calls[0]["temperature"] == 0.2
