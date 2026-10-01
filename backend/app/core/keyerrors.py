"""What a provider's refusal of a key means, and what to tell the person (#63).

A key can fail for ten reasons with ten different fixes: mistyped, expired, revoked,
an account with no credit, a spend limit, billing turned off, a plan's quota, a key
without permission, a region the provider doesn't serve, and — the two that need no
fix — a rate limit or an outage. Every place that sees a key fail (the Settings
check, a build's own model call, a connector's test) classifies it here and takes
its words from here, so each reason reads the same everywhere.

**Codes first, text last.** The HTTP status and the provider's own `type` / `code` /
`status` / `reason` decide; the message is only read when no code does, because the
providers say their wording may change. A 429 is not one thing: "slow down" is
retried, "your money ran out" never is — the code, and whether a `Retry-After` came
with it, tell them apart.

**Never the provider's text.** It is read to classify and then dropped — it may
quote the key. What is kept and shown is only this module's own sentences, plus at
most the status and a short code, which are the provider's vocabulary, not the key.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

# ── kinds ─────────────────────────────────────────────────────────────────────
INVALID = "invalid"  # mistyped / not a key / the wrong kind of key
EXPIRED = "expired"
REVOKED = "revoked"  # revoked, deleted, suspended, disabled after a leak
NO_CREDIT = "no_credit"  # prepaid balance used up, insufficient_quota, 402
SPEND_LIMIT = "spend_limit"  # an org / project / monthly cap, a daily quota
BILLING_DISABLED = "billing_disabled"  # billing off, plan lapsed, account not activated
PLAN_QUOTA = "plan_quota"  # a plan's daily / monthly allowance (Resend)
NOT_PERMITTED = "not_permitted"  # a restricted key, a missing scope
REGION = "region"  # the provider doesn't serve this server's location
RATE_LIMITED = "rate_limited"  # retried
PROVIDER_DOWN = "provider_down"  # 5xx, 529, overloaded — retried
UNKNOWN = "unknown"

KINDS = (
    INVALID, EXPIRED, REVOKED, NO_CREDIT, SPEND_LIMIT, BILLING_DISABLED, PLAN_QUOTA,
    NOT_PERMITTED, REGION, RATE_LIMITED, PROVIDER_DOWN, UNKNOWN,
)
#: Worth asking again: everything else fails the same way on every attempt.
RETRYABLE = frozenset({RATE_LIMITED, PROVIDER_DOWN})
#: The key itself won't do: a different key fixes it.
KEY_KINDS = frozenset({INVALID, EXPIRED, REVOKED})
#: The key is fine; the account behind it can't pay for the call.
MONEY_KINDS = frozenset({NO_CREDIT, SPEND_LIMIT, BILLING_DISABLED, PLAN_QUOTA})
#: Any of these means the key can't be used until someone does something.
BLOCKING = KEY_KINDS | MONEY_KINDS | frozenset({NOT_PERMITTED, REGION})


# ── reading an answer ─────────────────────────────────────────────────────────
def _norm(value: object) -> str:
    """`FAILED_PRECONDITION`, `failed-precondition`, `FailedPrecondition` → one spelling."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


