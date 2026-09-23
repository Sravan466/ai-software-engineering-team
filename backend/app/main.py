"""FastAPI entrypoint for the AI Software Engineering Team platform."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth_middleware import AuthMiddleware
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import init_db

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting AI Software Engineering Team (env=%s)", settings.app_env)
    init_db()
    log.info("Database initialised. Default routing mode: %s", settings.default_routing_mode)
    # The compile gate reads JavaScript with TypeScript's parser; fetch it now rather
    # than inside the first Frontend phase that needs it.
    from app.build import toolchain

    toolchain.warm_up()
    # Find the model runtimes on this machine now rather than inside the first
    # request that needs one. Loopback only; in the background, so a runtime that is
    # slow to answer never holds up startup.
    # Warmed for the owner, whose router is the one a self-hosted install uses; every
    # other account's router looks for itself the first time it is asked.
    import threading

    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import User
    from app.router.router import routers

    db = SessionLocal()
    try:
        owner = db.execute(select(User).where(User.is_owner.is_(True)).order_by(User.created_at)).scalars().first()
    finally:
        db.close()
    if owner is not None:
        threading.Thread(
            target=lambda: routers.for_user(owner.id).sources.ensure(), name="detect-sources", daemon=True
        ).start()
    yield
    log.info("Shutting down.")


app = FastAPI(
    title="AI Software Engineering Team",
    description=(
        "A multi-agent platform that turns a product idea into production-ready software. "
        "Specialist agents collaborate through a LangGraph pipeline with human approval gates, "
        "running on any local model runtime or on cloud (Claude/GPT/Gemini) models."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Added before CORS so that CORS wraps it: a 401 has to carry CORS headers, or the
# page sees a network error instead of "sign in".
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers (imported here so DB/graph modules initialise after settings are loaded).
from app.api.routes import (  # noqa: E402
    analytics,
    auth as auth_routes,
    github,
    models,
    preview,
    projects,
    rag,
    settings as settings_routes,
    skills,
)

app.include_router(auth_routes.router)
app.include_router(projects.router)
app.include_router(preview.router)
app.include_router(models.router)
app.include_router(rag.router)
app.include_router(skills.router)
app.include_router(analytics.router)
app.include_router(settings_routes.router)
app.include_router(github.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.get("/", tags=["health"])
def root() -> dict:
    return {
        "name": "AI Software Engineering Team",
        "docs": "/docs",
        "health": "/health",
    }
