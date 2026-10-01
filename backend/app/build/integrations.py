"""App connectors: the third-party services a generated app uses at run time (#59).

The database gate (`app.build.dbconnect`) solved this for one service: a table of the
variable *names* the code reads, a format check before anything touches the network,
a live read-only test, and a value that is stored encrypted and never reaches a
model. This module is the same idea for every other service, as one registry:

  **The catalog.** One row per connector: its category and capability, what the
  crew builds with it, the variables (and which side reads each — the server, or the
  browser bundle, where anything is public), a guide to where the key lives, and a
  declarative check. Connectors that aren't wired end to end yet are in the catalog
  too, as "coming soon", so the page shows where it is going.

  **Parsing.** What people paste: quotes, a `NAME=` prefix, a publishable key in the
  secret field, a secret key in a browser variable, a test secret beside a live
  publishable key. Caught here, before any request.

  **The check.** Data, not code: one `HttpCheck` per connector, whose outcomes are
  predicates on the status *and* the body. Only hosts on the connector's own
  allowlist are ever called. A timeout or a 5xx is "saved, not tested" — never
  "connected".

  **Relevance.** Which connectors a build uses, worked out from the idea and from
  Atlas's design with no model call — deterministic like `app.skills.selection`, and
  conservative: a connected service is never forced into a build that doesn't need it.

Nothing here stores anything: `app.core.connectors_store` (the account) and
`app.core.project_secrets` (a project's own keys) do that.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

import httpx

from app.build.dbconnect import CONNECTED, FAILED, UNCHECKED, CheckResult, Problem, Var, hint
from app.core.logging import get_logger

log = get_logger(__name__)

#: How long any one check may take, end to end.
TIMEOUT_SECONDS = 5.0

#: Tests swap in an `httpx.MockTransport`; nothing else sets it.
transport: Optional[httpx.BaseTransport] = None

#: Waves: 1 is wired end to end, 2 is next, 3 is on the roadmap. Only wave 1 can be
#: connected; the rest are shown so a search finds them and says when.
CONNECTABLE_WAVE = 1

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("payments", "Payments"),
    ("email", "Email"),
    ("auth", "Auth"),
    ("ai", "AI & LLMs"),
    ("storage", "Storage & media"),
    ("messaging", "Messaging"),
    ("maps", "Maps"),
    ("analytics", "Analytics"),
    ("search", "Search"),
    ("realtime", "Cache & realtime"),
    ("cms", "CMS"),
    ("commerce", "Commerce & CRM"),
)
CATEGORY_LABELS = dict(CATEGORIES)

#: What a capability is called when a reason or a chip names it.
CAPABILITY_LABELS: dict[str, str] = {
    "payments": "payments",
    "email": "email",
    "auth": "sign-in",
    "llm": "AI",
    "storage": "file storage",
    "sms": "messages",
    "maps": "maps",
    "analytics": "analytics",
    "errors": "error tracking",
    "search": "search",
    "vector": "vector search",
    "cache": "caching",
    "realtime": "realtime",
    "cms": "content",
    "commerce": "commerce",
    "crm": "CRM",
}


# ── checks, as data ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rule:
    """One outcome of a check: when the status (and optionally the body) match."""

    statuses: tuple[int, ...]
    #: `ok`, `restricted` (a valid key with fewer rights — still connected),
    #: `invalid` (the key is wrong) or `id_wrong` (an account or app id is).
    outcome: str
    #: A substring the body must contain, for providers that answer an error with
    #: a 200 or reuse one status for two meanings (Resend's 401).
    body: Optional[str] = None
    message: str = ""


@dataclass(frozen=True)
class HttpCheck:
    method: str
    url: str
    #: `basic` (key as user), `bearer`, or `header:<Name>`.
    auth: str
    #: Which variable carries the credential.
    key_var: str
    hosts: tuple[str, ...]
    rules: tuple[Rule, ...] = ()
    #: Pull the model list out of a 200, for the AI connectors' model picker.
    models: Optional[Callable[[dict], list[str]]] = None
    #: Read the mode from a 200 when the key alone can't say it.
    live: Optional[Callable[[dict], Optional[bool]]] = None


@dataclass(frozen=True)
class GuideStep:
    text: str
    #: A link this step opens, e.g. the dashboard page the key is on.
    url: str = ""


@dataclass(frozen=True)
class Integration:
    id: str
    label: str
    category: str
    capability: str
    wave: int
    #: One line on what the crew builds with it — the card's subtitle.
    blurb: str
    #: Three or four bullets for the detail view.
    builds: tuple[str, ...] = ()
    variables: tuple[Var, ...] = ()
    #: Words that name this connector outright ("stripe"). Always beats a capability.
    names: tuple[str, ...] = ()
    #: Capability phrases that pick this connector even when it isn't connected —
    #: the build then asks for it. Kept to phrases that can only mean this.
    strong: tuple[str, ...] = ()
    #: Phrases that pick it only when it's already connected. "Sign in" is in most
    #: ideas, and a crew that writes its own login shouldn't be stopped to ask for Clerk.
    soft: tuple[str, ...] = ()
    #: Names of alternatives that, named in the idea or the design, take the
    #: capability away from this connector ("next-auth" means no Clerk).
    rivals: tuple[str, ...] = ()
    check: Optional[HttpCheck] = None
    guide: tuple[GuideStep, ...] = ()
    docs_url: str = ""
    dashboard_url: str = ""
    #: Value prefixes that mean "live mode" — real money, real email.
    live_prefixes: tuple[str, ...] = ()
    #: Whether the service has a test mode at all. Resend and OpenAI don't.
    has_test_mode: bool = False
    #: The bundled skill the crew gets when a build uses it.
    skill: str = ""

    @property
    def connectable(self) -> bool:
        return self.wave <= CONNECTABLE_WAVE

    @property
    def names_tuple(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.variables)

    def var(self, name: str) -> Optional[Var]:
        return next((v for v in self.variables if v.name == name), None)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "capability": self.capability,
            "capability_label": CAPABILITY_LABELS.get(self.capability, self.capability),
            "wave": self.wave,
            "connectable": self.connectable,
            "blurb": self.blurb,
            "builds": list(self.builds),
            "variables": [v.as_dict() for v in self.variables],
            "guide": [{"text": s.text, "url": s.url} for s in self.guide],
            "docs_url": self.docs_url,
            "dashboard_url": self.dashboard_url,
            "has_test_mode": self.has_test_mode,
        }


# ── wave 1: wired end to end ─────────────────────────────────────────────────
def _openai_models(body: dict) -> list[str]:
    ids = [str(m.get("id")) for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")]
    return sorted(set(ids))


def _stripe_live(body: dict) -> Optional[bool]:
    live = body.get("livemode")
    return live if isinstance(live, bool) else None


_STRIPE = Integration(
    id="stripe",
    label="Stripe",
    category="payments",
    capability="payments",
    wave=1,
    blurb="Checkout, subscriptions and invoices",
    builds=(
        "A checkout page that takes real test payments",
        "Subscription plans with a customer portal",
        "Payment status by polling — no webhook needed to start",
        "Webhook handling once you add a signing secret",
    ),
    variables=(
        Var(
            "STRIPE_SECRET_KEY",
            "Secret key",
            placeholder="sk_test_…",
            help="A restricted key (rk_test_…) works too, and is safer.",
        ),
        Var(
            "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY",
            "Publishable key",
            secret=False,
            placeholder="pk_test_…",
            side="client",
        ),
        Var(
            "STRIPE_WEBHOOK_SECRET",
            "Webhook signing secret",
            required=False,
            placeholder="whsec_…",
            help="Optional. Without it the app polls for payment status instead.",
        ),
    ),
    names=("stripe",),
    strong=(
        "payment", "payments", "checkout", "subscription", "subscriptions",
        "billing", "pay online", "take payments", "accept payments", "paid plan",
        "paid plans", "recurring billing", "invoice payments",
    ),
    rivals=("paypal", "razorpay", "paddle", "lemon squeezy", "lemonsqueezy", "square payments", "braintree"),
    check=HttpCheck(
        "GET",
        "https://api.stripe.com/v1/balance",
        "basic",
        "STRIPE_SECRET_KEY",
        ("api.stripe.com",),
        rules=(
            Rule((401,), "invalid", message="Stripe didn't recognise that secret key."),
            # A restricted key without the balance permission: valid, just narrow.
            Rule((403,), "restricted", message="Connected with a restricted key."),
        ),
        live=_stripe_live,
    ),
    guide=(
        GuideStep("Open the Stripe dashboard and switch on Test mode (top right).", "https://dashboard.stripe.com/test/apikeys"),
        GuideStep("Go to Developers → API keys. Copy the Publishable key (pk_test_…)."),
        GuideStep("Reveal the Secret key (sk_test_…) and copy it — or create a restricted key for less risk."),
        GuideStep("Optional: Developers → Webhooks → add an endpoint, then copy its signing secret (whsec_…)."),
    ),
    docs_url="https://docs.stripe.com/keys",
    dashboard_url="https://dashboard.stripe.com/test/apikeys",
    live_prefixes=("sk_live_", "rk_live_", "pk_live_"),
    has_test_mode=True,
    skill="stripe-payments",
)

_RESEND = Integration(
    id="resend",
    label="Resend",
    category="email",
    capability="email",
    wave=1,
    blurb="Transactional email: confirmations, receipts, magic links",
    builds=(
        "Booking and order confirmation emails",
        "Password reset and magic-link sign-in emails",
        "Receipts and notifications from the server",
    ),
    variables=(
        Var("RESEND_API_KEY", "API key", placeholder="re_…", help="A sending-only key is enough."),
        Var(
            "EMAIL_FROM",
            "Send from",
            secret=False,
            required=False,
            placeholder="onboarding@resend.dev",
            kind="text",
            help="Use onboarding@resend.dev until you verify your own domain.",
        ),
    ),
    names=("resend",),
    strong=(
        "transactional email", "transactional emails", "email confirmation",
        "email confirmations", "confirmation email", "confirmation emails",
        "magic link", "magic links", "newsletter", "send email", "send emails",
        "sends email", "sends emails", "email notification", "email notifications",
        "email receipts", "receipt email", "receipt emails", "welcome email",
        "welcome emails", "password reset email", "email reminders", "reminder emails",
    ),
    rivals=("sendgrid", "postmark", "mailgun", "brevo", "amazon ses", "aws ses", "nodemailer", "smtp"),
    check=HttpCheck(
        "GET",
        "https://api.resend.com/domains",
        "bearer",
        "RESEND_API_KEY",
        ("api.resend.com",),
        rules=(
            # A sending-only key can't list domains, and says so: it's a real key.
            Rule((401,), "restricted", body="restricted_api_key", message="Connected with a sending-only key."),
            # Resend answers a bad key with 400, not 401.
            Rule((400,), "invalid", body="validation_error", message="Resend didn't recognise that API key."),
            Rule((400, 401, 403), "invalid", message="Resend didn't recognise that API key."),
        ),
    ),
    guide=(
        GuideStep("Open Resend and go to API Keys.", "https://resend.com/api-keys"),
        GuideStep("Create API Key → Sending access is enough. Copy it (re_…) — it's shown once."),
        GuideStep("Until you verify a domain, send from onboarding@resend.dev (to your own address only)."),
    ),
    docs_url="https://resend.com/docs",
    dashboard_url="https://resend.com/api-keys",
    skill="resend-email",
)

_CLERK = Integration(
    id="clerk",
    label="Clerk",
    category="auth",
    capability="auth",
    wave=1,
    blurb="Sign-in, sign-up and user accounts",
    builds=(
        "Sign-in and sign-up pages, with social logins you switch on in Clerk",
        "Protected pages and API routes through middleware",
        "A user menu and profile management",
    ),
    variables=(
        Var("CLERK_SECRET_KEY", "Secret key", placeholder="sk_test_…"),
        Var(
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
            "Publishable key",
            secret=False,
            placeholder="pk_test_…",
            side="client",
        ),
    ),
    names=("clerk",),
    strong=(),
    soft=(
        "sign in", "sign-in", "signin", "sign up", "sign-up", "signup", "log in", "login",
        "user accounts", "user account", "authentication", "auth",
    ),
    rivals=(
        "next-auth", "nextauth", "auth.js", "authjs", "auth0", "supabase auth",
        "firebase auth", "custom jwt", "jwt", "jsonwebtoken", "passport", "lucia",
        "better-auth", "kinde",
    ),
    check=HttpCheck(
        "GET",
        "https://api.clerk.com/v1/jwks",
        "bearer",
        "CLERK_SECRET_KEY",
        ("api.clerk.com",),
        rules=(Rule((401, 403), "invalid", message="Clerk didn't recognise that secret key."),),
    ),
    guide=(
        GuideStep("Open the Clerk dashboard and pick (or create) an application.", "https://dashboard.clerk.com"),
        GuideStep("Go to Configure → API keys. A development instance gives test keys."),
        GuideStep("Copy the Publishable key (pk_test_…) and the Secret key (sk_test_…)."),
    ),
    docs_url="https://clerk.com/docs/quickstarts/nextjs",
    dashboard_url="https://dashboard.clerk.com",
    live_prefixes=("sk_live_", "pk_live_"),
    has_test_mode=True,
    skill="clerk-auth",
)

_OPENAI = Integration(
    id="openai",
    label="OpenAI",
    category="ai",
    capability="llm",
    wave=1,
    blurb="AI features: chat, summaries, generated text",
    builds=(
        "A chat assistant inside the app, streamed through an API route",
        "Summaries, rewrites and generated descriptions",
        "The model is a setting (OPENAI_MODEL), never hardcoded",
    ),
    variables=(
        Var("OPENAI_API_KEY", "API key", placeholder="sk-proj-…"),
        Var(
            "OPENAI_MODEL",
            "Model",
            secret=False,
            required=False,
            placeholder="Pick one after testing the key",
            kind="text",
            help="Which model the app calls. Chosen from your account's list.",
        ),
    ),
    names=("openai", "chatgpt", "gpt-4", "gpt-4o", "gpt-5", "gpt"),
    strong=(
        "chatbot", "chat bot", "ai assistant", "ai chat", "ai-generated", "ai generated",
        "llm", "language model", "generative ai", "ai-powered", "ai powered",
    ),
    soft=("summarize", "summarise", "summaries", "summary of", "ai"),
    rivals=(
        "anthropic", "claude", "gemini", "groq", "mistral", "ollama", "openrouter",
        "deepseek", "llama", "cohere", "together ai",
    ),
    check=HttpCheck(
        "GET",
        "https://api.openai.com/v1/models",
        "bearer",
        "OPENAI_API_KEY",
        ("api.openai.com",),
        rules=(Rule((401, 403), "invalid", message="OpenAI didn't recognise that API key."),),
        models=_openai_models,
    ),
    guide=(
        GuideStep("Open the OpenAI platform and go to API keys.", "https://platform.openai.com/api-keys"),
        GuideStep("Create new secret key → give it a name and a project. Copy it (sk-proj-…)."),
        GuideStep("Test it here, then pick the model the app should use from your account's list."),
    ),
    docs_url="https://platform.openai.com/docs/api-reference",
    dashboard_url="https://platform.openai.com/api-keys",
    skill="openai-api",
)


# ── waves 2 and 3: in the catalog, not connectable yet ───────────────────────
def _soon(
    iid: str,
    label: str,
    category: str,
    capability: str,
    wave: int,
    blurb: str,
    variables: str,
    docs: str = "",
) -> Integration:
    """A catalog row for a connector that isn't wired yet: names, not checks.

    `variables` is a space-separated list; a leading `+` marks a client variable.
    """
    parsed: list[Var] = []
    for token in variables.split():
        client = token.startswith("+")
        name = token.lstrip("+")
        parsed.append(Var(name, name, secret=not client, side="client" if client else "server"))
    return Integration(
        id=iid,
        label=label,
        category=category,
        capability=capability,
        wave=wave,
        blurb=blurb,
        variables=tuple(parsed),
        names=(label.lower(),),
        docs_url=docs,
    )


_LATER: tuple[Integration, ...] = (
    # Payments
    _soon("paypal", "PayPal", "payments", "payments", 2, "PayPal checkout and payouts", "PAYPAL_CLIENT_ID PAYPAL_CLIENT_SECRET PAYPAL_ENV"),
    _soon("razorpay", "Razorpay", "payments", "payments", 2, "UPI, cards and netbanking in India", "RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET +NEXT_PUBLIC_RAZORPAY_KEY_ID"),
    _soon("lemonsqueezy", "Lemon Squeezy", "payments", "payments", 2, "Merchant of record for digital products", "LEMONSQUEEZY_API_KEY LEMONSQUEEZY_STORE_ID LEMONSQUEEZY_WEBHOOK_SECRET"),
    _soon("paddle", "Paddle", "payments", "payments", 3, "Subscriptions with tax handled for you", "PADDLE_API_KEY +NEXT_PUBLIC_PADDLE_CLIENT_TOKEN PADDLE_ENV"),
    # Email
    _soon("sendgrid", "SendGrid", "email", "email", 2, "Transactional and marketing email", "SENDGRID_API_KEY EMAIL_FROM"),
    _soon("postmark", "Postmark", "email", "email", 2, "Fast transactional email", "POSTMARK_SERVER_TOKEN EMAIL_FROM"),
    _soon("mailgun", "Mailgun", "email", "email", 2, "Email sending and routing", "MAILGUN_API_KEY MAILGUN_DOMAIN MAILGUN_REGION"),
    _soon("brevo", "Brevo", "email", "email", 3, "Email and SMS campaigns", "BREVO_API_KEY"),
    # Auth
    _soon("auth0", "Auth0", "auth", "auth", 2, "Hosted sign-in with enterprise SSO", "AUTH0_DOMAIN AUTH0_CLIENT_ID AUTH0_CLIENT_SECRET AUTH0_SECRET APP_BASE_URL"),
    _soon("google-signin", "Google sign-in", "auth", "auth", 2, "Sign in with Google", "GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET AUTH_SECRET"),
    _soon("github-signin", "GitHub sign-in", "auth", "auth", 2, "Sign in with GitHub", "GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET AUTH_SECRET"),
    # AI & LLMs
    _soon("anthropic", "Anthropic", "ai", "llm", 2, "Claude models for chat and text", "ANTHROPIC_API_KEY ANTHROPIC_MODEL"),
    _soon("gemini", "Google Gemini", "ai", "llm", 2, "Gemini models for text and images", "GEMINI_API_KEY GEMINI_MODEL"),
    _soon("groq", "Groq", "ai", "llm", 2, "Very fast open-model inference", "GROQ_API_KEY GROQ_MODEL"),
    _soon("openrouter", "OpenRouter", "ai", "llm", 2, "Many models behind one key", "OPENROUTER_API_KEY OPENROUTER_MODEL"),
    _soon("replicate", "Replicate", "ai", "llm", 2, "Image, audio and video models", "REPLICATE_API_TOKEN"),
    _soon("elevenlabs", "ElevenLabs", "ai", "llm", 2, "Text to speech and voices", "ELEVENLABS_API_KEY"),
    _soon("mistral", "Mistral", "ai", "llm", 3, "Mistral models for chat and text", "MISTRAL_API_KEY MISTRAL_MODEL"),
    _soon("deepseek", "DeepSeek", "ai", "llm", 3, "DeepSeek models for chat and code", "DEEPSEEK_API_KEY DEEPSEEK_MODEL"),
    _soon("together", "Together AI", "ai", "llm", 3, "Open models, hosted", "TOGETHER_API_KEY TOGETHER_MODEL"),
    _soon("cohere", "Cohere", "ai", "llm", 3, "Embeddings and reranking", "COHERE_API_KEY"),
    _soon("perplexity", "Perplexity", "ai", "llm", 3, "Answers grounded in web search", "PERPLEXITY_API_KEY"),
    _soon("firecrawl", "Firecrawl", "ai", "llm", 3, "Turn websites into clean data", "FIRECRAWL_API_KEY"),
    # Storage & media
    _soon("cloudinary", "Cloudinary", "storage", "storage", 2, "Image and video upload, resizing, delivery", "CLOUDINARY_CLOUD_NAME +NEXT_PUBLIC_CLOUDINARY_CLOUD_NAME CLOUDINARY_API_KEY CLOUDINARY_API_SECRET"),
    _soon("uploadthing", "UploadThing", "storage", "storage", 2, "File uploads for Next.js", "UPLOADTHING_TOKEN"),
    _soon("s3", "AWS S3", "storage", "storage", 2, "Object storage for files", "AWS_REGION AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY S3_BUCKET"),
    _soon("r2", "Cloudflare R2", "storage", "storage", 3, "S3-compatible storage, no egress fees", "R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET"),
    _soon("vercel-blob", "Vercel Blob", "storage", "storage", 3, "File storage on Vercel", "BLOB_READ_WRITE_TOKEN"),
    _soon("mux", "Mux", "storage", "storage", 3, "Video upload and streaming", "MUX_TOKEN_ID MUX_TOKEN_SECRET"),
    # Messaging
    _soon("twilio", "Twilio", "messaging", "sms", 2, "SMS and WhatsApp messages", "TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_PHONE_NUMBER"),
    _soon("slack", "Slack", "messaging", "sms", 2, "Post to Slack channels from your app", "SLACK_BOT_TOKEN SLACK_CHANNEL_ID"),
    _soon("discord", "Discord", "messaging", "sms", 3, "Post to a Discord channel by webhook", "DISCORD_WEBHOOK_URL"),
    _soon("telegram", "Telegram", "messaging", "sms", 3, "A Telegram bot for your app", "TELEGRAM_BOT_TOKEN"),
    # Maps
    _soon("google-maps", "Google Maps", "maps", "maps", 2, "Maps, places and geocoding", "+NEXT_PUBLIC_GOOGLE_MAPS_API_KEY"),
    _soon("mapbox", "Mapbox", "maps", "maps", 2, "Custom maps and directions", "+NEXT_PUBLIC_MAPBOX_TOKEN"),
    # Analytics
    _soon("posthog", "PostHog", "analytics", "analytics", 2, "Product analytics and feature flags", "+NEXT_PUBLIC_POSTHOG_KEY +NEXT_PUBLIC_POSTHOG_HOST"),
    _soon("sentry", "Sentry", "analytics", "errors", 2, "Error tracking and performance", "+NEXT_PUBLIC_SENTRY_DSN SENTRY_AUTH_TOKEN"),
    _soon("google-analytics", "Google Analytics", "analytics", "analytics", 3, "Traffic and audience reports", "+NEXT_PUBLIC_GA_MEASUREMENT_ID"),
    # Search & vector
    _soon("algolia", "Algolia", "search", "search", 2, "Instant search for your content", "+NEXT_PUBLIC_ALGOLIA_APP_ID +NEXT_PUBLIC_ALGOLIA_SEARCH_KEY ALGOLIA_ADMIN_KEY"),
    _soon("pinecone", "Pinecone", "search", "vector", 3, "Vector search for AI features", "PINECONE_API_KEY PINECONE_INDEX"),
    # Cache & realtime
    _soon("upstash", "Upstash Redis", "realtime", "cache", 2, "Serverless Redis for caching and rate limits", "UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN"),
    _soon("pusher", "Pusher", "realtime", "realtime", 3, "Realtime updates over websockets", "PUSHER_APP_ID +NEXT_PUBLIC_PUSHER_KEY PUSHER_SECRET +NEXT_PUBLIC_PUSHER_CLUSTER"),
    _soon("ably", "Ably", "realtime", "realtime", 3, "Realtime messaging and presence", "ABLY_API_KEY"),
    # CMS & content
    _soon("sanity", "Sanity", "cms", "cms", 3, "Structured content you edit in Sanity Studio", "+NEXT_PUBLIC_SANITY_PROJECT_ID +NEXT_PUBLIC_SANITY_DATASET SANITY_API_TOKEN"),
    _soon("contentful", "Contentful", "cms", "cms", 3, "Headless CMS content", "CONTENTFUL_SPACE_ID CONTENTFUL_ACCESS_TOKEN"),
    _soon("notion", "Notion", "cms", "cms", 3, "Use a Notion database as app data", "NOTION_TOKEN NOTION_DATABASE_ID"),
    _soon("airtable", "Airtable", "cms", "cms", 3, "Use an Airtable base as app data", "AIRTABLE_TOKEN AIRTABLE_BASE_ID"),
    # Commerce & CRM
    _soon("shopify", "Shopify", "commerce", "commerce", 3, "Products and carts from a Shopify store", "SHOPIFY_STORE_DOMAIN SHOPIFY_STOREFRONT_ACCESS_TOKEN"),
    _soon("hubspot", "HubSpot", "commerce", "crm", 3, "Contacts and deals in HubSpot", "HUBSPOT_ACCESS_TOKEN"),
)

REGISTRY: dict[str, Integration] = {
    i.id: i for i in (_STRIPE, _RESEND, _CLERK, _OPENAI, *_LATER)
}


def get(iid: Optional[str]) -> Optional[Integration]:
    return REGISTRY.get(iid or "")


def connectable(iid: Optional[str]) -> Optional[Integration]:
    found = get(iid)
    return found if found is not None and found.connectable else None


def catalog() -> list[Integration]:
    """Every connector, in category order, wired ones first within a category."""
    order = {c: n for n, (c, _) in enumerate(CATEGORIES)}
    return sorted(REGISTRY.values(), key=lambda i: (order.get(i.category, 99), i.wave, i.label.lower()))


def env_names(ids: Iterable[str]) -> tuple[str, ...]:
    """Every variable name the given connectors' code reads, in registry order."""
    out: list[str] = []
    for iid in ids:
        found = get(iid)
        if found is None:
            continue
        out += [v.name for v in found.variables if v.name not in out]
    return tuple(out)


