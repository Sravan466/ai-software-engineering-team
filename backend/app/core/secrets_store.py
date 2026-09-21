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
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from app.core.atomic import write_private

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
    write_private(_PATH, json.dumps(data, indent=2))


#: Every change is read-modify-write of the whole file. Without one lock around each,
#: two saves at once — a key and a source, say — each write back the file as it was
#: before the other, and one of them is lost; with keys in it, that is a key gone.
_LOCK = threading.RLock()


#: Keys in the file that are not a provider's entry.
LOCAL_KEY = "local"
SOURCES_KEY = "sources"
RESERVED = frozenset({LOCAL_KEY, SOURCES_KEY})


def get_all() -> dict:
    """Mapping of provider -> {api_key?, default_model?}. Reserved keys are left out."""
    return {k: v for k, v in _read().items() if k not in RESERVED and isinstance(v, dict)}


def get_local_default(cloud: tuple[str, ...]) -> Optional[str]:
    """The local default the user chose, as `source:model` — or None if never chosen.

    A file from before model sources kept it under the runtime's own name, with a
    bare model; `cloud` says which names are not that. The runtime's name is the
    source the model was on, so it becomes the prefix — the same meaning it had.
    """
    data = _read()
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


def set_local_default(spec: Optional[str], cloud: tuple[str, ...]) -> None:
    """Record (or, with None, clear) the local default, dropping the old-shape copy."""
    with _LOCK:
        _set_local_default(spec, cloud)


def _set_local_default(spec: Optional[str], cloud: tuple[str, ...]) -> None:
    data = _read()
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
    _write(data)


def get_sources() -> list[dict]:
    """Sources added in Settings, as saved. Malformed entries are skipped."""
    raw = _read().get(SOURCES_KEY)
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("id") and e.get("base_url")]


def save_sources(sources: list[dict]) -> None:
    with _LOCK:
        data = _read()
        if sources:
            data[SOURCES_KEY] = sources
        else:
            data.pop(SOURCES_KEY, None)
        _write(data)


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
    with _LOCK:
        _set_provider(provider, api_key, default_model)


def _set_provider(provider: str, api_key: Optional[str], default_model: Optional[str]) -> None:
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
