"""How a cloud provider holds its API key.

As a `SecretStr`, so a repr or a debug dump of the provider prints `**********`; the
key itself is read with `secret()` only at the moment a request is made. `usable` is
the last key check's verdict — set by the router — so a key the provider rejected is
not `available()` however present it is.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Union

from pydantic import SecretStr

from app.core import scrub
from app.core.logging import get_logger
from app.router.model_profile import ModelProfile, fallback_profile

log = get_logger(__name__)

#: A refusal that names the model's own reply limit (#101): OpenAI's "supports at most
#: N completion tokens", Anthropic's "max_tokens: X > N".
_REPLY_LIMIT = re.compile(r"supports at most (\d+) completion tokens|max_tokens: \d+ > (\d+)")


def _clean(key: Union[str, SecretStr, None]) -> Optional[SecretStr]:
    raw = key.get_secret_value() if isinstance(key, SecretStr) else key
    raw = (raw or "").strip()
    if not raw:
        return None
    scrub.register(raw)
    return SecretStr(raw)


class CloudKey:
    _key: Optional[SecretStr] = None
    #: False when the last check said this key won't work (rejected, no credit,
    #: model not available to it). Set by the router, never by the provider.
    usable: bool = True

    def _init_key(self, key: Union[str, SecretStr, None]) -> None:
        self._key = _clean(key)
        self.usable = True
        #: Input windows the provider reported per model when the key was checked —
        #: read, where it publishes one, rather than configured.
        self.known_context: dict[str, int] = {}
        #: Each model's own reply limit, read the same way (#101).
        self.known_output: dict[str, int] = {}

    def set_api_key(self, key: Union[str, SecretStr, None]) -> None:
        """Replace the key (Settings), and drop anything built on the old one."""
        scrub.forget(self.secret())
        self._key = _clean(key)
        self.usable = True
        if hasattr(self, "_client"):
            self._client = None

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def secret(self) -> Optional[str]:
        return self._key.get_secret_value() if self._key is not None else None

    def key_hint(self) -> Optional[str]:
        """The last four characters — all the API ever says about a key."""
        raw = self.secret()
        if not raw:
            return None
        return "…" + raw[-4:] if len(raw) >= 12 else "set"

    def available(self) -> bool:
        return self._key is not None and self.usable

    def reply_ceiling(self, model: str) -> Optional[int]:
        """The most one reply from `model` may be (#101).

        Its own published limit, when a key check read one — never above this
        provider's configured ceiling, which also bounds the bill and how long a reply
        may take. The configured ceiling as is for the configured default model, whose
        limit it was set from. Any other model: None, the local default every model
        takes, since sent a larger `max_tokens` than it allows a model refuses the call.
        """
        configured = getattr(self, "output_tokens", None)
        own = self.known_output.get(model)
        if own:
            return min(own, configured) if configured else own
        return configured if model == getattr(self, "default_model", None) else None

    def within_reply_limit(self, model: str, kwargs: dict, create: Callable, key: str = "max_tokens"):
        """`create(**kwargs)`, sent once more within the model's reply limit when it
        refuses one too large and names its own (#101): a model whose limit no check
        could read learns it here, for this call and every later one."""
        try:
            return create(**kwargs)
        except Exception as e:  # noqa: BLE001 - only a refusal naming the limit is handled
            found = _REPLY_LIMIT.search(str(e))
            limit = int(found.group(1) or found.group(2)) if found else 0
            if not limit or limit >= int(kwargs.get(key) or 0):
                raise
            self.known_output[model] = limit
            log.warning("%s:%s takes replies of at most %s tokens; sent once more within that.",
                        getattr(self, "name", "cloud"), model, f"{limit:,}")
            return create(**{**kwargs, key: limit})

    def profile(self, model: str) -> ModelProfile:
        window = self.known_context.get(model)
        published = getattr(self, "context_tokens", None)
        return fallback_profile(
            self.name,
            model,
            context_limit=window or published,
            source="probe" if window else ("configured" if published else "fallback"),
            max_output=self.reply_ceiling(model),
        )