def client_names() -> frozenset[str]:
    """Every variable that is public by design — the only ones the browser may hold."""
    return frozenset(v.name for i in REGISTRY.values() for v in i.variables if v.side == "client")


def server_names() -> frozenset[str]:
    return frozenset(v.name for i in REGISTRY.values() for v in i.variables if v.side == "server")


def secret_names() -> frozenset[str]:
    """Every variable that is a secret — server-only, never in the browser."""
    return frozenset(v.name for i in REGISTRY.values() for v in i.variables if v.secret and v.side == "server")


def var_for(name: str) -> Optional[tuple[Integration, Var]]:
    for found in REGISTRY.values():
        var = found.var(name)
        if var is not None:
            return found, var
    return None


def placeholder(name: str) -> Optional[str]:
    """The `.env.example` value for a connector variable, or None if not one of ours.

    Blank for every key — the example is a list of what to set, not a value to run
    with — and the documented default for the one that has a safe one.
    """
    found = var_for(name)
    if found is None:
        return None
    _, var = found
    if name == "EMAIL_FROM":
        return "onboarding@resend.dev"
    return ""


# ── parsing what was pasted ──────────────────────────────────────────────────
@dataclass
class Parsed:
    values: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    #: `test`, `live`, or None when the service has no test mode.
    mode: Optional[str] = None


