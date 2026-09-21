"""Finding the model runtimes on this machine — on loopback, and nowhere else.

Detection opens a TCP connection to each default port in the adapter table on
`127.0.0.1` and `::1`, and asks every port that accepts one who it is. It never
scans the local network: a runtime on another computer is only ever reached because
somebody typed its address, and confirmed it.

An answer is identified by what it says, never by the port it came from. A port
that answers but matches no adapter is reported as unknown — with a note on whether
it at least speaks the OpenAI dialect — and is not used until someone confirms it.
"""
from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.router.runtimes import table
from app.router.runtimes.openai_compat import speaks_openai
from app.router.runtimes.types import Hello

log = get_logger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
#: A closed port on loopback refuses at once; this only bounds a filtered one.
_CONNECT_TIMEOUT = 0.4


def is_loopback(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in _LOOPBACK_NAMES or host.startswith("127.")


def url_for(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def same_address(a: str, b: str) -> bool:
    """Whether two URLs name the same server, reading every loopback spelling as one."""
    pa, pb = urlparse(a), urlparse(b)
    if (pa.port or 80) != (pb.port or 80):
        return False
    ha, hb = (pa.hostname or "").lower(), (pb.hostname or "").lower()
    return ha == hb or (is_loopback(a) and is_loopback(b))


def _accepts(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(_CONNECT_TIMEOUT)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def identify(
    base_url: str, api_key: Optional[str] = None, *, prefer: Optional[str] = None
) -> Optional[Hello]:
    """Which runtime answers at `base_url`, asking each adapter in table order.

    `prefer` is asked first — a configured source that says what it is saves the
    others a round trip — but it still has to answer as that runtime.
    """
    specs = list(table.FINGERPRINTED)
    if prefer:
        specs.sort(key=lambda spec: spec.id != prefer)
    for spec in specs:
        assert spec.adapter is not None
        try:
            hello = spec.adapter.fingerprint(base_url, api_key)
        except Exception:  # noqa: BLE001 - one odd answer must not stop the next adapter
            hello = None
        if hello is not None:
            return hello
    return None


@dataclass
class Detection:
    found: list[Hello] = field(default_factory=list)
    #: `{base_url, openai: bool, note}` for ports that answered but matched nothing.
    unknown: list[dict] = field(default_factory=list)
    #: Every address that was tried, for "nothing is running — tried …".
    tried: list[str] = field(default_factory=list)


def _describe_unknown(base_url: str) -> dict:
    openai = speaks_openai(base_url)
    status: Optional[int] = None
    try:
        status = httpx.get(base_url + "/", timeout=1.0).status_code
    except Exception:  # noqa: BLE001
        pass
    if openai:
        note = "Answers the OpenAI API, but not as any runtime this app recognises."
    elif status is not None:
        note = f"Answers HTTP {status}, but doesn't look like a model runtime."
    else:
        note = "Accepts connections, but doesn't answer over HTTP."
    return {"base_url": base_url, "openai": openai, "note": note}


def detect() -> Detection:
    """Probe loopback on every default port in the adapter table."""
    result = Detection()
    if not settings.local_detect:
        return result
    own = settings.api_port
    targets = [
        (host, port)
        for port in table.probe_ports()
        for host in LOOPBACK_HOSTS
        # This backend's own port answers HTTP too, and it is not a model runtime.
        if port != own
    ]
    result.tried = [url_for(h, p) for h, p in targets if h == LOOPBACK_HOSTS[0]]
    with ThreadPoolExecutor(max_workers=min(len(targets), 16) or 1, thread_name_prefix="detect") as pool:
        open_ports = [t for t, ok in zip(targets, pool.map(lambda t: _accepts(*t), targets)) if ok]

    # A dual-stack server answers on both hosts; ask once, preferring IPv4.
    seen: set[int] = set()
    ordered: list[tuple[str, int]] = []
    for host in LOOPBACK_HOSTS:
        for h, port in open_ports:
            if h == host and port not in seen:
                seen.add(port)
                ordered.append((h, port))

    def _ask(target: tuple[str, int]) -> tuple[str, Optional[Hello]]:
        url = url_for(*target)
        return url, identify(url)

    if ordered:
        with ThreadPoolExecutor(max_workers=min(len(ordered), 8), thread_name_prefix="identify") as pool:
            answers = list(pool.map(_ask, ordered))
        for url, hello in answers:
            if hello is not None:
                result.found.append(hello)
            else:
                result.unknown.append(_describe_unknown(url))
    return result
