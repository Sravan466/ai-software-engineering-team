"""The embedding function Chroma collections use, served by whichever source has one.

Embeddings go through the router's `embed` operation, like every other model call:
the model is the one chosen for the embeddings role in Settings, or the configured
`EMBEDDING_MODEL` wherever a running source has it, or the first model any source
reports as embedding-only. So document search and memory work on any local runtime
that serves an embedding model — and when none does, callers degrade gracefully
(memory and RAG become no-ops) and Settings says why.

The model is resolved on every call rather than when a collection is opened, so
choosing a different one in Settings takes effect without a restart.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.core.model_roles import EMBEDDINGS_ROLE

log = get_logger(__name__)

#: The role embeddings run under, so they can be pointed at their own model in
#: Settings. They are a real share of a run's calls.
ROLE = EMBEDDINGS_ROLE


class SourceEmbeddingFunction:
    """Implements Chroma's EmbeddingFunction protocol: __call__(input) -> embeddings."""

    # Chroma calls this with a list of strings and expects a list of float vectors.
    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 - Chroma's name
        if not input:
            return []
        from app.router.base import ProviderError
        from app.router.router import router

        try:
            return router.embed(list(input))
        except ProviderError as e:
            raise RuntimeError(f"Embeddings failed: {e}") from e

    # Chroma >=0.5 also looks for a name() on custom embedding functions.
    @staticmethod
    def name() -> str:
        return "source-embed"
