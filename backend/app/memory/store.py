"""Long-term project memory over ChromaDB.

Stores a short summary of each completed project so future runs can recall preferred
frameworks, architectural decisions, and reusable patterns. Degrades to a no-op if the
vector store is unavailable.
"""
from __future__ import annotations
from typing import Optional

from app.core.logging import get_logger
from app.rag import chroma
from app.rag.embeddings import OllamaEmbeddingFunction

log = get_logger(__name__)

_COLLECTION = "project_memory"


class MemoryStore:
    def __init__(self) -> None:
        self._collection = None

    def _get_collection(self):
        if self._collection is None:
            # The shared client lives in `app.rag.chroma`, which opens it once,
            # behind the lock both stores take — see there for why.
            self._collection = chroma.collection(
                _COLLECTION, OllamaEmbeddingFunction(), owner="Memory store"
            )
        return self._collection

    def remember(self, project_id: str, idea: str, summary: str) -> None:
        col = self._get_collection()
        if col is None:
            return
        try:
            col.upsert(
                ids=[project_id],
                documents=[f"Idea: {idea}\n\nLessons & decisions:\n{summary}"],
                metadatas=[{"project_id": project_id, "idea": idea[:300]}],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to write project memory: %s", e)

    def recall(self, query: str, k: int = 3, exclude_project_id: Optional[str] = None) -> str:
        col = self._get_collection()
        if col is None or not query.strip():
            return ""
        try:
            res = col.query(query_texts=[query], n_results=k + 1)
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            blocks: list[str] = []
            for doc, meta in zip(docs, metas):
                if (
                    exclude_project_id
                    and isinstance(meta, dict)
                    and meta.get("project_id") == exclude_project_id
                ):
                    continue
                blocks.append(doc)
                if len(blocks) >= k:
                    break
            return "\n\n---\n\n".join(blocks)
        except Exception as e:  # noqa: BLE001
            log.warning("Memory recall failed: %s", e)
            return ""


memory_store = MemoryStore()
