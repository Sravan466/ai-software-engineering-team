"""RAG knowledge base: upload, list, and delete documents agents can draw on.

Each document belongs to the account that uploaded it, and only that account's
builds search it. Another account's document is a 404, like one that isn't there.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.base import get_db
from app.db.models import KnowledgeDoc, User
from app.rag.ingest import chunk_text, extract_text
from app.rag.knowledge_base import knowledge_base

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.post("/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    raw = await file.read()
    text = extract_text(file.filename or "upload", raw, file.content_type)
    if not text.strip():
        raise HTTPException(400, "Could not extract any text from the uploaded file.")

    doc = KnowledgeDoc(owner_id=user.id, filename=file.filename or "upload", content_type=file.content_type)
    db.add(doc)
    db.commit()
    db.refresh(doc)

    chunks = chunk_text(text)
    # Embedding calls a model runtime over the network. Off the event loop, so one
    # upload does not stall every other request while the runtime works.
    stored = await run_in_threadpool(knowledge_base.add_chunks, doc.id, chunks, doc.filename, user.id)
    doc.chunks = stored
    db.commit()

    if stored == 0:
        raise HTTPException(
            503,
            "Document saved but indexing failed — no local runtime is serving an embedding "
            "model, or the one chosen isn't answering. Settings shows which one is in use.",
        )
    return {"id": doc.id, "filename": doc.filename, "chunks": stored}


@router.get("/documents")
def list_documents(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    docs = db.execute(
        select(KnowledgeDoc)
        .where(KnowledgeDoc.owner_id == user.id)
        .order_by(KnowledgeDoc.created_at.desc())
    ).scalars()
    return [
        {"id": d.id, "filename": d.filename, "chunks": d.chunks, "created_at": d.created_at}
        for d in docs
    ]


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = db.get(KnowledgeDoc, doc_id)
    if doc is None or doc.owner_id != user.id:
        raise HTTPException(404, "Document not found")
    knowledge_base.delete_doc(doc_id, user.id)
    db.delete(doc)
    db.commit()


@router.get("/query")
def query_knowledge_base(q: str, k: int = 4, user: User = Depends(current_user)) -> dict:
    """Debug endpoint: see what context a query would retrieve from your documents."""
    return {"query": q, "context": knowledge_base.query(q, k=max(1, min(k, 20)), owner_id=user.id)}
