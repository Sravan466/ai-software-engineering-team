"""Deploying a frontend-only build straight to the user's own Vercel account.

No GitHub involved: the frontend's files go up through Vercel's API and Vercel builds
them. The token is one the user created in their Vercel account settings and pasted
in; it is held encrypted by `app.core.deploy_store` and handed in per call.

    POST /v11/projects                     make the project (once; 409 = it exists)
    POST /v10/projects/{name}/env?upsert   the frontend's public variables, if any
    POST /v13/deployments                  the file list by SHA-1; Vercel answers
                                           `missing_files` with the ones it lacks …
    POST /v2/files                         … which are uploaded, and it is sent again
    GET  /v13/deployments/{id}             QUEUED → BUILDING → READY | ERROR
    GET  /v3/deployments/{id}/events       the build log, for a failed build — kept,
                                           and sent back to the crew (#75)

Sending the list first means a redeploy uploads only what changed. The project is
named after the build, so every redeploy lands on the same `<name>.vercel.app`.

Every error that reaches the page is trimmed and scrubbed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core import scrub
from app.core.config import settings

_TIMEOUT = 60.0

#: Tests put an `httpx.MockTransport` here; None is the real network.
transport: Optional[httpx.BaseTransport] = None


class VercelError(Exception):
    """A problem to show as is: what happened, and what to do."""

    def __init__(self, message: str, reason: str = "failed") -> None:
        super().__init__(message)
        #: `rejected` (the token), `limit` (Vercel's daily cap), or `failed`.
        self.reason = reason


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=settings.vercel_api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
        transport=transport,
    )


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        text = (err or {}).get("message") if isinstance(err, dict) else None
        return str(text or body)[:300]
    except ValueError:
        return r.text[:300]


def _fail(r: httpx.Response, what: str) -> VercelError:
    if r.status_code in (401, 403):
        return VercelError("Vercel rejected the token — replace it with a new one.", "rejected")
    if r.status_code == 429:
        return VercelError(
            "Vercel's daily limit (100 deploys on the free plan) is reached — try again later.",
            "limit",
        )
    return VercelError(scrub.scrub(f"Vercel could not {what} ({r.status_code}): {_message(r)}"))


# ── the token ────────────────────────────────────────────────────────────────
@dataclass
class TokenCheck:
    ok: bool
    username: Optional[str] = None
    reason: str = "ok"
    message: str = ""


def check_token(token: str) -> TokenCheck:
    """Whether Vercel accepts `token`, and whose it is. Never raises for a bad token."""
    scrub.register(token)
    try:
        with _client(token) as c:
            r = c.get("/v2/user")
    except httpx.HTTPError:
        return TokenCheck(False, reason="unreachable", message="Vercel didn't answer. Check the connection and try again.")
    if r.status_code == 200:
        user = (r.json() or {}).get("user") or {}
        return TokenCheck(True, username=user.get("username") or user.get("email") or "your account")
    if r.status_code in (401, 403):
        return TokenCheck(
            False,
            reason="rejected",
            message="Vercel didn't accept that token. Create a new one and paste all of it.",
        )
    return TokenCheck(False, reason="unreachable", message=_fail(r, "check the token").args[0])


# ── deploying ────────────────────────────────────────────────────────────────
def _digest(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()  # noqa: S324 - Vercel names files by SHA-1


def deploy(
    token: str,
    name: str,
    files: dict[str, str],
    framework: Optional[str],
    public_env: Optional[dict[str, str]] = None,
    on_upload=None,
) -> dict:
    """Start a production deployment of `files`. Returns Vercel's deployment."""
    blobs = {path: content.encode("utf-8") for path, content in files.items()}
    shas = {path: _digest(data) for path, data in blobs.items()}
    listing = [{"file": path, "sha": shas[path], "size": len(data)} for path, data in blobs.items()]
    body = {
        "name": name,
        "target": "production",
        "files": listing,
        "projectSettings": {"framework": framework},
    }
    with _client(token) as c:
        r = c.post("/v11/projects", json={"name": name, "framework": framework})
        if r.status_code not in (200, 201, 409):
            raise _fail(r, "create the project")
        if public_env:
            env = [
                {"key": k, "value": v, "type": "plain", "target": ["production", "preview"]}
                for k, v in public_env.items()
            ]
            r = c.post(f"/v10/projects/{name}/env", params={"upsert": "true"}, json=env)
            if r.status_code not in (200, 201):
                raise _fail(r, "set the project's public variables")

        for attempt in range(2):
            r = c.post("/v13/deployments", params={"skipAutoDetectionConfirmation": "1"}, json=body)
            if r.status_code in (200, 201):
                return r.json()
            missing = _missing(r)
            if attempt or missing is None:
                raise _fail(r, "start the deployment")
            by_sha = {sha: path for path, sha in shas.items()}
            for i, sha in enumerate(missing):
                path = by_sha.get(sha)
                if path is None:
                    continue
                if on_upload:
                    on_upload(i + 1, len(missing))
                up = c.post(
                    "/v2/files",
                    content=blobs[path],
                    headers={"x-vercel-digest": sha, "Content-Type": "application/octet-stream"},
                )
                if up.status_code not in (200, 201):
                    raise _fail(up, f"upload {path}")
    raise VercelError("Vercel could not start the deployment.")


def _missing(r: httpx.Response) -> Optional[list[str]]:
    try:
        err = (r.json() or {}).get("error") or {}
    except ValueError:
        return None
    if err.get("code") == "missing_files" and isinstance(err.get("missing"), list):
        return [str(s) for s in err["missing"]]
    return None


def status(token: str, deployment_id: str) -> dict:
    with _client(token) as c:
        r = c.get(f"/v13/deployments/{deployment_id}")
    if r.status_code != 200:
        raise _fail(r, "read the deployment")
    return r.json()


#: Lines of a failed build's log kept (#75): a `next build` failure is a few hundred.
LOG_LINES = 3000


def events(token: str, deployment_id: str) -> list[str]:
    """The whole build log, line by line, scrubbed. Empty when it can't be read.

    Every event, not only `stderr`: `next build` prints "Failed to compile." and the
    file under it on stdout, and the parsers need both.
    """
    try:
        with _client(token) as c:
            # -1: every event Vercel kept, not the default page.
            r = c.get(f"/v3/deployments/{deployment_id}/events", params={"limit": -1, "builds": 1})
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    try:
        found = r.json()
    except ValueError:
        return []
    out: list[str] = []
    for event in found if isinstance(found, list) else []:
        if not isinstance(event, dict):
            continue
        text = event.get("text") or (event.get("payload") or {}).get("text")
        if isinstance(text, str) and text.strip():
            out.extend(line for line in text.splitlines() if line.strip())
    return [scrub.scrub(line)[:400] for line in out[-LOG_LINES:]]


def failure(deployment: dict) -> str:
    """Why Vercel says a deployment failed — its message, code and step — in one line."""
    text = str(deployment.get("errorMessage") or "").strip()
    code = str(deployment.get("errorCode") or "").strip()
    step = str(deployment.get("errorStep") or "").strip()
    extra = ", ".join(x for x in (code, f"during {step}" if step else "") if x)
    return f"{text} ({extra})" if text and extra else (text or extra)


def live_url(deployment: dict) -> Optional[str]:
    """The stable production URL when Vercel gives one, else the deployment's own."""
    aliases = deployment.get("alias") or []
    host = next((a for a in aliases if isinstance(a, str) and a.endswith(".vercel.app")), None)
    host = host or (aliases[0] if aliases else None) or deployment.get("url")
    if not host:
        return None
    return host if host.startswith("http") else f"https://{host}"