#: What each variable must look like, and the message when it doesn't.
_FORMAT: dict[str, tuple[re.Pattern, str]] = {
    "STRIPE_SECRET_KEY": (
        re.compile(r"^(sk|rk)_(test|live)_[A-Za-z0-9]{10,}$"),
        "A Stripe secret key starts with sk_test_ (or rk_test_ for a restricted key).",
    ),
    "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": (
        re.compile(r"^pk_(test|live)_[A-Za-z0-9]{10,}$"),
        "A Stripe publishable key starts with pk_test_.",
    ),
    "STRIPE_WEBHOOK_SECRET": (
        re.compile(r"^whsec_[A-Za-z0-9+/=]{10,}$"),
        "A webhook signing secret starts with whsec_.",
    ),
    "RESEND_API_KEY": (
        re.compile(r"^re_[A-Za-z0-9_]{10,}$"),
        "A Resend API key starts with re_.",
    ),
    "EMAIL_FROM": (
        re.compile(r"^([^<>@\s][^<>@]*<\s*)?[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+\s*>?$"),
        "Send-from is an email address, like onboarding@resend.dev or App <hello@yourdomain.com>.",
    ),
    "CLERK_SECRET_KEY": (
        re.compile(r"^sk_(test|live)_[A-Za-z0-9]{10,}$"),
        "A Clerk secret key starts with sk_test_.",
    ),
    "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": (
        re.compile(r"^pk_(test|live)_[A-Za-z0-9+/=_\-]{10,}$"),
        "A Clerk publishable key starts with pk_test_.",
    ),
    "OPENAI_API_KEY": (
        re.compile(r"^sk-(proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}$"),
        "An OpenAI API key starts with sk-proj- (or sk-).",
    ),
    "OPENAI_MODEL": (
        re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]{0,99}$"),
        "A model id has no spaces, like the ones in the list after testing the key.",
    ),
}

