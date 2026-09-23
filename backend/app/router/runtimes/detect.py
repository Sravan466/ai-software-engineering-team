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

import ipaddress
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
#: A closed port on loopback refuses at once; this only bounds a filtered one.
_CONNECT_TIMEOUT = 0.4
#: How long a port that accepted a connection gets to answer HTTP at all. A
#: listener that never replies would otherwise cost every fingerprint its timeout.
_HTTP_TIMEOUT = 1.0


def is_loopback(base_url: str) -> bool:
    """Whether an address is this machine: a loopback IP literal, or `localhost`.

    Only those. A name is never read by its spelling — `127.x.10.0.0.5.nip.io`
    starts like loopback and resolves to another computer — and resolving names
    here would make "is this machine" depend on whatever DNS says at the moment.
    The unspecified address (`0.0.0.0`, `::`) counts: connecting to it reaches this
    machine, and runtimes print it as their listen address, so people paste it.
    """
    try:
        host = (urlparse(base_url).hostname or "").strip("[]").rstrip(".").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(
        address.is_loopback
        or address.is_unspecified
        or (mapped is not None and (mapped.is_loopback or mapped.is_unspecified))
    )


def reaches_server_network(base_url: str) -> bool:
    """Whether an address lands on this server or the private network around it.

    Loopback, private, link-local, unspecified or reserved — as a literal, or as a
    name that resolves to one now. Used to keep an account that doesn't own the
    install from pointing the server at its own internal services. A name that
    doesn't resolve counts as reaching it: it can't be checked, so it isn't trusted.
    """
    if is_loopback(base_url):
        return True
    try:
        host = (urlparse(base_url).hostname or "").strip("[]").rstrip(".").lower()
    except ValueError:
        return True
    if not host:
        return True
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return True
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                return True
    for address in addresses:
        mapped = getattr(address, "ipv4_mapped", None)
        for a in (address, mapped):
            if a is not None and (
                a.is_loopback or a.is_private or a.is_link_local or a.is_unspecified or a.is_reserved
            ):
                return True
    return False


def url_for(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _port(url: str) -> int:
    try:
        parsed = urlparse(url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return -1


def same_address(a: str, b: str) -> bool:
    """Whether two URLs name the same server, reading every loopback spelling as one."""
    port_a, port_b = _port(a), _port(b)
    if port_a < 0 or port_a != port_b:
        return False
    try:
        ha, hb = (urlparse(a).hostname or "").lower(), (urlparse(b).hostname or "").lower()
    except ValueError:
        return False
    return ha == hb or (is_loopback(a) and is_loopback(b))


def answers_http(base_url: str) -> bool:
    """Whether anything answers HTTP here at all — any status counts."""
    try:
        httpx.get(base_url.rstrip("/") + "/", timeout=_HTTP_TIMEOUT)
        return True
    except Exception:  # noqa: BLE001
        return False


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


def _describe_unknown(base_url: str, *, http: bool = True) -> dict:
    status: Optional[int] = None
    if http:
        try:
            status = httpx.get(base_url + "/", timeout=_HTTP_TIMEOUT).status_code
        except Exception:  # noqa: BLE001
            pass
    openai = http and speaks_openai(base_url)
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

    def _ask(target: tuple[str, int]) -> tuple[str, bool, Optional[Hello]]:
        url = url_for(*target)
        # One short request first: a listener that never answers HTTP would
        # otherwise cost every adapter's fingerprint its timeout, in turn.
        if not answers_http(url):
            return url, False, None
        return url, True, identify(url)

    if ordered:
        with ThreadPoolExecutor(max_workers=min(len(ordered), 8), thread_name_prefix="identify") as pool:
            answers = list(pool.map(_ask, ordered))
        for url, http, hello in answers:
            if hello is not None:
                result.found.append(hello)
            else:
                result.unknown.append(_describe_unknown(url, http=http))
    return result
