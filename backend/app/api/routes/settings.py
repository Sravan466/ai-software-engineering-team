"""Runtime settings: provider keys, the local model, and which model each role runs on.

Keys are stored on this backend only (gitignored `data/providers.local.json`), never
returned to the client in full, and used immediately by the router.

Two things here used to contradict each other. `POST /local/pull` would download any
model the user named, progress bar and all — and the only way to *select* one went
through `PUT /providers/{provider}`, which rejected the local provider outright. So a
user could pull `llama3.1:8b`, watch it finish, and every agent would carry on running
on whatever `OLLAMA_DEFAULT_MODEL` said in `.env`. Selecting a model is now valid for
every provider, and `/roles` goes further: one model per agent, chosen from what has
actually been pulled.
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


class RoleModelUpdate(BaseModel):
    """Point one role at a model. Blank or null puts it back on the default.

    The value is a `provider:model` pair, or a bare tag meaning the local runtime —
    the same spelling `FALLBACK_CHAIN` uses, parsed by the same function, so a tag
    with a colon in it survives intact.
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
    """Set a provider's API key, its default model, or both.

    `ollama` is a valid provider here for the model half. It was not, which is why a
    model downloaded through this very page could never be the one the agents ran on.
    """
    try:
        model_router.set_provider_key(
            provider, api_key=body.api_key, default_model=body.default_model
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if provider == "ollama":
        return model_router.local_status()
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
    # The router's current default, not the one `.env` was started with: the two
    # differ the moment anyone selects a model on this page, and pulling the older of
    # them would download something nothing is going to run.
    model = body.model or model_router.default_model("ollama")
    base = settings.ollama_base_url.rstrip("/")

    def stream():
        # The model on disk is about to change, so whatever was probed about it is
        # already stale. Dropping the cache in a `finally` rather than after the loop
        # covers the case that actually happens: the user closes the tab mid-pull,
        # Starlette throws GeneratorExit — a BaseException, so no `except Exception`
        # sees it — and every later call would size prompts for the previous model.
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
        finally:
            prov = model_router.provider("ollama")
            if prov is not None and hasattr(prov, "forget_profile"):
                prov.forget_profile(model)

    return StreamingResponse(stream(), media_type="application/x-ndjson")