#: Shapes that are secret wherever they're pasted.
_SECRET_SHAPE = re.compile(r"^(sk|rk)_(test|live)_|^whsec_|^re_|^sk-")
_PUBLISHABLE_SHAPE = re.compile(r"^pk_(test|live)_")


def _trim(value: object) -> str:
    text = str(value or "").strip()
    for _ in range(3):
        # The `NAME=` from a `.env` line, then the quotes from it or from a code sample.
        text = re.sub(r"^(export\s+)?[A-Z][A-Z0-9_]*\s*=\s*", "", text)
        while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
            text = text[1:-1].strip()
    return text


def _mode_of(value: str) -> Optional[str]:
    m = re.match(r"^(sk|rk|pk)_(test|live)_", value)
    return m.group(2) if m else None


def parse(integration: Integration, values: dict[str, object]) -> Parsed:
    """Clean what was pasted, and say what is wrong with it — no network here."""
    out = Parsed()
    given = {str(k): v for k, v in (values or {}).items()}
    for name in given:
        if integration.var(name) is None:
            out.problems.append(Problem(name, f"{name} isn't a variable {integration.label} uses."))
    for var in integration.variables:
        text = _trim(given.get(var.name))
        if not text:
            if var.required:
                out.problems.append(Problem(var.name, f"{var.label} is missing."))
            continue
        if any(c.isspace() for c in text) and var.name != "EMAIL_FROM":
            out.problems.append(Problem(var.name, "There's a space or line break inside it. Copy it again, on one line."))
            continue
        if var.side == "client" and _SECRET_SHAPE.match(text):
            # Caught before anything else: this field ends up in the browser bundle.
            out.problems.append(
                Problem(
                    var.name,
                    "That's a secret key, and this field goes in the browser where anyone can read it. "
                    "Paste the publishable key (pk_…) here.",
                    step=_guide_step(integration, "publishable"),
                )
            )
            continue
        if var.secret and _PUBLISHABLE_SHAPE.match(text):
            out.problems.append(
                Problem(
                    var.name,
                    "That's the publishable key. This field takes the secret key, which starts "
                    f"with {var.placeholder.replace('…', '')}.",
                    step=_guide_step(integration, "secret"),
                )
            )
            continue
        rule = _FORMAT.get(var.name)
        if rule is not None and not rule[0].match(text):
            out.problems.append(Problem(var.name, rule[1], step=_guide_step(integration, "secret" if var.secret else "publishable")))
            continue
        out.values[var.name] = text
    if out.problems:
        return out
    modes = {m for m in (_mode_of(v) for v in out.values.values()) if m}
    if len(modes) > 1:
        out.problems.append(
            Problem(
                next(v.name for v in integration.variables if v.side == "client"),
                "One key is a test key and the other is live. Copy both from the same mode — "
                "Test mode while you build.",
                step=1,
            )
        )
        return out
    if integration.has_test_mode:
        out.mode = modes.pop() if modes else "test"
    return out


