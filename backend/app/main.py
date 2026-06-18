"""FastAPI entrypoint for the AI Software Engineering Team platform."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import init_db

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting AI Software Engineering Team (env=%s)", settings.app_env)
    init_db()
    log.info("Database initialised. Default routing mode: %s", settings.default_routing_mode)
    yield
    log.info("Shutting down.")


app = FastAPI(
    title="AI Software Engineering Team",
    description=(
        "A multi-agent platform that turns a product idea into production-ready software. "
        "Specialist agents collaborate through a LangGraph pipeline with human approval gates, "
        "running on local (Ollama) or cloud (Claude/GPT/Gemini) models."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers (imported here so DB/graph modules initialise after settings are loaded).
from app.api.routes import analytics, models, projects, rag, settings as settings_routes  # noqa: E402

app.include_router(projects.router)
app.include_router(models.router)
app.include_router(rag.router)
app.include_router(analytics.router)
app.include_router(settings_routes.router)


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
