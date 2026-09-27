"""What the connector keeps on this computer: its key, and the sources you added.

The **private key** goes into the OS keychain through `keyring` when one is
available (macOS Keychain, Windows Credential Manager, Secret Service on Linux).
When none is, it is written to a file only this user can read (0600), and the
connector says so every time it starts — a plaintext key can be read by any
program running as you.

Everything else lives in `state.json` beside it: which server this computer is
paired with, and any runtime address you added yourself. The website can't write
here; only this program, run by you, can.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from app.connector.protocol import b64d, b64e

SERVICE = "aiteam-connect"


def home() -> Path:
    override = os.environ.get("AITEAM_CONNECT_HOME")
    path = Path(override) if override else Path.home() / ".aiteam-connect"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    if sys.platform != "win32":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# ── state ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    path = home() / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    _write_private(home() / "state.json", json.dumps(state, indent=2, sort_keys=True))


# ── secrets: the keychain, or a 0600 file ────────────────────────────────────
def _keyring():
    if os.environ.get("AITEAM_CONNECT_NO_KEYRING"):
        return None
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - not installed, or no usable backend
        return None


def _file_for(name: str) -> Path:
    keys = home() / "keys"
    keys.mkdir(mode=0o700, exist_ok=True)
    return keys / (hashlib.sha256(name.encode("utf-8")).hexdigest()[:32] + ".secret")


def put_secret(name: str, value: str) -> str:
    """Store `value`; returns where it went: "keychain" or "file"."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, name, value)
            return "keychain"
        except Exception:  # noqa: BLE001 - a locked or broken keychain
            pass
    _write_private(_file_for(name), value)
    return "file"


def get_secret(name: str) -> Optional[str]:
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, name)
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
    try:
        return _file_for(name).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def delete_secret(name: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001
            pass
    try:
        _file_for(name).unlink()
    except OSError:
        pass


# ── the device key ──────────────────────────────────────────────────────────
def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def key_to_text(key: Ed25519PrivateKey) -> str:
    return b64e(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


def key_from_text(text: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b64d(text))


def public_text(key: Ed25519PrivateKey) -> str:
    return b64e(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def sign(key: Ed25519PrivateKey, message: bytes) -> str:
    return b64e(key.sign(message))


def key_name(origin: str) -> str:
    return f"device-key|{origin}"


def source_key_name(url: str) -> str:
    return f"source-key|{url}"
