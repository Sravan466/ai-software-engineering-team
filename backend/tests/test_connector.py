"""The connector: pairing, the device credential, the socket, and what a connector
will refuse whatever the server asks."""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.connector import pairing, protocol as P
from app.db.base import SessionLocal
from app.db.models import Device, PairingCode, User
from app.core import auth as _auth
from app.main import app
from aiteam_connect import store
from aiteam_connect.client import ConnectError, server_for
from aiteam_connect.local import Agent, Refused
from tests.conftest import TEST_USER_ID

HOST = "testserver"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    pairing.limits.reset()
    monkeypatch.setenv("AITEAM_CONNECT_HOME", str(tmp_path / "connect"))
    monkeypatch.setenv("AITEAM_CONNECT_NO_KEYRING", "1")
    yield
    with SessionLocal() as db:
        db.query(PairingCode).delete()
        db.query(Device).delete()
        db.commit()


def _code(client: TestClient) -> tuple[str, str]:
    r = client.post("/api/devices/pairing")
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(P.normalise_code(body["code"])) == 8
    return body["id"], body["code"]


def _claim(client: TestClient, code: str, key=None):
    key = key or store.new_key()
    r = client.post(
        "/api/connector/pair/claim",
        json={"code": code, "public_key": store.public_text(key), "name": "laptop", "os": "macOS",
              "connector_version": P.CONNECTOR_VERSION},
    )
    return r, key


def _headers(device_id: str, key, *, host: str = HOST, ts=None, nonce=None) -> dict:
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or secrets.token_hex(16)
    sig = store.sign(key, P.handshake_message(device_id, ts, nonce, host))
    return {"authorization": P.auth_header(device_id, ts, nonce, sig), "x-connector-version": P.CONNECTOR_VERSION}


def _paired(client):
    pairing_id, code = _code(client)
    r, key = _claim(client, code)
    assert r.status_code == 201, r.text
    return pairing_id, r.json()["device_id"], key


# ── codes ────────────────────────────────────────────────────────────────────
def test_codes_use_the_rfc8628_alphabet_and_need_a_session(client):
    _, code = _code(client)
    assert all(c in P.CODE_ALPHABET for c in P.normalise_code(code))
    with TestClient(app) as anon:
        assert anon.post("/api/devices/pairing").status_code == 401


def test_lookup_names_the_account_and_a_code_is_single_use(client):
    _, code = _code(client)
    r = client.post("/api/connector/pair/lookup", json={"code": code.lower()})
    assert r.status_code == 200 and r.json()["account"] == "tester@example.com"
    first, _ = _claim(client, code)
    assert first.status_code == 201
    again, _ = _claim(client, code)
    assert again.status_code == 400


def test_an_expired_code_is_refused(client):
    pairing_id, code = _code(client)
    with SessionLocal() as db:
        db.get(PairingCode, pairing_id).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    r, _ = _claim(client, code)
    assert r.status_code == 400
    assert client.get(f"/api/devices/pairing/{pairing_id}").json()["state"] == "expired"


def test_a_code_burns_after_its_attempts(client):
    _, code = _code(client)
    for _ in range(P.CODE_MAX_ATTEMPTS):
        assert client.post("/api/connector/pair/lookup", json={"code": code}).status_code == 200
    r, _ = _claim(client, code)
    assert r.status_code == 400


def test_attempts_are_limited_per_address(client):
    for _ in range(pairing.IP_LIMIT):
        client.post("/api/connector/pair/lookup", json={"code": "BBBBBBBB"})
    assert client.post("/api/connector/pair/lookup", json={"code": "BBBBBBBB"}).status_code == 429


# ── the socket ────────────────────────────────────────────────────────────────
def _answer(ws, agent_result=None):
    """Answer the next request the server sends; returns the op it asked for."""
    msg = ws.receive_json()
    assert msg["type"] == "request"
    ws.send_text(json.dumps({"type": "response", "id": msg["id"], "ok": True, "result": agent_result(msg)}))
    return msg["op"]


def _hello(device_id):
    return {
        "device_id": device_id, "connector_version": P.CONNECTOR_VERSION, "protocol": P.PROTOCOL,
        "os": "macOS", "ram_bytes": 16 * 2**30, "capabilities": list(P.OPS),
        "sources": [{"id": "ollama", "runtime": "ollama", "label": "Ollama", "base_url": "http://127.0.0.1:11434",
                     "models": [{"name": "qwen2.5:7b", "kind": "chat"}, {"name": "nomic-embed-text", "kind": "embedding"},
                                {"name": "gpt-oss:120b-cloud", "kind": "chat", "is_local": False}]}],
    }


def test_a_bad_signature_or_a_replayed_nonce_never_opens_a_socket(client):
    _, device_id, key = _paired(client)
    stranger = store.new_key()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, stranger)):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key, host="other.example")):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key, ts=int(time.time()) - 600)):
            pass
    nonce = secrets.token_hex(16)
    with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key, nonce=nonce)) as ws:
        assert ws.receive_json()["state"] == "pending"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key, nonce=nonce)):
            pass


