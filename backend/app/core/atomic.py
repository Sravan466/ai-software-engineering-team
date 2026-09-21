"""Writing a small settings file so that no reader ever sees half of it.

Both settings files hold choices someone made by hand — keys, sources, which model
each agent runs on — and both are rewritten whole on every change. Writing in place
truncates the file first, so a reader that arrives in between sees nothing, and a
crash in between leaves nothing. A reader that finds nothing reads it as "no
settings", and the next save makes that permanent.

So the new content goes to a temporary file beside the old one, created owner-only,
and replaces it in one rename — which is atomic on the same filesystem.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text` atomically, leaving it readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
