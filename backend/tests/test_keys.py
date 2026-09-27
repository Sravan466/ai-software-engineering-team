"""Cloud API keys: checked when saved, encrypted at rest, and never quoted back (#34)."""
from __future__ import annotations

import json
import logging
import os
import stat
import uuid

import httpx
import pytest

from app.core import scrub, secretbox, secrets_store, userdata
from app.core.constants import RoutingMode
from app.router import keycheck
from app.router.base import ProviderError
from app.router.router import ModelRouter

LOCAL = {"host": "localhost"}


def _provider(routes: dict):
    """A fake provider API: `{(method, path-suffix): (status, body)}`. Records calls."""
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.headers)))
        for (method, suffix), (status, body) in routes.items():
            if request.method == method and request.url.path.endswith(suffix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": {"message": "no route"}})

    return httpx.MockTransport(handler), calls


@pytest.fixture
def provider(monkeypatch):
    def use(routes: dict):
        transport, calls = _provider(routes)
        monkeypatch.setattr(keycheck, "transport", transport)
        return calls

    return use


@pytest.fixture
def router():
    return ModelRouter(uuid.uuid4().hex, owner=False)


OK_OPENAI = {
    ("GET", "/models/gpt-test"): (200, {"id": "gpt-test"}),
    ("POST", "/chat/completions"): (200, {"choices": []}),
}


# ── the check ────────────────────────────────────────────────────────────────
def test_a_working_key_is_valid_and_the_key_goes_in_a_header(provider):
    calls = provider(OK_OPENAI)
    found = keycheck.check("openai", "sk-good-0000000000", "gpt-test")
    assert found.status == keycheck.VALID and found.checked_at and found.key_id
    assert [c[0] for c in calls] == ["GET", "POST"]
    assert calls[0][2]["authorization"] == "Bearer sk-good-0000000000"
    assert all("sk-good" not in path for _, path, _ in calls)


def test_a_mistyped_key_is_invalid_and_the_providers_words_are_dropped(provider):
    echo = "Incorrect API key provided: sk-qVL45***...D1Vi"
    provider({("GET", "/models/gpt-test"): (401, {"error": {"message": echo, "code": "invalid_api_key"}})})
    found = keycheck.check("openai", "sk-qVL45abcdefD1Vi", "gpt-test")
    assert found.status == keycheck.INVALID and found.reason == "rejected"
    assert "qVL45" not in json.dumps(found.to_dict()) and "D1Vi" not in json.dumps(found.to_dict())


def test_no_credit_is_billing_not_valid(provider):
    provider({
        ("GET", "/models/claude-x"): (200, {"id": "claude-x", "max_input_tokens": 123456}),
        ("POST", "/messages"): (400, {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API."}}),
    })
    found = keycheck.check("anthropic", "sk-ant-nocredit-00000", "claude-x")
    assert found.status == keycheck.BILLING and found.rejected
    assert found.context_tokens == 123456


def test_a_model_the_key_cannot_use_lists_the_ones_it_can(provider):
    provider({
        ("GET", "/models/gpt-nope"): (404, {"error": {"message": "The model does not exist"}}),
        ("GET", "/models"): (200, {"data": [{"id": "gpt-b"}, {"id": "gpt-a"}]}),
    })
    found = keycheck.check("openai", "sk-fine-000000000000", "gpt-nope")
    assert found.status == keycheck.MODEL_UNAVAILABLE and found.models == ["gpt-a", "gpt-b"]


def test_an_outage_is_unverified_not_invalid(provider):
    provider({("GET", "/models/gpt-test"): (503, {"error": {"message": "overloaded"}})})
    assert keycheck.check("openai", "sk-x-00000000000000", "gpt-test").status == keycheck.UNVERIFIED

    def down(request):
        raise httpx.ConnectError("refused")

    keycheck.transport = httpx.MockTransport(down)
    try:
        assert keycheck.check("openai", "sk-x-00000000000000", "gpt-test").status == keycheck.UNVERIFIED
    finally:
        keycheck.transport = httpx.MockTransport(lambda r: httpx.Response(503, json={}))


def test_rate_limited_still_counts_as_working(provider):
    provider({("GET", "/models/gpt-test"): (200, {}), ("POST", "/chat/completions"): (429, {
        "error": {"type": "requests", "code": "rate_limit_exceeded", "message": "Rate limit reached"}})})
    found = keycheck.check("openai", "sk-busy-00000000000", "gpt-test")
    assert found.status == keycheck.RATE_LIMITED and not found.rejected


def test_gemini_key_is_a_header_and_a_bad_one_is_invalid(provider):
    calls = provider({("GET", "/models/gemini-x"): (400, {"error": {
        "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key.",
        "details": [{"reason": "API_KEY_INVALID"}]}})})
    assert keycheck.check("gemini", "AIzaSyBADBADBADBADBAD", "gemini-x").status == keycheck.INVALID
    assert calls[0][2]["x-goog-api-key"] == "AIzaSyBADBADBADBADBAD"
    assert "key=" not in str(calls[0][1])


# ── saving ───────────────────────────────────────────────────────────────────
def test_a_rejected_key_never_replaces_one_that_works(provider, router):
    provider(OK_OPENAI)
    router.set_default_model("openai", "gpt-test")
    assert router.save_provider_key("openai", api_key="sk-works-0000000000")["applied"]
    assert router.provider_settings()["openai"]["status"] == keycheck.VALID

    provider({("GET", "/models/gpt-test"): (401, {"error": {"code": "invalid_api_key"}})})
    result = router.save_provider_key("openai", api_key="sk-typo-00000000000")
    assert result["applied"] is False and result["check"]["status"] == keycheck.INVALID
    row = router.provider_settings()["openai"]
    assert row["key_hint"] == "…0000" and row["status"] == keycheck.VALID and row["available"]
    assert router._cloud["openai"].secret() == "sk-works-0000000000"


def test_a_rejected_key_is_not_routed_to(provider, router):
    provider({("GET", "/models/gpt-test"): (200, {}), ("POST", "/chat/completions"): (429, {
        "error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})})
    router.set_default_model("openai", "gpt-test")
    result = router.save_provider_key("openai", api_key="sk-broke-0000000000")
    assert result["applied"] and result["check"]["status"] == keycheck.BILLING
    assert router.provider_settings()["openai"]["available"] is False
    assert router._auto_pick("high") != ("openai", "gpt-test")
    assert "openai:gpt-test" not in router.role_settings()["cloud_models"]


def test_an_unverified_key_is_saved_and_marked(router):
    router.set_default_model("anthropic", "claude-x")
    result = router.save_provider_key("anthropic", api_key="sk-ant-later-000000")
    assert result["applied"] and router.provider_settings()["anthropic"]["status"] == keycheck.UNVERIFIED


def test_a_new_model_the_key_cannot_use_is_not_switched_to(provider, router):
    provider(OK_OPENAI)
    router.set_default_model("openai", "gpt-test")
    router.save_provider_key("openai", api_key="sk-works-0000000000")
    provider({("GET", "/models/gpt-nope"): (404, {}), ("GET", "/models"): (200, {"data": [{"id": "gpt-test"}]})})
    result = router.save_provider_key("openai", default_model="gpt-nope")
    assert result["applied"] is False and result["check"]["models"] == ["gpt-test"]
    assert router.default_model("openai") == "gpt-test"


def test_the_verdict_survives_a_restart_and_belongs_to_its_key(provider, router):
    provider(OK_OPENAI)
    router.set_default_model("openai", "gpt-test")
    router.save_provider_key("openai", api_key="sk-works-0000000000")
    again = ModelRouter(router.user_id, owner=False)
    assert again.provider_settings()["openai"]["status"] == keycheck.VALID
    stored = json.loads(userdata.path(router.user_id, "providers.local.json").read_text())
    stored["openai"]["check"]["key_id"] = "someone-else"
    userdata.path(router.user_id, "providers.local.json").write_text(json.dumps(stored))
    assert ModelRouter(router.user_id, owner=False).provider_settings()["openai"]["status"] == keycheck.UNCHECKED


# ── before and during a build ────────────────────────────────────────────────
def test_a_key_revoked_since_it_was_saved_is_caught_before_the_build(provider, router):
    provider(OK_OPENAI)
    router.set_default_model("openai", "gpt-test")
    router.save_provider_key("openai", api_key="sk-revoked-00000000")
    found = router._checks["openai"]
    found.checked_at = "2000-01-01T00:00:00+00:00"  # long ago
    provider({("GET", "/models/gpt-test"): (401, {"error": {"code": "invalid_api_key"}})})
    ready = router.readiness(RoutingMode.MANUAL, "openai:gpt-test", roles=[], recheck_keys=True)
    assert not ready.ok and "OpenAI key isn't working" in ready.reason
    assert router.provider_settings()["openai"]["status"] == keycheck.INVALID


def test_a_401_mid_build_marks_the_key_instead_of_falling_back_quietly(provider, router):
    provider(OK_OPENAI)
    router.set_default_model("openai", "gpt-test")
    router.save_provider_key("openai", api_key="sk-midbuild-0000000")
    router._note_failure("openai", "gpt-test", ProviderError("OpenAI call failed: 401", retryable=False, status=401))
    row = router.provider_settings()["openai"]
    assert row["status"] == keycheck.INVALID and row["reason"] == keycheck.REJECTED_DURING_BUILD
    assert row["available"] is False


# ── at rest ──────────────────────────────────────────────────────────────────
def test_no_plaintext_key_is_written_and_the_file_is_owner_only(router):
    router.set_provider_key("gemini", api_key="AIzaSyMARKERMARKER1234")
    path = userdata.path(router.user_id, "providers.local.json")
    assert "MARKERMARKER" not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert ModelRouter(router.user_id, owner=False)._cloud["gemini"].secret() == "AIzaSyMARKERMARKER1234"


def test_a_plaintext_store_is_migrated_and_still_works(tmp_path, monkeypatch):
    user = uuid.uuid4().hex
    path = userdata.path(user, "providers.local.json")
    path.write_text(json.dumps({
        "openai": {"api_key": "sk-legacy-plaintext-1"},
        "sources": [{"id": "gpu", "base_url": "http://127.0.0.1:9", "api_key": "sk-src-plaintext-2"}],
    }))
    aside = tmp_path / "providers.local.json.moved-to-account-20260101"
    aside.write_text(json.dumps({"anthropic": {"api_key": "sk-ant-aside-plain-3"}}))
    monkeypatch.setattr(secrets_store, "_PATH", tmp_path / "providers.local.json")
    assert secrets_store.migrate_all() >= 3
    for f in (path, aside):
        assert "plain" not in f.read_text()
    assert secrets_store.migrate_all() == 0  # nothing left to do
    store = secrets_store.for_user(user)
    assert store.get_all()["openai"]["api_key"] == "sk-legacy-plaintext-1"
    assert secrets_store.reveal(store.get_sources()[0]["api_key"]) == "sk-src-plaintext-2"


def test_a_corrupt_store_is_reported_and_never_overwritten(router):
    path = userdata.path(router.user_id, "providers.local.json")
    path.write_text("{not json")
    router2 = ModelRouter(router.user_id, owner=False)
    assert router2.store_error()
    with pytest.raises(secrets_store.StoreUnreadable):
        router2.set_provider_key("openai", api_key="sk-would-erase-000")
    assert path.read_text() == "{not json"


def test_a_key_under_another_encryption_key_is_locked_not_lost(router):
    router.set_provider_key("openai", api_key="sk-locked-00000000000")
    path = userdata.path(router.user_id, "providers.local.json")
    from cryptography.fernet import Fernet

    data = json.loads(path.read_text())
    data["openai"]["api_key"] = "enc:v1:" + Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    path.write_text(json.dumps(data))
    again = ModelRouter(router.user_id, owner=False)
    assert again.provider_settings()["openai"]["status"] == keycheck.LOCKED
    # Another write to the file keeps the ciphertext it can't open, as it was.
    again._secrets.set_check("anthropic", {"status": "valid"})
    assert json.loads(path.read_text())["openai"]["api_key"] == data["openai"]["api_key"]


def test_rotation_decrypts_under_an_old_key(monkeypatch):
    from cryptography.fernet import Fernet

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    from pydantic import SecretStr

    from app.core.config import settings

    monkeypatch.setattr(settings, "secrets_encryption_key", SecretStr(old))
    secretbox.reset()
    sealed = secretbox.encrypt("sk-rotate-me-000000")
    monkeypatch.setattr(settings, "secrets_encryption_key", SecretStr(f"{new},{old}"))
    secretbox.reset()
    try:
        assert secretbox.decrypt(sealed) == "sk-rotate-me-000000"
        assert secretbox.decrypt(secretbox.rotate(sealed)) == "sk-rotate-me-000000"
    finally:
        monkeypatch.undo()
        secretbox.reset()


# ── never quoted back ────────────────────────────────────────────────────────
def test_scrub_takes_out_every_shape_of_key():
    scrub.register("sk-proj-MARKERabcdefghijkl0123")
    text = (
        "Incorrect API key provided: sk-qVL45***...D1Vi; key sk-ant-api03-xyzXYZ123; "
        "AIzaSyA1234567890abcdef; fragment MARKERabcdefgh; task-list stays"
    )
    out = scrub.scrub(text)
    for leaked in ("qVL45", "D1Vi", "xyzXYZ", "AIzaSy", "MARKERabcdefgh"):
        assert leaked not in out
    assert "task-list stays" in out


def test_a_provider_error_and_the_log_never_carry_the_key(caplog):
    scrub.register("sk-LOGMARKER-00001111")
    err = ProviderError("OpenAI call failed: Incorrect API key provided: sk-LOGMARKER-00001111")
    assert "LOGMARKER" not in str(err)
    with caplog.at_level(logging.WARNING):
        logging.getLogger("anything").warning("raw %s", "sk-LOGMARKER-00001111")
    assert "LOGMARKER" not in caplog.text


def test_settings_never_prints_an_env_key():
    from pydantic import SecretStr

    from app.core.config import Settings

    assert "sk-env-secret" not in repr(Settings(openai_api_key=SecretStr("sk-env-secret-000")))


# ── the routes ───────────────────────────────────────────────────────────────
def test_key_routes_are_refused_from_an_untrusted_host(client):
    assert client.put("/api/settings/providers/openai", json={"api_key": "sk-x"}).status_code == 403
    assert client.post("/api/settings/providers/openai/check").status_code == 403
    assert client.delete("/api/settings/providers/openai").status_code == 403


def test_the_check_route_is_rate_limited(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "key_checks_per_window", 2)
    codes = [client.post("/api/settings/providers/openai/check", headers=LOCAL).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_api_returns_at_most_the_last_four_characters(client, provider):
    provider({("GET", "/models/"): (200, {}), ("POST", "/chat/completions"): (200, {})})
    r = client.put("/api/settings/providers/openai", json={"api_key": "sk-APIMARKER-00009876"}, headers=LOCAL)
    try:
        assert r.status_code == 200 and "APIMARKER" not in r.text
        body = client.get("/api/settings/providers").text
        assert "APIMARKER" not in body and "…9876" in body
    finally:
        client.delete("/api/settings/providers/openai", headers=LOCAL)


# ── regressions from review ──────────────────────────────────────────────────
def test_uvicorn_access_log_still_formats():
    """The scrubber once emptied record.args; uvicorn's access formatter unpacks them."""
    from uvicorn.logging import AccessFormatter

    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/health", "1.1", 200), None,
    )
    record = logging.getLogRecordFactory()(
        record.name, record.levelno, record.pathname, record.lineno, record.msg, record.args, None
    )
    assert "GET /health" in AccessFormatter("%(message)s").format(record)
    ok = logging.getLogRecordFactory()("x", logging.INFO, __file__, 1, "API key: %s", ("sk-TEMPLATE-000000",), None)
    assert ok.getMessage().startswith("API key: ") and "TEMPLATE" not in ok.getMessage()


def test_a_key_that_may_request_but_not_read_models_is_accepted(provider):
    provider({
        ("GET", "/models/gpt-test"): (403, {"error": {"message": "You have insufficient permissions for this "
                                                      "operation. Missing scopes: api.model.read"}}),
        ("POST", "/chat/completions"): (200, {}),
    })
    assert keycheck.check("openai", "sk-restricted-0000000", "gpt-test").status == keycheck.VALID


def test_a_gemini_rate_limit_is_not_no_credit(provider):
    provider({
        ("GET", "/models/gemini-x"): (200, {}),
        ("POST", ":generateContent"): (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message":
            "You exceeded your current quota, please check your plan and billing details."}}),
    })
    assert keycheck.check("gemini", "AIzaSyLIMITEDLIMITED1", "gemini-x").status == keycheck.RATE_LIMITED


def test_a_blank_key_is_refused_not_a_removal(provider, router):
    provider(OK_OPENAI)
    router.set_default_model("openai", "gpt-test")
    router.save_provider_key("openai", api_key="sk-works-0000000000")
    with pytest.raises(ValueError):
        router.save_provider_key("openai", api_key="   ")
    assert router._cloud["openai"].has_key


def test_a_failed_write_leaves_the_key_in_memory_unchanged(router):
    router.set_provider_key("openai", api_key="sk-before-000000000")
    userdata.path(router.user_id, "providers.local.json").write_text("{broken")
    with pytest.raises(secrets_store.StoreUnreadable):
        router.set_provider_key("openai", api_key="sk-after-0000000000")
    assert router._cloud["openai"].secret() == "sk-before-000000000"
    assert router.store_error()


def test_the_preflight_reads_the_verdict_without_spending_a_call(provider, router):
    calls = provider(OK_OPENAI)
    router.set_provider_key("openai", api_key="sk-env-like-0000000", default_model="gpt-test")  # unchecked
    router.readiness(RoutingMode.AUTO, None, roles=[])
    assert calls == []
    router.readiness(RoutingMode.AUTO, None, roles=[], recheck_keys=True)
    assert calls and router.provider_settings()["openai"]["status"] == keycheck.VALID
