"""How a cloud provider holds its API key.

As a `SecretStr`, so a repr or a debug dump of the provider prints `**********`; the
key itself is read with `secret()` only at the moment a request is made. `usable` is
the last key check's verdict — set by the router — so a key the provider rejected is
not `available()` however present it is.
"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import SecretStr

from app.core import scrub
from app.router.model_profile import ModelProfile, fallback_profile


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

    def profile(self, model: str) -> ModelProfile:
        window = self.known_context.get(model)
        if window:
            return fallback_profile(self.name, model, context_limit=window, source="probe")
        return super().profile(model)  # type: ignore[misc]