#: Codes, in their `_norm` spelling, and what each one means. Checked before any text.
_CODES: dict[str, str] = {
    # mistyped, or not a key at all
    "invalidapikey": INVALID,          # OpenAI
    "apikeyinvalid": INVALID,          # Gemini ErrorInfo reason
    "authenticationerror": INVALID,    # Anthropic
    "clerkkeyinvalid": INVALID,        # Clerk
    "missingapikey": INVALID,          # Resend
    "secretkeyrequired": INVALID,      # Stripe: a publishable key pasted as the secret
    "tokeninvalid": INVALID,           # Mapbox
    "tokenmalformed": INVALID,
    # expired
    "apikeyexpired": EXPIRED,          # Stripe; Gemini's reason, when it sends one
    "platformapikeyexpired": EXPIRED,  # Stripe Connect
    "tokenexpired": EXPIRED,           # Mapbox
    "expiredapikey": EXPIRED,
    # revoked / suspended
    "suspendedapikey": REVOKED,        # Resend
    "apikeyrevoked": REVOKED,
    "revokedapikey": REVOKED,
    "accountdeactivated": REVOKED,
    # no credit
    "insufficientquota": NO_CREDIT,    # OpenAI 429
    "creditbalanceexhausted": NO_CREDIT,
    "billingerror": NO_CREDIT,         # Anthropic 402
    "paymentrequired": NO_CREDIT,      # Gemini 402
    "billinghardlimitreached": SPEND_LIMIT,
    # spend limits
    "organizationspendlimitexceeded": SPEND_LIMIT,
    "projectspendlimitexceeded": SPEND_LIMIT,
    "organizationusagelimitexceeded": SPEND_LIMIT,
    # billing off / account not activated
    "billingnotactive": BILLING_DISABLED,
    "billingdisabled": BILLING_DISABLED,
    "testmodechargesonly": BILLING_DISABLED,  # Stripe: account not activated for live
    # a plan's allowance
    "dailyquotaexceeded": PLAN_QUOTA,  # Resend
    "monthlyquotaexceeded": PLAN_QUOTA,
    # permission
    "permissionerror": NOT_PERMITTED,  # Anthropic 403
    "restrictedapikey": NOT_PERMITTED,
    "insufficientpermissions": NOT_PERMITTED,
    # region
    "unsupportedcountryregionterritory": REGION,  # OpenAI 403
    "unsupportedcountry": REGION,
    # retry
    "ratelimitexceeded": RATE_LIMITED,
    "ratelimiterror": RATE_LIMITED,
    "ratelimit": RATE_LIMITED,
    "slowdown": RATE_LIMITED,
    "toomanyrequests": RATE_LIMITED,
    "overloadederror": PROVIDER_DOWN,  # Anthropic 529
    "apierror": PROVIDER_DOWN,         # Anthropic 500
    "serviceunavailable": PROVIDER_DOWN,
    "unavailable": PROVIDER_DOWN,
    "internal": PROVIDER_DOWN,
}

#: Text, only when no code decided — in the order a message is most specific.
_TEXT: tuple[tuple[str, tuple[str, ...]], ...] = (
    (REVOKED, ("reported as leaked", "has been revoked", "was revoked", "key revoked", "been deleted",
               "been suspended", "is suspended", "deactivated", "disabled api key", "api key disabled")),
    (EXPIRED, ("api key expired", "key has expired", "key is expired", "token expired", "token has expired",
               "renew the api key")),
    (REGION, ("location is not supported", "unsupported_country", "country, region", "country or region",
              "region is not supported", "not available in your country", "territory")),
    (BILLING_DISABLED, ("billing is not enabled", "billing has been disabled", "billing is disabled",
                        "enable billing", "billing account", "billing not active", "plan has expired",
                        "subscription has expired", "subscription is inactive")),
    (NO_CREDIT, ("credit balance", "out of credit", "insufficient credit", "no credits", "prepay",
                 "insufficient_quota", "insufficient funds", "payment required")),
    (SPEND_LIMIT, ("spend limit", "spending limit", "usage limit", "hard limit", "monthly limit",
                   "budget exceeded")),
    (NOT_PERMITTED, ("insufficient permissions", "missing scopes", "not have permission",
                     "does not have access", "not allowed to", "restricted key", "model.request")),
    (INVALID, ("incorrect api key", "invalid api key", "invalid x-api-key", "api key not valid",
               "invalid authentication", "invalid token", "not a valid key", "api key is invalid",
               "secret key is invalid", "unauthorized")),
)

#: Google's quota violations name the window they count: a per-minute one resets in
#: seconds (a rate limit); a per-day one, or any quota whose limit is 0 (a free tier
#: that doesn't cover this model), doesn't — and one such violation is enough.
_LASTING_QUOTA = re.compile(r"perday|daily|limit0\b|limit0$|quotavalue0\b|billing")


@dataclass
class Failure:
    kind: str
    retryable: bool
    status: Optional[int] = None
    #: A short code the provider sent (`insufficient_quota`), for "Technical details".
    #: Never its message.
    code: Optional[str] = None


def _error_dict(body: object) -> Optional[dict]:
    """The part of a body that describes the error: `{"error": {...}}`'s inside,
    Clerk's first `errors[]`, or the body itself (Resend, and OpenAI's SDK, which
    hands over the inside already)."""
    if not isinstance(body, dict):
        return None
    inner = body.get("error")
    if isinstance(inner, dict):
        return inner
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0]
    return body


