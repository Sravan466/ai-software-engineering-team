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


class OllamaEmbeddingFunction:
    """Implements Chroma's EmbeddingFunction protocol: __call__(input) -> embeddings."""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self.model = model or settings.embedding_model
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
