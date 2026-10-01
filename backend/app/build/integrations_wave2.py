"""Wave 2 of the app connectors (#59): 26 more services, wired end to end.

Each is one registry row — variables, format rules, a declarative read-only check,
a guide, and the bundled skill the crew gets — in the same shape as Stripe, Resend,
Clerk and OpenAI in `app.build.integrations`. The checks follow the issue's
"Bad key →" table, which was taken from what each provider actually answers to a
fake key: Slack, Mapbox and Google Maps answer with a 200 and the error in the body,
Gemini with a 400, and a wrong account id (Twilio's SID, Algolia's app id, an Upstash
URL, a Cloudinary cloud name, an Auth0 domain) is a missing host or a 404 that points
at that field, not at the key.

Where nothing free and read-only exists (OAuth client secrets, public analytics ids),
the check is the format, and the result is "Saved, not tested" — never "connected".
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

from app.build.dbconnect import CONNECTED, FAILED, CheckResult, Var
from app.build.integrations import (
    _OPENAI,
    _RESEND,
    _STRIPE,
    Checked,
    GuideStep,
    HttpCheck,
    Integration,
    Rule,
    _unchecked,
)

# Phrases that pick a connector only when it's the one connected for its job: a
# second payments connector reads "checkout" the way Stripe does, but never asks
# for itself over Stripe when neither is connected.
_PAY = _STRIPE.strong
_MAIL = _RESEND.strong
_LLM = _OPENAI.strong + _OPENAI.soft


def _models_openai_style(body: dict) -> list[str]:
    return sorted({str(m.get("id")) for m in body.get("data") or [] if isinstance(m, dict) and m.get("id")})


def _models_gemini(body: dict) -> list[str]:
    out = set()
    for m in body.get("models") or []:
        if not isinstance(m, dict):
            continue
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        name = str(m.get("name") or "")
        if name:
            out.add(name.split("/", 1)[-1])
    return sorted(out)


def _model_var(name: str) -> Var:
    return Var(
        name,
        "Model",
        secret=False,
        required=False,
        placeholder="Pick one after testing the key",
        kind="text",
        help="Which model the app calls. Chosen from your account's list.",
    )


def _guide(*steps: tuple[str, str] | str) -> tuple[GuideStep, ...]:
    return tuple(GuideStep(s) if isinstance(s, str) else GuideStep(*s) for s in steps)


def _auth_secret() -> Var:
    return Var(
        "AUTH_SECRET",
        "Session secret",
        required=False,
        generate=True,
        placeholder="Leave blank to generate one",
        help="Signs the session cookie. Leave it blank and a random one is generated for you.",
    )


# ── payments ─────────────────────────────────────────────────────────────────
_PAYPAL = Integration(
    id="paypal",
    label="PayPal",
    category="payments",
    capability="payments",
    wave=2,
    blurb="PayPal checkout and payouts",
    builds=(
        "A PayPal checkout button, with the order created and captured on the server",
        "Sandbox payments with test buyer accounts while you build",
        "Order status kept on your side after capture",
    ),
    variables=(
        Var("PAYPAL_CLIENT_ID", "Client ID", secret=False, placeholder="AXk…", help="From your REST app's credentials."),
        Var("PAYPAL_CLIENT_SECRET", "Secret", placeholder="EL…"),
        Var("PAYPAL_ENV", "Environment", secret=False, options=("sandbox", "live"), kind="text"),
    ),
    names=("paypal",),
    soft=_PAY,
    formats=(
        ("PAYPAL_CLIENT_ID", r"^[A-Za-z0-9_\-]{20,}$", "A PayPal client ID is a long string of letters and digits."),
        ("PAYPAL_CLIENT_SECRET", r"^[A-Za-z0-9_\-]{20,}$", "A PayPal secret is a long string of letters and digits."),
    ),
    check=HttpCheck(
        "POST",
        "https://{pp_host}/v1/oauth2/token",
        "basic_pair:PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        ("api-m.sandbox.paypal.com", "api-m.paypal.com"),
        rules=(Rule((401,), "invalid", message="PayPal didn't accept that client ID and secret together."),),
        body="grant_type=client_credentials",
        derive=lambda v: {"pp_host": "api-m.paypal.com" if v.get("PAYPAL_ENV") == "live" else "api-m.sandbox.paypal.com"},
    ),
    guide=_guide(
        ("Open the PayPal Developer dashboard and stay on Sandbox (top left).", "https://developer.paypal.com/dashboard/applications/sandbox"),
        "Apps & Credentials → open your app, or Create App.",
        "Copy the Client ID and the Secret key 1 (the secret).",
    ),
    docs_url="https://developer.paypal.com/api/rest/",
    dashboard_url="https://developer.paypal.com/dashboard/applications/sandbox",
    live_choice=("PAYPAL_ENV", "live"),
    has_test_mode=True,
    skill="paypal-checkout",
)

_RAZORPAY = Integration(
    id="razorpay",
    label="Razorpay",
    category="payments",
    capability="payments",
    wave=2,
    blurb="UPI, cards and netbanking in India",
    builds=(
        "Checkout with UPI, cards and netbanking via Razorpay Checkout",
        "Orders created on the server, payments verified by signature",
        "Test mode payments while you build",
    ),
    variables=(
        Var("RAZORPAY_KEY_ID", "Key ID", secret=False, placeholder="rzp_test_…"),
        Var("RAZORPAY_KEY_SECRET", "Key secret", placeholder="Shown once when the key is generated"),
        Var("NEXT_PUBLIC_RAZORPAY_KEY_ID", "Key ID (browser)", secret=False, side="client", copy_of="RAZORPAY_KEY_ID"),
    ),
    names=("razorpay",),
    strong=("upi", "upi payments", "netbanking", "rupees", "inr payments"),
    soft=_PAY,
    formats=(
        ("RAZORPAY_KEY_ID", r"^rzp_(test|live)_[A-Za-z0-9]{10,}$", "A Razorpay key ID starts with rzp_test_."),
        ("RAZORPAY_KEY_SECRET", r"^[A-Za-z0-9]{16,}$", "A Razorpay key secret is a string of letters and digits."),
    ),
    check=HttpCheck(
        "GET",
        "https://api.razorpay.com/v1/payments?count=1",
        "basic_pair:RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        ("api.razorpay.com",),
        rules=(Rule((401,), "invalid", message="Razorpay didn't accept that key ID and secret together."),),
    ),
    guide=_guide(
        ("Open the Razorpay Dashboard and switch to Test Mode.", "https://dashboard.razorpay.com/app/website-app-settings/api-keys"),
        "Account & Settings → API Keys → Generate Test Key.",
        "Copy the Key ID (rzp_test_…) and the Key Secret — the secret is shown once.",
    ),
    docs_url="https://razorpay.com/docs/api/",
    dashboard_url="https://dashboard.razorpay.com/app/website-app-settings/api-keys",
    live_prefixes=("rzp_live_",),
    has_test_mode=True,
    skill="razorpay-payments",
)

_LEMON = Integration(
    id="lemonsqueezy",
    label="Lemon Squeezy",
    category="payments",
    capability="payments",
    wave=2,
    blurb="Merchant of record for digital products",
    builds=(
        "Checkout links for your products and subscriptions",
        "Sales tax and VAT handled by Lemon Squeezy as merchant of record",
        "Webhook-verified order and subscription status",
    ),
    variables=(
        Var("LEMONSQUEEZY_API_KEY", "API key", placeholder="eyJ…"),
        Var("LEMONSQUEEZY_STORE_ID", "Store ID", secret=False, placeholder="12345", kind="text"),
        Var("LEMONSQUEEZY_WEBHOOK_SECRET", "Webhook signing secret", required=False, help="Optional. The secret you set on the webhook."),
    ),
    names=("lemon squeezy", "lemonsqueezy"),
    strong=("merchant of record", "sell digital products", "digital downloads"),
    soft=_PAY,
    formats=(
        ("LEMONSQUEEZY_API_KEY", r"^eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$", "A Lemon Squeezy API key is a long token starting with eyJ."),
        ("LEMONSQUEEZY_STORE_ID", r"^\d{1,12}$", "The store ID is a number — Settings → Stores shows it."),
        ("LEMONSQUEEZY_WEBHOOK_SECRET", r"^\S{6,}$", "The webhook secret is the string you typed when you made the webhook."),
    ),
    check=HttpCheck(
        "GET",
        "https://api.lemonsqueezy.com/v1/users/me",
        "bearer",
        "LEMONSQUEEZY_API_KEY",
        ("api.lemonsqueezy.com",),
        rules=(Rule((401,), "invalid", message="Lemon Squeezy didn't recognise that API key."),),
        headers=(("Accept", "application/vnd.api+json"),),
    ),
    guide=_guide(
        ("Open Lemon Squeezy → Settings → API.", "https://app.lemonsqueezy.com/settings/api"),
        "Create an API key (+). Copy it — it's shown once.",
        "Settings → Stores: the number beside your store is the store ID.",
    ),
    docs_url="https://docs.lemonsqueezy.com/api",
    dashboard_url="https://app.lemonsqueezy.com/settings/api",
    skill="lemonsqueezy-payments",
)

# ── email ────────────────────────────────────────────────────────────────────
_FROM_REQUIRED = Var(
    "EMAIL_FROM",
    "Send from",
    secret=False,
    placeholder="App <hello@yourdomain.com>",
    kind="text",
    help="A sender address you've verified with the provider.",
)

_SENDGRID = Integration(
    id="sendgrid",
    label="SendGrid",
    category="email",
    capability="email",
    wave=2,
    blurb="Transactional and marketing email",
    builds=("Confirmation and receipt emails from the server", "Password reset emails", "Dynamic templates by id"),
    variables=(Var("SENDGRID_API_KEY", "API key", placeholder="SG.…", help="Restricted access with Mail Send is enough."), _FROM_REQUIRED),
    names=("sendgrid", "send grid"),
    soft=_MAIL,
    formats=(("SENDGRID_API_KEY", r"^SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}$", "A SendGrid API key starts with SG. and has two dots."),),
    check=HttpCheck(
        "GET",
        "https://api.sendgrid.com/v3/scopes",
        "bearer",
        "SENDGRID_API_KEY",
        ("api.sendgrid.com",),
        rules=(Rule((401, 403), "invalid", message="SendGrid didn't recognise that API key."),),
    ),
    guide=_guide(
        ("Open SendGrid → Settings → API Keys.", "https://app.sendgrid.com/settings/api_keys"),
        "Create API Key → Restricted Access → turn on Mail Send. Copy it (SG.…) — it's shown once.",
        "Settings → Sender Authentication: verify the address you'll send from.",
    ),
    docs_url="https://www.twilio.com/docs/sendgrid/api-reference",
    dashboard_url="https://app.sendgrid.com/settings/api_keys",
    skill="sendgrid-email",
)

_POSTMARK = Integration(
    id="postmark",
    label="Postmark",
    category="email",
    capability="email",
    wave=2,
    blurb="Fast transactional email",
    builds=("Transactional emails with fast delivery", "Templates by alias", "Bounce-aware sending"),
    variables=(Var("POSTMARK_SERVER_TOKEN", "Server API token", placeholder="xxxxxxxx-xxxx-…"), _FROM_REQUIRED),
    names=("postmark",),
    soft=_MAIL,
    formats=(
        (
            "POSTMARK_SERVER_TOKEN",
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            "A Postmark server token looks like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.",
        ),
    ),
    check=HttpCheck(
        "GET",
        "https://api.postmarkapp.com/server",
        "header:X-Postmark-Server-Token",
        "POSTMARK_SERVER_TOKEN",
        ("api.postmarkapp.com",),
        rules=(Rule((401,), "invalid", message="Postmark didn't recognise that server token."),),
    ),
    guide=_guide(
        ("Open Postmark → Servers → your server.", "https://account.postmarkapp.com/servers"),
        "API Tokens tab → copy the Server API token.",
        "Sender Signatures: verify the address you'll send from.",
    ),
    docs_url="https://postmarkapp.com/developer",
    dashboard_url="https://account.postmarkapp.com/servers",
    skill="postmark-email",
)

_MAILGUN = Integration(
    id="mailgun",
    label="Mailgun",
    category="email",
    capability="email",
    wave=2,
    blurb="Email sending and routing",
    builds=("Transactional email from your sending domain", "EU or US region, as your account is", "Tagged sends for tracking"),
    variables=(
        Var("MAILGUN_API_KEY", "API key", placeholder="key-… or xxxxxxxx-xxxxxxxx-xxxxxxxx"),
        Var("MAILGUN_DOMAIN", "Sending domain", secret=False, placeholder="mg.yourdomain.com", kind="text"),
        Var("MAILGUN_REGION", "Region", secret=False, options=("us", "eu"), kind="text"),
    ),
    names=("mailgun",),
    soft=_MAIL,
    formats=(
        ("MAILGUN_API_KEY", r"^[A-Za-z0-9\-]{20,}$", "A Mailgun API key is a long string of letters, digits and dashes."),
        ("MAILGUN_DOMAIN", r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", "The sending domain looks like mg.yourdomain.com."),
    ),
    check=HttpCheck(
        "GET",
        "https://{mg_host}/v3/domains",
        "basic_user:api",
        "MAILGUN_API_KEY",
        ("api.mailgun.net", "api.eu.mailgun.net"),
        rules=(Rule((401, 403), "invalid", message="Mailgun didn't recognise that API key (or it's for the other region)."),),
        derive=lambda v: {"mg_host": "api.eu.mailgun.net" if v.get("MAILGUN_REGION") == "eu" else "api.mailgun.net"},
    ),
    guide=_guide(
        ("Open Mailgun → API Security.", "https://app.mailgun.com/settings/api_security"),
        "Add new key → copy it — it's shown once.",
        "Sending → Domains: copy your sending domain, and note whether it's US or EU.",
    ),
    docs_url="https://documentation.mailgun.com/docs/mailgun/api-reference/",
    dashboard_url="https://app.mailgun.com/settings/api_security",
    skill="mailgun-email",
)

# ── auth ─────────────────────────────────────────────────────────────────────
_AUTH0 = Integration(
    id="auth0",
    label="Auth0",
    category="auth",
    capability="auth",
    wave=2,
    blurb="Hosted sign-in with enterprise SSO",
    builds=("Universal Login for sign-in and sign-up", "Protected routes through the Auth0 SDK", "Social and enterprise connections you switch on in Auth0"),
    variables=(
        Var("AUTH0_DOMAIN", "Domain", secret=False, placeholder="your-tenant.us.auth0.com", kind="text"),
        Var("AUTH0_CLIENT_ID", "Client ID", secret=False, placeholder="32 characters"),
        Var("AUTH0_CLIENT_SECRET", "Client secret"),
        Var("AUTH0_SECRET", "Session secret", required=False, generate=True, placeholder="Leave blank to generate one", help="Encrypts the session cookie. Leave it blank and one is generated."),
        Var("APP_BASE_URL", "App URL", secret=False, required=False, placeholder="http://localhost:3000", kind="url", help="Where the app runs. Add its /auth/callback to Allowed Callback URLs."),
    ),
    names=("auth0",),
    soft=("enterprise sso", "single sign-on", "sso"),
    formats=(
        ("AUTH0_DOMAIN", r"^[a-z0-9][a-z0-9-]*(\.[a-z]{2})?\.auth0\.com$", "Use your tenant's Auth0 domain, like your-tenant.us.auth0.com."),
        ("AUTH0_CLIENT_ID", r"^[A-Za-z0-9]{20,64}$", "An Auth0 client ID is 32 letters and digits."),
        ("AUTH0_CLIENT_SECRET", r"^[A-Za-z0-9_\-]{32,}$", "An Auth0 client secret is 64 characters."),
        ("AUTH0_SECRET", r"^\S{32,}$", "The session secret needs at least 32 characters — or leave it blank."),
        ("APP_BASE_URL", r"^https?://[^\s/]+(:\d+)?/?$", "The app URL looks like http://localhost:3000."),
    ),
    check=HttpCheck(
        "GET",
        "https://{AUTH0_DOMAIN}/.well-known/openid-configuration",
        "none",
        "AUTH0_CLIENT_SECRET",
        ("*.auth0.com",),
        rules=(Rule((404,), "id_wrong", message="Auth0 has no tenant at that domain. Check the Domain.", field="AUTH0_DOMAIN"),),
        id_var="AUTH0_DOMAIN",
        partial="The domain answered. The client secret can only be checked when someone signs in.",
    ),
    guide=_guide(
        ("Open Auth0 → Applications → Applications.", "https://manage.auth0.com/#/applications"),
        "Create Application → Regular Web Application (or open yours) → Settings.",
        "Copy Domain, Client ID and Client Secret. Add http://localhost:3000/auth/callback to Allowed Callback URLs.",
    ),
    docs_url="https://auth0.com/docs/quickstart/webapp/nextjs",
    dashboard_url="https://manage.auth0.com/",
    skill="auth0-auth",
)

_GOOGLE_SIGNIN = Integration(
    id="google-signin",
    label="Google sign-in",
    category="auth",
    capability="auth",
    wave=2,
    blurb="Sign in with Google",
    builds=("A Sign in with Google button through Auth.js", "Sessions in a signed cookie", "The user's name, email and avatar from Google"),
    variables=(
        Var("GOOGLE_CLIENT_ID", "Client ID", secret=False, placeholder="…apps.googleusercontent.com"),
        Var("GOOGLE_CLIENT_SECRET", "Client secret", placeholder="GOCSPX-…"),
        _auth_secret(),
    ),
    names=("google sign-in", "google signin", "sign in with google", "google login", "google oauth", "login with google"),
    formats=(
        ("GOOGLE_CLIENT_ID", r"^\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com$", "A Google client ID ends in .apps.googleusercontent.com."),
        ("GOOGLE_CLIENT_SECRET", r"^GOCSPX-[A-Za-z0-9_\-]{20,}$", "A Google client secret starts with GOCSPX-."),
        ("AUTH_SECRET", r"^\S{32,}$", "The session secret needs at least 32 characters — or leave it blank."),
    ),
    guide=_guide(
        ("Open Google Cloud → APIs & Services → Credentials.", "https://console.cloud.google.com/apis/credentials"),
        "Create credentials → OAuth client ID → Web application.",
        "Add http://localhost:3000/api/auth/callback/google as a redirect URI, then copy the Client ID and secret.",
    ),
    docs_url="https://authjs.dev/getting-started/providers/google",
    dashboard_url="https://console.cloud.google.com/apis/credentials",
    skill="oauth-signin",
)

_GITHUB_SIGNIN = Integration(
    id="github-signin",
    label="GitHub sign-in",
    category="auth",
    capability="auth",
    wave=2,
    blurb="Sign in with GitHub",
    builds=("A Sign in with GitHub button through Auth.js", "Sessions in a signed cookie", "The user's GitHub name and avatar"),
    variables=(
        Var("GITHUB_CLIENT_ID", "Client ID", secret=False, placeholder="Ov23…"),
        Var("GITHUB_CLIENT_SECRET", "Client secret", placeholder="40 characters"),
        _auth_secret(),
    ),
    names=("github sign-in", "github signin", "sign in with github", "github login", "github oauth", "login with github"),
    formats=(
        ("GITHUB_CLIENT_ID", r"^(Ov2[0-9][A-Za-z0-9]{16}|Iv1\.[a-f0-9]{16}|Iv23[A-Za-z0-9]{16}|[a-f0-9]{20})$", "A GitHub client ID starts with Ov23 (or Iv1.)."),
        ("GITHUB_CLIENT_SECRET", r"^[a-f0-9]{40}$", "A GitHub client secret is 40 hexadecimal characters."),
        ("AUTH_SECRET", r"^\S{32,}$", "The session secret needs at least 32 characters — or leave it blank."),
    ),
    guide=_guide(
        ("Open GitHub → Settings → Developer settings → OAuth Apps.", "https://github.com/settings/developers"),
        "New OAuth App. Homepage http://localhost:3000; callback http://localhost:3000/api/auth/callback/github.",
        "Copy the Client ID, then Generate a new client secret and copy it — it's shown once.",
    ),
    docs_url="https://authjs.dev/getting-started/providers/github",
    dashboard_url="https://github.com/settings/developers",
    skill="oauth-signin",
)

# ── AI & LLMs ────────────────────────────────────────────────────────────────
_ANTHROPIC = Integration(
    id="anthropic",
    label="Anthropic",
    category="ai",
    capability="llm",
    wave=2,
    blurb="Claude models for chat and text",
    builds=("A chat assistant on Claude, streamed through an API route", "Summaries and rewrites", "The model is a setting (ANTHROPIC_MODEL)"),
    variables=(Var("ANTHROPIC_API_KEY", "API key", placeholder="sk-ant-…"), _model_var("ANTHROPIC_MODEL")),
    names=("anthropic", "claude"),
    soft=_LLM,
    formats=(("ANTHROPIC_API_KEY", r"^sk-ant-[A-Za-z0-9_\-]{20,}$", "An Anthropic API key starts with sk-ant-."),),
    check=HttpCheck(
        "GET",
        "https://api.anthropic.com/v1/models",
        "header:x-api-key",
        "ANTHROPIC_API_KEY",
        ("api.anthropic.com",),
        rules=(Rule((401, 403), "invalid", message="Anthropic didn't recognise that API key."),),
        models=_models_openai_style,
        headers=(("anthropic-version", "2023-06-01"),),
    ),
    guide=_guide(
        ("Open the Claude Console → API keys.", "https://console.anthropic.com/settings/keys"),
        "Create Key → name it, pick a workspace. Copy it (sk-ant-…) — it's shown once.",
        "Test it here, then pick the model the app should use.",
    ),
    docs_url="https://docs.anthropic.com/en/api",
    dashboard_url="https://console.anthropic.com/settings/keys",
    skill="anthropic-api",
)

_GEMINI = Integration(
    id="gemini",
    label="Google Gemini",
    category="ai",
    capability="llm",
    wave=2,
    blurb="Gemini models for text and images",
    builds=("Chat and text generation on Gemini, server-side", "Image understanding", "The model is a setting (GEMINI_MODEL)"),
    variables=(Var("GEMINI_API_KEY", "API key", placeholder="AIza…"), _model_var("GEMINI_MODEL")),
    names=("gemini", "google gemini"),
    soft=_LLM,
    formats=(("GEMINI_API_KEY", r"^AIza[0-9A-Za-z_\-]{30,}$", "A Gemini API key starts with AIza."),),
    check=HttpCheck(
        "GET",
        "https://generativelanguage.googleapis.com/v1beta/models",
        "query:key",
        "GEMINI_API_KEY",
        ("generativelanguage.googleapis.com",),
        rules=(
            # Gemini answers a bad key with a 400, not a 401.
            Rule((400,), "invalid", body="API key not valid", message="Google didn't recognise that API key."),
            Rule((401, 403), "invalid", message="Google refused that API key for Gemini."),
        ),
        models=_models_gemini,
    ),
    guide=_guide(
        ("Open Google AI Studio → Get API key.", "https://aistudio.google.com/app/apikey"),
        "Create API key → pick a project. Copy it (AIza…).",
        "Test it here, then pick the model the app should use.",
    ),
    docs_url="https://ai.google.dev/api",
    dashboard_url="https://aistudio.google.com/app/apikey",
    skill="gemini-api",
)

_GROQ = Integration(
    id="groq",
    label="Groq",
    category="ai",
    capability="llm",
    wave=2,
    blurb="Very fast open-model inference",
    builds=("Fast chat and text generation on open models", "OpenAI-compatible calls through the Groq SDK", "The model is a setting (GROQ_MODEL)"),
    variables=(Var("GROQ_API_KEY", "API key", placeholder="gsk_…"), _model_var("GROQ_MODEL")),
    names=("groq",),
    soft=_LLM,
    formats=(("GROQ_API_KEY", r"^gsk_[A-Za-z0-9]{20,}$", "A Groq API key starts with gsk_."),),
    check=HttpCheck(
        "GET",
        "https://api.groq.com/openai/v1/models",
        "bearer",
        "GROQ_API_KEY",
        ("api.groq.com",),
        rules=(Rule((401, 403), "invalid", message="Groq didn't recognise that API key."),),
        models=_models_openai_style,
    ),
    guide=_guide(
        ("Open the Groq console → API Keys.", "https://console.groq.com/keys"),
        "Create API Key → copy it (gsk_…) — it's shown once.",
        "Test it here, then pick the model the app should use.",
    ),
    docs_url="https://console.groq.com/docs/api-reference",
    dashboard_url="https://console.groq.com/keys",
    skill="groq-api",
)

_OPENROUTER = Integration(
    id="openrouter",
    label="OpenRouter",
    category="ai",
    capability="llm",
    wave=2,
    blurb="Many models behind one key",
    builds=("Chat on any model OpenRouter serves, through one key", "OpenAI-compatible calls", "The model is a setting (OPENROUTER_MODEL)"),
    variables=(Var("OPENROUTER_API_KEY", "API key", placeholder="sk-or-v1-…"), _model_var("OPENROUTER_MODEL")),
    names=("openrouter", "open router"),
    soft=_LLM,
    formats=(("OPENROUTER_API_KEY", r"^sk-or-v1-[A-Za-z0-9]{32,}$", "An OpenRouter API key starts with sk-or-v1-."),),
    check=HttpCheck(
        "GET",
        "https://openrouter.ai/api/v1/key",
        "bearer",
        "OPENROUTER_API_KEY",
        ("openrouter.ai",),
        rules=(Rule((401, 403), "invalid", message="OpenRouter didn't recognise that API key."),),
        models_url="https://openrouter.ai/api/v1/models",
    ),
    guide=_guide(
        ("Open OpenRouter → Keys.", "https://openrouter.ai/settings/keys"),
        "Create Key → set a credit limit if you like. Copy it (sk-or-v1-…).",
        "Test it here, then pick the model the app should use.",
    ),
    docs_url="https://openrouter.ai/docs",
    dashboard_url="https://openrouter.ai/settings/keys",
    skill="openrouter-api",
)

_REPLICATE = Integration(
    id="replicate",
    label="Replicate",
    category="ai",
    capability="media_ai",
    wave=2,
    blurb="Image, audio and video models",
    builds=("Image generation from a prompt", "Predictions run on the server and polled for the result", "Model versions pinned in code"),
    variables=(Var("REPLICATE_API_TOKEN", "API token", placeholder="r8_…"),),
    names=("replicate",),
    strong=("image generation", "generate images", "generates images", "ai images", "text to image", "text-to-image", "ai art"),
    formats=(("REPLICATE_API_TOKEN", r"^r8_[A-Za-z0-9]{20,}$", "A Replicate API token starts with r8_."),),
    check=HttpCheck(
        "GET",
        "https://api.replicate.com/v1/account",
        "bearer",
        "REPLICATE_API_TOKEN",
        ("api.replicate.com",),
        rules=(Rule((401, 403), "invalid", message="Replicate didn't recognise that API token."),),
    ),
    guide=_guide(
        ("Open Replicate → Account → API tokens.", "https://replicate.com/account/api-tokens"),
        "Create token → copy it (r8_…).",
        "Set up billing if the models you'll call need it.",
    ),
    docs_url="https://replicate.com/docs/reference/http",
    dashboard_url="https://replicate.com/account/api-tokens",
    skill="replicate-api",
)

_ELEVENLABS = Integration(
    id="elevenlabs",
    label="ElevenLabs",
    category="ai",
    capability="voice",
    wave=2,
    blurb="Text to speech and voices",
    builds=("Text to speech from the server, streamed to the page", "A voice picker from your voice library", "Audio cached so the same text isn't paid for twice"),
    variables=(Var("ELEVENLABS_API_KEY", "API key", placeholder="sk_…"),),
    names=("elevenlabs", "eleven labs"),
    strong=("text to speech", "text-to-speech", "voiceover", "voice over", "ai voice", "voice generation", "read aloud"),
    formats=(("ELEVENLABS_API_KEY", r"^(sk_)?[A-Za-z0-9]{32,}$", "An ElevenLabs API key starts with sk_."),),
    check=HttpCheck(
        "GET",
        "https://api.elevenlabs.io/v1/user",
        "header:xi-api-key",
        "ELEVENLABS_API_KEY",
        ("api.elevenlabs.io",),
        rules=(
            Rule((401,), "invalid", body="invalid_api_key", message="ElevenLabs didn't recognise that API key."),
            # A key scoped without user access is refused here and still works for speech.
            Rule((401, 403), "unchecked", message="This key can't read the account, so it couldn't be tested — scoped keys are like that."),
        ),
    ),
    guide=_guide(
        ("Open ElevenLabs → Developers → API Keys.", "https://elevenlabs.io/app/developers/api-keys"),
        "Create API Key → give it Text to Speech access. Copy it — it's shown once.",
        "Pick a voice in the Voice Library; the app chooses by voice id.",
    ),
    docs_url="https://elevenlabs.io/docs/api-reference",
    dashboard_url="https://elevenlabs.io/app/developers/api-keys",
    skill="elevenlabs-api",
)

# ── storage & media ──────────────────────────────────────────────────────────
def _expand_cloudinary(given: dict) -> dict:
    """`cloudinary://API_KEY:API_SECRET@CLOUD_NAME`, pasted into any field."""
    for value in given.values():
        m = re.match(r"^\s*(?:CLOUDINARY_URL\s*=\s*)?cloudinary://([^:\s]+):([^@\s]+)@([A-Za-z0-9_\-]+)\s*$", str(value or ""))
        if m:
            return {"CLOUDINARY_API_KEY": m.group(1), "CLOUDINARY_API_SECRET": m.group(2), "CLOUDINARY_CLOUD_NAME": m.group(3)}
    return {}


_CLOUDINARY = Integration(
    id="cloudinary",
    label="Cloudinary",
    category="storage",
    capability="storage",
    wave=2,
    blurb="Image and video upload, resizing, delivery",
    builds=("Signed uploads from the browser, signed on the server", "Resized, optimised images by URL", "An image library per user"),
    variables=(
        Var("CLOUDINARY_CLOUD_NAME", "Cloud name", secret=False, placeholder="your-cloud", help="Or paste the whole CLOUDINARY_URL into any field."),
        Var("NEXT_PUBLIC_CLOUDINARY_CLOUD_NAME", "Cloud name (browser)", secret=False, side="client", copy_of="CLOUDINARY_CLOUD_NAME"),
        Var("CLOUDINARY_API_KEY", "API key", secret=False, placeholder="15 digits"),
        Var("CLOUDINARY_API_SECRET", "API secret"),
    ),
    names=("cloudinary",),
    strong=("image upload", "image uploads", "photo upload", "photo uploads", "image hosting", "profile pictures", "avatar upload", "upload photos", "upload images"),
    expand=_expand_cloudinary,
    formats=(
        ("CLOUDINARY_CLOUD_NAME", r"^[A-Za-z0-9_\-]{2,}$", "The cloud name is the short name at the top of your Cloudinary dashboard."),
        ("CLOUDINARY_API_KEY", r"^\d{10,20}$", "A Cloudinary API key is a long number."),
        ("CLOUDINARY_API_SECRET", r"^[A-Za-z0-9_\-]{20,}$", "A Cloudinary API secret is about 27 characters."),
    ),
    check=HttpCheck(
        "GET",
        "https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/ping",
        "basic_pair:CLOUDINARY_API_KEY",
        "CLOUDINARY_API_SECRET",
        ("api.cloudinary.com",),
        rules=(
            Rule((401,), "invalid", message="Cloudinary didn't accept that API key and secret together."),
            Rule((404,), "id_wrong", message="Cloudinary has no cloud by that name. Check the Cloud name.", field="CLOUDINARY_CLOUD_NAME"),
        ),
    ),
    guide=_guide(
        ("Open the Cloudinary Console → Settings → API Keys.", "https://console.cloudinary.com/settings/api-keys"),
        "Copy the Cloud name, the API Key and the API Secret — or the whole API environment variable.",
        "For browser uploads, Settings → Upload: note an upload preset, or let the server sign uploads.",
    ),
    docs_url="https://cloudinary.com/documentation",
    dashboard_url="https://console.cloudinary.com/settings/api-keys",
    skill="cloudinary-media",
)


def _uploadthing_key(token: str) -> Optional[str]:
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4)).decode()
        data = json.loads(raw)
        key = data.get("apiKey")
        return key if isinstance(key, str) and key else None
    except Exception:  # noqa: BLE001 - not a token we can read
        return None


_UPLOADTHING = Integration(
    id="uploadthing",
    label="UploadThing",
    category="storage",
    capability="storage",
    wave=2,
    blurb="File uploads for Next.js",
    builds=("A file router with size and type limits per route", "Upload buttons and drop zones", "Who uploaded what, kept with your data"),
    variables=(Var("UPLOADTHING_TOKEN", "Token", placeholder="eyJhcGlLZXkiOi…"),),
    names=("uploadthing", "upload thing"),
    strong=("file upload", "file uploads", "upload files", "document upload", "document uploads", "pdf upload"),
    formats=(("UPLOADTHING_TOKEN", r"^[A-Za-z0-9+/=]{40,}$", "An UploadThing token is a long base64 string (eyJ…)."),),
    cross=lambda v: None if _uploadthing_key(v.get("UPLOADTHING_TOKEN", "")) else (
        "UPLOADTHING_TOKEN",
        "That doesn't read as an UploadThing token. Copy UPLOADTHING_TOKEN from the API Keys page again.",
    ),
    check=HttpCheck(
        "POST",
        "https://api.uploadthing.com/v6/listFiles",
        "none",
        "UPLOADTHING_TOKEN",
        ("api.uploadthing.com",),
        rules=(Rule((401, 403), "invalid", message="UploadThing didn't accept that token."),),
        headers=(("x-uploadthing-api-key", "{ut_key}"),),
        body="{}",
        body_type="json",
        derive=lambda v: {"ut_key": _uploadthing_key(v.get("UPLOADTHING_TOKEN", "")) or ""},
    ),
    guide=_guide(
        ("Open the UploadThing dashboard → your app → API Keys.", "https://uploadthing.com/dashboard"),
        "Copy the UPLOADTHING_TOKEN value (the V7 token, not the secret).",
        "Set file size and type limits in the code's file router.",
    ),
    docs_url="https://docs.uploadthing.com",
    dashboard_url="https://uploadthing.com/dashboard",
    skill="uploadthing-files",
)


def _s3_check(values: dict) -> Checked:
    """Signed, so not one plain request: done with boto3 when it's installed."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return _unchecked("", "The AWS signer (boto3) isn't installed on this server, so the keys couldn't be tested.")
    session = boto3.session.Session(
        aws_access_key_id=values.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=values.get("AWS_SECRET_ACCESS_KEY"),
        region_name=values.get("AWS_REGION"),
    )
    from botocore.config import Config  # type: ignore

    cfg = Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 1})
    try:
        session.client("sts", config=cfg).get_caller_identity()
    except ClientError as e:
        return Checked(CheckResult(FAILED, f"AWS refused those keys ({e.response.get('Error', {}).get('Code', 'error')}).", reason="invalid", name="AWS_SECRET_ACCESS_KEY", host="sts.amazonaws.com"))
    except BotoCoreError:
        return _unchecked("sts.amazonaws.com", "We couldn't reach AWS from this server.")
    try:
        session.client("s3", config=cfg).head_bucket(Bucket=values.get("S3_BUCKET"))
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchBucket"):
            return Checked(CheckResult(FAILED, "There's no bucket by that name in this account. Check the bucket name.", reason="id_wrong", name="S3_BUCKET", host="s3.amazonaws.com"))
        if code in ("403", "AccessDenied"):
            return Checked(CheckResult(FAILED, "The keys work, but they aren't allowed to use that bucket. Give the IAM user access to it.", reason="invalid", name="S3_BUCKET", host="s3.amazonaws.com"))
        return _unchecked("s3.amazonaws.com", f"AWS answered {code or 'an error'} for the bucket.")
    except BotoCoreError:
        return _unchecked("s3.amazonaws.com", "We couldn't reach S3 from this server.")
    return Checked(CheckResult(CONNECTED, "Connected to AWS S3", reason="ok", host="s3.amazonaws.com"))


