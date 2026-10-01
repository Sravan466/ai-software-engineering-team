"""A bad, expired or unpaid key says exactly what's wrong — Settings, builds, connectors (#63)."""
from __future__ import annotations

import uuid
from typing import Optional

import httpx
import pytest

from app.build import integrations
from app.core import keyerrors, scrub
from app.core.config import settings
from app.router import keycheck
from app.router.base import LLMProvider, ProviderError, cloud_error
from app.router.router import ModelRouter
from app.schemas.llm import ChatMessage, GenerationOptions

K = keyerrors
SENTINEL = "sk-proj-SENTINELSENTINEL0042"


# ── the classifier: one row per cause ────────────────────────────────────────
@pytest.mark.parametrize(
    "provider, status, body, headers, kind",
    [
        # OpenAI
        ("openai", 401, {"error": {"code": "invalid_api_key", "message": "Incorrect API key provided"}}, None, K.INVALID),
        ("openai", 429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}, None, K.NO_CREDIT),
        ("openai", 429, {"error": {"code": "credit_balance_exhausted"}}, None, K.NO_CREDIT),
        ("openai", 429, {"error": {"code": "organization_spend_limit_exceeded"}}, None, K.SPEND_LIMIT),
        ("openai", 429, {"error": {"code": "project_spend_limit_exceeded"}}, None, K.SPEND_LIMIT),
        ("openai", 429, {"error": {"type": "requests", "code": "rate_limit_exceeded"}}, {"retry-after": "2"}, K.RATE_LIMITED),
        ("openai", 403, {"error": {"code": "unsupported_country_region_territory"}}, None, K.REGION),
        ("openai", 500, {"error": {"message": "server"}}, None, K.PROVIDER_DOWN),
        # Anthropic
        ("anthropic", 401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, None, K.INVALID),
        ("anthropic", 401, {"type": "error", "error": {"type": "authentication_error", "message": "This API key has expired"}}, None, K.EXPIRED),
        ("anthropic", 402, {"type": "error", "error": {"type": "billing_error", "message": "Your credit balance is too low"}}, None, K.NO_CREDIT),
        ("anthropic", 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                      "message": "You have reached your specified API usage limits."}}, None, K.SPEND_LIMIT),
        ("anthropic", 429, {"type": "error", "error": {"type": "rate_limit_error"}}, {}, K.SPEND_LIMIT),
        ("anthropic", 429, {"type": "error", "error": {"type": "rate_limit_error"}}, {"retry-after": "5"}, K.RATE_LIMITED),
        ("anthropic", 403, {"type": "error", "error": {"type": "permission_error"}}, None, K.NOT_PERMITTED),
        ("anthropic", 529, {"type": "error", "error": {"type": "overloaded_error"}}, None, K.PROVIDER_DOWN),
        # Gemini
        ("gemini", 400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid.",
                                   "details": [{"reason": "API_KEY_INVALID"}]}}, None, K.INVALID),
        ("gemini", 400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key expired. Please renew the API key.",
                                   "details": [{"reason": "API_KEY_INVALID"}]}}, None, K.EXPIRED),
        ("gemini", 403, {"error": {"status": "PERMISSION_DENIED", "message": "Your API key was reported as leaked."}}, None, K.REVOKED),
        ("gemini", 402, {"error": {"status": "PAYMENT_REQUIRED", "message": "Your Prepay credit balance is depleted."}}, None, K.NO_CREDIT),
        ("gemini", 400, {"error": {"status": "FAILED_PRECONDITION", "message": "Billing is not enabled for this project."}}, None, K.BILLING_DISABLED),
        ("gemini", 400, {"error": {"status": "FAILED_PRECONDITION", "message": "User location is not supported for the API use."}}, None, K.REGION),
        ("gemini", 429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota",
                                   "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}, None, K.SPEND_LIMIT),
        ("gemini", 429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota, please check your plan and billing details.",
                                   "details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]}}, None, K.RATE_LIMITED),
        # Connectors
        ("stripe", 401, {"error": {"code": "api_key_expired", "type": "invalid_request_error"}}, None, K.EXPIRED),
        ("stripe", 401, {"error": {"message": "Invalid API Key provided: sk_test_****", "type": "invalid_request_error"}}, None, K.INVALID),
        ("stripe", 400, {"error": {"code": "secret_key_required"}}, None, K.INVALID),
        ("stripe", 400, {"error": {"code": "testmode_charges_only"}}, None, K.BILLING_DISABLED),
        ("resend", 403, {"statusCode": 403, "name": "suspended_api_key"}, None, K.REVOKED),
        ("resend", 429, {"statusCode": 429, "name": "monthly_quota_exceeded"}, None, K.PLAN_QUOTA),
        ("resend", 429, {"statusCode": 429, "name": "daily_quota_exceeded"}, None, K.PLAN_QUOTA),
        ("resend", 429, {"statusCode": 429, "name": "rate_limit_exceeded"}, None, K.RATE_LIMITED),
        ("clerk", 401, {"errors": [{"code": "clerk_key_invalid", "message": "Secret Key is invalid"}]}, None, K.INVALID),
        # Anything: 402 is money, an unknown 4xx is unknown — never "it works".
        ("someone", 402, {}, None, K.NO_CREDIT),
        ("someone", 418, {"error": {"code": "teapot"}}, None, K.UNKNOWN),
    ],
)
def test_each_cause_is_its_own_kind(provider, status, body, headers, kind):
    found = K.classify(provider, status, body, headers)
    assert found.kind == kind
    assert found.retryable == (kind in K.RETRYABLE)


