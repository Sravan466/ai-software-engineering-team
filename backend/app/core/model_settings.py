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
import time
from pathlib import Path
from typing import Optional

from app.core.atomic import write_private
from app.core.logging import get_logger

log = get_logger(__name__)

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "model_settings.local.json"

_LOCK = threading.Lock()


class _Unreadable(Exception):
    """The file is there but isn't a settings file: never write over it blind."""


def _load() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        raise _Unreadable(str(e)) from e
    if not isinstance(data, dict):
        raise _Unreadable("the file doesn't hold an object")
    return data


def _read() -> dict:
    """What is saved — `{}` for a corrupt file, which must not stop the server booting."""
    try:
        return _load()
    except _Unreadable:
        return {}


def all_settings() -> dict[str, dict]:
    """spec -> stored settings, as written. Callers re-validate what they use."""
    return {k: v for k, v in _read().items() if isinstance(k, str) and isinstance(v, dict)}


def get(spec: str) -> Optional[dict]:
    entry = _read().get(spec)
    return entry if isinstance(entry, dict) else None


def put(spec: str, values: Optional[dict]) -> None:
    """Save `values` for `spec`, replacing what was there. Empty or None clears it."""
    with _LOCK:
        try:
            data = _load()
        except _Unreadable as e:
            # Rewriting it now would save this one model and drop every other one's
            # settings; the file is set aside instead, so nothing is lost.
            aside = _PATH.with_name(f"{_PATH.name}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            _PATH.replace(aside)
            log.warning("%s couldn't be read (%s); moved it to %s and started afresh.", _PATH, e, aside)
            data = {}
        if values:
            data[spec] = values
        else:
            data.pop(spec, None)
        write_private(_PATH, json.dumps(data, indent=2, sort_keys=True))