def _codes(body: object) -> tuple[list[str], str, str]:
    """(codes in the order they speak, the raw first code to show, quota detail text)."""
    err = _error_dict(body)
    if err is None:
        return [], "", ""
    found: list[str] = []
    shown = ""
    quota = ""
    outer = body if isinstance(body, dict) else {}
    # Most specific first: a Gemini ErrorInfo reason beats its gRPC status, and
    # OpenAI's `code` beats its `type`.
    for detail in err.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("reason"):
            found.append(str(detail["reason"]))
        for v in detail.get("violations") or []:
            if isinstance(v, dict):
                # One line per violation, so each is judged on its own.
                one = " ".join(str(v.get(k, "")) for k in ("quotaMetric", "quotaId", "subject"))
                if "quotaValue" in v:
                    one += " limit" + str(v.get("quotaValue"))
                quota += "|" + one
    for key in ("code", "reason", "name", "type", "status"):
        value = err.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    # Anthropic: `{"type": "error", "error": {...}}` — the outer type says nothing.
    for key in ("code", "name"):
        value = outer.get(key)
        if isinstance(value, str) and value and outer is not err:
            found.append(value)
    for value in found:
        if _norm(value) not in ("error", ""):
            shown = value
            break
    return found, shown[:60], "|".join(_norm(q) for q in quota.split("|") if q.strip())


def _message(body: object, fallback: str) -> str:
    err = _error_dict(body)
    text = ""
    if err is not None:
        text = " ".join(str(err.get(k) or "") for k in ("message", "detail", "long_message"))
    return (text + " " + (fallback or "")).lower()


def signals(body: object, text: str = "") -> str:
    """Every code and the message, lowercased — for callers that refine a kind
    (a restricted key, an IP allowlist). Read, never kept."""
    return (" ".join(_codes(body)[0]) + " " + _message(body, text)).lower()


def _has_retry_after(headers: Optional[Mapping[str, Any]]) -> Optional[bool]:
    if headers is None:
        return None
    try:
        return any(str(k).lower() in ("retry-after", "retry-after-ms") for k in headers.keys())
    except Exception:  # noqa: BLE001 - an odd headers object: say nothing
        return None


def classify(
    provider: str,
    status: Optional[int],
    body: object = None,
    headers: Optional[Mapping[str, Any]] = None,
    text: str = "",
) -> Failure:
    """What an error answer means. `body` is the parsed JSON (or None), `text` the raw
    text when there's no JSON; both are only read here."""
    codes, shown, quota = _codes(body)
    words = _message(body, text)
    retry_after = _has_retry_after(headers)

    def made(kind: str) -> Failure:
        return Failure(kind=kind, retryable=kind in RETRYABLE, status=status, code=shown or None)

    if status is not None and 200 <= status < 300:
        return made(UNKNOWN)

    # 1 — codes. Some say the same thing for two causes; the text breaks the tie.
    for code in codes:
        kind = _CODES.get(_norm(code))
        if kind is None:
            continue
        if kind == INVALID:
            # One code for "mistyped", "expired" and "revoked" (Anthropic, Gemini,
            # Clerk): the message is the only place that says which.
            for refined in (REVOKED, EXPIRED):
                if any(w in words for w in dict(_TEXT)[refined]):
                    return made(refined)
        if kind == RATE_LIMITED and provider == "anthropic" and status == 429 and retry_after is False:
            # Anthropic's monthly tier cap is a 429 with nothing to wait for.
            return made(SPEND_LIMIT)
        if kind == PROVIDER_DOWN and status is not None and status < 500:
            continue  # a gRPC "unavailable" on a 4xx: let the rest decide
        return made(kind)

    normalized = {_norm(c) for c in codes}
    if "resourceexhausted" in normalized or (provider == "gemini" and status == 429):
        # Google: per-minute is a rate limit; per-day, free-tier zero, or billing isn't.
        if any(_LASTING_QUOTA.search(v) for v in quota.split("|") if v):
            return made(SPEND_LIMIT)
        if any(w in words for w in dict(_TEXT)[NO_CREDIT]):
            return made(NO_CREDIT)
        return made(RATE_LIMITED)
    if "failedprecondition" in normalized:
        # Gemini says both "billing off" and "not in your region" this way.
        if any(w in words for w in dict(_TEXT)[REGION]):
            return made(REGION)
        return made(BILLING_DISABLED)
    if "permissiondenied" in normalized:
        for kind in (REVOKED, REGION):
            if any(w in words for w in dict(_TEXT)[kind]):
                return made(kind)
        return made(NOT_PERMITTED)
    if "unauthenticated" in normalized or ("invalidargument" in normalized and "api key" in words):
        for kind in (REVOKED, EXPIRED):
            if any(w in words for w in dict(_TEXT)[kind]):
                return made(kind)
        if "unauthenticated" in normalized or "not valid" in words:
            return made(INVALID)

    # 2 — the status where it can only mean one thing.
    if status == 402:
        return made(BILLING_DISABLED if any(w in words for w in dict(_TEXT)[BILLING_DISABLED]) else NO_CREDIT)
    if status is not None and (status >= 500 or status == 408):
        return made(PROVIDER_DOWN)

    # 3 — text, for answers that carry no code we know.
    if status is not None and status >= 400:
        for kind, phrases in _TEXT:
            if kind in (NOT_PERMITTED, INVALID) and status not in (401, 403):
                # A 400 that says "not allowed to use system" is about the request's
                # shape, not the key: only an auth status makes these key verdicts.
                continue
            if any(p in words for p in phrases):
                return made(kind)

    # 4 — the status alone.
    if status == 401:
        return made(INVALID)
    if status == 403:
        return made(NOT_PERMITTED)
    if status == 429:
        if provider == "anthropic" and retry_after is False:
            return made(SPEND_LIMIT)
        return made(RATE_LIMITED)
    return made(UNKNOWN)