def _guide_step(integration: Integration, what: str) -> Optional[int]:
    """Which guide step fixes it, 1-based: the one that mentions the key asked for."""
    words = {"secret": ("secret", "create api key", "create new", "api key"), "publishable": ("publishable",)}[what]
    for word in words:
        for n, step in enumerate(integration.guide, start=1):
            if word in step.text.lower():
                return n
    return None


def is_live(integration: Integration, values: dict[str, str]) -> bool:
    return any(v.startswith(integration.live_prefixes) for v in values.values() if integration.live_prefixes)


# ── what counts as secret in a saved value ───────────────────────────────────
def secret_parts(integration: Optional[Integration], values: dict[str, str]) -> list[str]:
    """The values worth scrubbing: every secret one. Never a public one."""
    if integration is None:
        return []
    parts = []
    for name, value in values.items():
        var = integration.var(name)
        if var is not None and var.secret and value and len(value) >= 8:
            parts.append(value)
    return list(dict.fromkeys(parts))


def hints(integration: Integration, values: dict[str, str]) -> list[dict]:
    """Each saved variable's name and what the page may show of it."""
    return [
        {"name": name, "hint": hint(integration.var(name), value), "side": (integration.var(name) or Var(name, name)).side}
        for name, value in values.items()
        if value
    ]


