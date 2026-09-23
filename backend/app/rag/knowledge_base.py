"""RAG knowledge base over ChromaDB.

Documents uploaded by the user are chunked, embedded (by a local source), and stored.
Agents query it for relevant context. Everything degrades to a no-op if Chroma or the
embedding model is unavailable, so the pipeline never hard-fails on RAG.

The collection is shared, and a search is always narrowed to the documents of one
account — read from the database, where every document has an owner — so one
account's uploads never reach another's builds. Chunks written before accounts
need no rewrite: they are found through their document's id, and the migration
gave every document an owner.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from app.core import identity
from app.core.logging import get_logger
from app.rag import chroma
from app.rag.embeddings import SourceEmbeddingFunction

log = get_logger(__name__)

_COLLECTION = "knowledge_base"


class KnowledgeBase:
    def __init__(self) -> None:
        self._collection = None

    def _get_collection(self):
        if self._collection is None:
            # The shared client lives in `app.rag.chroma`, which opens it once,
            # behind the lock both stores take — see there for why.
            self._collection = chroma.collection(
                _COLLECTION, SourceEmbeddingFunction(), owner="Knowledge base"
            )
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

    @staticmethod
    def _doc_ids(owner_id: str) -> list[str]:
        from app.db.base import SessionLocal
        from app.db.models import KnowledgeDoc

        db = SessionLocal()
        try:
            return list(db.execute(select(KnowledgeDoc.id).where(KnowledgeDoc.owner_id == owner_id)).scalars())
        finally:
            db.close()

    def query(self, text: str, k: int = 4, owner_id: Optional[str] = None) -> str:
        """Return concatenated relevant chunks as a single context string (or '').

        Only `owner_id`'s documents are searched — the account the work is bound
        to when not given. With no account, or no documents, there is nothing.
        """
        owner_id = owner_id or identity.current_user_id()
        if not owner_id or not text.strip():
            return ""
        doc_ids = self._doc_ids(owner_id)
        if not doc_ids:
            return ""
        col = self._get_collection()
        if col is None:
            return ""
        try:
            res = col.query(
                query_texts=[text],
                n_results=k,
                where={"doc_id": doc_ids[0]} if len(doc_ids) == 1 else {"doc_id": {"$in": doc_ids}},
            )
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
