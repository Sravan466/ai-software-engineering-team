"""Checking that a cloud API key actually works, for the model it will be used with.

A key used to count as working when it was not empty, so a mistyped one showed as
available and failed halfway through a build. A check is two requests, each bounded
(`KEY_CHECK_TIMEOUT_SECONDS`, no retries, no fallback to anything else):

1. **Free:** fetch the chosen model. That proves the key authenticates and the model
   is there for it. When the model isn't, the models the key *can* use are listed.
2. **One token:** a real request for one output token. Listing models is not enough —
   an OpenAI key can be allowed to read models but not to request them, and a key on
   an account with no credit authenticates fine and fails on the first real call.

The result is classified into a status and a reason code. The provider's own message
is read to classify it and is then dropped — it may quote the key — so what is kept,
shown and logged is only ever this module's own sentence.

The requests are made with httpx directly, not the SDKs: the key goes in a header
(Gemini's too — never `?key=`, which lands in access logs), and nothing retries.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# ── statuses ──────────────────────────────────────────────────────────────────
VALID = "valid"  # ✅ works for the chosen model
RATE_LIMITED = "rate_limited"  # ✅ works; the provider is limiting requests right now
INVALID = "invalid"  # ❌ rejected, or not allowed from here
MODEL_UNAVAILABLE = "model_unavailable"  # ⚠️ the key works, the chosen model isn't there for it
BILLING = "billing"  # ⚠️ the key works, the account has no credit
UNVERIFIED = "unverified"  # ❔ the provider couldn't be reached to check
UNCHECKED = "unchecked"  # never checked (a key from `.env`, or saved before checks)
LOCKED = "locked"  # saved under another encryption key; can't be read

#: A key in one of these is not used: routing skips it and a build that names it
#: is refused before it starts.
REJECTED = frozenset({INVALID, MODEL_UNAVAILABLE, BILLING, LOCKED})
#: Worth checking again before a build, whatever the time since the last check.
UNSETTLED = frozenset({UNVERIFIED, UNCHECKED})

#: Reason code for a key a build's own call saw rejected.
REJECTED_DURING_BUILD = "rejected_during_build"

_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
_ANTHROPIC_VERSION = "2023-06-01"
_LABEL = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google"}
#: How many of the models a key can use are kept, to show.
_MAX_MODELS = 60

#: Tests replace this with an `httpx.MockTransport`; nothing else should.
transport: Optional[httpx.BaseTransport] = None


@dataclass
class KeyCheck:
    status: str
    #: A stable code the page picks its advice and links from.
    reason: str
    #: One sentence of our own. Never the provider's text.
    message: str
    checked_at: str = ""
    #: The model the key was checked against.
    model: Optional[str] = None
    #: The models this key can use — filled when the chosen one isn't among them.
    models: list[str] = field(default_factory=list)
    #: The model's input window, when the provider publishes it per model.
    context_tokens: Optional[int] = None
    #: Which key this is about: a truncated hash, so a verdict never outlives its key.
    key_id: Optional[str] = None

    @property
    def rejected(self) -> bool:
        return self.status in REJECTED

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> Optional["KeyCheck"]:
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            return None
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None

    def age_seconds(self) -> float:
        try:
            then = datetime.fromisoformat(self.checked_at)
        except (TypeError, ValueError):
            return float("inf")
        return (datetime.now(timezone.utc) - then).total_seconds()


def key_id(key: str) -> str:
    """Enough of a hash to tell two keys apart; nothing that leads back to the key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unchecked(key: Optional[str], model: Optional[str]) -> KeyCheck:
    return KeyCheck(
        status=UNCHECKED,
        reason="not_checked",
        message="Not checked yet — it will be before a build uses it.",
        model=model,
        key_id=key_id(key) if key else None,
    )


# ── one provider's requests ───────────────────────────────────────────────────
def _headers(provider: str, key: str) -> dict:
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}
    if provider == "gemini":
        return {"x-goog-api-key": key}
    return {"Authorization": f"Bearer {key}"}


def _model_path(provider: str, model: str) -> str:
    if provider == "gemini":
        return f"/models/{model.removeprefix('models/')}"
    return f"/models/{model}"


