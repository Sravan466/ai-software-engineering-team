"""RAG knowledge base over ChromaDB.

Documents uploaded by the user are chunked, embedded (locally via Ollama), and stored.
Agents query it for relevant context. Everything degrades to a no-op if Chroma or the
embedding model is unavailable, so the pipeline never hard-fails on RAG.
"""
from __future__ import annotations

import os

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.embeddings import OllamaEmbeddingFunction

log = get_logger(__name__)

_COLLECTION = "knowledge_base"


class KnowledgeBase:
    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._enabled = True

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb

            os.makedirs(settings.chroma_persist_dir, exist_ok=True)
            self._client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
            self._collection = self._client.get_or_create_collection(
                name=_COLLECTION,
                embedding_function=OllamaEmbeddingFunction(),
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Knowledge base disabled (Chroma init failed): %s", e)
            self._enabled = False
            return None
        return self._collection

    def add_chunks(self, doc_id: str, chunks: list[str], filename: str) -> int:
        col = self._get_collection()
        if col is None or not chunks:
            return 0
        ids = [f"{doc_id}-{i}" for i in range(len(chunks))]
        metadatas = [{"doc_id": doc_id, "filename": filename, "chunk": i} for i in range(len(chunks))]
        try:
            col.add(ids=ids, documents=chunks, metadatas=metadatas)
            return len(chunks)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to add chunks to knowledge base: %s", e)
            return 0

    def query(self, text: str, k: int = 4) -> str:
        """Return concatenated relevant chunks as a single context string (or '')."""
        col = self._get_collection()
        if col is None or not text.strip():
            return ""
        try:
            res = col.query(query_texts=[text], n_results=k)
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            blocks = []
            for doc, meta in zip(docs, metas):
                src = meta.get("filename", "doc") if isinstance(meta, dict) else "doc"
                blocks.append(f"[source: {src}]\n{doc}")
            return "\n\n".join(blocks)
        except Exception as e:  # noqa: BLE001
            log.warning("Knowledge base query failed: %s", e)
            return ""

    def delete_doc(self, doc_id: str) -> None:
        col = self._get_collection()
        if col is None:
            return
        try:
            col.delete(where={"doc_id": doc_id})
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to delete doc %s: %s", doc_id, e)


knowledge_base = KnowledgeBase()