# ── testing it ───────────────────────────────────────────────────────────────
@dataclass
class Checked:
    """A check's result, plus what only a connector check knows."""

    result: CheckResult
    mode: Optional[str] = None
    models: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {**self.result.as_dict(), "mode": self.mode, "models": self.models}


def _unchecked(host: str, why: str) -> Checked:
    return Checked(CheckResult(UNCHECKED, f"Saved, not tested. {why}", reason="unreachable", host=host))


def _body(response: httpx.Response) -> tuple[dict, str]:
    text = response.text[:4000]
    try:
        data = response.json()
    except ValueError:
        data = {}
    return (data if isinstance(data, dict) else {}), text


def check(integration: Integration, values: dict[str, str], mode: Optional[str] = None) -> Checked:
    """Try the key against the service, read-only. Never raises."""
    spec = integration.check
    if spec is None:
        return _unchecked("", "There's no read-only way to test this one, so it's saved as it is.")
    host = urlsplit(spec.url).hostname or ""
    try:
        if host not in spec.hosts:
            # Never reached for a fixed URL; the guard is here for the connectors
            # whose URL is built from a field (a domain, an app id).
            log.warning("Refused a %s check to %s: not on its allowlist.", integration.id, host)
            return Checked(CheckResult(FAILED, "That host isn't one this connector talks to.", reason="host", host=host))
        key = values.get(spec.key_var, "")
        headers = {"Accept": "application/json"}
        auth = None
        if spec.auth == "basic":
            auth = (key, "")
        elif spec.auth == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif spec.auth.startswith("header:"):
            headers[spec.auth.split(":", 1)[1]] = key
        started = time.monotonic()
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
                response = client.request(spec.method, spec.url, headers=headers, auth=auth)
        except httpx.TimeoutException:
            return _unchecked(host, f"{integration.label} didn't answer in time. Try Re-test in a minute.")
        except httpx.HTTPError:
            return _unchecked(host, f"We couldn't reach {integration.label} from this server.")
        latency = int((time.monotonic() - started) * 1000)
        data, text = _body(response)
        status = response.status_code
        for rule in spec.rules:
            if status in rule.statuses and (rule.body is None or rule.body in text):
                if rule.outcome in ("ok", "restricted"):
                    return _connected(integration, spec, data, host, latency, mode, rule.message, values)
                field = spec.key_var
                return Checked(
                    CheckResult(
                        FAILED,
                        rule.message + " " + _fix_for(integration),
                        reason=rule.outcome,
                        name=field,
                        step=_guide_step(integration, "secret"),
                        host=host,
                        latency_ms=latency,
                    ),
                    mode=mode,
                )
        if 200 <= status < 300:
            return _connected(integration, spec, data, host, latency, mode, "", values)
        if status == 429:
            return _unchecked(host, f"{integration.label} is rate limiting checks right now.")
        if status >= 500:
            return _unchecked(host, f"{integration.label} had a problem of its own (HTTP {status}).")
        return Checked(
            CheckResult(
                FAILED,
                f"{integration.label} refused it (HTTP {status}). " + _fix_for(integration),
                reason="other",
                name=spec.key_var,
                host=host,
                latency_ms=latency,
            ),
            mode=mode,
        )
    except Exception:  # noqa: BLE001 - a check must never take the save down with it
        log.exception("Connector check failed unexpectedly for %s", integration.label)
        return _unchecked(host, "The check itself failed.")


