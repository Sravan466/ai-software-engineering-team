"""Document ingestion: extract text (txt/md/pdf), chunk, and store in the knowledge base."""
from __future__ import annotations
from typing import Optional

import io

from app.core.logging import get_logger

log = get_logger(__name__)


def extract_text(filename: str, raw: bytes, content_type: Optional[str]) -> str:
    name = filename.lower()
    if name.endswith(".pdf") or (content_type or "").endswith("pdf"):
        return _extract_pdf(raw)
    # Treat everything else as UTF-8 text (txt, md, code, json, ...).
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _extract_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:  # noqa: BLE001
        log.warning("PDF extraction failed: %s", e)
        return ""


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Simple character-based chunking with overlap. Good enough for a knowledge base."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end])
        if end == n:
            break
        start = end - overlap
    return chunks
