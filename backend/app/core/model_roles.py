"""Which model each role runs on, chosen by the user and persisted locally.

The platform used to have exactly one local model: whatever `OLLAMA_DEFAULT_MODEL`
said. A user could download a second one through Settings, watch the progress bar
finish, and every agent would carry on running on the first — because the only way
to *select* a model was an endpoint that rejected the local provider outright.

This is the selection. One entry per role: the eight pipeline agents, the debate
moderator, the visual preview, and embeddings. An absent or blank entry means "use
the provider's default model", which is the state every role starts in — so this
file is empty until somebody makes a choice, and a fresh install behaves exactly as
it did before.

Values are `provider:model` pairs, or a bare model name meaning the local runtime
(the same spelling `FALLBACK_CHAIN` uses, parsed by the same function). No model
name is ever written here by the application itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from app.core.constants import PHASE_LABELS, PHASE_ORDER

# Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_PATH = Path("data") / "model_roles.local.json"

#: Roles that are not a pipeline phase but still spend a model call. They are listed
#: because they are otherwise invisible: a run's cost includes a mockup and a pile of
#: embeddings, and neither had anywhere to be pointed at a smaller model.
EXTRA_ROLES: tuple[tuple[str, str], ...] = (
    ("debate", "Debate moderator"),
    ("preview", "Visual mockup"),
    ("embeddings", "Embeddings (memory & knowledge base)"),
)

#: The role a caller names when it has none of its own — spelled once so the router
#: and the API cannot disagree about what "no role" is called.
DEFAULT_ROLE = ""


def catalogue() -> list[dict]:
    """Every role a model can be chosen for, in the order they are shown.

    Derived from `PHASE_ORDER` rather than listed again, so adding a ninth agent
    gives it a row here without anyone remembering to add one.
    """
    rows = [
        {"role": phase.value, "label": PHASE_LABELS.get(phase.value, phase.value), "kind": "phase"}
        for phase in PHASE_ORDER
    ]
    rows += [{"role": role, "label": label, "kind": "support"} for role, label in EXTRA_ROLES]
    return rows


def known_roles() -> set[str]:
    return {row["role"] for row in catalogue()}


def _read() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt file must not stop the server booting
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(_PATH, 0o600)
    except OSError:
        pass


def get_all() -> dict[str, str]:
    """role -> model spec, for roles that have been given one. Never invents a name."""
    known = known_roles()
    return {
        role: spec
        for role, spec in _read().items()
        if role in known and isinstance(spec, str) and spec.strip()
    }


def get(role: Optional[str]) -> Optional[str]:
    """The model chosen for `role`, or None for "whatever the provider defaults to"."""
    if not role:
        return None
    spec = _read().get(role)
    return spec.strip() if isinstance(spec, str) and spec.strip() else None


def set_role(role: str, spec: Optional[str]) -> None:
    """Point one role at a model. `None` or `""` puts it back on the default.

    Clearing removes the key rather than storing an empty string, so "this role has
    never been chosen for" and "this role was chosen and then cleared" cannot end up
    reading differently anywhere downstream.
    """
    if role not in known_roles():
        raise ValueError(f"'{role}' is not a role a model can be chosen for.")
    data = _read()
    if spec and spec.strip():
        data[role] = spec.strip()
    else:
        data.pop(role, None)
    _write(data)


def clear_all() -> None:
    """Put every role back on the default model."""
    _write({})
