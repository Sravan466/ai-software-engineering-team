"""Concurrent builds on local models share one computer's RAM budget (#42).

Each model's window is clamped so its KV cache fits in `LOCAL_RAM_FRACTION` of RAM.
Builds on different projects generate at the same time, so the fraction has to cover
everything one computer generates at once — not each generation separately.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core.config import settings
from app.router import inflight, memory_gate
from app.router.base import RequestCancelled
from app.router.model_profile import kv_bytes
from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.provider import Source, SourceProvider
from app.router.runtimes.types import ChatResult, ModelEntry, ModelInfo
from app.schemas.llm import ChatMessage, GenerationOptions

GIB = 2**30
RAM = 16 * GIB
KV_PER_TOKEN = 128 * 1024  # a 7B-class f16 cache: 16 GiB * 0.6 holds ~78k tokens


class SlowRuntime(RuntimeAdapter):
    """A runtime whose generations last until the test lets them finish.

    Every model claims a 128k window, so RAM is what bounds it. While a call is
    generating, the KV cache its window asks for is counted in `live`.
    """

    runtime = "openai-compatible"

    def __init__(self, port: int, meter: "Meter") -> None:
        super().__init__(f"http://127.0.0.1:{port}")
        self.meter = meter

    @classmethod
    def fingerprint(cls, base_url, api_key=None, *, timeout=1.0):
        return None

    def list_models(self):
        return [ModelEntry(name=m, kind="chat") for m in ("small", "large")]

    def model_info(self, model):
        return ModelInfo(name=model, context_window=131072, context_source="reported",
                         kind="chat", kv_bytes_per_token=KV_PER_TOKEN, structured_output="schema")

    def chat(self, request):
        self.meter.enter(request.context_window * KV_PER_TOKEN)
        try:
            self.meter.release.wait(5)
        finally:
            self.meter.leave(request.context_window * KV_PER_TOKEN)
        return ChatResult(text="{}", prompt_tokens=5, completion_tokens=2, finish_reason="stop")


class Meter:
    """The KV cache being generated against right now, and the most it ever was."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = self.peak = self.calls = self.peak_calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def enter(self, kv: int) -> None:
        with self.lock:
            self.live += kv
            self.calls += 1
            self.peak = max(self.peak, self.live)
            self.peak_calls = max(self.peak_calls, self.calls)
        self.started.set()

    def leave(self, kv: int) -> None:
        with self.lock:
            self.live -= kv
            self.calls -= 1


@pytest.fixture
def machine(monkeypatch):
    """Two runtimes on this 16 GiB machine — Ollama-like and llama.cpp-like."""
    monkeypatch.setattr("app.router.runtimes.provider.total_ram_bytes", lambda: RAM)
    monkeypatch.setattr(settings, "local_ram_fraction", 0.6)
    monkeypatch.setattr(memory_gate, "_pools", {})
    meter = Meter()
    sources = [
        SourceProvider(Source(id=f"rt{port}", label=f"RT{port}", base_url=f"http://127.0.0.1:{port}",
                              runtime="openai-compatible", origin="detected"),
                       SlowRuntime(port, meter))
        for port in (11434, 8080)
    ]
    return meter, sources


