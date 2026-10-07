"""The `builder` service: runs generated code for a backend that has no Docker (#75).

A hosted backend (docker-compose, `python:3.12-slim`) has no Node and must never run
generated code in its own container. This service sits beside it on the compose
network with the Docker socket, and runs each build in the same sandbox a local
backend uses (`sandbox.py`) — so the API process never touches untrusted code, and
the builder never touches the platform's database, secrets or `data/`.

    GET  /health   {"docker": true, "version": "27.1.1"} — what Settings shows
    POST /run      {id, image, files, steps, limits} → {"steps": [...]}
    POST /cancel   {id} — Stop pressed on the build

Standard library only, so its image is Docker's CLI plus a Python interpreter. Set
`BUILDER_TOKEN` (and `BUILD_RUNNER_TOKEN` on the backend) to require a shared secret.

    python -m app.build.builder_service --port 8100
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from app.build import sandbox

#: A request bigger than this is refused: a build's source is a few hundred KB.
MAX_BODY = 25 * 1024 * 1024
#: Caps the service enforces whatever the backend asks for.
MAX_SECONDS = 900.0
MAX_MEMORY_MB = 4096
MAX_CPUS = 4.0

_running: dict[str, sandbox.Sandbox] = {}
_running_lock = threading.Lock()
_slots = threading.BoundedSemaphore(max(int(os.environ.get("BUILDER_CONCURRENCY", "2") or 2), 1))


def _token() -> str:
    return os.environ.get("BUILDER_TOKEN", "")


def _limits(data: dict) -> sandbox.Limits:
    asked = sandbox.Limits.from_dict(data or {})
    return sandbox.Limits(
        seconds=min(asked.seconds, MAX_SECONDS),
        memory_mb=min(asked.memory_mb, MAX_MEMORY_MB),
        cpus=min(asked.cpus, MAX_CPUS),
        pids=min(asked.pids, 1024),
        cache=asked.cache,
    )


def run(body: dict) -> dict:
    """One build, from the backend's request. Raises `ValueError` for a bad request."""
    image = str(body.get("image") or "")
    if image not in sandbox.IMAGES:
        raise ValueError(f"{image or 'No image'} is not an image builds may run in.")
    files = body.get("files")
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ValueError("`files` must map paths to text.")
    steps = [sandbox.Step.from_dict(s) for s in body.get("steps") or [] if isinstance(s, dict)]
    if not steps:
        raise ValueError("No steps to run.")
    build_id = str(body.get("id") or "")
    limits = _limits(body.get("limits") or {})
    with _slots:
        sandbox.ensure_image(image)
        box = sandbox.Sandbox(image, limits)
        if build_id:
            with _running_lock:
                _running[build_id] = box
        try:
            results = box.run(files, steps)
        finally:
            if build_id:
                with _running_lock:
                    _running.pop(build_id, None)
    return {"steps": [r.as_dict() for r in results]}


def cancel(build_id: str) -> bool:
    with _running_lock:
        box = _running.get(build_id)
    if box is None:
        return False
    box.cancel()
    return True


class _Handler(BaseHTTPRequestHandler):
    server_version = "aiteam-builder/1"

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        token = _token()
        if not token:
            return True
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(given.encode(), token.encode())

    def _body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path != "/health":
            self._send(404, {"detail": "Not found."})
            return
        ok, reason, version = sandbox.available()
        self._send(200, {"docker": ok, "reason": reason, "version": version, "images": list(sandbox.IMAGES)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorised():
            self._send(401, {"detail": "A builder token is required."})
            return
        body = self._body()
        if body is None:
            self._send(400, {"detail": "Send a JSON body under 25 MB."})
            return
        if self.path == "/cancel":
            self._send(200, {"cancelled": cancel(str(body.get("id") or ""))})
            return
        if self.path != "/run":
            self._send(404, {"detail": "Not found."})
            return
        try:
            self._send(200, run(body))
        except ValueError as e:
            self._send(400, {"detail": str(e)})
        except sandbox.SandboxError as e:
            self._send(503, {"detail": str(e)})

    def log_message(self, fmt: str, *args) -> None:  # one line per request, no bodies
        print(f"builder: {self.address_string()} {fmt % args}", flush=True)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run generated code in throwaway containers.")
    parser.add_argument("--host", default=os.environ.get("BUILDER_HOST", "0.0.0.0"))  # noqa: S104 - compose network only
    parser.add_argument("--port", type=int, default=int(os.environ.get("BUILDER_PORT", "8100")))
    args = parser.parse_args(argv)
    sandbox.sweep()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"builder: listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