def _one_token(provider: str, model: str) -> tuple[str, dict]:
    ping = "ping"
    if provider == "anthropic":
        return "/messages", {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": ping}]}
    if provider == "gemini":
        return (
            f"{_model_path(provider, model)}:generateContent",
            {"contents": [{"role": "user", "parts": [{"text": ping}]}], "generationConfig": {"maxOutputTokens": 1}},
        )
    # `max_completion_tokens` is what every current chat model accepts; `max_tokens`
    # is refused by the reasoning ones.
    return "/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": ping}],
        "max_completion_tokens": 1,
    }


def _list_models(client: httpx.Client, provider: str) -> list[str]:
    try:
        if provider == "gemini":
            r = client.get("/models", params={"pageSize": 200})
            items = r.json().get("models", []) if r.status_code == 200 else []
            names = [
                str(m.get("name", "")).removeprefix("models/")
                for m in items
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
            ]
        else:
            params = {"limit": 100} if provider == "anthropic" else None
            r = client.get("/models", params=params)
            items = r.json().get("data", []) if r.status_code == 200 else []
            names = [str(m.get("id", "")) for m in items]
    except (httpx.HTTPError, ValueError, AttributeError):
        return []
    return sorted(n for n in names if n)[:_MAX_MODELS]


def _context_tokens(provider: str, body: object) -> Optional[int]:
    if not isinstance(body, dict):
        return None
    value = body.get("max_input_tokens") if provider == "anthropic" else body.get("inputTokenLimit")
    return value if isinstance(value, int) and value > 0 else None


# ── classifying an answer ─────────────────────────────────────────────────────
def _error_text(response: httpx.Response) -> str:
    """The provider's error type, code and message, lowercased — read, never kept."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500].lower()
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        parts = [err.get("type"), err.get("code"), err.get("status"), err.get("message")]
        for detail in err.get("details") or []:
            if isinstance(detail, dict):
                parts.append(detail.get("reason"))
        return " ".join(str(p) for p in parts if p).lower()
    return str(body)[:500].lower()


def _says(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def _verdict(provider: str, status: int, text: str, model: str) -> KeyCheck:
    """What an error answer means for this key. `text` is only read here."""
    who = _LABEL.get(provider, provider)

    def made(state: str, reason: str, message: str) -> KeyCheck:
        return KeyCheck(status=state, reason=reason, message=message, model=model)

    billing = _says(
        text, "credit balance", "insufficient_quota", "billing", "spend limit", "exceeded your current quota",
        "payment",
    )
    if billing and status in (400, 402, 403, 429):
        return made(BILLING, "no_credit", f"The key works, but the {who} account has no credit or has reached its spend limit.")
    if status == 401 or (provider == "gemini" and _says(text, "api_key_invalid", "api key not valid", "api key expired")):
        if re.search(r"\bip\b", text) and "invalid_api_key" not in text:
            return made(INVALID, "network_not_allowed", f"{who} refused this key from this network (its IP allowlist doesn't include this server).")
        if _says(text, "scope", "insufficient permissions", "model.request"):
            return made(INVALID, "restricted", "This key can't make requests — it is restricted. Give it permission to use models, or create a key that can.")
        return made(INVALID, "rejected", f"{who} rejected this key. Check it was copied completely, or create a new one.")
    if status == 403:
        if _says(text, "country", "region", "territory", "unsupported_country"):
            return made(INVALID, "region_not_supported", f"{who} doesn't serve requests from the country or region this server is in.")
        if _says(text, "scope", "insufficient permissions", "model.request"):
            return made(INVALID, "restricted", "This key can't make requests — it is restricted. Give it permission to use models, or create a key that can.")
        if _says(text, "model"):
            return made(MODEL_UNAVAILABLE, "model_not_permitted", f"This key isn't allowed to use {model}.")
        return made(INVALID, "not_permitted", f"{who} says this key isn't allowed to do that. Check the key's permissions.")
    if status == 404:
        return made(MODEL_UNAVAILABLE, "model_not_found", f"{model} isn't available to this key.")
    if status == 429:
        return made(RATE_LIMITED, "rate_limited", f"The key works; {who} is limiting requests right now.")
    if status == 400 and _says(text, "model") and _says(
        text, "not found", "does not exist", "not supported", "not a chat model", "invalid model", "unknown model",
    ):
        return made(MODEL_UNAVAILABLE, "model_not_supported", f"{model} can't be used for chat with this key.")
    if status >= 500:
        return made(UNVERIFIED, "provider_error", f"{who} had a problem answering. The key is saved but not verified yet.")
    # Any other 4xx came from past authentication: the key was accepted, and it was
    # the request's shape the provider disliked. That is ours to fix, not the user's.
    return made(VALID, "accepted", "The key works.")


def verdict_from_error(provider: str, status: Optional[int], text: str, model: str) -> Optional[KeyCheck]:
    """A build's own failed call, read the same way — only when it condemns the key."""
    if status is None:
        return None
    found = _verdict(provider, status, (text or "").lower(), model)
    if found.status in (INVALID, BILLING):
        found.reason = REJECTED_DURING_BUILD if found.status == INVALID else found.reason
        if found.status == INVALID:
            found.message = f"{_LABEL.get(provider, provider)} rejected this key during a build."
        found.checked_at = _now()
        return found
    return None


