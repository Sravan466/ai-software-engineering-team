"""RAG knowledge base: upload, list, and delete documents agents can draw on."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import KnowledgeDoc
from app.rag.ingest import chunk_text, extract_text
from app.rag.knowledge_base import knowledge_base

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.post("/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...), db: Session = Depends(get_db)
) -> dict:
    raw = await file.read()
    text = extract_text(file.filename or "upload", raw, file.content_type)
    if not text.strip():
        raise HTTPException(400, "Could not extract any text from the uploaded file.")

    doc = KnowledgeDoc(filename=file.filename or "upload", content_type=file.content_type)
    db.add(doc)
    db.commit()
    db.refresh(doc)

    chunks = chunk_text(text)
    stored = knowledge_base.add_chunks(doc.id, chunks, doc.filename)
    doc.chunks = stored
    db.commit()

    if stored == 0:
        raise HTTPException(
            503,
            "Document saved but indexing failed — is Ollama running with the embedding "
            "model pulled? (`ollama pull nomic-embed-text`)",
        )
    return {"id": doc.id, "filename": doc.filename, "chunks": stored}


@router.get("/documents")
def list_documents(db: Session = Depends(get_db)) -> list[dict]:
    docs = db.execute(select(KnowledgeDoc).order_by(KnowledgeDoc.created_at.desc())).scalars()
    return [
        {"id": d.id, "filename": d.filename, "chunks": d.chunks, "created_at": d.created_at}
        for d in docs
    ]


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(KnowledgeDoc, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    knowledge_base.delete_doc(doc_id)
    db.delete(doc)
    db.commit()


@router.get("/query")
def query_knowledge_base(q: str, k: int = 4) -> dict:
    """Debug endpoint: see what context a query would retrieve."""
    return {"query": q, "context": knowledge_base.query(q, k=k)}
