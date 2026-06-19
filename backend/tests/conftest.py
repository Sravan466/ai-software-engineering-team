"""Test fixtures.

Points the app at a throwaway SQLite DB and stubs the LLM router with a fake provider so
the whole pipeline (orchestration, approvals, debate, persistence) can be exercised without
Ollama or any cloud key.
"""
from __future__ import annotations

import os
import tempfile

# Must be set BEFORE importing the app (settings are read at import time).
_tmp = tempfile.mkdtemp(prefix="aiteam_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["DEFAULT_ROUTING_MODE"] = "local_only"
os.environ["ENABLE_DEBATE"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.router.router import router as model_router  # noqa: E402
from app.schemas.llm import LLMResponse, Usage  # noqa: E402

# A JSON blob that satisfies every agent's and the debate's parser.
MOCK_JSON = (
    '{"summary": "mock deliverable", '
    '"product_name": "Demo", '
    '"tech_stack": {"frontend": ["Next.js"], "backend": ["FastAPI"], "database": ["PostgreSQL"]}, '
    '"overall_posture": "low risk", '
    '"estimated_timeline_weeks": 6, '
    '"total_monthly_low_usd": 50, "total_monthly_high_usd": 120, '
    '"decision": "PostgreSQL", "arguments": [{"agent": "Security", "position": "Postgres", '
    '"rationale": "relational integrity"}], "rationale": "fits the relational data model"}'
)


def _fake_complete(messages, **kwargs) -> LLMResponse:
    return LLMResponse(
        text=MOCK_JSON,
        provider="mock",
        model="mock-model",
        usage=Usage(prompt_tokens=12, completion_tokens=34, total_tokens=46),
        latency_ms=3,
    )


@pytest.fixture
def stub_router(monkeypatch):
    """Replace the LLM router with the deterministic fake (no Ollama / cloud keys)."""
    monkeypatch.setattr(model_router, "complete", _fake_complete)


@pytest.fixture
def client(stub_router):
    with TestClient(app) as c:
        yield c
