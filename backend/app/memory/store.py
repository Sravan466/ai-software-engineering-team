"""Long-term project memory over ChromaDB.

Stores a short summary of each completed project so future runs can recall preferred
frameworks, architectural decisions, and reusable patterns. Degrades to a no-op if the
vector store is unavailable.

Recall is narrowed to the projects of one account, read from the database, so what
one account learned is never handed to another's builds.
"""
from __future__ import annotations
from typing import Optional

from sqlalchemy import select

from app.core import identity
from app.core.logging import get_logger
from app.rag import chroma
from app.rag.embeddings import SourceEmbeddingFunction

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
                _COLLECTION, SourceEmbeddingFunction(), owner="Memory store"
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

    @staticmethod
    def _project_ids(owner_id: str, exclude: Optional[str]) -> list[str]:
        from app.db.base import SessionLocal
        from app.db.models import Project

        db = SessionLocal()
        try:
            ids = db.execute(select(Project.id).where(Project.owner_id == owner_id)).scalars()
            return [i for i in ids if i != exclude]
        finally:
            db.close()

    def recall(
        self,
        query: str,
        k: int = 3,
        exclude_project_id: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> str:
        owner_id = owner_id or identity.current_user_id()
        if not owner_id or not query.strip():
            return ""
        project_ids = self._project_ids(owner_id, exclude_project_id)
        if not project_ids:
            return ""
        col = self._get_collection()
        if col is None:
            return ""
        try:
            res = col.query(
                query_texts=[query],
                n_results=k + 1,
                where=(
                    {"project_id": project_ids[0]}
                    if len(project_ids) == 1
                    else {"project_id": {"$in": project_ids}}
                ),
            )
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
