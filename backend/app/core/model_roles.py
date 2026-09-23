"""Which model each role runs on, chosen by the user and persisted locally.

The platform used to have exactly one local model: whatever `.env` said. A user could download a second one through Settings, watch the progress bar
finish, and every agent would carry on running on the first — because the only way
to *select* a model was an endpoint that rejected the local provider outright.

This is the selection. One entry per role: the eight pipeline agents, the debate
moderator, the visual preview, and embeddings. An absent or blank entry means "use
the provider's default model", which is the state every role starts in — so this
file is empty until somebody makes a choice, and a fresh install behaves exactly as
it did before.

Values are `source:model` pairs (or `provider:model` for the cloud), parsed by the
router. A bare model name in a file written before model sources existed is read
with the meaning it had then. No model name is ever written here by the
application itself.

Each account has its own file, held by a `RoleStore`. The module-level functions
are the store at `_PATH`, the global file from before accounts — which the
accounts migration moves into the first account's directory.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from app.core.atomic import write_private

from app.core.constants import PHASE_LABELS, PHASE_ORDER

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "model_roles.local.json"

#: Roles that are not a pipeline phase but still spend a model call: (role, name,
#: what it is for). They are listed because they are otherwise invisible — a run's
#: cost includes a debate, a mockup and a pile of embeddings, and none of the three
#: had anywhere to be pointed at a smaller model.
EXTRA_ROLES: tuple[tuple[str, str, str], ...] = (
    ("debate", "Debate", "Settles the stack before the architecture is drawn"),
    ("preview", "Mockup", "The visual preview of the front end"),
    ("embeddings", "Embeddings", "Long-term memory and the knowledge base"),
)

#: The support role whose model turns documents and memories into vectors.
EMBEDDINGS_ROLE = "embeddings"

#: The role a caller names when it has none of its own — spelled once so the router
#: and the API cannot disagree about what "no role" is called.
DEFAULT_ROLE = ""


def catalogue() -> list[dict]:
    """Every role a model can be chosen for, in the order they are shown.

    Derived from `PHASE_ORDER` rather than listed again, so adding a ninth agent
    gives it a row here without anyone remembering to add one.
    """
    rows = [
        {
            "role": phase.value,
            "label": PHASE_LABELS.get(phase.value, phase.value),
            "what": PHASE_LABELS.get(phase.value, phase.value),
            "kind": "phase",
        }
        for phase in PHASE_ORDER
    ]
    rows += [
        {"role": role, "label": name, "what": what, "kind": "support"}
        for role, name, what in EXTRA_ROLES
    ]
    return rows


def known_roles() -> set[str]:
    return {row["role"] for row in catalogue()}


class RoleStore:
    """One account's choice of model for each role — one file."""

    def __init__(self, path: Callable[[], Path]) -> None:
        self._path = path
        #: One read-modify-write at a time, so two roles set at once both stay set.
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 - a corrupt file must not stop the server booting
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        write_private(self._path(), json.dumps(data, indent=2, sort_keys=True))

    def get_all(self) -> dict[str, str]:
        """role -> model spec, for roles that have been given one. Never invents a name."""
        known = known_roles()
        return {
            role: spec
            for role, spec in self._read().items()
            if role in known and isinstance(spec, str) and spec.strip()
        }

    def get(self, role: Optional[str]) -> Optional[str]:
        """The model chosen for `role`, or None for "whatever the provider defaults to"."""
        if not role:
            return None
        spec = self._read().get(role)
        return spec.strip() if isinstance(spec, str) and spec.strip() else None

    def set_role(self, role: str, spec: Optional[str]) -> None:
        """Point one role at a model. `None` or `""` puts it back on the default.

        Clearing removes the key rather than storing an empty string, so "this role has
        never been chosen for" and "this role was chosen and then cleared" cannot end up
        reading differently anywhere downstream.
        """
        if role not in known_roles():
            raise ValueError(f"'{role}' is not a role a model can be chosen for.")
        with self._lock:
            data = self._read()
            if spec and spec.strip():
                data[role] = spec.strip()
            else:
                data.pop(role, None)
            self._write(data)

    def clear_all(self) -> None:
        """Put every role back on the default model."""
        with self._lock:
            self._write({})


#: The global file from before accounts, read through `_PATH` on every call.
default_store = RoleStore(lambda: _PATH)


def for_user(user_id: str) -> RoleStore:
    """The store for one account's own file."""
    from app.core import userdata

    return RoleStore(lambda: userdata.path(user_id, "model_roles.local.json"))


def get_all() -> dict[str, str]:
    return default_store.get_all()


def get(role: Optional[str]) -> Optional[str]:
    return default_store.get(role)


def set_role(role: str, spec: Optional[str]) -> None:
    default_store.set_role(role, spec)


def clear_all() -> None:
    default_store.clear_all()
