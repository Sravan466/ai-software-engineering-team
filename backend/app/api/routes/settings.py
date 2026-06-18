"""Runtime settings: cloud API keys (self-host) + local Ollama model management.

Keys are stored on this backend only (gitignored `data/providers.local.json`), never
returned to the client in full, and used immediately by the router.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import settings
from app.router.router import router as model_router

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProviderKeyUpdate(BaseModel):
    # None = leave unchanged, "" = clear, "sk-..." = set
    api_key: Optional[str] = None
    default_model: Optional[str] = None


class PullRequest(BaseModel):
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
    try:
        model_router.set_provider_key(
            provider, api_key=body.api_key, default_model=body.default_model
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return model_router.provider_settings()[provider]


# ── Local model (Ollama) ─────────────────────────────────────────────────────
@router.get("/local")
def get_local() -> dict:
    return model_router.local_status()


@router.post("/local/pull")
def pull_local(body: PullRequest):
    """Proxy Ollama's streaming model pull so the UI can show live progress.

    Streams NDJSON lines straight from Ollama, e.g.
    {"status":"pulling ...","total":...,"completed":...} ... {"status":"success"}.
    """
    model = body.model or settings.ollama_default_model
    base = settings.ollama_base_url.rstrip("/")

    def stream():
        try:
            with httpx.stream(
                "POST", f"{base}/api/pull", json={"name": model}, timeout=None
            ) as r:
                if r.status_code != 200:
                    r.read()
                    yield json.dumps(
                        {"error": f"Ollama returned {r.status_code}: {r.text[:200]}"}
                    ) + "\n"
                    return
                for line in r.iter_lines():
                    if line:
                        yield line if line.endswith("\n") else line + "\n"
        except Exception as e:  # noqa: BLE001 - surface a clean error line to the client
            yield json.dumps(
                {
                    "error": (
                        f"Could not reach Ollama at {base}: {e}. "
                        "Is Ollama installed and running?"
                    )
                }
            ) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
