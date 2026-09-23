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

Each account has its own file, held by a `ModelSettingsStore`. The module-level
functions are the store at `_PATH`, the global file from before accounts.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app.core.atomic import write_private
from app.core.logging import get_logger

log = get_logger(__name__)

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "model_settings.local.json"


class _Unreadable(Exception):
    """The file is there but isn't a settings file: never write over it blind."""


class ModelSettingsStore:
    """One account's per-model generation settings — one file."""

    def __init__(self, path: Callable[[], Path]) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            raise _Unreadable(str(e)) from e
        if not isinstance(data, dict):
            raise _Unreadable("the file doesn't hold an object")
        return data

    def _read(self) -> dict:
        """What is saved — `{}` for a corrupt file, which must not stop the server booting."""
        try:
            return self._load()
        except _Unreadable:
            return {}

    def all_settings(self) -> dict[str, dict]:
        """spec -> stored settings, as written. Callers re-validate what they use."""
        return {k: v for k, v in self._read().items() if isinstance(k, str) and isinstance(v, dict)}

    def get(self, spec: str) -> Optional[dict]:
        entry = self._read().get(spec)
        return entry if isinstance(entry, dict) else None

    def put(self, spec: str, values: Optional[dict]) -> None:
        """Save `values` for `spec`, replacing what was there. Empty or None clears it."""
        path = self._path()
        with self._lock:
            try:
                data = self._load()
            except _Unreadable as e:
                # Rewriting it now would save this one model and drop every other one's
                # settings; the file is set aside instead, so nothing is lost.
                aside = path.with_name(f"{path.name}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
                path.replace(aside)
                log.warning("%s couldn't be read (%s); moved it to %s and started afresh.", path, e, aside)
                data = {}
            if values:
                data[spec] = values
            else:
                data.pop(spec, None)
            write_private(path, json.dumps(data, indent=2, sort_keys=True))


#: The global file from before accounts, read through `_PATH` on every call.
default_store = ModelSettingsStore(lambda: _PATH)


def for_user(user_id: str) -> ModelSettingsStore:
    """The store for one account's own file."""
    from app.core import userdata

    return ModelSettingsStore(lambda: userdata.path(user_id, "model_settings.local.json"))


def all_settings() -> dict[str, dict]:
    return default_store.all_settings()


def get(spec: str) -> Optional[dict]:
    return default_store.get(spec)


def put(spec: str, values: Optional[dict]) -> None:
    default_store.put(spec, values)