_S3 = Integration(
    id="s3",
    label="AWS S3",
    category="storage",
    capability="storage",
    wave=2,
    blurb="Object storage for files",
    builds=("Uploads straight to S3 with presigned URLs", "Private files served by short-lived links", "One bucket, keys scoped to it"),
    variables=(
        Var("AWS_REGION", "Region", secret=False, placeholder="us-east-1", kind="text"),
        Var("AWS_ACCESS_KEY_ID", "Access key ID", placeholder="AKIA…"),
        Var("AWS_SECRET_ACCESS_KEY", "Secret access key", placeholder="40 characters"),
        Var("S3_BUCKET", "Bucket", secret=False, placeholder="my-app-uploads", kind="text"),
    ),
    names=("s3", "amazon s3", "aws s3"),
    soft=("file storage", "object storage"),
    formats=(
        ("AWS_REGION", r"^[a-z]{2}(-gov)?-[a-z]+-\d$", "A region looks like us-east-1 or eu-west-2."),
        ("AWS_ACCESS_KEY_ID", r"^(AKIA|ASIA)[A-Z0-9]{16}$", "An access key ID is 20 characters and starts with AKIA."),
        ("AWS_SECRET_ACCESS_KEY", r"^[A-Za-z0-9/+=]{40}$", "A secret access key is 40 characters long."),
        ("S3_BUCKET", r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$", "A bucket name is 3–63 lowercase letters, digits, dots and dashes."),
    ),
    custom=_s3_check,
    guide=_guide(
        ("Open the S3 console and create a bucket (or pick one).", "https://s3.console.aws.amazon.com/s3/buckets"),
        "IAM → Users → create a user with access to that bucket only.",
        "Security credentials → Create access key → copy both halves; the secret is shown once.",
    ),
    docs_url="https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html",
    dashboard_url="https://s3.console.aws.amazon.com/s3/buckets",
    skill="s3-storage",
)

# ── messaging ────────────────────────────────────────────────────────────────
_TWILIO = Integration(
    id="twilio",
    label="Twilio",
    category="messaging",
    capability="sms",
    wave=2,
    blurb="SMS and WhatsApp messages",
    builds=("SMS confirmations and reminders from the server", "WhatsApp messages through the same API", "Phone numbers validated before sending"),
    variables=(
        Var("TWILIO_ACCOUNT_SID", "Account SID", secret=False, placeholder="AC…"),
        Var("TWILIO_AUTH_TOKEN", "Auth token", placeholder="32 characters"),
        Var("TWILIO_PHONE_NUMBER", "Send from", secret=False, required=False, placeholder="+15551234567", kind="text", help="A number on your Twilio account, in +country format."),
    ),
    names=("twilio",),
    strong=("sms", "text message", "text messages", "whatsapp", "sms reminders", "sms notifications", "otp by sms"),
    formats=(
        ("TWILIO_ACCOUNT_SID", r"^AC[a-f0-9]{32}$", "An Account SID is AC followed by 32 characters."),
        ("TWILIO_AUTH_TOKEN", r"^[a-f0-9]{32}$", "An auth token is 32 characters."),
        ("TWILIO_PHONE_NUMBER", r"^\+[1-9]\d{6,14}$", "A phone number starts with + and the country code, like +15551234567."),
    ),
    check=HttpCheck(
        "GET",
        "https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}.json",
        "basic_pair:TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        ("api.twilio.com",),
        rules=(
            Rule((401,), "invalid", message="Twilio didn't accept that Account SID and auth token together."),
            Rule((404,), "id_wrong", message="Twilio has no account with that SID. Check the Account SID.", field="TWILIO_ACCOUNT_SID"),
        ),
    ),
    guide=_guide(
        ("Open the Twilio Console home page.", "https://console.twilio.com/"),
        "Account Info: copy the Account SID, then Show and copy the Auth Token.",
        "Phone Numbers → Manage → Active numbers: copy the number to send from.",
    ),
    docs_url="https://www.twilio.com/docs/messaging/api",
    dashboard_url="https://console.twilio.com/",
    skill="twilio-messaging",
)

_SLACK = Integration(
    id="slack",
    label="Slack",
    category="messaging",
    capability="sms",
    wave=2,
    blurb="Post to Slack channels from your app",
    builds=("Notifications posted to a channel when something happens", "Rich messages with blocks", "The channel is a setting, not hardcoded"),
    variables=(
        Var("SLACK_BOT_TOKEN", "Bot token", placeholder="xoxb-…"),
        Var("SLACK_CHANNEL_ID", "Channel ID", secret=False, required=False, placeholder="C0123456789", kind="text", help="Optional. Right-click the channel → View details → the ID at the bottom."),
    ),
    names=("slack",),
    strong=("slack notification", "slack notifications", "post to slack", "slack alerts", "slack bot"),
    formats=(
        ("SLACK_BOT_TOKEN", r"^xoxb-[A-Za-z0-9\-]{20,}$", "A Slack bot token starts with xoxb-."),
        ("SLACK_CHANNEL_ID", r"^[CGD][A-Z0-9]{8,}$", "A channel ID looks like C0123456789."),
    ),
    check=HttpCheck(
        "POST",
        "https://slack.com/api/auth.test",
        "bearer",
        "SLACK_BOT_TOKEN",
        ("slack.com",),
        rules=(
            # Slack answers a bad token with a 200 and the error in the body.
            Rule((200,), "invalid", body='"ok":false', message="Slack didn't accept that bot token."),
            Rule((401, 403), "invalid", message="Slack didn't accept that bot token."),
        ),
    ),
    guide=_guide(
        ("Open api.slack.com → Your Apps → Create New App (or open yours).", "https://api.slack.com/apps"),
        "OAuth & Permissions → add the chat:write scope → Install to Workspace.",
        "Copy the Bot User OAuth Token (xoxb-…), and invite the bot to the channel.",
    ),
    docs_url="https://api.slack.com/methods/chat.postMessage",
    dashboard_url="https://api.slack.com/apps",
    skill="slack-bot",
)

# ── maps ─────────────────────────────────────────────────────────────────────
_MAP_PHRASES = ("store locator", "interactive map", "show on a map", "on a map", "map view", "directions", "geocoding", "nearby places")

_GOOGLE_MAPS = Integration(
    id="google-maps",
    label="Google Maps",
    category="maps",
    capability="maps",
    wave=2,
    blurb="Maps, places and geocoding",
    builds=("An interactive map with markers", "Address search and geocoding", "Directions between places"),
    variables=(Var("NEXT_PUBLIC_GOOGLE_MAPS_API_KEY", "API key", secret=False, side="client", placeholder="AIza…", help="Public by design: restrict it to your site's address in Google Cloud."),),
    names=("google maps", "google map"),
    strong=_MAP_PHRASES,
    formats=(("NEXT_PUBLIC_GOOGLE_MAPS_API_KEY", r"^AIza[0-9A-Za-z_\-]{30,}$", "A Google Maps API key starts with AIza."),),
    check=HttpCheck(
        "GET",
        "https://maps.googleapis.com/maps/api/geocode/json?address=London",
        "query:key",
        "NEXT_PUBLIC_GOOGLE_MAPS_API_KEY",
        ("maps.googleapis.com",),
        rules=(
            # A key restricted to your site's address is refused from this server: valid, and right.
            Rule((200,), "restricted", body="referer restrictions", message="Connected with a key restricted to your site."),
            # A bad key is a 200 with REQUEST_DENIED in the body.
            Rule((200,), "invalid", body="REQUEST_DENIED", message="Google refused that key for the Maps APIs (or Geocoding isn't enabled)."),
        ),
    ),
    guide=_guide(
        ("Open Google Cloud → Google Maps Platform → Keys & Credentials.", "https://console.cloud.google.com/google/maps-apis/credentials"),
        "Create credentials → API key. Enable the Maps JavaScript and Geocoding APIs.",
        "Restrict the key to your site's address — it's public in the browser.",
    ),
    docs_url="https://developers.google.com/maps/documentation",
    dashboard_url="https://console.cloud.google.com/google/maps-apis/credentials",
    skill="google-maps",
)

_MAPBOX = Integration(
    id="mapbox",
    label="Mapbox",
    category="maps",
    capability="maps",
    wave=2,
    blurb="Custom maps and directions",
    builds=("A styled interactive map with markers", "Search for places", "Routes and directions"),
    variables=(Var("NEXT_PUBLIC_MAPBOX_TOKEN", "Public token", secret=False, side="client", placeholder="pk.…", help="A public token (pk.). A secret token (sk.) is refused here."),),
    names=("mapbox",),
    soft=_MAP_PHRASES,
    formats=(("NEXT_PUBLIC_MAPBOX_TOKEN", r"^pk\.[A-Za-z0-9._\-]{20,}$", "A Mapbox public token starts with pk."),),
    check=HttpCheck(
        "GET",
        "https://api.mapbox.com/tokens/v2",
        "query:access_token",
        "NEXT_PUBLIC_MAPBOX_TOKEN",
        ("api.mapbox.com",),
        rules=(
            Rule((200, 401), "invalid", body="TokenInvalid", message="Mapbox didn't recognise that token."),
            Rule((200, 401), "invalid", body="TokenMalformed", message="Mapbox couldn't read that token."),
            Rule((200, 401), "invalid", body="TokenExpired", message="That Mapbox token has expired."),
            Rule((200,), "ok", body="TokenValid"),
        ),
    ),
    guide=_guide(
        ("Open Mapbox → Account → Tokens.", "https://account.mapbox.com/access-tokens/"),
        "Copy the Default public token (pk.…) — or create one with URL restrictions.",
        "Never paste a secret token (sk.…): this one goes in the browser.",
    ),
    docs_url="https://docs.mapbox.com/mapbox-gl-js/",
    dashboard_url="https://account.mapbox.com/access-tokens/",
    skill="mapbox-maps",
)

# ── analytics & monitoring ───────────────────────────────────────────────────
_POSTHOG = Integration(
    id="posthog",
    label="PostHog",
    category="analytics",
    capability="analytics",
    wave=2,
    blurb="Product analytics and feature flags",
    builds=("Page views and product events, captured client-side", "Feature flags read on the page", "Users identified after sign-in"),
    variables=(
        Var("NEXT_PUBLIC_POSTHOG_KEY", "Project API key", secret=False, side="client", placeholder="phc_…"),
        Var("NEXT_PUBLIC_POSTHOG_HOST", "Region", secret=False, side="client", options=("https://us.i.posthog.com", "https://eu.i.posthog.com"), kind="text"),
    ),
    names=("posthog", "post hog"),
    strong=("product analytics", "feature flags", "session replay", "funnels"),
    soft=("analytics",),
    formats=(("NEXT_PUBLIC_POSTHOG_KEY", r"^phc_[A-Za-z0-9]{20,}$", "A PostHog project API key starts with phc_."),),
    guide=_guide(
        ("Open PostHog → Project settings.", "https://us.posthog.com/settings/project"),
        "Copy the Project API key (phc_…) — it's public and ingest-only.",
        "Choose the region your project is in: US or EU.",
    ),
    docs_url="https://posthog.com/docs/libraries/next-js",
    dashboard_url="https://us.posthog.com/settings/project",
    skill="posthog-analytics",
)

_SENTRY = Integration(
    id="sentry",
    label="Sentry",
    category="analytics",
    capability="errors",
    wave=2,
    blurb="Error tracking and performance",
    builds=("Errors captured from the browser and the server", "Source maps uploaded at build time", "Releases tagged so errors point at the code"),
    variables=(
        Var("NEXT_PUBLIC_SENTRY_DSN", "DSN", secret=False, side="client", placeholder="https://…@o123.ingest.sentry.io/456", kind="url"),
        Var("SENTRY_AUTH_TOKEN", "Auth token", required=False, placeholder="sntrys_…", help="Optional. Only for uploading source maps at build time."),
    ),
    names=("sentry",),
    strong=("error tracking", "crash reporting", "error monitoring", "exception tracking"),
    formats=(
        ("NEXT_PUBLIC_SENTRY_DSN", r"^https://[a-f0-9]{32}@o\d+\.ingest\.([a-z]{2}\.)?sentry\.io/\d+$", "A DSN looks like https://…@o123.ingest.sentry.io/456."),
        ("SENTRY_AUTH_TOKEN", r"^sntr[yu]s_[A-Za-z0-9_=+/\-]{20,}$", "A Sentry auth token starts with sntrys_."),
    ),
    check=HttpCheck(
        "GET",
        "https://sentry.io/api/0/organizations/",
        "bearer",
        "SENTRY_AUTH_TOKEN",
        ("sentry.io",),
        rules=(Rule((401, 403), "invalid", message="Sentry didn't recognise that auth token."),),
        optional_key=True,
    ),
    guide=_guide(
        ("Open Sentry → Settings → Projects → your project → Client Keys (DSN).", "https://sentry.io/settings/projects/"),
        "Copy the DSN — it's public by design.",
        "Optional: Settings → Auth Tokens → create one for source map uploads (sntrys_…).",
    ),
    docs_url="https://docs.sentry.io/platforms/javascript/guides/nextjs/",
    dashboard_url="https://sentry.io/settings/projects/",
    skill="sentry-errors",
)

# ── search ───────────────────────────────────────────────────────────────────
_ALGOLIA = Integration(
    id="algolia",
    label="Algolia",
    category="search",
    capability="search",
    wave=2,
    blurb="Instant search for your content",
    builds=("Search as you type, from the browser with the search-only key", "Records indexed from the server with the admin key", "Facets and filters"),
    variables=(
        Var("NEXT_PUBLIC_ALGOLIA_APP_ID", "Application ID", secret=False, side="client", placeholder="ABC123DEF4"),
        Var("NEXT_PUBLIC_ALGOLIA_SEARCH_KEY", "Search-only API key", secret=False, side="client", placeholder="32 characters", help="The search-only key. Never the admin key."),
        Var("ALGOLIA_ADMIN_KEY", "Admin API key", placeholder="32 characters", help="Server only: it can change your indexes."),
    ),
    names=("algolia",),
    strong=("instant search", "search as you type", "typo-tolerant search", "faceted search"),
    formats=(
        ("NEXT_PUBLIC_ALGOLIA_APP_ID", r"^[A-Z0-9]{10}$", "An Algolia Application ID is 10 capital letters and digits."),
        ("NEXT_PUBLIC_ALGOLIA_SEARCH_KEY", r"^[a-f0-9]{32}$", "An Algolia API key is 32 characters."),
        ("ALGOLIA_ADMIN_KEY", r"^[a-f0-9]{32}$", "An Algolia API key is 32 characters."),
    ),
    cross=lambda v: (
        "NEXT_PUBLIC_ALGOLIA_SEARCH_KEY",
        "That's the Admin API key — it can change and delete your indexes, and this field goes in the browser. "
        "Paste the Search-Only API Key here.",
    )
    if v.get("NEXT_PUBLIC_ALGOLIA_SEARCH_KEY") and v.get("NEXT_PUBLIC_ALGOLIA_SEARCH_KEY") == v.get("ALGOLIA_ADMIN_KEY")
    else None,
    check=HttpCheck(
        "GET",
        "https://{NEXT_PUBLIC_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes",
        "header:X-Algolia-API-Key",
        "ALGOLIA_ADMIN_KEY",
        ("*.algolia.net",),
        rules=(Rule((403, 401), "invalid", message="Algolia didn't accept that Admin API key for this application."),),
        headers=(("X-Algolia-Application-Id", "{NEXT_PUBLIC_ALGOLIA_APP_ID}"),),
        id_var="NEXT_PUBLIC_ALGOLIA_APP_ID",
    ),
    guide=_guide(
        ("Open the Algolia dashboard → Settings → API Keys.", "https://dashboard.algolia.com/account/api-keys/all"),
        "Copy the Application ID and the Search-Only API Key (public, for the browser).",
        "Copy the Admin API Key for the server — it never goes in the browser.",
    ),
    docs_url="https://www.algolia.com/doc/",
    dashboard_url="https://dashboard.algolia.com/account/api-keys/all",
    skill="algolia-search",
)

# ── cache & realtime ─────────────────────────────────────────────────────────
_UPSTASH = Integration(
    id="upstash",
    label="Upstash Redis",
    category="realtime",
    capability="cache",
    wave=2,
    blurb="Serverless Redis for caching and rate limits",
    builds=("Rate limiting on API routes", "A cache for slow or paid calls", "Short-lived state over HTTP — no connection pool"),
    variables=(
        Var("UPSTASH_REDIS_REST_URL", "REST URL", secret=False, placeholder="https://your-db.upstash.io", kind="url"),
        Var("UPSTASH_REDIS_REST_TOKEN", "REST token"),
    ),
    names=("upstash",),
    strong=("rate limiting", "rate limit", "rate-limit", "redis cache"),
    soft=("caching", "cache"),
    formats=(
        ("UPSTASH_REDIS_REST_URL", r"^https://[a-z0-9\-]+\.upstash\.io/?$", "The REST URL looks like https://your-db.upstash.io."),
        ("UPSTASH_REDIS_REST_TOKEN", r"^[A-Za-z0-9_=\-]{20,}$", "The REST token is a long string — copy it from the REST API section."),
    ),
    check=HttpCheck(
        "GET",
        "{UPSTASH_REDIS_REST_URL}/ping",
        "bearer",
        "UPSTASH_REDIS_REST_TOKEN",
        ("*.upstash.io",),
        rules=(
            Rule((200,), "ok", body="PONG"),
            Rule((401, 403), "invalid", message="Upstash didn't accept that REST token for this database."),
        ),
        id_var="UPSTASH_REDIS_REST_URL",
    ),
    guide=_guide(
        ("Open the Upstash console → Redis → your database.", "https://console.upstash.com/redis"),
        "REST API section → copy UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN.",
        "Pick the region closest to where the app runs.",
    ),
    docs_url="https://upstash.com/docs/redis/features/restapi",
    dashboard_url="https://console.upstash.com/redis",
    skill="upstash-redis",
)


WAVE2: tuple[Integration, ...] = (
    _PAYPAL,
    _RAZORPAY,
    _LEMON,
    _SENDGRID,
    _POSTMARK,
    _MAILGUN,
    _AUTH0,
    _GOOGLE_SIGNIN,
    _GITHUB_SIGNIN,
    _ANTHROPIC,
    _GEMINI,
    _GROQ,
    _OPENROUTER,
    _REPLICATE,
    _ELEVENLABS,
    _CLOUDINARY,
    _UPLOADTHING,
    _S3,
    _TWILIO,
    _SLACK,
    _GOOGLE_MAPS,
    _MAPBOX,
    _POSTHOG,
    _SENTRY,
    _ALGOLIA,
    _UPSTASH,
)
