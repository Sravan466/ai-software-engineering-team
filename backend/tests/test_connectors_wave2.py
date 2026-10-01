"""Wave 2 of the app connectors (#59): 26 more services, connectable end to end.

Each check runs against its recorded fake-key response (the issue's "Bad key →"
column), so a provider changing its error shape fails here instead of reading as a
wrong "connected". Plus the cases only Wave 2 has: a 200 that is an error, an account
id that names no host, fields that build the URL and must never reach another host,
format-only connectors, generated secrets, mirrored browser variables and pastes that
hold several fields.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.build import integrations
from app.build.integrations_wave2 import WAVE2
from app.core import userdata
from app.skills import registry
from tests.conftest import TEST_USER_ID

LOCAL = {"host": "localhost"}
UT_TOKEN = base64.b64encode(json.dumps({"apiKey": "sk_live_" + "a" * 40, "appId": "app1"}).encode()).decode()

#: A set of values each connector's format rules accept.
GOOD: dict[str, dict[str, str]] = {
    "paypal": {"PAYPAL_CLIENT_ID": "AXk" + "a" * 30, "PAYPAL_CLIENT_SECRET": "EL" + "b" * 30, "PAYPAL_ENV": "sandbox"},
    "razorpay": {"RAZORPAY_KEY_ID": "rzp_test_abcdefghijklmn", "RAZORPAY_KEY_SECRET": "a" * 24},
    "lemonsqueezy": {"LEMONSQUEEZY_API_KEY": "eyJabc.eyJdef.ghi", "LEMONSQUEEZY_STORE_ID": "12345"},
    "sendgrid": {"SENDGRID_API_KEY": "SG." + "a" * 22 + "." + "b" * 40, "EMAIL_FROM": "hello@example.com"},
    "postmark": {"POSTMARK_SERVER_TOKEN": "12345678-1234-1234-1234-123456789012", "EMAIL_FROM": "hello@example.com"},
    "mailgun": {"MAILGUN_API_KEY": "a" * 32 + "-" + "b" * 8 + "-" + "c" * 8, "MAILGUN_DOMAIN": "mg.example.com", "MAILGUN_REGION": "us"},
    "auth0": {"AUTH0_DOMAIN": "tenant.us.auth0.com", "AUTH0_CLIENT_ID": "a" * 32, "AUTH0_CLIENT_SECRET": "b" * 64},
    "google-signin": {"GOOGLE_CLIENT_ID": "1234567890-" + "a" * 32 + ".apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "GOCSPX-" + "a" * 28},
    "github-signin": {"GITHUB_CLIENT_ID": "Ov23" + "a" * 16, "GITHUB_CLIENT_SECRET": "a" * 40},
    "anthropic": {"ANTHROPIC_API_KEY": "sk-ant-api03-" + "a" * 40},
    "gemini": {"GEMINI_API_KEY": "AIza" + "a" * 35},
    "groq": {"GROQ_API_KEY": "gsk_" + "a" * 40},
    "openrouter": {"OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 64},
    "replicate": {"REPLICATE_API_TOKEN": "r8_" + "a" * 37},
    "elevenlabs": {"ELEVENLABS_API_KEY": "sk_" + "a" * 48},
    "cloudinary": {"CLOUDINARY_CLOUD_NAME": "demo", "CLOUDINARY_API_KEY": "123456789012345", "CLOUDINARY_API_SECRET": "a" * 27},
    "uploadthing": {"UPLOADTHING_TOKEN": UT_TOKEN},
    "s3": {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "AKIA" + "A" * 16, "AWS_SECRET_ACCESS_KEY": "a" * 40, "S3_BUCKET": "my-bucket"},
    "twilio": {"TWILIO_ACCOUNT_SID": "AC" + "a" * 32, "TWILIO_AUTH_TOKEN": "b" * 32},
    "slack": {"SLACK_BOT_TOKEN": "xoxb-" + "1" * 12 + "-" + "a" * 24},
    "google-maps": {"NEXT_PUBLIC_GOOGLE_MAPS_API_KEY": "AIza" + "a" * 35},
    "mapbox": {"NEXT_PUBLIC_MAPBOX_TOKEN": "pk." + "a" * 60},
    "posthog": {"NEXT_PUBLIC_POSTHOG_KEY": "phc_" + "a" * 40},
    "sentry": {"NEXT_PUBLIC_SENTRY_DSN": "https://" + "a" * 32 + "@o123.ingest.sentry.io/456"},
    "algolia": {"NEXT_PUBLIC_ALGOLIA_APP_ID": "ABC123DEF4", "NEXT_PUBLIC_ALGOLIA_SEARCH_KEY": "a" * 32, "ALGOLIA_ADMIN_KEY": "b" * 32},
    "upstash": {"UPSTASH_REDIS_REST_URL": "https://eu1-good-db.upstash.io", "UPSTASH_REDIS_REST_TOKEN": "A" * 40},
}

#: What each provider answered an obviously fake key with (the issue's table).
BAD_KEY: dict[str, tuple[int, object]] = {
    "paypal": (401, {"error": "invalid_client"}),
    "razorpay": (401, {"error": {"code": "BAD_REQUEST_ERROR"}}),
    "lemonsqueezy": (401, {"errors": [{"status": "401"}]}),
    "sendgrid": (401, {"errors": [{"message": "authorization required"}]}),
    "postmark": (401, {"ErrorCode": 10, "Message": "No Account or Server API tokens were supplied"}),
    "mailgun": (401, "Forbidden"),
    "anthropic": (401, {"type": "error", "error": {"type": "authentication_error"}}),
    "gemini": (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}),
    "groq": (401, {"error": {"code": "invalid_api_key"}}),
    "openrouter": (401, {"error": {"message": "No auth credentials found"}}),
    "replicate": (401, {"detail": "Invalid token."}),
    "elevenlabs": (401, {"detail": {"status": "invalid_api_key"}}),
    "cloudinary": (401, {"error": {"message": "api_secret mismatch"}}),
    "uploadthing": (401, {"error": "Invalid API key"}),
    "twilio": (401, {"code": 20003, "message": "Authenticate"}),
    # 200, with the error in the body.
    "slack": (200, {"ok": False, "error": "invalid_auth"}),
    "google-maps": (200, {"results": [], "status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."}),
    "mapbox": (200, {"code": "TokenInvalid"}),
    "algolia": (403, {"message": "Invalid Application-ID or API key", "status": 403}),
    "upstash": (401, {"error": "Unauthorized"}),
}


def _respond(status: int, body: object):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    integrations.transport = httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clean():
    path = userdata.path(TEST_USER_ID, "connectors.local.json")
    if path.exists():
        path.unlink()
    unreachable = integrations.transport
    yield
    integrations.transport = unreachable
    if path.exists():
        path.unlink()


def test_every_wave2_connector_is_connectable_and_complete():
    lib = {s.name for s in registry.library() if s.usable}
    assert len(WAVE2) == 26
    for found in WAVE2:
        assert found.connectable, found.id
        assert found.guide and found.blurb and found.builds, found.id
        assert found.skill in lib, f"{found.id} has no usable skill {found.skill!r}"
        assert found.check is not None or found.custom is not None or found.id in {
            "google-signin", "github-signin", "posthog",
        }, found.id
    assert set(GOOD) == {i.id for i in WAVE2}


@pytest.mark.parametrize("iid", sorted(GOOD))
def test_good_shapes_parse(iid):
    parsed = integrations.parse(integrations.REGISTRY[iid], GOOD[iid])
    assert not parsed.problems, [p.as_dict() for p in parsed.problems]


@pytest.mark.parametrize("iid", sorted(BAD_KEY))
def test_each_check_reads_its_providers_bad_key_answer(iid):
    status, body = BAD_KEY[iid]
    _respond(status, body)
    found = integrations.REGISTRY[iid]
    parsed = integrations.parse(found, GOOD[iid])
    result = integrations.check(found, parsed.values, parsed.mode)
    assert result.result.status == "failed", (iid, result.result.message)


@pytest.mark.parametrize("iid", sorted(set(GOOD) - {"google-signin", "github-signin", "posthog", "s3", "auth0", "sentry"}))
def test_each_check_says_connected_on_a_good_answer(iid):
    body: dict = {"ok": True, "status": "OK", "code": "TokenValid", "result": "PONG", "data": [], "models": []}
    _respond(200, body)
    found = integrations.REGISTRY[iid]
    parsed = integrations.parse(found, GOOD[iid])
    assert integrations.check(found, parsed.values, parsed.mode).result.status == "connected", iid


@pytest.mark.parametrize("iid", ["google-signin", "github-signin", "posthog", "sentry"])
def test_format_only_is_never_connected(iid):
    _respond(200, {"ok": True})
    found = integrations.REGISTRY[iid]
    parsed = integrations.parse(found, GOOD[iid])
    assert integrations.check(found, parsed.values, parsed.mode).result.status == "unchecked"


def test_auth0_proves_the_domain_only():
    _respond(200, {"issuer": "https://tenant.us.auth0.com/"})
    found = integrations.REGISTRY["auth0"]
    parsed = integrations.parse(found, GOOD["auth0"])
    assert integrations.check(found, parsed.values).result.status == "unchecked"
    _respond(404, {})
    result = integrations.check(found, parsed.values).result
    assert result.status == "failed" and result.name == "AUTH0_DOMAIN"


def test_a_key_scoped_away_from_the_user_endpoint_is_saved_not_tested():
    _respond(401, {"detail": {"status": "missing_permissions"}})
    found = integrations.REGISTRY["elevenlabs"]
    assert integrations.check(found, GOOD["elevenlabs"]).result.status == "unchecked"


@pytest.mark.parametrize(
    "iid, field",
    [("algolia", "NEXT_PUBLIC_ALGOLIA_APP_ID"), ("upstash", "UPSTASH_REDIS_REST_URL")],
)
def test_an_id_that_names_no_host_points_at_that_field(iid, field):
    def no_such_host(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)

    integrations.transport = httpx.MockTransport(no_such_host)
    result = integrations.check(integrations.REGISTRY[iid], GOOD[iid]).result
    assert result.status == "failed" and result.reason == "id_wrong" and result.name == field


@pytest.mark.parametrize("iid, field", [("twilio", "TWILIO_ACCOUNT_SID"), ("cloudinary", "CLOUDINARY_CLOUD_NAME")])
def test_a_404_on_the_account_points_at_the_id(iid, field):
    _respond(404, {"message": "not found"})
    result = integrations.check(integrations.REGISTRY[iid], GOOD[iid]).result
    assert result.status == "failed" and result.name == field


@pytest.mark.parametrize(
    "iid, field, value",
    [
        ("auth0", "AUTH0_DOMAIN", "127.0.0.1"),
        ("auth0", "AUTH0_DOMAIN", "evil.example"),
        ("upstash", "UPSTASH_REDIS_REST_URL", "https://evil.example"),
        ("upstash", "UPSTASH_REDIS_REST_URL", "https://127.0.0.1"),
        # Rewrites the host (a bare name would only become *-dsn.algolia.net, still Algolia).
        ("algolia", "NEXT_PUBLIC_ALGOLIA_APP_ID", "evil.example/x"),
    ],
)
def test_a_pasted_host_is_refused_before_any_request(iid, field, value):
    seen = []
    integrations.transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    found = integrations.REGISTRY[iid]
    parsed = integrations.parse(found, {**GOOD[iid], field: value})
    assert any(p.name == field for p in parsed.problems)
    # And the check itself refuses, even if a value got past the format rule.
    result = integrations.check(found, {**GOOD[iid], field: value}).result
    assert result.status == "failed" and not seen


def test_host_allowlist():
    allowed = ("*.upstash.io", "api.stripe.com")
    assert integrations.host_allowed("eu1-x.upstash.io", allowed)
    assert integrations.host_allowed("api.stripe.com", allowed)
    for host in ("upstash.io", "upstash.io.evil.example", "evil.example", "127.0.0.1", "localhost", "api.stripe.com.evil"):
        assert not integrations.host_allowed(host, allowed), host


def test_public_fields_refuse_secrets():
    mapbox = integrations.parse(integrations.REGISTRY["mapbox"], {"NEXT_PUBLIC_MAPBOX_TOKEN": "sk." + "a" * 60})
    assert "browser" in mapbox.problems[0].message
    same = "c" * 32
    algolia = integrations.parse(
        integrations.REGISTRY["algolia"],
        {**GOOD["algolia"], "NEXT_PUBLIC_ALGOLIA_SEARCH_KEY": same, "ALGOLIA_ADMIN_KEY": same},
    )
    assert algolia.problems[0].name == "NEXT_PUBLIC_ALGOLIA_SEARCH_KEY" and "Admin" in algolia.problems[0].message


def test_generated_mirrored_and_expanded_values():
    google = integrations.parse(integrations.REGISTRY["google-signin"], GOOD["google-signin"])
    assert len(google.values["AUTH_SECRET"]) == 64
    razor = integrations.parse(integrations.REGISTRY["razorpay"], GOOD["razorpay"])
    assert razor.values["NEXT_PUBLIC_RAZORPAY_KEY_ID"] == GOOD["razorpay"]["RAZORPAY_KEY_ID"]
    assert razor.mode == "test"
    cloud = integrations.parse(
        integrations.REGISTRY["cloudinary"],
        {"CLOUDINARY_API_KEY": "CLOUDINARY_URL=cloudinary://123456789012345:" + "s" * 27 + "@demo"},
    )
    assert not cloud.problems
    assert cloud.values["CLOUDINARY_CLOUD_NAME"] == "demo" == cloud.values["NEXT_PUBLIC_CLOUDINARY_CLOUD_NAME"]
    mailgun = integrations.parse(integrations.REGISTRY["mailgun"], {k: v for k, v in GOOD["mailgun"].items() if k != "MAILGUN_REGION"})
    assert mailgun.values["MAILGUN_REGION"] == "us"


def test_mailgun_and_paypal_follow_their_region_and_environment():
    hosts = []
    integrations.transport = httpx.MockTransport(lambda r: hosts.append(r.url.host) or httpx.Response(200, json={}))
    integrations.check(integrations.REGISTRY["mailgun"], {**GOOD["mailgun"], "MAILGUN_REGION": "eu"})
    integrations.check(integrations.REGISTRY["paypal"], GOOD["paypal"])
    integrations.check(integrations.REGISTRY["paypal"], {**GOOD["paypal"], "PAYPAL_ENV": "live"})
    assert hosts == ["api.eu.mailgun.net", "api-m.sandbox.paypal.com", "api-m.paypal.com"]


def test_ai_connectors_return_models_for_the_picker():
    _respond(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})
    for iid in ("anthropic", "groq"):
        assert integrations.check(integrations.REGISTRY[iid], GOOD[iid]).models == ["model-a", "model-b"]
    _respond(200, {"models": [
        {"name": "models/gem-a", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embed-only", "supportedGenerationMethods": ["embedContent"]},
    ]})
    assert integrations.check(integrations.REGISTRY["gemini"], GOOD["gemini"]).models == ["gem-a"]
    # OpenRouter's key check has no models; its public list fills the picker.
    _respond(200, {"data": [{"id": "vendor/model-x"}]})
    assert integrations.check(integrations.REGISTRY["openrouter"], GOOD["openrouter"]).models == ["vendor/model-x"]


def test_s3_without_a_signer_is_saved_not_tested():
    pytest.importorskip("pytest")
    try:
        import boto3  # noqa: F401
    except ImportError:
        result = integrations.check(integrations.REGISTRY["s3"], GOOD["s3"])
        assert result.result.status == "unchecked"


def test_paypal_live_needs_confirmation(client):
    _respond(200, {"access_token": "x"})
    body = {"values": {**GOOD["paypal"], "PAYPAL_ENV": "live"}}
    r = client.put("/api/connectors/paypal", json=body, headers=LOCAL)
    assert r.status_code == 409 and r.json()["detail"]["status"] == "needs_live_confirm"
    r = client.put("/api/connectors/paypal", json={**body, "confirm_live": True}, headers=LOCAL)
    assert r.json()["ok"] and r.json()["connector"]["mode"] == "live"


# ── detection, now that a capability can have several connectors ────────────
@pytest.mark.parametrize(
    "idea, connected, expect",
    [
        ("Appointment reminders by SMS", [], ["twilio"]),
        ("A storybook app that reads aloud with text to speech", [], ["elevenlabs"]),
        ("A portfolio site with image uploads", [], ["cloudinary"]),
        ("A store locator for our cafes", [], ["google-maps"]),
        ("An AI chatbot", ["anthropic"], ["anthropic"]),
        ("An AI chatbot on Claude", ["openai", "anthropic"], ["anthropic"]),
        ("A store with checkout", ["razorpay"], ["razorpay"]),
        ("Sign in with Google for a recipe box", [], ["google-signin"]),
        # "Error tracking" is in every security note: Sentry only once it's connected.
        ("A dashboard with error tracking and product analytics", [], ["posthog"]),
        ("A dashboard with error tracking and product analytics", ["sentry"], ["posthog", "sentry"]),
        ("Sign in with Google and GitHub for a recipe box", [], ["github-signin", "google-signin"]),
        ("A daily horoscope for Gemini and Leo", ["openai"], []),
        ("A todo app", ["twilio", "anthropic", "cloudinary"], []),
    ],
)
def test_detection_with_wave2(idea, connected, expect):
    assert [m.iid for m in integrations.relevant(idea, (), connected)] == expect


def test_two_connected_for_one_job_uses_the_default_and_never_both():
    both = ["stripe", "razorpay"]
    picked = integrations.relevant("A store with checkout", (), both, defaults={"payments": "razorpay"})
    assert [m.iid for m in picked] == ["razorpay"]
    picked = integrations.relevant("A store with checkout", (), both, recent={"stripe": "2026-10-01", "razorpay": "2026-09-01"})
    assert [m.iid for m in picked] == ["stripe"]
    assert [m.iid for m in integrations.relevant("A store with checkout with Razorpay", (), both)] == ["razorpay"]


def test_new_build_check_rule_catches_wave2_secrets():
    from app.build.check import secret_leaks

    files = {
        "frontend/app/a/page.tsx": "'use client'\nconst k = process.env.TWILIO_AUTH_TOKEN",
        "frontend/lib/b.ts": "const k = process.env.NEXT_PUBLIC_ALGOLIA_ADMIN_KEY",
        "frontend/lib/c.ts": "const k = process.env.NEXT_PUBLIC_ALGOLIA_SEARCH_KEY",
    }
    assert {p.path for p in secret_leaks(files, files)} == {"frontend/app/a/page.tsx", "frontend/lib/b.ts"}


# ── review fixes ─────────────────────────────────────────────────────────────
def test_routine_design_notes_dont_ask_for_services():
    design = ['{"security": "rate limiting on login, error tracking, file uploads, profile pictures"}']
    assert integrations.relevant("A notes app", design, []) == []


@pytest.mark.parametrize(
    "iid, status, body, expect",
    [
        ("google-maps", 200, {"status": "REQUEST_DENIED", "error_message": "This API key is not authorized to use this service or API."}, "connected"),
        ("sentry", 403, {"detail": "You do not have permission"}, "unchecked"),
        ("mapbox", 200, {"code": "TokenRevoked"}, "failed"),
        ("mailgun", 404, {"message": "Domain not found"}, "failed"),
    ],
)
def test_review_status_mappings(iid, status, body, expect):
    _respond(status, body)
    values = {**GOOD[iid], "SENTRY_AUTH_TOKEN": "sntrys_" + "a" * 40} if iid == "sentry" else GOOD[iid]
    assert integrations.check(integrations.REGISTRY[iid], values).result.status == expect


def test_a_wrong_cloud_name_points_at_the_cloud_name():
    _respond(401, {"error": {"message": "Invalid cloud_name demo"}})
    result = integrations.check(integrations.REGISTRY["cloudinary"], GOOD["cloudinary"]).result
    assert result.name == "CLOUDINARY_CLOUD_NAME"


def test_mailgun_checks_the_domain_itself():
    seen = []
    integrations.transport = httpx.MockTransport(lambda r: seen.append(str(r.url)) or httpx.Response(200, json={}))
    integrations.check(integrations.REGISTRY["mailgun"], GOOD["mailgun"])
    assert seen[0].endswith("/v3/domains/mg.example.com")


def test_check_never_raises_on_an_unparseable_stored_url():
    found = integrations.REGISTRY["upstash"]
    result = integrations.check(found, {**GOOD["upstash"], "UPSTASH_REDIS_REST_URL": "https://evil.com\uff0f.upstash.io"})
    assert result.result.status in ("failed", "unchecked")


def test_temporary_aws_keys_need_their_session_token():
    parsed = integrations.parse(integrations.REGISTRY["s3"], {**GOOD["s3"], "AWS_ACCESS_KEY_ID": "ASIA" + "A" * 16})
    assert parsed.problems and parsed.problems[0].name == "AWS_SESSION_TOKEN"


@pytest.mark.parametrize(
    "idea, connected, expect",
    [
        ("A SaaS on Clerk with sign in with Google", ["clerk"], ["clerk"]),
        ("Auth0 login plus Google login", [], ["auth0"]),
        ("Newsletter emails via SendGrid, with a resend link button", [], ["sendgrid"]),
        ("A chatbot built with Gemini", ["gemini"], ["gemini"]),
    ],
)
def test_one_job_one_connector_unless_a_known_pair(idea, connected, expect):
    assert [m.iid for m in integrations.relevant(idea, (), connected)] == expect
