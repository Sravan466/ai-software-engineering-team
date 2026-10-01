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
from typing import Callable, Mapping, Optional

import httpx

from app.core import keyerrors
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
#: Of those, the ones about the key itself. MODEL_UNAVAILABLE is about one model —
#: the others the key can use still work.
KEY_REJECTED = frozenset({INVALID, BILLING, LOCKED})
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
LABEL = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google"}
#: How many of the models a key can use are kept, to show.
_MAX_MODELS = 60
_NOT_CHAT = re.compile(
    r"embed|tts|whisper|sora|dall-e|davinci|babbage|moderation|audio|transcribe|image|realtime|search|aqa|imagen|veo",
    re.I,
)

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
    #: Set when a build's own call, not a check, gave this verdict — checked again
    #: before the next build.
    during_build: bool = False

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
    # Chat models only, newest first: a provider lists embeddings, speech and image
    # models beside them, and alphabetical order puts its oldest names at the top.
    chat = [n for n in names if n and not _NOT_CHAT.search(n)]
    return sorted(chat, reverse=True)[:_MAX_MODELS]


def _context_tokens(provider: str, body: object) -> Optional[int]:
    if not isinstance(body, dict):
        return None
    value = body.get("max_input_tokens") if provider == "anthropic" else body.get("inputTokenLimit")
    return value if isinstance(value, int) and value > 0 else None


# ── classifying an answer ─────────────────────────────────────────────────────
def _answer(response: httpx.Response) -> tuple[object, str]:
    """The provider's error body (parsed, when it is JSON) and its raw text — read
    by the classifier, never kept."""
    try:
        return response.json(), ""
    except ValueError:
        return None, response.text[:500]


def _says(text: str, *words: str) -> bool:
    return any(w in text for w in words)


#: The status a key is given for each kind the shared classifier can return.
_STATUS_FOR = {
    keyerrors.INVALID: INVALID,
    keyerrors.EXPIRED: INVALID,
    keyerrors.REVOKED: INVALID,
    keyerrors.REGION: INVALID,
    keyerrors.NOT_PERMITTED: INVALID,
    keyerrors.NO_CREDIT: BILLING,
    keyerrors.SPEND_LIMIT: BILLING,
    keyerrors.BILLING_DISABLED: BILLING,
    keyerrors.PLAN_QUOTA: BILLING,
}
#: The reason code the page reads. `rejected` stays the mistyped key's, as before.
_REASON_FOR = {
    keyerrors.INVALID: "rejected",
    keyerrors.REGION: "region_not_supported",
    keyerrors.NOT_PERMITTED: "not_permitted",
}


def _verdict(
    provider: str,
    status: int,
    body: object,
    model: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    text: str = "",
) -> KeyCheck:
    """What an error answer means for this key. `body` and `text` are only read here."""
    who = LABEL.get(provider, provider)
    failure = keyerrors.classify(provider, status, body, headers, text=text)
    words = keyerrors.signals(body, text)

    def made(state: str, reason: str, message: str) -> KeyCheck:
        return KeyCheck(status=state, reason=reason, message=message, model=model)

    kind = failure.kind
    if kind in (keyerrors.INVALID, keyerrors.NOT_PERMITTED) and status in (401, 403):
        # Two shapes of "not allowed" that aren't about the key's spelling.
        if _says(words, "scope", "insufficient permissions", "model.request"):
            return made(INVALID, "restricted", "This key can't make requests — it is restricted. Give it permission to use models, or create a key that can.")
        if re.search(r"\bip\b", words) and "invalid_api_key" not in words:
            return made(INVALID, "network_not_allowed", f"{who} refused this key from this network (its IP allowlist doesn't include this server).")
    if kind == keyerrors.NOT_PERMITTED and status == 403 and _says(words, "model"):
        return made(MODEL_UNAVAILABLE, "model_not_permitted", f"This key isn't allowed to use {model}.")
    if kind in _STATUS_FOR:
        advice = keyerrors.advice(kind, provider, label=who)
        return made(_STATUS_FOR[kind], _REASON_FOR.get(kind, kind), advice.sentence())
    if status == 404:
        return made(MODEL_UNAVAILABLE, "model_not_found", f"{model} isn't available to this key.")
    if status == 400 and _says(words, "model") and _says(
        words, "not found", "does not exist", "not supported", "not a chat model", "invalid model", "unknown model",
    ):
        return made(MODEL_UNAVAILABLE, "model_not_supported", f"{model} can't be used for chat with this key.")
    if kind == keyerrors.RATE_LIMITED:
        return made(RATE_LIMITED, "rate_limited", f"The key works; {who} is limiting requests right now.")
    if kind == keyerrors.PROVIDER_DOWN:
        return made(UNVERIFIED, "provider_error", f"{who} had a problem answering. The key is saved but not verified yet.")
    # Nothing we recognise. Not "the key works" — that turned every new error shape
    # into a green badge — but not a verdict on the key either: saved, unverified,
    # and checked again before a build, with what little the answer did say.
    advice = keyerrors.advice(keyerrors.UNKNOWN, provider, label=who, status=status, code=failure.code)
    return made(UNVERIFIED, "unknown", f"{advice.title}. The key is saved but not verified — check it in your {who} dashboard.")