def test_codes_win_over_text():
    # The message says "billing", the code says rate limit: the code decides.
    body = {"error": {"code": "rate_limit_exceeded", "message": "check your plan and billing details"}}
    assert K.classify("openai", 429, body, {"retry-after": "1"}).kind == K.RATE_LIMITED


def test_each_kind_has_distinct_words_and_one_action():
    seen = set()
    for kind in K.KINDS:
        a = K.advice(kind, "openai")
        assert a.title and a.body and a.badge
        assert (a.title, a.badge) not in seen
        seen.add((a.title, a.badge))
        if kind in K.MONEY_KINDS:
            assert "billing" in a.action_url or "limits" in a.action_url
        if kind in (K.EXPIRED, K.REVOKED):
            assert a.action_label == "Create a new key" and "api-keys" in a.action_url
            assert "copied" not in a.body.lower()  # never blame the typing for an expired key


# ── Settings ────────────────────────────────────────────────────────────────
def _provider(monkeypatch, routes: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        for (method, suffix), answer in routes.items():
            if request.method == method and request.url.path.endswith(suffix):
                status, body, *rest = answer
                return httpx.Response(status, json=body, headers=rest[0] if rest else None)
        return httpx.Response(404, json={"error": {"message": "no route"}})

    monkeypatch.setattr(keycheck, "transport", httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "provider, answer, status, reason",
    [
        ("openai", (401, {"error": {"code": "invalid_api_key"}}), keycheck.INVALID, "rejected"),
        ("openai", (401, {"error": {"message": "This key has been revoked"}}), keycheck.INVALID, K.REVOKED),
        ("openai", (429, {"error": {"code": "insufficient_quota"}}), keycheck.BILLING, K.NO_CREDIT),
        ("openai", (429, {"error": {"code": "organization_spend_limit_exceeded"}}), keycheck.BILLING, K.SPEND_LIMIT),
        ("openai", (429, {"error": {"code": "rate_limit_exceeded"}}, {"retry-after": "1"}), keycheck.RATE_LIMITED, "rate_limited"),
        ("anthropic", (402, {"type": "error", "error": {"type": "billing_error"}}), keycheck.BILLING, K.NO_CREDIT),
        ("anthropic", (429, {"type": "error", "error": {"type": "rate_limit_error"}}), keycheck.BILLING, K.SPEND_LIMIT),
        ("gemini", (402, {"error": {"status": "PAYMENT_REQUIRED"}}), keycheck.BILLING, K.NO_CREDIT),
        ("gemini", (400, {"error": {"status": "FAILED_PRECONDITION", "message": "Billing is not enabled"}}), keycheck.BILLING, K.BILLING_DISABLED),
        ("gemini", (400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key expired. Please renew the API key."}}), keycheck.INVALID, K.EXPIRED),
        ("gemini", (403, {"error": {"status": "PERMISSION_DENIED", "message": "Your API key was reported as leaked."}}), keycheck.INVALID, K.REVOKED),
        # An answer nobody recognises is no longer "the key works".
        ("openai", (418, {"error": {"code": "teapot"}}), keycheck.UNVERIFIED, "unknown"),
    ],
)
def test_settings_check_names_the_cause(monkeypatch, provider, answer, status, reason):
    path = ":generateContent" if provider == "gemini" else ("/messages" if provider == "anthropic" else "/chat/completions")
    _provider(monkeypatch, {("GET", "/models/m-1"): (200, {}), ("POST", path): answer})
    found = keycheck.check(provider, "sk-ant-or-AIza-0000000000000", "m-1")
    assert (found.status, found.reason) == (status, reason), found.message
    assert "0000000000000" not in found.message


def test_the_settings_row_carries_the_advice(monkeypatch):
    _provider(monkeypatch, {("GET", "/models/m-1"): (200, {}), ("POST", "/chat/completions"): (429, {"error": {"code": "insufficient_quota"}})})
    router = ModelRouter(uuid.uuid4().hex, owner=False)
    router.set_default_model("openai", "m-1")
    router.save_provider_key("openai", api_key="sk-nocredit-00000000")
    row = router.provider_settings()["openai"]
    assert row["reason"] == K.NO_CREDIT and row["advice"]["action_label"] == "Add credits"


# ── during a build ──────────────────────────────────────────────────────────
class _SdkError(Exception):
    """What the OpenAI and Anthropic SDKs raise: a status, the parsed body, the response."""

    def __init__(self, status: int, body: dict, headers: Optional[dict] = None):
        super().__init__(f"Error code: {status} - {body} key={SENTINEL}")
        self.status_code = status
        self.body = body
        self.response = httpx.Response(status, json=body, headers=headers or {})


class _Failing(LLMProvider):
    name = "openai"

    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def available(self) -> bool:
        return True

    def generate(self, messages, model, options):
        self.calls += 1
        raise cloud_error("openai", "OpenAI", self.error)


def test_no_credit_mid_build_is_called_once_and_said_plainly(monkeypatch):
    scrub.register(SENTINEL)
    monkeypatch.setattr(settings, "provider_retry_attempts", 3)
    monkeypatch.setattr(settings, "provider_retry_backoff_seconds", 0.0)
    prov = _Failing(_SdkError(429, {"message": "You exceeded your current quota", "type": "insufficient_quota",
                                    "code": "insufficient_quota"}))
    router = ModelRouter(uuid.uuid4().hex, owner=False)
    with pytest.raises(ProviderError) as caught:
        router._generate(prov, [ChatMessage(role="user", content="hi")], "gpt-x", GenerationOptions())
    assert prov.calls == 1
    e = caught.value
    assert e.kind == K.NO_CREDIT and e.provider == "openai" and not e.retryable
    assert str(e).startswith("Your OpenAI account is out of credit")
    assert "{" not in str(e) and "local runtime" not in str(e)
    assert SENTINEL not in str(e) and SENTINEL not in (e.technical or "")


def test_a_real_rate_limit_is_still_retried(monkeypatch):
    monkeypatch.setattr(settings, "provider_retry_attempts", 2)
    monkeypatch.setattr(settings, "provider_retry_backoff_seconds", 0.0)
    prov = _Failing(_SdkError(429, {"code": "rate_limit_exceeded"}, {"retry-after": "1"}))
    router = ModelRouter(uuid.uuid4().hex, owner=False)
    with pytest.raises(ProviderError) as caught:
        router._generate(prov, [ChatMessage(role="user", content="hi")], "gpt-x", GenerationOptions())
    assert prov.calls == 3 and caught.value.kind == K.RATE_LIMITED


def test_a_dropped_connection_carries_no_kind():
    e = cloud_error("openai", "OpenAI", ConnectionError("reset"))
    assert e.kind is None and e.retryable


def test_the_key_is_marked_with_its_cause_and_the_skip_says_why(monkeypatch):
    _provider(monkeypatch, {("GET", "/models/gpt-x"): (200, {}), ("POST", "/chat/completions"): (200, {"choices": []})})
    router = ModelRouter(uuid.uuid4().hex, owner=False)
    router.set_default_model("openai", "gpt-x")
    router.save_provider_key("openai", api_key="sk-expired-0000000")
    router._note_failure("openai", "gpt-x", ProviderError("x", status=401, kind=K.EXPIRED, provider="openai"))
    row = router.provider_settings()["openai"]
    assert row["reason"] == K.EXPIRED and row["advice"]["badge"] == "Expired" and row["during_build"]
    skipped = router._skipped("openai", "gpt-x", router._refuses("openai", "gpt-x"))
    assert skipped["why"] == "key expired" and skipped["kind"] == K.EXPIRED


def test_the_project_offers_the_fix_for_its_kind():
    from app.db.models import Project

    p = Project(last_error_kind=K.NO_CREDIT, last_error_provider="anthropic")
    help_ = p.last_error_help
    assert help_["action_label"] == "Add credits" and "anthropic" in help_["action_url"]
    assert Project(last_error_kind=None).last_error_help is None


def test_the_migration_adds_the_columns_to_an_old_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR PRIMARY KEY, last_error TEXT)"))
        conn.execute(text("INSERT INTO projects (id, last_error) VALUES ('p1', 'old failure')"))
    applied = run_migrations(engine)
    assert {"projects.last_error_kind", "projects.last_error_provider"} <= set(applied)
    cols = {c["name"] for c in inspect(engine).get_columns("projects")}
    assert {"last_error_kind", "last_error_provider"} <= cols
    with engine.begin() as conn:
        row = conn.execute(text("SELECT last_error, last_error_kind FROM projects")).one()
    assert row == ("old failure", None)


# ── connectors ──────────────────────────────────────────────────────────────
@pytest.fixture
def respond():
    before = integrations.transport

    def use(handler):
        integrations.transport = httpx.MockTransport(handler)

    yield use
    integrations.transport = before


@pytest.mark.parametrize(
    "iid, values, status, body, reason",
    [
        ("stripe", {"STRIPE_SECRET_KEY": "sk_test_abcdefghijkl"}, 401, {"error": {"code": "api_key_expired"}}, K.EXPIRED),
        ("resend", {"RESEND_API_KEY": "re_abc12345_xyz98765"}, 403, {"name": "suspended_api_key"}, K.REVOKED),
        ("resend", {"RESEND_API_KEY": "re_abc12345_xyz98765"}, 429, {"name": "monthly_quota_exceeded"}, K.PLAN_QUOTA),
        ("clerk", {"CLERK_SECRET_KEY": "sk_test_abcdefghijk"}, 402, {}, K.NO_CREDIT),
        ("gemini", {"GEMINI_API_KEY": "AIza" + "a" * 35}, 400,
         {"error": {"status": "INVALID_ARGUMENT", "message": "API key expired. Please renew the API key."}}, K.EXPIRED),
        ("openai", {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrst"}, 418, {"error": {"code": "teapot"}}, K.UNKNOWN),
    ],
)
def test_a_connector_names_the_cause(respond, iid, values, status, body, reason):
    respond(lambda request: httpx.Response(status, json=body))
    result = integrations.check(integrations.REGISTRY[iid], values)
    assert result.result.status == integrations.FAILED and result.result.reason == reason
    assert result.result.advice and result.result.advice["kind"] == reason


@pytest.mark.parametrize(
    "iid, values, listing, answer",
    [
        ("openai", {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrst"}, {"data": [{"id": "model-a"}]},
         (429, {"error": {"code": "insufficient_quota"}})),
        ("anthropic", {"ANTHROPIC_API_KEY": "sk-ant-api03-" + "a" * 40}, {"data": [{"id": "model-a"}]},
         (402, {"type": "error", "error": {"type": "billing_error"}})),
        ("gemini", {"GEMINI_API_KEY": "AIza" + "a" * 35},
         {"models": [{"name": "models/model-a", "supportedGenerationMethods": ["generateContent"]}]},
         (402, {"error": {"status": "PAYMENT_REQUIRED"}})),
    ],
)
def test_an_ai_connector_with_no_credit_is_not_connected(respond, iid, values, listing, answer):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(answer[0], json=answer[1])
        if request.url.path.endswith("/models/model-a"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json=listing)

    respond(handler)
    result = integrations.check(integrations.REGISTRY[iid], values)
    assert result.result.status == integrations.FAILED and result.result.reason == K.NO_CREDIT
    assert result.result.advice["action_label"] == "Add credits"


def test_the_gemini_connector_sends_its_key_in_a_header(respond):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"models": []})

    respond(handler)
    key = "AIza" + "b" * 35
    integrations.check(integrations.REGISTRY["gemini"], {"GEMINI_API_KEY": key})
    assert seen and all(key not in str(r.url) for r in seen)
    assert seen[0].headers["x-goog-api-key"] == key


def test_no_unknown_4xx_reads_as_working(monkeypatch):
    _provider(monkeypatch, {("GET", "/models/m-1"): (200, {}), ("POST", "/chat/completions"): (422, {})})
    found = keycheck.check("openai", "sk-whatever-00000000", "m-1")
    assert found.status != keycheck.VALID and found.message != "The key works."


# ── the review's findings ────────────────────────────────────────────────────
def test_a_zero_free_tier_quota_is_a_limit_even_beside_a_per_minute_one():
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "0"},
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "0"},
    ]}]}}
    assert K.classify("gemini", 429, body).kind == K.SPEND_LIMIT
    minute = {"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"violations": [
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel", "quotaValue": "15"}]}]}}
    assert K.classify("gemini", 429, minute).kind == K.RATE_LIMITED


def test_a_request_shape_400_is_never_a_key_verdict():
    body = {"error": {"code": "unsupported_value", "message": "Not allowed to use system messages with this model"}}
    assert K.classify("openai", 400, body).kind == K.UNKNOWN


def test_a_google_sdk_quota_error_keeps_its_window():
    Err = type("ResourceExhausted", (Exception,), {"__module__": "google.api_core.exceptions"})
    e = Err("429 quota")
    e.code, e.message = 429, "Quota exceeded"
    e.details = ['violations { quota_id: "GenerateRequestsPerDayPerProjectPerModel-FreeTier" }']
    assert K.from_exception("gemini", e).kind == K.SPEND_LIMIT


def test_an_error_that_isnt_about_the_key_keeps_its_own_reason():
    e = cloud_error("openai", "OpenAI", _SdkError(400, {"message": "This model's maximum context length is 8192 tokens",
                                                       "code": "context_length_exceeded"}))
    assert e.kind is None and "maximum context length" in str(e) and "dashboard" not in str(e)


def _two_link_router(monkeypatch, attempts_kinds):
    router = ModelRouter(uuid.uuid4().hex, owner=False)
    chain = [("anthropic", "a-1"), ("openai", "o-1")]
    monkeypatch.setattr(router, "_resolve_chain", lambda *a, **k: chain)
    errors = iter(attempts_kinds)

    class P(LLMProvider):
        def available(self):
            return True

        def generate(self, messages, model, options):
            kind = next(errors)
            raise ProviderError("x", retryable=False, kind=kind, provider=self.name)

    provs = {}
    for name in ("anthropic", "openai"):
        p = P()
        p.name = name
        provs[name] = p
    monkeypatch.setattr(router, "provider", lambda n: provs.get(n))
    monkeypatch.setattr(router, "_refuses", lambda *a: None)
    monkeypatch.setattr(router, "_note_failure", lambda *a: None)
    return router


def test_every_model_failing_carries_the_blocking_refusal_not_a_rate_limit(monkeypatch):
    router = _two_link_router(monkeypatch, [K.RATE_LIMITED, K.NO_CREDIT])
    with pytest.raises(ProviderError) as caught:
        router.complete([ChatMessage(role="user", content="hi")])
    e = caught.value
    assert (e.kind, e.provider) == (K.NO_CREDIT, "openai")
    assert "out of credit" in str(e) and "rate-limited" in str(e)


def test_a_key_ruled_out_earlier_still_says_why_when_skipped(monkeypatch):
    _provider(monkeypatch, {("GET", "/models/gpt-x"): (200, {}), ("POST", "/chat/completions"): (429, {"error": {"code": "insufficient_quota"}})})
    router = ModelRouter(uuid.uuid4().hex, owner=False)
    router.set_default_model("openai", "gpt-x")
    router.save_provider_key("openai", api_key="sk-nocredit-11111111")
    assert not router.provider("openai").available()
    skipped = router._skipped("openai", "gpt-x", None)
    assert skipped["kind"] == K.NO_CREDIT and skipped["why"] == "out of credit"


def test_only_a_blocking_kind_reaches_the_project():
    from app.db.models import Project
    from app.orchestration.runner import PipelineRunner

    class _Db:
        def commit(self):
            pass

    p = Project()
    runner = PipelineRunner.__new__(PipelineRunner)
    runner._fail(_Db(), p, "slow down", kind=K.RATE_LIMITED, provider="openai")
    assert p.last_error_kind is None and p.last_error_help is None
    runner._fail(_Db(), p, "no credit", kind=K.NO_CREDIT, provider="openai")
    assert p.last_error_kind == K.NO_CREDIT