def provider_message(error: BaseException) -> str:
    """An SDK error's own message, without the JSON around it — for an error that
    isn't about the key, where the provider's reason is the only useful thing.
    The caller scrubs it."""
    body = getattr(error, "body", None)
    err = _error_dict(body)
    if err is not None and isinstance(err.get("message"), str):
        return err["message"]
    message = getattr(error, "message", None)
    return message if isinstance(message, str) else ""


def from_exception(provider: str, error: BaseException) -> Optional[Failure]:
    """Classify an SDK's exception, or None when it carries no HTTP answer (a dropped
    socket: transient, and nothing to say about the key)."""
    from app.router.base import status_of

    status = status_of(error)  # type: ignore[arg-type]
    if status is None and type(error).__module__.startswith("google.api_core"):
        code = getattr(error, "code", None)
        status = code if isinstance(code, int) and 100 <= code < 600 else None
    if status is None:
        return None
    body = getattr(error, "body", None)
    headers = None
    response = getattr(error, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if body is None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = None
    if body is None and type(error).__module__.startswith("google.api_core"):
        # Google's client keeps the pieces as attributes, not a body.
        details: list = [{"reason": getattr(error, "reason", None)}] if getattr(error, "reason", None) else []
        # QuotaFailure and friends arrive as protobuf messages; their text names the
        # quota (`quota_id: "GenerateRequestsPerDay…"`) — enough to tell a day from a minute.
        for d in getattr(error, "details", None) or []:
            text_d = str(d)
            if text_d:
                details.append({"violations": [{"quotaId": text_d[:400]}]})
        body = {
            "error": {
                "status": type(error).__name__,
                "message": str(getattr(error, "message", "") or ""),
                "details": details,
            }
        }
    return classify(provider, status, body, headers, text=str(error))


# ── what to say ───────────────────────────────────────────────────────────────
#: Per provider: where to make a key, add credit, raise a limit, see status.
LINKS: dict[str, dict[str, str]] = {
    "openai": {
        "keys": "https://platform.openai.com/api-keys",
        "billing": "https://platform.openai.com/settings/organization/billing/overview",
        "limits": "https://platform.openai.com/settings/organization/limits",
        "status": "https://status.openai.com",
    },
    "anthropic": {
        "keys": "https://console.anthropic.com/settings/keys",
        "billing": "https://console.anthropic.com/settings/billing",
        "limits": "https://console.anthropic.com/settings/limits",
        "status": "https://status.anthropic.com",
    },
    "gemini": {
        "keys": "https://aistudio.google.com/app/apikey",
        "billing": "https://console.cloud.google.com/billing",
        "limits": "https://aistudio.google.com/usage",
        "status": "https://aistudio.google.com/status",
    },
    "stripe": {
        "keys": "https://dashboard.stripe.com/apikeys",
        "billing": "https://dashboard.stripe.com/account/onboarding",
        "status": "https://status.stripe.com",
    },
    "resend": {
        "keys": "https://resend.com/api-keys",
        "billing": "https://resend.com/settings/billing",
        "limits": "https://resend.com/settings/billing",
        "status": "https://resend-status.com",
    },
    "clerk": {"keys": "https://dashboard.clerk.com", "status": "https://status.clerk.com"},
}

LABELS = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini"}


@dataclass
class Advice:
    kind: str
    #: Two or three words for a badge: "Expired", "No credit".
    badge: str
    #: What happened, as a sentence without a full stop: "This OpenAI key has expired".
    title: str
    #: What to do about it.
    body: str
    action_label: str = ""
    action_url: str = ""
    #: True when the person must do something; false for a rate limit or an outage.
    blocking: bool = True

    def sentence(self) -> str:
        return f"{self.title}. {self.body}"

    def as_dict(self) -> dict:
        return asdict(self)


#: Two words, for "openai:gpt-x → no credit" and the "every model failed" line.
SHORT = {
    INVALID: "key not recognised",
    EXPIRED: "key expired",
    REVOKED: "key revoked or suspended",
    NO_CREDIT: "out of credit",
    SPEND_LIMIT: "spend limit reached",
    BILLING_DISABLED: "billing is off",
    PLAN_QUOTA: "plan quota used up",
    NOT_PERMITTED: "key lacks permission",
    REGION: "not offered in this region",
    RATE_LIMITED: "rate-limited",
    PROVIDER_DOWN: "having problems",
    UNKNOWN: "refused the request",
}


def advice(
    kind: Optional[str],
    provider: str,
    *,
    label: Optional[str] = None,
    links: Optional[Mapping[str, str]] = None,
    status: Optional[int] = None,
    code: Optional[str] = None,
) -> Advice:
    """The one place the words for each kind live."""
    who = label or LABELS.get(provider, provider.title() if provider else "The provider")
    url = {**LINKS.get(provider, {}), **(links or {})}
    keys = url.get("keys", "")
    billing = url.get("billing") or keys
    limits = url.get("limits") or billing
    kind = kind if kind in KINDS else UNKNOWN

    if kind == INVALID:
        return Advice(kind, "Not recognised", f"{who} didn't recognise this key",
                      "It may be cut short or belong to another account. Copy it again from your "
                      f"{who} dashboard.", "Copy the key again", keys)
    if kind == EXPIRED:
        return Advice(kind, "Expired", f"This {who} key has expired",
                      "An expired key can't be renewed. Create a new key and paste it here.",
                      "Create a new key", keys)
    if kind == REVOKED:
        return Advice(kind, "Revoked", f"This {who} key was revoked or suspended",
                      f"It was revoked, deleted or suspended — {who} also disables a key that leaked. "
                      "Create a new key, or ask the account's owner.", "Create a new key", keys)
    if kind == NO_CREDIT:
        return Advice(kind, "No credit", f"Your {who} account is out of credit",
                      "The key is fine, but the account has no credit left. Add credits, then try again.",
                      "Add credits", billing)
    if kind == SPEND_LIMIT:
        return Advice(kind, "Limit reached", f"Your {who} account reached its spending limit",
                      "The key is fine, but a usage or spend limit on the account has been reached. "
                      "Raise the limit, or wait for it to reset.", "Raise the limit", limits)
    if kind == BILLING_DISABLED:
        return Advice(kind, "Billing off", f"Billing is off for this {who} account",
                      "The key is fine, but the account's billing is off or its plan has lapsed. "
                      "Turn billing on or renew the plan.", "Turn on billing", billing)
    if kind == PLAN_QUOTA:
        return Advice(kind, "Quota used up", f"Your {who} plan's quota is used up",
                      "The plan's daily or monthly allowance has been reached. Upgrade the plan, "
                      "or wait for it to reset.", "See your plan", limits)
    if kind == NOT_PERMITTED:
        return Advice(kind, "No permission", f"This {who} key isn't allowed to do this",
                      "The key is real but restricted. Give it the permission it needs, or create "
                      "a key that has it.", "Check permissions", keys)
    if kind == REGION:
        return Advice(kind, "Region", f"{who} isn't available where this server runs",
                      f"Nothing is wrong with the key: {who} doesn't serve requests from this "
                      "server's country or region.", blocking=True)
    if kind == RATE_LIMITED:
        return Advice(kind, "Rate-limited", f"{who} is limiting requests right now",
                      "The key works. Requests are retried by themselves.", blocking=False)
    if kind == PROVIDER_DOWN:
        return Advice(kind, "Provider issue", f"{who} is having problems",
                      "Nothing to change on your side. Try again in a few minutes.",
                      "Check status" if url.get("status") else "", url.get("status", ""), blocking=False)
    said = " · ".join(str(x) for x in (f"HTTP {status}" if status else "", code or "") if x)
    return Advice(kind, "Refused", f"{who} refused the request" + (f" ({said})" if said else ""),
                  f"We couldn't tell why from its answer. Check the key and the account in your {who} "
                  "dashboard.", "Open dashboard" if keys else "", keys)