def _build(project: str, provider: SourceProvider, model: str, errors: list) -> threading.Thread:
    def run() -> None:
        with inflight.building(project):
            try:
                provider.generate([ChatMessage(role="user", content="hi")], model, GenerationOptions())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _wait_for(predicate, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.mark.parametrize("slots", [1, 2])
def test_two_builds_on_different_local_models_stay_inside_the_ram_fraction(machine, monkeypatch, slots):
    meter, (ollama, llamacpp) = machine
    monkeypatch.setattr(settings, "local_concurrent_generations", slots)
    budget = RAM * settings.local_ram_fraction
    small, large = ollama.profile("small"), llamacpp.profile("large")
    # Each model on its own still gets the window its share of RAM holds.
    for profile in (small, large):
        assert profile.clamp_reason and "RAM" in profile.clamp_reason
        assert kv_bytes(profile, profile.context_window) <= budget / slots

    errors: list = []
    threads = [_build("p1", ollama, "small", errors), _build("p2", llamacpp, "large", errors)]
    _wait_for(meter.started.is_set)
    time.sleep(0.2)  # long enough for the second build to start if nothing held it back
    assert meter.peak_calls == min(slots, 2)
    meter.release.set()
    for t in threads:
        t.join(5)
    assert not errors
    assert meter.peak <= budget
    assert meter.peak_calls == min(slots, 2)


def test_a_window_does_not_change_with_what_else_is_running(machine, monkeypatch):
    meter, (ollama, _) = machine
    monkeypatch.setattr(settings, "local_concurrent_generations", 1)
    first = ollama.profile("small").context_window
    errors: list = []
    t = _build("p1", ollama, "small", errors)
    _wait_for(meter.started.is_set)
    ollama.forget_profile("small")
    assert ollama.profile("small").context_window == first
    meter.release.set()
    t.join(5)
    assert not errors


def test_zero_leaves_it_to_the_runtime(machine, monkeypatch):
    meter, (ollama, llamacpp) = machine
    monkeypatch.setattr(settings, "local_concurrent_generations", 0)
    errors: list = []
    threads = [_build("p1", ollama, "small", errors), _build("p2", llamacpp, "large", errors)]
    _wait_for(lambda: meter.peak_calls == 2)
    meter.release.set()
    for t in threads:
        t.join(5)
    # The documented trade: both run, each budgeted the whole fraction.
    assert meter.peak > RAM * settings.local_ram_fraction


def test_stop_reaches_a_build_still_waiting_for_its_turn(machine, monkeypatch):
    meter, (ollama, llamacpp) = machine
    monkeypatch.setattr(settings, "local_concurrent_generations", 1)
    errors: list = []
    running = _build("p1", ollama, "small", errors)
    _wait_for(meter.started.is_set)
    waiting_errors: list = []
    queued = _build("p2", llamacpp, "large", waiting_errors)
    _wait_for(lambda: memory_gate.waiting(memory_gate.THIS_MACHINE) == 1)

    assert inflight.cancel("p2") == 1
    queued.join(2)
    assert not queued.is_alive()
    assert len(waiting_errors) == 1 and isinstance(waiting_errors[0], RequestCancelled)
    assert memory_gate.waiting(memory_gate.THIS_MACHINE) == 0
    assert memory_gate.running(memory_gate.THIS_MACHINE) == 1  # the other build is untouched

    meter.release.set()
    running.join(5)
    assert not errors
    assert memory_gate.running(memory_gate.THIS_MACHINE) == 0


def test_builds_take_turns_in_the_order_they_asked(machine, monkeypatch):
    meter, (ollama, llamacpp) = machine
    monkeypatch.setattr(settings, "local_concurrent_generations", 1)
    order: list[str] = []
    real_chat = SlowRuntime.chat

    def chat(self, request):
        order.append(request.model)
        return real_chat(self, request)

    monkeypatch.setattr(SlowRuntime, "chat", chat)
    errors: list = []
    threads = [_build("p0", ollama, "small", errors)]
    _wait_for(meter.started.is_set)
    for i, model in enumerate(["large", "small", "large"], start=1):
        threads.append(_build(f"p{i}", llamacpp if model == "large" else ollama, model, errors))
        _wait_for(lambda i=i: memory_gate.waiting(memory_gate.THIS_MACHINE) == i)
    meter.release.set()
    for t in threads:
        t.join(5)
    assert not errors
    assert order == ["small", "large", "small", "large"]


def test_only_memory_this_backend_budgeted_is_queued(machine):
    _, (ollama, _) = machine
    assert ollama.memory_pool() == memory_gate.THIS_MACHINE
    elsewhere = SourceProvider(
        Source(id="lan", label="LAN", base_url="http://192.168.1.20:11434",
               runtime="openai-compatible", origin="configured"),
        SlowRuntime(1, Meter()),
    )
    # Another host's RAM is unknown, its window isn't clamped, so it never queues here.
    assert elsewhere.memory_pool() is None
