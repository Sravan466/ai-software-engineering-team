"""Runtime-editable provider secrets, persisted to a gitignored local file.

Lets the Settings UI add/update cloud API keys at runtime without editing `.env`.
The file lives under the data dir (already gitignored) and is written owner-only.

This is intended for the *self-hosted* deployment model: the keys live on the
operator's own backend, never in the browser and never in version control.

Two more things live here beside the cloud keys, under reserved names, because they
are the same kind of fact — the operator's choices about where models come from:

    "local":   {"default_model": "<source>:<model>"}   the model every role falls back to
    "sources": [{id, label, base_url, api_key?, runtime}]  sources added in Settings

A runtime's API key is stored exactly like a cloud key — this file, owner-only — and
is returned to the browser only as a hint. Files written before model sources kept
the local default under the runtime's own name (`{"<runtime>": {"default_model":
...}}`); that is still read, and rewritten in the new shape on the next save.

Each account has its own file (`app.core.userdata`), held by a `ProviderStore`. The
module-level functions are the store at `_PATH` — the single global file from before
accounts, which the accounts migration moves into the first account's directory.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from app.core.atomic import write_private

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "providers.local.json"

#: Keys in the file that are not a provider's entry.
LOCAL_KEY = "local"
SOURCES_KEY = "sources"
RESERVED = frozenset({LOCAL_KEY, SOURCES_KEY})

#: Every change is read-modify-write of the whole file. Without one lock around each,
#: two saves at once — a key and a source, say — each write back the file as it was
#: before the other, and one of them is lost; with keys in it, that is a key gone.
#: One lock per file, so two accounts never wait on each other.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve()) if path.is_absolute() else str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


class ProviderStore:
    """One account's cloud keys, local default and added sources — one file."""

    def __init__(self, path: Callable[[], Path]) -> None:
        #: Resolved on every use, so a test that moves the directory moves the store.
        self._path = path

    @property
    def path(self) -> Path:
        return self._path()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 - a corrupt file should not crash startup
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        write_private(self.path, json.dumps(data, indent=2))

    def get_all(self) -> dict:
        """Mapping of provider -> {api_key?, default_model?}. Reserved keys are left out."""
        return {k: v for k, v in self._read().items() if k not in RESERVED and isinstance(v, dict)}

    def get_local_default(self, cloud: tuple[str, ...]) -> Optional[str]:
        """The local default the user chose, as `source:model` — or None if never chosen.

        A file from before model sources kept it under the runtime's own name, with a
        bare model; `cloud` says which names are not that. The runtime's name is the
        source the model was on, so it becomes the prefix — the same meaning it had.
        """
        data = self._read()
        local = data.get(LOCAL_KEY)
        if isinstance(local, dict) and isinstance(local.get("default_model"), str):
            return local["default_model"].strip() or None
        for name, entry in data.items():
            if name in RESERVED or name in cloud or not isinstance(entry, dict):
                continue
            model = entry.get("default_model")
            if isinstance(model, str) and model.strip():
                return f"{name}:{model.strip()}"
        return None

    def set_local_default(self, spec: Optional[str], cloud: tuple[str, ...]) -> None:
        """Record (or, with None, clear) the local default, dropping the old-shape copy."""
        with _lock_for(self.path):
            data = self._read()
            for name in [n for n in data if n not in RESERVED and n not in cloud]:
                entry = data.get(name)
                if isinstance(entry, dict):
                    entry.pop("default_model", None)
                    if not entry:
                        data.pop(name, None)
            if spec:
                data[LOCAL_KEY] = {"default_model": spec}
            else:
                data.pop(LOCAL_KEY, None)
            self._write(data)

    def get_sources(self) -> list[dict]:
        """Sources added in Settings, as saved. Malformed entries are skipped."""
        raw = self._read().get(SOURCES_KEY)
        if not isinstance(raw, list):
            return []
        return [e for e in raw if isinstance(e, dict) and e.get("id") and e.get("base_url")]

    def save_sources(self, sources: list[dict]) -> None:
        with _lock_for(self.path):
            data = self._read()
            if sources:
                data[SOURCES_KEY] = sources
            else:
                data.pop(SOURCES_KEY, None)
            self._write(data)

    def set_provider(
        self,
        provider: str,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
    ) -> None:
        """Upsert one provider's overrides.

        - api_key is None  -> leave the stored key unchanged
        - api_key == ""    -> remove the stored key
        - api_key == "..." -> store it
        """
        with _lock_for(self.path):
            data = self._read()
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
            self._write(data)


#: The global file from before accounts. Read through `_PATH` on every call, so a
#: test that points it elsewhere is honoured.
default_store = ProviderStore(lambda: _PATH)


def for_user(user_id: str) -> ProviderStore:
    """The store for one account's own file."""
    from app.core import userdata

    return ProviderStore(lambda: userdata.path(user_id, "providers.local.json"))


def get_all() -> dict:
    return default_store.get_all()


def get_local_default(cloud: tuple[str, ...]) -> Optional[str]:
    return default_store.get_local_default(cloud)


def set_local_default(spec: Optional[str], cloud: tuple[str, ...]) -> None:
    default_store.set_local_default(spec, cloud)


def get_sources() -> list[dict]:
    return default_store.get_sources()


def save_sources(sources: list[dict]) -> None:
    default_store.save_sources(sources)


def set_provider(
    provider: str,
    api_key: Optional[str] = None,
    default_model: Optional[str] = None,
) -> None:
    default_store.set_provider(provider, api_key, default_model)