# ── the check ────────────────────────────────────────────────────────────────
def check(provider: str, key: str, model: str, *, spend: bool = True) -> KeyCheck:
    """Check `key` against `model`. Never raises; never waits past the timeout per step."""
    started = time.perf_counter()
    result = _check(provider, key, model, spend=spend)
    result.checked_at = _now()
    result.model = model
    result.key_id = key_id(key)
    log.info(
        "Key check: %s against %s -> %s (%s) in %.1fs",
        provider, model, result.status, result.reason, time.perf_counter() - started,
    )
    return result


def _check(provider: str, key: str, model: str, *, spend: bool) -> KeyCheck:
    who = _LABEL.get(provider, provider)
    if provider not in _BASE:
        return KeyCheck(status=INVALID, reason="unknown_provider", message=f"'{provider}' isn't a cloud provider.")
    if not model:
        return KeyCheck(status=UNVERIFIED, reason="no_model", message="Choose a default model to check the key against.")
    timeout = httpx.Timeout(max(float(settings.key_check_timeout_seconds), 1.0))
    try:
        with httpx.Client(
            base_url=_BASE[provider],
            headers=_headers(provider, key),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        ) as client:
            # 1 — free: does the key authenticate, and is the model there for it?
            r = client.get(_model_path(provider, model))
            if r.status_code != 200:
                found = _verdict(provider, r.status_code, _error_text(r), model)
                if found.status == MODEL_UNAVAILABLE:
                    found.models = _list_models(client, provider)
                if found.status != VALID:
                    return found
            try:
                context = _context_tokens(provider, r.json()) if r.status_code == 200 else None
            except ValueError:
                context = None
            if not spend:
                return KeyCheck(status=VALID, reason="authenticated", message="The key authenticates.", context_tokens=context)

            # 2 — one token: can it actually make a request, on an account that pays?
            path, body = _one_token(provider, model)
            r = client.post(path, json=body)
            if r.status_code == 200:
                return KeyCheck(status=VALID, reason="ok", message=f"The key works with {model}.", context_tokens=context)
            found = _verdict(provider, r.status_code, _error_text(r), model)
            if found.status == MODEL_UNAVAILABLE:
                found.models = _list_models(client, provider)
            found.context_tokens = context
            return found
    except httpx.TimeoutException:
        return KeyCheck(status=UNVERIFIED, reason="timeout", message=f"{who} didn't answer in time. The key is saved but not verified yet.")
    except httpx.HTTPError:
        return KeyCheck(status=UNVERIFIED, reason="unreachable", message=f"Couldn't reach {who} to check. The key is saved but not verified yet.")
    except Exception:  # noqa: BLE001 - a check must never take Save down with it
        log.exception("Key check for %s failed unexpectedly", provider)
        return KeyCheck(status=UNVERIFIED, reason="check_failed", message="The check itself failed. The key is saved but not verified yet.")


# ── rate limiting ─────────────────────────────────────────────────────────────
class RateLimiter:
    """At most N checks per account per window — a check route is otherwise a free
    way to try stolen keys against the providers."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        with self._lock:
            now = self._clock()
            window = max(settings.key_check_window_seconds, 1)
            hits = [t for t in self._hits.get(who, []) if now - t < window]
            allowed = len(hits) < max(settings.key_checks_per_window, 1)
            if allowed:
                hits.append(now)
            self._hits[who] = hits
            return allowed

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