def _connected(
    integration: Integration,
    spec: HttpCheck,
    data: dict,
    host: str,
    latency: int,
    mode: Optional[str],
    note: str,
    values: dict[str, str],
) -> Checked:
    models = spec.models(data) if spec.models else []
    if spec.live is not None and integration.has_test_mode:
        live = spec.live(data)
        if live is not None:
            mode = "live" if live else "test"
    shown = f"Connected to {integration.label}"
    if mode == "test":
        shown += " · Test mode"
    elif mode == "live":
        shown += " · Live"
    if note:
        # "Connected with a restricted key." → "· restricted key"
        shown += f" · {note.rstrip('.').replace('Connected with a ', '')}"
    return Checked(
        CheckResult(CONNECTED, shown, reason="ok", host=host, latency_ms=latency),
        mode=mode,
        models=models,
    )


def _fix_for(integration: Integration) -> str:
    first = integration.guide[0].text if integration.guide else ""
    return f"Copy it again from your {integration.label} dashboard." + (f" ({first})" if first else "")


# ── which connectors a build uses ────────────────────────────────────────────
@dataclass(frozen=True)
class Match:
    iid: str
    reason: str
    #: `idea`, `design`, or `user` (added by hand).
    source: str

    def as_dict(self) -> dict:
        return {"id": self.iid, "reason": self.reason, "source": self.source}


_NEGATION = re.compile(
    r"\b(no|not|without|dont|do not|doesnt|does not|never|wont|will not|isnt|zero|skip|skipping)\b"
)


