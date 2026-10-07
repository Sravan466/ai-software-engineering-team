"""The running app's front door (#78): requests to `<token>.localhost:8000`.

Outermost of the backend's middleware, so the API's own CORS and sign-in never see
these requests — they are not the API's. A request whose `Host` names a preview is
answered here, from that preview's sandbox, or refused; every other request goes on
to the API untouched.

What the app sends back is passed on as it is, with four exceptions:

  * **No cookies.** `Set-Cookie` is dropped: whatever the app thinks it is setting, it
    sets nothing in the person's browser.
  * **A strict CSP.** Scripts and data only from the preview's own origin; the network
    only back to it; images may come from anywhere (a generated landing page is mostly
    stock photos); fonts from Google Fonts, which the platform's own theme uses; and
    only this app's pages may frame it.
  * **No caching** of pages, so a rebuilt app is what the frame shows.
  * **One script tag** at the top of each page — a few lines that wait for the Preview
    tab to send its element picker, and do nothing in a tab of their own.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import anyio

from app.api.auth_middleware import _header
from app.core.logging import get_logger
from app.preview import app_runtime

log = get_logger(__name__)

#: The largest request body the preview passes on.
MAX_BODY = 10 * 1024 * 1024
#: Threads the previews may hold at once, apart from the API's own: a generated app
#: that hangs holds these for up to a minute each, and must never hold the ones every
#: sync route of the API runs on.
THREADS = 24
_limiter: Optional[anyio.CapacityLimiter] = None


def _threads() -> anyio.CapacityLimiter:
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(THREADS)
    return _limiter
#: What a served app may not set or override about how it is served.
_DROP = frozenset({
    "set-cookie", "set-cookie2", "content-security-policy", "content-security-policy-report-only",
    "x-frame-options", "strict-transport-security", "content-length", "transfer-encoding", "connection",
    "keep-alive", "content-encoding", "access-control-allow-origin", "access-control-allow-credentials",
    "clear-site-data", "permissions-policy", "cross-origin-opener-policy", "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
})
_FORWARD_SKIP = frozenset({"cookie", "connection", "accept-encoding", "content-length", "origin", "referer"})
#: A redirect the app built against the host it is served from inside the box.
_LOOPBACK = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(?=/|$)", re.IGNORECASE)


def _frame_ancestors() -> list[str]:
    from app.api.auth_middleware import _trusted_origins

    return sorted(_trusted_origins())


def csp() -> str:
    ancestors = " ".join(_frame_ancestors()) or "'none'"
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: blob: https:",
        "media-src 'self' data: blob: https:",
        "connect-src 'self'",
        "frame-src 'self'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        f"frame-ancestors {ancestors}",
    ])


def stub() -> str:
    """The script each page gets: it waits for the Preview tab to send the picker.

    Only the frame's parent, and only from one of this app's own origins, may install
    it; opened in a tab of its own (no parent), it does nothing at all."""
    origins = json.dumps(_frame_ancestors())
    return (
        "<script data-aiteam-preview>(function(){"
        "if(window.parent===window||window.__pvStub)return;window.__pvStub=1;"
        f"var ok={origins};"
        "window.addEventListener('message',function(e){var d=e.data;"
        "if(!d||!d.__preview||d.type!=='install'||window.__pvBridge)return;"
        "if(e.source!==window.parent||ok.indexOf(e.origin)<0)return;"
        "try{window.__pvAttr='data-src';(0,eval)(String(d.code));}catch(err){}});"
        "try{parent.postMessage({__preview:true,type:'stub'},'*');}catch(_){}"
        "})();</script>"
    )


def inject(html: bytes) -> bytes:
    """`stub()` at the top of `<head>` — or first, for a page with no head."""
    tag = stub().encode("utf-8")
    lower = html[:4096].lower()
    at = lower.find(b"<head")
    if at >= 0:
        close = html.find(b">", at)
        if close >= 0:
            return html[: close + 1] + tag + html[close + 1 :]
    at = lower.find(b"<html")
    if at >= 0:
        close = html.find(b">", at)
        if close >= 0:
            return html[: close + 1] + tag + html[close + 1 :]
    return tag + html


def _page(status: int, title: str, text: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{title}</title><style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;display:grid;"
        "place-items:center;min-height:100vh;background:#0f1115;color:#d7dbe3}main{max-width:28rem;padding:2rem}"
        "h1{font-size:1.05rem;margin:0 0 .5rem}p{margin:0;color:#9aa3b2}</style></head>"
        f"<body><main><h1>{title}</h1><p>{text}</p></main></body></html>"
    ).encode("utf-8")
    return status, [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"content-security-policy", _PAGE_CSP().encode("latin-1")),
    ], body


def _PAGE_CSP() -> str:
    ancestors = " ".join(_frame_ancestors()) or "'none'"
    return f"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors {ancestors}"


class AppPreviewMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = _header(scope, b"host")
        token = app_runtime.token_from_host(host)
        if token is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Live reload and app sockets aren't part of a built app's preview.
            await send({"type": "websocket.close", "code": 1008})
            return
        status, headers, body = await self._answer(scope, receive, token)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _answer(self, scope, receive, token: str):
        inst = app_runtime.lookup(token)
        if inst is None:
            return _page(
                404,
                "This preview has stopped",
                "Previews stop after a while unused, and every start gets a new address. "
                "Open the Preview tab again to start the app.",
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return 499, [], b""
            part = message.get("body", b"")
            size += len(part)
            if size > MAX_BODY:
                return 413, [(b"content-type", b"text/plain; charset=utf-8")], b"That request is too large for the preview."
            chunks.append(part)
            if not message.get("more_body"):
                break
        path = scope.get("raw_path", b"").decode("latin-1") or scope.get("path", "/")
        if scope.get("query_string"):
            path = f"{path}?{scope['query_string'].decode('latin-1')}"
        forwarded = {}
        for key, value in scope.get("headers") or []:
            name = key.decode("latin-1").lower()
            if name not in _FORWARD_SKIP:
                forwarded[name] = value.decode("latin-1")
        method = scope.get("method", "GET").upper()
        try:
            status, upstream, body = await anyio.to_thread.run_sync(
                inst.request, method, path, forwarded, b"".join(chunks), limiter=_threads()
            )
        except Exception as e:  # noqa: BLE001 - the page says so, the API is unaffected
            log.info("Preview request failed for %s: %s", inst.project_id, e)
            return _page(503, "The app isn't answering", "It may have stopped. Open the Preview tab again to restart it.")
        html = self._content_type(upstream).startswith("text/html")
        out = inject(body) if html and method != "HEAD" else body
        return status, self._headers(upstream, len(out), html), out

    @staticmethod
    def _content_type(upstream: dict) -> str:
        for key, value in upstream.items():
            if key.lower() == "content-type":
                return str(value if not isinstance(value, list) else value[0]).lower()
        return ""

    def _headers(self, upstream: dict, length: int, html: bool) -> list[tuple[bytes, bytes]]:
        out: list[tuple[bytes, bytes]] = []
        for key, value in upstream.items():
            name = key.lower()
            if name in _DROP or (html and name in ("cache-control", "etag", "last-modified")):
                continue
            values = value if isinstance(value, list) else [value]
            for v in values:
                if name == "location":
                    # Back onto the preview's own origin, whatever host the app thought it had.
                    v = _LOOPBACK.sub("", str(v)) or "/"
                out.append((name.encode("latin-1"), str(v).encode("latin-1", errors="replace")))
        out += [
            (b"content-length", str(length).encode("ascii")),
            (b"content-security-policy", csp().encode("latin-1")),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-resource-policy", b"same-origin"),
        ]
        if html:
            out.append((b"cache-control", b"no-store"))
        return out


