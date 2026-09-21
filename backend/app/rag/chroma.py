"""The one lock both vector stores take to open their shared Chroma directory.

The knowledge base and project memory each open `chromadb.PersistentClient` on the
same `CHROMA_PERSIST_DIR`, lazily, the first time anything asks. That used to be
safe by accident: every graph node ran under the runner's process-wide lock, so no
two first calls could ever overlap. Builds now run side by side, and two first
phases starting together would each build a client on the same directory at once —
Chroma does not lock its own system creation, and two writers on one SQLite file and
one index is how "database is locked" and a corrupted index happen.

One lock for both stores, not one each: they share the directory, so they share the
race.
"""
from __future__ import annotations

import threading

init_lock = threading.Lock()
