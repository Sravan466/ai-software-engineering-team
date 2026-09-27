"""Connector, part 2: builds that run on the user's own computer.

The model call over the connector (`chat`, `embed`, `cancel`), the limits the
computer enforces, a build that pauses when the computer goes and resumes when it
comes back, and Stop reaching a call that is still generating.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import pytest

from app.connector import pairing, protocol as P
from app.core import identity
from app.db.base import SessionLocal
from app.db.models import Device, PairingCode, Project
from app.main import app
from app.router import inflight
from app.router.base import ComputerDisconnected, ProviderError, RequestCancelled
from app.router.runtimes.base import RuntimeAdapter
from app.router.runtimes.types import ChatResult, ModelEntry
from app.schemas.llm import ChatMessage
from aiteam_connect import limits as L
from aiteam_connect import store
from aiteam_connect.local import Agent, Cancelled, Paused, Refused, Source
from tests.conftest import TEST_USER_ID, stub


# ── a runtime that answers on cue ────────────────────────────────────────────
class FakeRuntime(RuntimeAdapter):
    runtime = "ollama"
    sampling_supported = frozenset({"temperature", "top_p"})

    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:1")
        self.requests: list = []
        self.block = False
        self.started = threading.Event()
        self.stopped = threading.Event()

    @classmethod
    def fingerprint(cls, base_url, api_key=None, *, timeout=0.0):
        return None

    def list_models(self):
        return [ModelEntry(name="m", kind="chat")]

    def model_info(self, model):
        return None

    def chat(self, request):
        self.requests.append(request)
        self.started.set()
        if self.block:
            if not self.stopped.wait(20):
                raise AssertionError("never cancelled")
            raise ProviderError("The model server closed the connection.")
        return ChatResult(text='{"ok": true}', reasoning="let me think", prompt_tokens=7,
                          completion_tokens=3, finish_reason="stop", structured_output="json")

    def embed(self, model, inputs, *, request_id=None):
        return [[float(len(t)), 1.0] for t in inputs]

    def cancel(self, request_id):
        self.stopped.set()
        return True


def _agent(runtime: FakeRuntime) -> Agent:
    agent = Agent("0" * 32)
    source = Source("ollama", "ollama", "http://127.0.0.1:1", runtime, remote=False, version=None)
    source.models = ["m"]
    agent._sources = {"ollama": source}
    agent.scan = lambda: ([], [], [])  # never probe this machine from a test
    return agent


def _chat_args(**extra) -> dict:
    args = {"source": "ollama", "model": "m", "messages": [{"role": "user", "content": "the secret prompt"}],
            "max_tokens": 100, "context_window": 4096}
    args.update(extra)
    return args


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITEAM_CONNECT_HOME", str(tmp_path / "connect"))
    monkeypatch.setenv("AITEAM_CONNECT_NO_KEYRING", "1")
    pairing.limits.reset()
    yield
    with SessionLocal() as db:
        db.query(PairingCode).delete()
        db.query(Device).delete()
        db.commit()


def _set_state(**changes) -> None:
    state = store.load_state()
    state.update(changes)
    store.save_state(state)


# ── the protocol ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", ["gpu_layers", "num_gpu", "threads", "keep_alive", "num_thread"])
def test_a_machine_setting_from_the_server_is_refused_not_applied(key):
    agent = _agent(FakeRuntime())
    with pytest.raises(Refused) as caught:
        agent.handle("chat", _chat_args(**{key: 8}), "a" * 16)
    assert "set here" in str(caught.value)
    assert agent._sources["ollama"].adapter.requests == []


def test_chat_keeps_the_reasoning_apart_and_clamps_output_to_the_local_limit():
    runtime = FakeRuntime()
    _set_state(limits={"max_output_tokens": 50}, machine={"gpu_layers": 12, "threads": 4})
    report = _agent(runtime).handle("chat", _chat_args(max_tokens=10_000, temperature=0.3), "a" * 16)
    assert P.ChatReport.model_validate(report).text == '{"ok": true}'
    assert report["reasoning"] == "let me think"
    sent = runtime.requests[0]
    assert sent.max_tokens == 50  # clamped to what this computer allows
    assert (sent.gpu_layers, sent.threads) == (12, 4)  # this computer's own settings
    assert sent.temperature == 0.3 and sent.request_id == "a" * 16


def test_over_a_limit_is_refused_with_how_to_change_it():
    agent = _agent(FakeRuntime())
    _set_state(limits={"max_prompt_chars": 1000})
    with pytest.raises(L.LimitRefused) as caught:
        agent.handle("chat", _chat_args(messages=[{"role": "user", "content": "x" * 2000}]), "a" * 16)
    assert caught.value.code == P.ERR_LIMIT and "--max-prompt-chars" in str(caught.value)

    _set_state(limits={"requests_per_minute": 1})
    agent.handle("chat", _chat_args(), "b" * 16)
    with pytest.raises(L.LimitRefused) as caught:
        agent.handle("chat", _chat_args(), "c" * 16)
    assert "a minute" in str(caught.value)


def test_paused_on_this_computer_refuses_model_calls():
    _set_state(paused=True)
    with pytest.raises(Paused) as caught:
        _agent(FakeRuntime()).handle("chat", _chat_args(), "a" * 16)
    assert caught.value.code == P.ERR_PAUSED


def test_cancel_passes_through_to_the_runtime():
    runtime = FakeRuntime()
    runtime.block = True
    agent = _agent(runtime)
    outcome: dict = {}

    def call():
        try:
            agent.handle("chat", _chat_args(), "d" * 16)
        except Exception as e:  # noqa: BLE001
            outcome["error"] = e

    worker = threading.Thread(target=call)
    worker.start()
    assert runtime.started.wait(5)
    assert agent.handle("cancel", {"id": "d" * 16}) == {"cancelled": True}
    worker.join(5)
    assert isinstance(outcome.get("error"), Cancelled)
    assert runtime.stopped.is_set()


def test_the_activity_log_has_counts_and_never_contents():
    _agent(FakeRuntime()).handle("chat", _chat_args(), "a" * 16)
    log = (store.home() / "activity.log").read_text(encoding="utf-8")
    assert "model=m" in log and "prompt_tokens=7" in log
    assert "secret prompt" not in log and "let me think" not in log


def test_embed_returns_one_vector_per_input():
    report = _agent(FakeRuntime()).handle("embed", {"source": "ollama", "model": "m", "inputs": ["ab", "c"]}, "e" * 16)
    assert report["vectors"] == [[2.0, 1.0], [1.0, 1.0]]


# ── the whole path, over a real socket ────────────────────────────────────────
class _Live:
    """A real server, a paired and approved computer, and its connector running."""

    def __init__(self, monkeypatch, runtime: FakeRuntime) -> None:
        import httpx
        import uvicorn

        from aiteam_connect import client as C
        from tests.conftest import TEST_EMAIL, TEST_PASSWORD

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)
        base = f"http://127.0.0.1:{self.port}"
        self.web = httpx.Client(base_url=base, timeout=30)
        assert self.web.post("/api/auth/signin", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}).status_code == 200
        code = self.web.post("/api/devices/pairing").json()["code"]
        self.target = C.server_for(base)
        answers = iter([code, "y"])
        C.pair(self.target, lambda _p: next(answers), lambda _t: None)
        self.device_id, self.key, _ = C.credential(self.target)

        hello = {
            "device_id": self.device_id, "connector_version": P.CONNECTOR_VERSION, "protocol": P.PROTOCOL,
            "os": "macOS", "ram_bytes": 16 * 2**30,
            "sources": [{"id": "ollama", "runtime": "ollama", "label": "Ollama",
                         "base_url": "http://127.0.0.1:11434", "models": [{"name": "m", "kind": "chat"}]}],
            "capabilities": list(P.OPS),
        }
        monkeypatch.setattr(Agent, "hello", lambda self: hello)
        self.agent = _agent(runtime)
        self.agent.device_id = self.device_id
        self.said: list[str] = []
        self.agent._say = self.said.append
        self.C = C
        self.start()
        self.web.post(f"/api/devices/{self.device_id}/approve")
        assert self.web.patch(f"/api/devices/{self.device_id}", json={"chat_model": "ollama:m"}).status_code == 200
        self.wait_online()

    def start(self) -> None:
        self.result: dict = {}
        self.session = threading.Thread(
            target=lambda: self.result.update(code=self.C.session(self.target, self.device_id, self.key, self.agent,
                                                                  self.said.append)),
            daemon=True,
        )
        self.session.start()

    def wait_online(self) -> None:
        from app.connector.hub import hub

        for _ in range(200):
            link = hub.live(self.device_id)
            if link is not None and link.approved:
                return
            time.sleep(0.02)
        raise AssertionError("the computer never came online")

    def close(self) -> None:
        self.web.post(f"/api/devices/{self.device_id}/disconnect")
        self.session.join(10)
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def live(monkeypatch):
    runtime = FakeRuntime()
    env = _Live(monkeypatch, runtime)
    env.runtime = runtime
    try:
        yield env
    finally:
        env.close()


def _router():
    from app.router.router import ModelRouter

    return ModelRouter(TEST_USER_ID)


def test_a_build_call_runs_on_the_computers_model(live):
    router = _router()
    with identity.acting_as(TEST_USER_ID):
        assert router.local_default() == (f"pc-{live.device_id[:8]}-ollama", "m")
        resp = router.complete([ChatMessage(role="user", content="hello")])
    assert resp.text == '{"ok": true}'
    # The answer and the reasoning arrive apart, so JSON is parsed from the answer.
    assert resp.reasoning == "let me think"
    assert resp.is_local is True and resp.usage.completion_tokens == 3
    # Sized with the computer's RAM, not this server's.
    prov = router.source(f"pc-{live.device_id[:8]}-ollama")
    assert prov.ram_bytes() == 16 * 2**30


def test_the_setup_test_prompt_is_a_real_round_trip(live):
    body = live.web.post(f"/api/devices/{live.device_id}/test", json={}).json()
    assert body["ok"] is True and body["answer"] == '{"ok": true}' and body["seconds"] >= 0
    assert live.runtime.requests[-1].messages[0]["content"].startswith("Reply with one short sentence")


def test_a_limit_refusal_reaches_the_website_with_its_message(live):
    _set_state(limits={"max_prompt_chars": 1000})
    router = _router()
    with identity.acting_as(TEST_USER_ID), pytest.raises(ProviderError) as caught:
        router.complete([ChatMessage(role="user", content="x" * 5000)])
    assert "refused" in str(caught.value) and "max-prompt-chars" in str(caught.value)
    assert not caught.value.retryable


def test_stop_interrupts_a_call_in_flight_and_the_runtime_stops(live):
    live.runtime.block = True
    router = _router()
    outcome: dict = {}

    def call():
        with identity.acting_as(TEST_USER_ID), inflight.building("p-stop", "Stop test"):
            try:
                router.complete([ChatMessage(role="user", content="long job")])
            except Exception as e:  # noqa: BLE001
                outcome["error"] = e

    worker = threading.Thread(target=call)
    worker.start()
    assert live.runtime.started.wait(10)
    started = time.monotonic()
    assert inflight.cancel("p-stop") == 1
    worker.join(10)
    assert isinstance(outcome.get("error"), RequestCancelled)
    assert time.monotonic() - started < 5
    assert live.runtime.stopped.wait(5)  # the cancel reached the runtime
    # The terminal said what it was doing, and that the website stopped it.
    assert any("Answering" in s for s in live.said), live.said
    for _ in range(100):
        if any("stopped by the website" in s for s in live.said):
            break
        time.sleep(0.02)
    assert any("stopped by the website" in s for s in live.said), live.said


def test_a_computer_that_disconnects_mid_call_pauses_the_build(live, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "connector_grace_seconds", 1)
    live.runtime.block = True
    router = _router()
    outcome: dict = {}

    def call():
        with identity.acting_as(TEST_USER_ID):
            try:
                router.complete([ChatMessage(role="user", content="long job")])
            except Exception as e:  # noqa: BLE001
                outcome["error"] = e

    worker = threading.Thread(target=call)
    worker.start()
    assert live.runtime.started.wait(10)
    live.web.post(f"/api/devices/{live.device_id}/disconnect")
    worker.join(15)
    error = outcome.get("error")
    assert isinstance(error, ComputerDisconnected), error
    assert error.device_id == live.device_id
    # The connector stopped generating an answer nobody would read.
    assert live.runtime.stopped.wait(5)


# ── the runner: pause, then resume on reconnect ──────────────────────────────
def test_a_disconnect_pauses_the_build_and_reconnecting_resumes_it(client, monkeypatch):
    from app.api.routes import projects as routes
    from tests.conftest import _fake_complete

    device = "f" * 32
    calls = {"n": 0}

    def flaky(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ComputerDisconnected("laptop disconnected.", device_id=device, device_name="laptop")
        return _fake_complete(messages, **kwargs)

    stub(monkeypatch, "complete", flaky)
    pid = client.post("/api/projects", json={"idea": "A todo app for teams", "routing_mode": "local_only"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    paused = client.get(f"/api/projects/{pid}").json()
    assert paused["status"] == "paused"
    assert "laptop disconnected" in paused["last_error"] and "paused" in paused["last_error"]
    with SessionLocal() as db:
        assert db.get(Project, pid).paused_device_id == device

    # Another computer coming back doesn't touch it.
    routes._resume_paused(TEST_USER_ID, "e" * 32)
    assert client.get(f"/api/projects/{pid}").json()["status"] == "paused"

    routes._resume_paused(TEST_USER_ID, device)
    for _ in range(200):
        state = client.get(f"/api/projects/{pid}").json()
        if state["status"] not in ("running", "paused"):
            break
        time.sleep(0.05)
    # Carried on to the first review, and the phase that paused ran once, not twice.
    assert state["status"] == "awaiting_approval", state
    assert [p["phase"] for p in state["phases"]].count("product_manager") == 1
    assert state["last_error"] is None


def test_a_paused_build_can_be_stopped_or_resumed_by_hand(client, monkeypatch):
    def gone(messages, **kwargs):
        raise ComputerDisconnected("laptop disconnected.", device_id="a" * 32)

    stub(monkeypatch, "complete", gone)
    pid = client.post("/api/projects", json={"idea": "A todo app for teams", "routing_mode": "local_only"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    assert client.get(f"/api/projects/{pid}").json()["status"] == "paused"
    assert client.post(f"/api/projects/{pid}/stop", json={}).json()["status"] == "cancelled"
    assert client.post(f"/api/projects/{pid}/resume").json()["status"] == "running"
    assert client.get(f"/api/projects/{pid}").json()["status"] == "paused"


def test_the_migration_adds_the_paused_column_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY, idea TEXT, status VARCHAR(32))"))
        conn.execute(text("INSERT INTO projects (id, idea, status) VALUES ('p1', 'x', 'failed')"))
    applied = run_migrations(engine)
    assert "projects.paused_device_id" in applied
    assert "paused_device_id" in {c["name"] for c in inspect(engine).get_columns("projects")}
    assert run_migrations(engine) == []  # idempotent
    with engine.begin() as conn:
        assert conn.execute(text("SELECT status FROM projects")).scalar() == "failed"


# ── regressions from review ──────────────────────────────────────────────────
def test_a_terminal_that_cannot_print_never_leaks_the_only_slot():
    runtime = FakeRuntime()
    agent = _agent(runtime)

    def broken(_text):
        raise UnicodeEncodeError("cp1252", "…", 0, 1, "can't encode")

    agent._say = broken
    for rid in ("a" * 16, "b" * 16):  # the second would wait for ever on a leaked slot
        agent.handle("chat", _chat_args(), rid)
    assert agent.gate.running == 0 and agent._running == {}


def test_the_context_window_is_held_to_this_computers_limit_and_the_model():
    from app.router.runtimes.types import ModelInfo

    runtime = FakeRuntime()
    runtime.model_info = lambda model: ModelInfo(name=model, context_window=16_384)
    _set_state(limits={"max_context_tokens": 8192})
    _agent(runtime).handle("chat", _chat_args(context_window=9_000_000), "a" * 16)
    assert runtime.requests[-1].context_window == 8192
    _set_state(limits={"max_context_tokens": 65_536})
    _agent(runtime).handle("chat", _chat_args(context_window=9_000_000), "b" * 16)
    assert runtime.requests[-1].context_window == 16_384


def test_a_cancel_for_a_queued_call_stops_it_before_the_runtime_is_asked():
    runtime = FakeRuntime()
    runtime.block = True
    agent = _agent(runtime)
    outcomes: dict = {}

    def call(rid):
        try:
            agent.handle("chat", _chat_args(), rid)
        except Exception as e:  # noqa: BLE001
            outcomes[rid] = e

    first = threading.Thread(target=call, args=("a" * 16,))
    first.start()
    assert runtime.started.wait(5)
    second = threading.Thread(target=call, args=("b" * 16,))
    second.start()  # waits for the one slot
    time.sleep(0.2)
    agent.handle("cancel", {"id": "b" * 16})
    second.join(5)
    assert isinstance(outcomes.get("b" * 16), Cancelled)
    assert len(runtime.requests) == 1  # the queued one never reached the runtime
    agent.handle("cancel", {"id": "a" * 16})
    first.join(5)


def test_a_cancel_that_beats_the_send_is_still_honoured():
    import asyncio

    from app.connector.hub import ConnectorError, Link

    class _Sock:
        async def send_text(self, text):
            pass

    async def scenario():
        # Made inside the loop: on Python 3.9 its lock binds to the running one.
        link = Link(_Sock(), device_id="0" * 32, owner_id="u", public_key="k", approved=True)
        assert await link.cancel_request("c" * 16) is True  # nothing in flight yet
        with pytest.raises(ConnectorError) as caught:
            await link.request("chat", {}, request_id="c" * 16)
        return caught.value.code

    assert asyncio.run(scenario()) == P.ERR_CANCELLED


def test_a_connected_computer_whose_runtime_is_down_is_an_error_not_a_pause(monkeypatch):
    from app.connector.remote import ConnectorProvider, DeviceView

    device = DeviceView(id="d" * 32, name="laptop", hello={}, chat_model=None, embed_model=None)
    prov = ConnectorProvider(device, {"id": "ollama", "runtime": "ollama", "reachable": False,
                                      "error": "connection refused", "models": []})
    monkeypatch.setattr(ConnectorProvider, "connected", lambda self: True)
    err = prov.unavailable_error()
    assert not isinstance(err, ComputerDisconnected) and "isn't answering" in str(err)
    monkeypatch.setattr(ConnectorProvider, "connected", lambda self: False)
    monkeypatch.setattr(ConnectorProvider, "in_grace", lambda self: False)
    assert isinstance(prov.unavailable_error(), ComputerDisconnected)


def test_memory_and_search_never_swallow_a_pause():
    from unittest.mock import patch

    from app.rag.embeddings import SourceEmbeddingFunction
    from app.router.router import ModelRouter

    def gone(self, inputs):
        raise ComputerDisconnected("laptop disconnected.", device_id="d" * 32)

    with patch.object(ModelRouter, "embed", gone), identity.acting_as(TEST_USER_ID):
        with pytest.raises(ComputerDisconnected):
            SourceEmbeddingFunction()(["x"])


def test_an_out_of_range_saved_limit_is_held_to_its_bound_not_fatal():
    limits = L.read({"limits": {"max_context_tokens": 100, "timeout_seconds": 10**9}})
    assert limits["max_context_tokens"] == 256 and limits["timeout_seconds"] == 24 * 3600


def test_model_info_reports_the_window_this_computer_will_run():
    from app.router.runtimes.types import ModelInfo

    runtime = FakeRuntime()
    runtime.model_info = lambda model: ModelInfo(name=model, context_window=131_072)
    _set_state(limits={"max_context_tokens": 32_768})
    report = _agent(runtime).handle("model_info", {"source": "ollama", "model": "m"})
    assert report["context_window"] == 32_768


def test_a_cancelled_request_returns_at_once_even_if_the_socket_never_answers():
    """Closing the client stops the runtime, but doesn't wake a thread blocked on the
    socket (macOS): the caller must leave on the cancel, not on the read timeout."""
    import httpx

    from app.router.runtimes.openai_compat import OpenAICompatAdapter

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held: list = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
    adapter = OpenAICompatAdapter(f"http://127.0.0.1:{listener.getsockname()[1]}")
    outcome: dict = {}

    def call():
        try:
            adapter._post_cancellable("/v1/chat/completions", {}, timeout=60, request_id="r" * 16)
        except httpx.HTTPError as e:
            outcome["error"] = e

    worker = threading.Thread(target=call)
    worker.start()
    time.sleep(0.5)
    started = time.monotonic()
    assert adapter.cancel("r" * 16) is True
    worker.join(5)
    assert not worker.is_alive() and "cancelled" in str(outcome.get("error"))
    assert time.monotonic() - started < 2
    listener.close()
