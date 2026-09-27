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

Every API key in the file — a cloud provider's or a source's — is encrypted
(`app.core.secretbox`); a file from before that is encrypted in place at startup
(`migrate_all`). Beside each cloud key sits its last check (`"check"`): status,
reason, when, and which key it was about — never the provider's own words.

A file that can't be read is an error, never an empty file: reading it as empty
used to make the next save write back one provider and erase the rest.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from app.core import secretbox
from app.core.atomic import write_private
from app.core.logging import get_logger

log = get_logger(__name__)

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


class StoreUnreadable(RuntimeError):
    """The settings file exists and can't be read. It is left exactly as it is."""


def reveal(value: object) -> Optional[str]:
    """A saved key, decrypted. Raises `secretbox.SecretsLocked` if it can't be."""
    return secretbox.decrypt(value) if isinstance(value, str) else None


def _sealed(data: dict) -> tuple[dict, int]:
    """`data` with every plaintext key in it encrypted, and how many there were."""
    count = 0
    for name, entry in data.items():
        if name in RESERVED or not isinstance(entry, dict):
            continue
        key = entry.get("api_key")
        if isinstance(key, str) and key and not secretbox.is_encrypted(key):
            entry["api_key"] = secretbox.encrypt(key)
            count += 1
    sources = data.get(SOURCES_KEY)
    if isinstance(sources, list):
        for entry in sources:
            key = entry.get("api_key") if isinstance(entry, dict) else None
            if isinstance(key, str) and key and not secretbox.is_encrypted(key):
                entry["api_key"] = secretbox.encrypt(key)
                count += 1
    return data, count


class ProviderStore:
    """One account's cloud keys, local default and added sources — one file."""

    def __init__(self, path: Callable[[], Path]) -> None:
        #: Resolved on every use, so a test that moves the directory moves the store.
        self._path = path
        #: Why the file couldn't be read, the last time it couldn't. Shown in Settings.
        self.error: Optional[str] = None

    @property
    def path(self) -> Path:
        return self._path()

    def _read(self, *, strict: bool = False) -> dict:
        """The file's content. Unreadable: `{}` for a reader, an error for a writer —
        so nothing is ever saved over a file that could not be read."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("it doesn't hold a JSON object")
        except FileNotFoundError:
            self.error = None
            return {}
        except Exception as e:  # noqa: BLE001 - reported, never treated as empty on write
            self.error = (
                f"The saved settings file ({self.path.name}) can't be read, so nothing is being "
                f"saved over it: {type(e).__name__}. Restore it from a backup, or move it aside "
                "to start again."
            )
            log.error("%s", self.error)
            if strict:
                raise StoreUnreadable(self.error) from e
            return {}
        self.error = None
        return data

    def _write(self, data: dict) -> None:
        data, _ = _sealed(data)
        write_private(self.path, json.dumps(data, indent=2))

    def get_all(self) -> dict:
        """Mapping of provider -> {api_key?, default_model?, check?, locked?}.

        Keys come back decrypted. One that can't be decrypted comes back as
        `locked: True`, with no key — and stays in the file as it was.
        """
        out: dict = {}
        for name, entry in self._read().items():
            if name in RESERVED or not isinstance(entry, dict):
                continue
            entry = dict(entry)
            if "api_key" in entry:
                try:
                    entry["api_key"] = reveal(entry.get("api_key"))
                except secretbox.SecretsLocked:
                    entry.pop("api_key", None)
                    entry["locked"] = True
            out[name] = entry
        return out

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
            data = self._read(strict=True)
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
        """Sources added in Settings, as saved — keys still encrypted (`reveal` them).
        Malformed entries are skipped."""
        raw = self._read().get(SOURCES_KEY)
        if not isinstance(raw, list):
            return []
        return [e for e in raw if isinstance(e, dict) and e.get("id") and e.get("base_url")]

    def save_sources(self, sources: list[dict]) -> None:
        with _lock_for(self.path):
            data = self._read(strict=True)
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
            data = self._read(strict=True)
            entry = dict(data.get(provider, {}))

            if api_key is not None:
                # A new key, or none: the old key's check is about the old key.
                entry.pop("check", None)
                if api_key == "":
                    entry.pop("api_key", None)
                else:
                    entry["api_key"] = secretbox.encrypt(api_key)
            if default_model:
                entry["default_model"] = default_model

            if entry:
                data[provider] = entry
            else:
                data.pop(provider, None)
            self._write(data)


    def set_check(self, provider: str, check: Optional[dict]) -> None:
        """Record (or, with None, clear) the last check of a provider's key."""
        with _lock_for(self.path):
            data = self._read(strict=True)
            entry = dict(data.get(provider, {}))
            if check:
                entry["check"] = check
            else:
                entry.pop("check", None)
            if entry:
                data[provider] = entry
            else:
                data.pop(provider, None)
            self._write(data)

    def migrate(self) -> int:
        """Encrypt every plaintext key in the file, in place. Returns how many."""
        with _lock_for(self.path):
            if not self.path.is_file():
                return 0
            try:
                data = self._read(strict=True)
            except StoreUnreadable:
                return 0  # reported; never rewritten
            data, count = _sealed(data)
            if count:
                write_private(self.path, json.dumps(data, indent=2))
            return count


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


def migrate_all() -> int:
    """Encrypt the plaintext keys in every settings file under the data directory.

    The per-account files, the global file from before accounts, and the copies and
    backups beside it — those hold the same keys. Run at startup, after the
    accounts migration; a second run finds nothing to do.
    """
    from app.core import userdata

    # `providers.local.json.*`: the copies set aside by migrations and the `.bak-*`
    # backups taken before them — the same keys, just as readable.
    paths: list[Path] = [_PATH, *sorted(_PATH.parent.glob(f"{_PATH.name}.*"))]
    if userdata.ROOT.is_dir():
        paths += sorted(userdata.ROOT.glob("*/providers.local.json"))
    total = 0
    for path in paths:
        try:
            count = ProviderStore(lambda p=path: p).migrate()
        except Exception as e:  # noqa: BLE001 - one file must not stop the rest, or startup
            log.error("Could not encrypt the keys in %s: %s", path, type(e).__name__)
            continue
        if count:
            log.warning(
                "Encrypted %d API key(s) that were saved in plain text in %s. If a backup or "
                "copy of that file exists from before, rotate those keys at the provider.",
                count,
                path,
            )
        total += count
    return total


def set_check(provider: str, check: Optional[dict]) -> None:
    default_store.set_check(provider, check)
