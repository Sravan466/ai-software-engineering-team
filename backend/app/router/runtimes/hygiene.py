"""Runtime hygiene: is a runtime older than a known security fix, and can others reach it?

Two questions, asked of every source whose runtime is known, in direct mode and on
a paired computer alike — and answered as warnings, never as a refusal. A person may
have reasons to run an old version; what they should never be is unaware of it.

**Outdated.** `advisories.json`, beside this file, is the minimum-version table:
per runtime, each advisory's id, the first version that fixes it and a link to the
advisory. It is data: a new advisory is a new line in that file (or in the file
`RUNTIME_ADVISORIES_FILE` names), not a change to this code. A version this module
cannot read is never taken for an old one.

**Exposed.** A runtime reached on loopback may also be listening on every interface
— vLLM, KoboldCpp and LocalAI do by default — and then anyone on the same network
can use it, and the model's memory with it. That is checked the only honest way: by
connecting to the same port on this machine's own network address. A server bound
to 127.0.0.1 refuses that; one bound to 0.0.0.0 accepts it. Nothing beyond this
machine is contacted.

The advice given never includes `OLLAMA_ORIGINS=*` or binding to `0.0.0.0`: both
turn a local runtime into one any web page or neighbour can drive (CVE-2024-28224).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from app.core.logging import get_logger

log = get_logger(__name__)

ADVISORIES_FILE = Path(__file__).with_name("advisories.json")
#: Names a replacement table, for an install that tracks advisories faster than releases.
ADVISORIES_ENV = "RUNTIME_ADVISORIES_FILE"

KIND_OUTDATED = "outdated"
KIND_EXPOSED = "exposed"

#: How long a connection to this machine's own address may take. It is never
#: filtered, so a refusal is immediate; this only bounds something unusual.
_CONNECT_TIMEOUT = 0.3

_lock = threading.Lock()
_cache: dict = {"key": None, "table": {}}


# ── versions ─────────────────────────────────────────────────────────────────
_VERSION = re.compile(r"^[vb]?(\d+(?:\.\d+)*)")


def parse_version(text: Optional[str]) -> Optional[tuple[int, ...]]:
    """A runtime's version as numbers, or None when it can't be read.

    Reads `0.17.1`, `v3.5.0` and llama.cpp's `b5662-3f8a…` (the build number). Only
    the leading run of numbers counts, so a commit hash after it is never read as
    part of the version. None — not zero — for anything else, so an unreadable
    version is never taken for an old one.
    """
    match = _VERSION.match((text or "").strip().lower())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _older(version: tuple[int, ...], fixed: tuple[int, ...]) -> bool:
    width = max(len(version), len(fixed))
    return version + (0,) * (width - len(version)) < fixed + (0,) * (width - len(fixed))


# ── the table ────────────────────────────────────────────────────────────────
def _path() -> Path:
    from app.core.config import settings

    override = (settings.runtime_advisories_file or os.environ.get(ADVISORIES_ENV) or "").strip()
    return Path(override) if override else ADVISORIES_FILE


def _valid(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and parse_version(entry.get("fixed")) is not None
        and isinstance(entry.get("url"), str)
        and entry["url"].startswith("https://")
    )


def table() -> dict[str, list[dict]]:
    """`{runtime: [advisory, …]}`, read from the file — again whenever it changes.

    An entry without an id, a readable fixed version and an https link is skipped
    with a log line rather than guessed at; a file that can't be read at all leaves
    the table empty, and nothing is said to be outdated.
    """
    path = _path()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), None)
    with _lock:
        if _cache["key"] == key:
            return _cache["table"]
        out: dict[str, list[dict]] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            runtimes = raw.get("runtimes") if isinstance(raw, dict) else None
            for runtime, entries in (runtimes or {}).items():
                kept = [e for e in entries or [] if _valid(e)]
                if len(kept) != len(entries or []):
                    log.warning("Skipped %d malformed advisory entries for %s in %s.",
                                len(entries or []) - len(kept), runtime, path)
                out[str(runtime)] = kept
        except (OSError, ValueError, AttributeError, TypeError) as e:
            log.warning("The runtime advisory table at %s can't be read (%s); no version warnings.", path, e)
        _cache.update(key=key, table=out)
        return out


def advisories(runtime: Optional[str], version: Optional[str]) -> list[dict]:
    """The advisories `version` of `runtime` predates the fix for, newest fix first."""
    parsed = parse_version(version)
    if not runtime or parsed is None:
        return []
    hits = [e for e in table().get(runtime, []) if _older(parsed, parse_version(e["fixed"]) or ())]
    return sorted(hits, key=lambda e: parse_version(e["fixed"]) or (), reverse=True)


# ── who can reach it ─────────────────────────────────────────────────────────
def own_addresses() -> list[str]:
    """This machine's addresses on its networks — never loopback.

    The address the OS would use to reach the internet (found by *connecting* a UDP
    socket, which sends nothing), plus whatever the hostname resolves to locally.
    """
    found: list[str] = []

    def keep(text: str) -> None:
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            return
        if address.is_loopback or address.is_unspecified or address.is_link_local:
            return
        if str(address) not in found:
            found.append(str(address))

    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))
                keep(sock.getsockname()[0])
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            keep(str(info[4][0]))
    except OSError:
        pass
    return found[:4]


def _accepts(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(_CONNECT_TIMEOUT)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def exposed_on(base_url: str, addresses: Optional[Iterable[str]] = None) -> Optional[list[str]]:
    """The network addresses a loopback runtime also answers on; [] when none.

    None when the question doesn't apply: an address that isn't loopback is another
    computer, and whether *it* listens widely can't be seen from here.
    """
    from app.router.runtimes.detect import is_loopback

    if not is_loopback(base_url):
        return None
    try:
        parsed = urlparse(base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    candidates = list(addresses) if addresses is not None else own_addresses()
    return [host for host in candidates if _accepts(host, port)]


# ── what a person is told ────────────────────────────────────────────────────
def _label(runtime: Optional[str]) -> str:
    from app.router.runtimes import table as runtimes

    return runtimes.spec_for(runtime).label if runtime else "This runtime"


def _how_to_close(runtime: Optional[str]) -> str:
    from app.router.runtimes import table as runtimes

    spec = runtimes.spec_for(runtime)
    return spec.setup.exposure if spec.setup is not None and spec.setup.exposure else (
        "Restart it listening on 127.0.0.1 only."
    )


def warnings(
    runtime: Optional[str], version: Optional[str], exposed: Optional[list[str]] = None
) -> list[dict]:
    """`[{kind, title, detail, url?, ids?}]` for one source — empty when all is well."""
    out: list[dict] = []
    label = _label(runtime)
    found = advisories(runtime, version)
    if found:
        newest = found[0]
        ids = [e["id"] for e in found]
        out.append(
            {
                "kind": KIND_OUTDATED,
                "title": f"{label} {version} is older than a known security fix",
                "detail": (
                    f"Update {label} to {newest['fixed']} or newer. "
                    + (f"{newest['summary']} " if newest.get("summary") else "")
                    + ("Fixed there: " + ", ".join(ids) + "." if len(ids) > 1 else f"({ids[0]})")
                ).strip(),
                "url": newest["url"],
                "ids": ids,
                "fixed": newest["fixed"],
            }
        )
    if exposed:
        out.append(
            {
                "kind": KIND_EXPOSED,
                "title": f"{label} is reachable from your network",
                "detail": (
                    f"It also answers on {', '.join(exposed)}, so anyone on the same network can use it "
                    f"and the models on it. {_how_to_close(runtime)}"
                ),
            }
        )
    return out


def terminal_lines(label: str, base_url: str, found: list[dict]) -> list[str]:
    """The same warnings, as the connector prints them in its terminal."""
    lines = []
    for w in found:
        lines.append(f"⚠ {label} at {base_url}: {w['title']}.")
        lines.append(f"  {w['detail']}")
        if w.get("url"):
            lines.append(f"  {w['url']}")
    return lines
