"""Generation settings saved per model, persisted locally beside each role's model.

The same persistence `model_roles` uses: a gitignored file under `data/`, rewritten
whole under a lock and replaced atomically, owner-only. One entry per model, keyed
by its spec (`source:model`), holding only what somebody set — an absent entry, or
an absent field, means "whatever the running server or the model file says", so
this file is empty until someone tunes a model, and nothing is ever written here by
the application on its own.

    {"<source>:<model>": {"sampling": {...}, "limits": {...}, "machine": {...}}}

Values are validated by `app.router.generation` before they are written, and again
field by field when read, because the file can be edited by hand.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from app.core.atomic import write_private

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "model_settings.local.json"

_LOCK = threading.Lock()


def _read() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt file must not stop the server booting
        return {}
    return data if isinstance(data, dict) else {}


def all_settings() -> dict[str, dict]:
    """spec -> stored settings, as written. Callers re-validate what they use."""
    return {k: v for k, v in _read().items() if isinstance(k, str) and isinstance(v, dict)}


def get(spec: str) -> Optional[dict]:
    entry = _read().get(spec)
    return entry if isinstance(entry, dict) else None


def put(spec: str, values: Optional[dict]) -> None:
    """Save `values` for `spec`, replacing what was there. Empty or None clears it."""
    with _LOCK:
        data = _read()
        if values:
            data[spec] = values
        else:
            data.pop(spec, None)
        write_private(_PATH, json.dumps(data, indent=2, sort_keys=True))