def _norm(text: object) -> str:
    # Apostrophes go first, so "don't" stays one word for the negation check.
    low = re.sub(r"['’]", "", str(text or "").lower())
    return " " + re.sub(r"[^a-z0-9+.\-]+", " ", low).strip() + " "


def _find(phrase: str, haystack: str) -> Optional[int]:
    """Where `phrase` sits in `haystack` as whole words, or None."""
    m = re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", haystack)
    return m.start() if m else None


def _negated(haystack: str, at: int) -> bool:
    """Whether a negation sits just before `at`, within the same clause."""
    window = haystack[max(0, at - 32):at]
    # A clause break between the negation and the phrase ends it.
    window = re.split(r"\b(but|and then|however|although)\b", window)[-1]
    return bool(_NEGATION.search(window))


def _hit(phrases: Iterable[str], haystack: str) -> Optional[str]:
    """The first phrase present and not negated."""
    for phrase in phrases:
        at = _find(phrase, haystack)
        if at is not None and not _negated(haystack, at):
            return phrase
    return None


def _named(phrases: Iterable[str], haystack: str) -> bool:
    return any(_find(p, haystack) is not None for p in phrases)


def relevant(
    idea: str,
    design_texts: Iterable[object] = (),
    connected: Iterable[str] = (),
    *,
    use: Iterable[str] = (),
    skip: Iterable[str] = (),
    defaults: Optional[dict[str, str]] = None,
    recent: Optional[dict[str, str]] = None,
) -> list[Match]:
    """Which connectors this build uses, and why — one per capability.

    1. A connector the idea (or the design) names outright wins: "…with Stripe".
    2. A rival named in the idea or the design takes the capability away from this
       one: "next-auth" means no Clerk.
    3. Otherwise a capability phrase picks the connected connector for it; with
       several connected, the user's default for it, then the most recently
       connected. A *strong* phrase ("checkout") picks one that isn't connected too
       — the build then asks for it. A *soft* one ("sign in") only picks a connected one.
    4. `use` adds, `skip` removes, and `skip` always wins.
    """
    on = set(connected)
    skipped = set(skip)
    defaults = defaults or {}
    recent = recent or {}
    idea_text = _norm(idea)
    design_text = _norm(" ".join(str(t) for t in design_texts))
    chosen: dict[str, Match] = {}

    pool = [i for i in catalog() if i.connectable]
    by_capability: dict[str, list[Integration]] = {}
    for i in pool:
        by_capability.setdefault(i.capability, []).append(i)

    for capability, members in by_capability.items():
        # 1. named outright, idea first.
        named = next((i for i in members if _hit(i.names, idea_text)), None)
        source = "idea"
        if named is None:
            named = next((i for i in members if _hit(i.names, design_text)), None)
            source = "design"
        if named is not None:
            who = "your idea" if source == "idea" else "Atlas's design"
            chosen[capability] = Match(named.id, f"{who} names {named.label}", source)
            continue
        # 2. a rival named takes the capability.
        rivals = {r for i in members for r in i.rivals} | {
            n for other in REGISTRY.values() if other.capability == capability and not other.connectable for n in other.names
        }
        if _named(rivals, idea_text) or _named(rivals, design_text):
            continue
        # 3. a capability phrase.
        for text, where, src in ((idea_text, "your idea", "idea"), (design_text, "Atlas's design", "design")):
            hits: list[tuple[Integration, str]] = []
            for i in members:
                phrase = _hit(i.strong, text) or (_hit(i.soft, text) if i.id in on else None)
                if phrase:
                    hits.append((i, phrase))
            if not hits:
                continue
            ready = [(i, p) for i, p in hits if i.id in on]
            if ready:
                # The user's default for this capability, else the most recently connected.
                preferred = defaults.get(capability)
                ready.sort(key=lambda h: recent.get(h[0].id) or "", reverse=True)
                ready.sort(key=lambda h: h[0].id != preferred)
                pick, phrase = ready[0]
            else:
                pick, phrase = hits[0]
            chosen[capability] = Match(pick.id, f"{where} mentions {phrase}", src)
            break

    out = [m for m in chosen.values() if m.iid not in skipped]
    for iid in use:
        found = connectable(iid)
        if found is None or iid in skipped or any(m.iid == iid for m in out):
            continue
        # Added by hand replaces whatever the matcher picked for the capability:
        # a build never uses two connectors for one job unless asked for both.
        out = [m for m in out if get(m.iid).capability != found.capability or m.source == "user"]
        out.append(Match(iid, "you added it", "user"))
    order = {i.id: n for n, i in enumerate(catalog())}
    return sorted(out, key=lambda m: order.get(m.iid, 999))


def design_texts(design_output: object) -> list[str]:
    """What of Atlas's architecture is read for connectors: its words, bounded."""
    import json

    if not isinstance(design_output, dict):
        return []
    try:
        return [json.dumps(design_output, default=str)[:12000]]
    except (TypeError, ValueError):
        return []


# ── what Atlas is told ───────────────────────────────────────────────────────
def available_note(connected_ids: Iterable[str]) -> str:
    """The names-only block System Design reads: what the person has connected."""
    found = [connectable(i) for i in connected_ids]
    found = [f for f in found if f is not None]
    if not found:
        return ""
    listed = ", ".join(f"{f.label} ({CAPABILITY_LABELS.get(f.capability, f.capability)})" for f in found)
    return (
        "# Services the user has connected\n"
        f"The user has connected: {listed}. When the idea needs one of these capabilities, "
        "design with that service rather than an alternative, and name it in the tech stack. "
        "Don't add a service the idea doesn't need. You will never see their keys — the code "
        "reads them from environment variables.\n"
    )
