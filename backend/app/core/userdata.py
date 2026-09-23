"""Where one account's settings files live.

The settings files used to be three global files under `data/`: cloud keys and
added sources, which model each agent runs on, and per-model generation settings.
Each account now has its own copy of all three, in a directory named by its id:

    data/users/<user id>/providers.local.json
    data/users/<user id>/model_roles.local.json
    data/users/<user id>/model_settings.local.json

Same format, same owner-only atomic writes, same locks — only the path is per user.
The directory is created owner-only, so one account's keys are not readable by
anything else on the machine. The global files are moved into the first account's
directory by the accounts migration (`app.db.accounts`).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from app.core.config import settings

#: cwd-relative like the database. Tests point it somewhere throwaway.
ROOT = Path(settings.user_data_dir)

#: A user id is a uuid4 hex. Anything else is refused before it becomes a path, so
#: no id can walk out of `ROOT`.
_ID = re.compile(r"^[0-9a-f]{32}$")


def directory(user_id: str) -> Path:
    if not _ID.match(user_id or ""):
        raise ValueError("That isn't an account id.")
    path = ROOT / user_id
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def path(user_id: str, name: str) -> Path:
    """`name` in `user_id`'s directory — resolved on every call, so `ROOT` can move."""
    return directory(user_id) / name