def verdict_for(provider: str, kind: Optional[str], model: str) -> Optional[KeyCheck]:
    """A build's own failed call, already classified — only when it condemns the key."""
    if kind not in _STATUS_FOR or kind == keyerrors.NOT_PERMITTED:
        # Not permitted mid-build is about what was asked, not the key everywhere.
        return None
    advice = keyerrors.advice(kind, provider, label=LABEL.get(provider, provider))
    return KeyCheck(
        status=_STATUS_FOR[kind],
        reason=_REASON_FOR.get(kind, kind),
        message=f"{advice.title} — a build's call was refused. {advice.body}",
        model=model,
        checked_at=_now(),
        during_build=True,
    )


def verdict_from_error(provider: str, status: Optional[int], text: str, model: str) -> Optional[KeyCheck]:
    """A failed call known only by its status and text — classified, then as above."""
    if status is None:
        return None
    return verdict_for(provider, keyerrors.classify(provider, status, None, None, text=text or "").kind, model)


# ── the check ────────────────────────────────────────────────────────────────
def kind_of(found: KeyCheck) -> Optional[str]:
    """A check's reason as a `keyerrors` kind, when it is one."""
    if found.reason in keyerrors.KINDS:
        return found.reason
    return {
        "rejected": keyerrors.INVALID,
        REJECTED_DURING_BUILD: keyerrors.INVALID,
        "region_not_supported": keyerrors.REGION,
        "restricted": keyerrors.NOT_PERMITTED,
        # `network_not_allowed` has no kind on purpose: its own sentence (allow this
        # server's IP) is the fix, and "check the key's permissions" would hide it.
    }.get(found.reason)


def check(
    provider: str, key: str, model: str, *, via: Optional[httpx.BaseTransport] = None
) -> KeyCheck:
    """Check `key` against `model`. Never raises; never waits past the timeout per step.

    `via` is another module's test transport (the connectors'), when it has one.
    """
    started = time.perf_counter()
    result = _check(provider, key, model, via)
    result.checked_at = _now()
    result.model = model
    result.key_id = key_id(key)
    log.info(
        "Key check: %s against %s -> %s (%s) in %.1fs",
        provider, model, result.status, result.reason, time.perf_counter() - started,
    )
    return result


def _check(provider: str, key: str, model: str, via: Optional[httpx.BaseTransport] = None) -> KeyCheck:
    who = LABEL.get(provider, provider)
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
            transport=via if via is not None else transport,
            follow_redirects=False,
        ) as client:
            # 1 — free: does the key authenticate, and is the model there for it?
            r = client.get(_model_path(provider, model))
            if r.status_code != 200:
                body, raw = _answer(r)
                found = _verdict(provider, r.status_code, body, model, headers=r.headers, text=raw)
                if found.status == MODEL_UNAVAILABLE:
                    found.models = _list_models(client, provider)
                # A key restricted to making requests may not be allowed to *read*
                # models — the safer kind of key. The one-token request decides.
                if found.status != VALID and found.reason != "restricted":
                    return found
            try:
                context = _context_tokens(provider, r.json()) if r.status_code == 200 else None
            except ValueError:
                context = None
            # 2 — one token: can it actually make a request, on an account that pays?
            path, body = _one_token(provider, model)
            r = client.post(path, json=body)
            if r.status_code == 200:
                return KeyCheck(status=VALID, reason="ok", message=f"The key works with {model}.", context_tokens=context)
            body, raw = _answer(r)
            found = _verdict(provider, r.status_code, body, model, headers=r.headers, text=raw)
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
