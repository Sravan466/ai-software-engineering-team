"""Local embedding function backed by Ollama, for ChromaDB collections.

Uses Ollama's batch `/api/embed` endpoint so memory + RAG stay fully offline. If the
embedding model isn't available, callers degrade gracefully (memory/RAG become no-ops).
"""
from __future__ import annotations
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: The role embeddings run under, so they can be pointed at their own model in
#: Settings. They are a real share of a run's calls and had nowhere to be chosen —
#: the model came from `.env` and stayed there.
ROLE = "embeddings"


def _chosen_model() -> str:
    """The embedding model the user selected, or the configured default.

    This endpoint is Ollama's and nothing else's — there is no cloud path here — so a
    selection naming another provider is declined rather than forwarded. Sending
    `anthropic:claude-…` on to `/api/embed` as a model name would fail every RAG and
    memory write from that moment on, with an error about a model Ollama has never
    heard of. A bare tag is what the Settings dropdown writes; an `ollama:` prefix is
    reduced to its model half.
    """
    from app.core import model_roles

    spec = model_roles.get(ROLE)
    if not spec:
        return settings.embedding_model
    provider, _, model = spec.partition(":")
    if model and provider.strip() != "ollama":
        log.warning(
            "Embeddings are set to '%s', which is not a local model — they run "
            "against Ollama only. Using '%s' instead.",
            spec,
            settings.embedding_model,
        )
        return settings.embedding_model
    return model.strip() if model else spec


class OllamaEmbeddingFunction:
    """Implements Chroma's EmbeddingFunction protocol: __call__(input) -> embeddings."""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self.model = model or _chosen_model()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")

    # Chroma calls this with a list of strings and expects a list of float vectors.
    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 - Chroma's name
        if not input:
            return []
        try:
            r = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": input},
                timeout=120.0,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("embeddings", [])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Ollama embeddings failed (model '{self.model}'). "
                f"Pull it with `ollama pull {self.model}`. Cause: {e}"
            ) from e

    # Chroma >=0.5 also looks for a name() on custom embedding functions.
    @staticmethod
    def name() -> str:
        return "ollama-embed"
