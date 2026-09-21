"""Runtime settings: cloud keys, local model sources, and which model each role runs on.

Keys — a cloud provider's, or a local runtime's — are stored on this backend only
(gitignored `data/providers.local.json`), never returned to the client in full, and
used immediately by the router.

Local models come from **sources**: runtimes found running on this machine, sources
configured in `.env`, and sources added here. Every model is named `source:model`,
the local default is one of them, and `/roles` goes further: one model per agent,
chosen from what the sources actually serve.
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import settings
from app.router.runtimes.detect import is_loopback
from app.router.runtimes.sources import SourceError, normalise_url
from app.router.router import router as model_router

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: What a model name sent to a runtime's download API may look like: a name, tags,
#: a namespace and a registry host — nothing that could walk a path.
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")


class ProviderKeyUpdate(BaseModel):
    # None = leave unchanged, "" = clear, "sk-..." = set
    api_key: Optional[str] = None
    default_model: Optional[str] = None


class PullRequest(BaseModel):
    model: str


class LocalDefaultUpdate(BaseModel):
    """`source:model` — the model every role falls back to."""

    model: str


class SourceCreate(BaseModel):
    base_url: str
    label: Optional[str] = None
    api_key: Optional[str] = None
    #: Required for an address that is not on this machine: prompts sent there
    #: leave it, and that has to be a decision rather than a default.
    confirm_remote: bool = False


class SourceUpdate(BaseModel):
    # None = leave unchanged, "" = clear, "..." = set
    api_key: Optional[str] = None


class RoleModelUpdate(BaseModel):
    """Point one role at a model. Blank or null puts it back on the default.

    The value is `source:model` (or `provider:model` for the cloud), the spelling
    every picker writes. A name with no source in front of it is refused.
    """

    model: Optional[str] = None


# ── Cloud provider API keys ──────────────────────────────────────────────────
@router.get("/providers")
def get_providers() -> dict:
    return {
        "providers": model_router.provider_settings(),
        "default_mode": settings.default_routing_mode,
    }


@router.put("/providers/{provider}")
def set_provider(provider: str, body: ProviderKeyUpdate) -> dict:
    """Set a cloud provider's API key, its default model, or both."""
    try:
        model_router.set_provider_key(
            provider, api_key=body.api_key, default_model=body.default_model
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.provider_settings()[provider]


# ── which model each role runs on ────────────────────────────────────────────
@router.get("/roles")
def get_roles() -> dict:
    """Every agent (plus the debate, the mockup and embeddings) and its model."""
    return model_router.role_settings()


@router.put("/roles/{role}")
def set_role(role: str, body: RoleModelUpdate) -> dict:
    try:
        model_router.set_role_model(role, body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.role_settings()


# ── Local model sources ──────────────────────────────────────────────────────
@router.get("/local")
def get_local(refresh: bool = False) -> dict:
    """Every source, what it serves, and the local default. `refresh` re-probes."""
    return model_router.local_status(refresh=refresh)


@router.put("/local/default")
def set_local_default(body: LocalDefaultUpdate) -> dict:
    try:
        model_router.set_local_default(body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.local_status()


def _trusted_host(request: Request) -> bool:
    """Whether this request reached the backend under a name it is actually served at.

    A page on some other site can rebind its own hostname to this machine and then
    call this API "same-origin". What it cannot change is the `Host` it sends, which
    still names that site. Loopback, and the configured public URL, are the names
    this backend is served at.
    """
    raw = (request.headers.get("host") or "").strip()
    if not raw:
        return False
    if is_loopback(f"http://{raw}"):
        return True
    try:
        host = (urlparse(f"http://{raw}").hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    public = (urlparse(settings.backend_public_url).hostname or "").rstrip(".").lower()
    return bool(public) and host == public


@router.post("/sources", status_code=201)
def add_source(body: SourceCreate, request: Request) -> dict:
    """Add a source by address. It has to answer, so its runtime can be identified."""
    try:
        remote = not is_loopback(normalise_url(body.base_url))
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if remote and not _trusted_host(request):
        # Sending every prompt to another computer is the one change here worth a
        # rebinding page's effort, so it is only taken from a name this backend is
        # served at. Set BACKEND_PUBLIC_URL to the name you reach it by.
        raise HTTPException(
            status_code=403,
            detail=(
                "A source on another computer can only be added from an address this "
                "backend is served at (localhost, or BACKEND_PUBLIC_URL)."
            ),
        )
    try:
        model_router.add_source(
            body.base_url,
            label=body.label,
            api_key=body.api_key,
            confirm_remote=body.confirm_remote,
        )
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.local_status()


@router.put("/sources/{source_id}")
def update_source(source_id: str, body: SourceUpdate) -> dict:
    try:
        model_router.set_source_key(source_id, body.api_key)
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.local_status()


@router.delete("/sources/{source_id}")
def remove_source(source_id: str) -> dict:
    try:
        model_router.remove_source(source_id)
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.local_status()


@router.post("/sources/{source_id}/pull")
def pull_model(source_id: str, body: PullRequest):
    """Download a model through a source's own API, streaming its progress.

    Only offered where the runtime's adapter says it has a download API; anywhere
    else this refuses, with how to add a model in that runtime instead. Streams
    NDJSON, e.g. {"status":"pulling ...","total":...,"completed":...} ... {"status":"success"}.
    """
    name = (body.model or "").strip()
    if not _MODEL_NAME.match(name) or ".." in name:
        raise HTTPException(status_code=400, detail=f"'{name}' isn't a model name this can download.")
    source = model_router.source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"No model source is called '{source_id}'.")
    if not source.adapter.can_download:
        raise HTTPException(
            status_code=400,
            detail=f"{source.source.label} doesn't download models through its API.",
        )

    def stream():
        try:
            for line in model_router.pull(source_id, name):
                yield json.dumps(line) + "\n"
        except SourceError as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