def test_nothing_is_sent_until_approved_then_hello_is_stored(client):
    pairing_id, device_id, key = _paired(client)
    state = client.get(f"/api/devices/pairing/{pairing_id}").json()
    assert state["state"] == "claimed" and state["device"]["status"] == "pending"
    with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key)) as ws:
        assert ws.receive_json() == {"type": "state", "state": "pending", "account": "tester@example.com"}
        # A model query to a pending computer is refused by the server itself.
        assert client.get(f"/api/devices/{device_id}/model", params={"spec": "ollama:x"}).status_code == 404

        result = {}
        t = threading.Thread(target=lambda: result.update(r=client.post(f"/api/devices/{device_id}/approve")))
        t.start()
        assert ws.receive_json()["state"] == "approved"
        assert _answer(ws, lambda m: _hello(device_id)) == "hello"
        t.join(10)
        body = result["r"].json()
        assert body["status"] == "approved" and body["online"] is True
        assert body["hello"]["sources"][0]["models"][0]["name"] == "qwen2.5:7b"
        assert body["advice"]["quantization"]

        # Choices are checked against what the computer reported.
        ok = client.patch(f"/api/devices/{device_id}", json={"chat_model": "ollama:qwen2.5:7b",
                                                            "embed_model": "ollama:nomic-embed-text"})
        assert ok.status_code == 200 and ok.json()["chat_model"] == "ollama:qwen2.5:7b"
        assert client.patch(f"/api/devices/{device_id}", json={"chat_model": "ollama:nomic-embed-text"}).status_code == 422
        assert client.patch(f"/api/devices/{device_id}", json={"chat_model": "ollama:gpt-oss:120b-cloud"}).status_code == 422
        assert client.patch(f"/api/devices/{device_id}", json={"chat_model": "ollama:nope"}).status_code == 422


def test_forget_closes_the_live_connection_and_revokes_the_key(client):
    _, device_id, key = _paired(client)
    with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key)) as ws:
        ws.receive_json()
        assert client.delete(f"/api/devices/{device_id}").status_code == 200
        bye = ws.receive_json()
        assert bye["type"] == "bye" and bye["code"] == P.CLOSE_FORGOTTEN
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == P.CLOSE_FORGOTTEN
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key)):
            pass


def test_a_message_off_the_schema_closes_the_socket(client):
    _, device_id, key = _paired(client)
    with client.websocket_connect(P.WS_PATH, headers=_headers(device_id, key)) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "response", "id": "abcdef12", "ok": True, "url": "http://evil"}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
            ws.receive_json()
        assert closed.value.code == P.CLOSE_PROTOCOL


def test_an_outdated_connector_is_told_so(client, monkeypatch):
    _, device_id, key = _paired(client)
    headers = _headers(device_id, key)
    headers["x-connector-version"] = "0.0.1"
    with client.websocket_connect(P.WS_PATH, headers=headers) as ws:
        assert ws.receive_json()["code"] == P.CLOSE_OUTDATED


def test_another_accounts_device_is_not_found(client):
    _, device_id, _ = _paired(client)
    with SessionLocal() as db:
        other = User(email=f"other-{secrets.token_hex(3)}@example.com", password_hash=_auth.hash_password("x" * 12))
        db.add(other)
        db.commit()
        db.get(Device, device_id).owner_id = other.id
        db.commit()
    assert client.post(f"/api/devices/{device_id}/approve").status_code == 404
    assert client.delete(f"/api/devices/{device_id}").status_code == 404
    assert all(d["id"] != device_id for d in client.get("/api/devices").json()["devices"])
    with SessionLocal() as db:
        db.get(Device, device_id).owner_id = TEST_USER_ID
        db.commit()


def test_setup_cards_come_from_the_adapter_table(client):
    body = client.get("/api/devices/setup").json()
    ids = [c["id"] for c in body["runtimes"]]
    assert ids[0] == "ollama" and "openai-compatible" in ids
    assert f"{P.PACKAGE}=={P.CONNECTOR_VERSION}" in body["connector"]["commands"]["macos"]


# ── what the connector itself will do ────────────────────────────────────────
@pytest.mark.parametrize(
    "op,args",
    [
        ("pull", {"model": "x"}),
        ("delete", {"model": "x"}),
        ("chat", {}),
        ("/api/pull", {}),
        ("hello", {"url": "http://10.0.0.5:11434"}),
        ("list_models", {"path": "/api/delete"}),
        ("model_info", {"source": "ollama", "model": "x", "url": "http://169.254.169.254"}),
        ("model_info", {"source": "ollama", "model": "../../etc/passwd"}),
    ],
)
def test_the_connector_refuses_anything_but_typed_operations_and_logs_it(op, args, tmp_path):
    agent = Agent("0" * 32)
    with pytest.raises(Refused):
        agent.handle(op, args)
    log = (store.home() / "connector.log").read_text(encoding="utf-8")
    assert "refused" in log


def test_the_connector_only_dials_https_or_this_computer():
    assert server_for("https://example.com").ws_url == "wss://example.com/api/connector/ws"
    assert server_for("http://localhost:8000").ws_url == "ws://localhost:8000/api/connector/ws"
    with pytest.raises(ConnectError):
        server_for("http://example.com")
