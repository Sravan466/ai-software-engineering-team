"""Runtime-editable provider secrets, persisted to a gitignored local file.

Lets the Settings UI add/update cloud API keys at runtime without editing `.env`.
The file lives under the data dir (already gitignored) and is written owner-only.

This is intended for the *self-hosted* deployment model: the keys live on the
operator's own backend, never in the browser and never in version control.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "providers.local.json"


def _read() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt file should not crash startup
        return {}


def _write(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(_PATH, 0o600)
    except OSError:
        pass


def get_all() -> dict:
    """Mapping of provider -> {api_key?, default_model?}."""
    return _read()


def set_provider(
    provider: str,
    api_key: Optional[str] = None,
    default_model: Optional[str] = None,
) -> None:
    """Upsert one provider's overrides.

    - api_key is None  -> leave the stored key unchanged
    - api_key == ""    -> remove the stored key
    - api_key == "..." -> store it
    """
    data = _read()
    entry = dict(data.get(provider, {}))

    if api_key is not None:
        if api_key == "":
            entry.pop("api_key", None)
        else:
            entry["api_key"] = api_key
    if default_model:
        entry["default_model"] = default_model

    if entry:
        data[provider] = entry
    else:
        data.pop(provider, None)
    _write(data)
