"""The one Chroma client both vector stores share, opened once, behind one lock.

The knowledge base and project memory used to each open their own
`chromadb.PersistentClient` on the same `CHROMA_PERSIST_DIR`, lazily, the first time
anything asked. That was safe only by accident: every graph node ran under the
runner's process-wide lock, so no two first calls could overlap. Builds now run side
by side, and two clients created at once on one directory — Chroma does not lock its
own system creation — is two writers on one SQLite file and one index, which is how
"database is locked" and a corrupted index happen.

So there is one client, and opening it is serialised. A failed open is remembered
for a short while rather than retried on every call: behind a lock, a directory that
will not open would otherwise make every concurrent build queue for its own turn to
fail, on every retrieval.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: How long a failed open is left alone before it is tried again. Short enough that
#: fixing the directory, or installing Chroma, is picked up without a restart.
_RETRY_AFTER_SECONDS = 60.0

_lock = threading.Lock()
_client: Any = None
_failed_until = 0.0


def collection(name: str, embedding_function: Any, owner: str) -> Optional[Any]:
    """The named collection on the shared client, or None if Chroma will not open.

    `owner` names the store in the warning, so a disabled store still says which one.
    """
    global _client, _failed_until
    with _lock:
        if _client is None:
            if time.monotonic() < _failed_until:
                return None
            try:
                import chromadb

                os.makedirs(settings.chroma_persist_dir, exist_ok=True)
                _client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
            except Exception as e:  # noqa: BLE001 - not installed, corrupt, incompatible
                _failed_until = time.monotonic() + _RETRY_AFTER_SECONDS
                log.warning("%s disabled (Chroma init failed): %s", owner, e)
                return None
        try:
            return _client.get_or_create_collection(
                name=name,
                embedding_function=embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("%s disabled (Chroma collection failed): %s", owner, e)
            return None
